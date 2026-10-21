"""iOS background sampling with a cancellable process per device/target."""
import atexit
import json
from pathlib import Path
import subprocess
import sys
import threading
import time

from logzero import logger
from perfcat.iosperf._perf import DataType

START_TIMEOUT = 20.0
STALE_TTL = 5.0
IDLE_TIMEOUT = 60.0
MAX_RESTARTS = 3


class IOSRecoveryExhausted(RuntimeError):
    """A new collection is required after bounded recovery fails."""


class _Session:
    def __init__(self, device, package, process="", process_start="", network_process="", network_process_start=""):
        self.device = device
        self.package = package
        self.target = (process, process_start, network_process, network_process_start)
        self.cv = threading.Condition()
        self.closed = threading.Event()
        self.samples = {}
        self._network_pending = {"downFlow": 0, "upFlow": 0}
        self._network_read_marker = None
        self._network_read_value = None
        self.metric_errors = {}
        self.requested = set()
        self.last_access = time.monotonic()
        self.error = None
        self.exhausted = False
        self.process = None
        self.modern = False
        self.thread = threading.Thread(target=self._supervise, name="ios-session", daemon=True)
        self.thread.start()

    def read(self, metric):
        deadline = time.monotonic() + START_TIMEOUT
        with self.cv:
            self.last_access = time.monotonic()
            self.requested.add(metric.value)
            self.cv.notify_all()
            while not self.closed.is_set():
                if self.error:
                    if self.exhausted:
                        raise IOSRecoveryExhausted(self.error)
                    raise RuntimeError(self.error)
                if metric.value in self.metric_errors:
                    raise IOSRecoveryExhausted(self.metric_errors[metric.value])
                sample = self.samples.get(metric.value)
                if sample:
                    if time.monotonic() - sample[0] > STALE_TTL:
                        raise TimeoutError("iOS {} cached sample expired".format(metric.value))
                    if self.modern and metric == DataType.NETWORK:
                        if self._network_read_marker != sample[0]:
                            self._network_read_value = dict(sample[1], **self._network_pending)
                            self._network_pending = {"downFlow": 0, "upFlow": 0}
                            self._network_read_marker = sample[0]
                        return dict(self._network_read_value)
                    return dict(sample[1])
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("iOS {} sample unavailable: {}".format(metric.value, self.device))
                self.cv.wait(min(remaining, 0.5))
        raise RuntimeError("iOS sampling session stopped")

    def _receive(self, process):
        try:
            for line in process.stdout:
                message = json.loads(line)
                with self.cv:
                    if self.closed.is_set() or self.process is not process:
                        return
                    if message.get("metric_error"):
                        self.metric_errors[message["metric"]] = message["metric_error"]
                        self.samples.pop(message["metric"], None)
                    elif message.get("error"):
                        self.error = message["error"]
                        self.exhausted = bool(message.get("fatal"))
                    else:
                        # CPU/FPS use latest values; network increments must survive slower readers.
                        if self.modern and message["metric"] == DataType.NETWORK.value:
                            for key in self._network_pending:
                                self._network_pending[key] += message["value"][key]
                        self.samples[message["metric"]] = (time.monotonic(), message["value"])
                    self.cv.notify_all()
        except Exception as exc:
            with self.cv:
                if not self.closed.is_set():
                    self.error = "iOS worker communication failed: {}".format(exc)
                    self.cv.notify_all()

    @staticmethod
    def _reap(process):
        try:
            if process.poll() is None:
                # EOF requests shutdown; kill remains the bound for blocked I/O.
                process.stdin.close()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=3)
        finally:
            if not process.stdin.closed:
                process.stdin.close()

    def _supervise(self):
        try:
            from perfcat.service.ios_dependencies import IOSDependencies, ios_dependencies
            version = IOSDependencies.device_version(self.device, timeout=5)
            self.modern = int(version.split(".")[0]) >= 17
            command = [sys.executable, "-u", "-m", "perfcat.collector.ios_worker"]
            if self.modern:
                runtime = ios_dependencies["modern"]
                if not runtime.python.is_file():
                    raise IOSRecoveryExhausted("Start collection to check and install missing modern iOS dependencies.")
                command = [str(runtime.python), "-u", str(Path(__file__).with_name("ios_modern_worker.py"))]
            elif self.target[0] or self.target[2]:
                raise IOSRecoveryExhausted("Explicit iOS process selection requires the modern iOS backend (iOS 17+).")
            arguments = [self.device, self.package] + (list(self.target) if self.modern else [])
            for attempt in range(MAX_RESTARTS + 1):
                if self.closed.is_set():
                    break
                if attempt and self.closed.wait(min(2 ** attempt, 10)):
                    break
                process = None
                reader = None
                try:
                    with self.cv:
                        self.samples.clear()
                        self.metric_errors.clear()
                        self.error = None
                    process = subprocess.Popen(
                        command + arguments,
                        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                        stderr=None, text=True, encoding="utf-8", bufsize=1,
                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                    )
                    with self.cv:
                        self.process = process
                    logger.info("iOS session started device=%s app=%s pid=%s attempt=%s",
                                self.device, self.package, process.pid, attempt + 1)
                    reader = threading.Thread(target=self._receive, args=(process,),
                                              name="ios-stream-reader", daemon=True)
                    reader.start()
                    subscribed = {}
                    launched = time.monotonic()
                    while not self.closed.wait(0.1):
                        now = time.monotonic()
                        with self.cv:
                            if now - self.last_access > IDLE_TIMEOUT:
                                self.closed.set()
                                break
                            if self.error:
                                raise RuntimeError(self.error)
                            if process.poll() is not None:
                                raise RuntimeError("iOS worker exited: {}".format(process.returncode))
                            if self.modern and not self.samples and now - launched > 150:
                                raise TimeoutError("Modern iOS initialization timed out; check the device and network.")
                            for metric in self.requested - subscribed.keys():
                                process.stdin.write(json.dumps({"metric": metric}) + "\n")
                                process.stdin.flush()
                                subscribed[metric] = now
                            for metric, started in subscribed.items():
                                if metric in self.metric_errors:
                                    continue
                                sample = self.samples.get(metric)
                                last = sample[0] if sample else started
                                # Legacy network notifications pause at idle; modern gauges reply each interval.
                                timeout = 150 if self.modern and not self.samples else START_TIMEOUT
                                if (self.modern or metric != DataType.NETWORK.value) and now - last > timeout:
                                    if self.modern and sample is None and self.samples:
                                        self.metric_errors[metric] = "iOS {} is unavailable on this device for this collection".format(metric)
                                        self.cv.notify_all()
                                        continue
                                    raise TimeoutError("iOS {} stream stalled".format(metric))
                except Exception as exc:
                    with self.cv:
                        self.error = str(exc)
                        self.samples.clear()
                        self.cv.notify_all()
                    logger.warning("iOS session failed device=%s app=%s: %s",
                                   self.device, self.package, exc)
                finally:
                    if process is not None:
                        self._reap(process)
                        if reader is not None:
                            reader.join(timeout=2)
                        process.stdout.close()
                    with self.cv:
                        self.process = None
                if self.exhausted:
                    break
            with self.cv:
                if not self.closed.is_set() and not self.exhausted:
                    self.exhausted = True
                    self.error = "iOS recovery limit reached; restart collection: {}".format(self.error)
        except Exception as exc:
            with self.cv:
                self.error = str(exc)
                self.exhausted = True
        finally:
            with self.cv:
                self.cv.notify_all()

    def stop(self):
        self.closed.set()
        with self.cv:
            self.cv.notify_all()
        self.thread.join(timeout=8)
        if self.thread.is_alive():
            raise RuntimeError("iOS sampling worker did not stop")


class IOSPerformance:
    """Metric-compatible facade; session ownership is shared across requests."""

    _sessions = {}
    _lock = threading.RLock()
    _requests = threading.Condition(_lock)
    _active_requests = 0
    _accepting = True
    _generation = 0
    _request_context = threading.local()

    def __init__(self, pkgName, deviceId, process=None, process_start="", network_process="", network_process_start=""):
        self.pkgName = pkgName
        self.deviceId = deviceId
        self.target = (process or "", process_start, network_process, network_process_start) if process is not None or network_process else None
        self.cpu = DataType.CPU
        self.memory = DataType.MEMORY
        self.network = DataType.NETWORK
        self.fps = DataType.FPS
        self.gpu = DataType.GPU

    def getPerformance(self, perfType):
        metric = DataType(perfType)
        cls = type(self)
        with cls._lock:
            target = self.target or getattr(cls._request_context, "target", ("", "", "", ""))
            generation = getattr(cls._request_context, "generation", cls._generation)
            if generation != cls._generation:
                raise RuntimeError("iOS request belongs to a stopped collection")
            session = cls._sessions.get(self.deviceId)
            if session and (session.package != self.pkgName or session.target != target or session.closed.is_set()):
                session.stop()
                session = None
            if session is None:
                session = _Session(self.deviceId, self.pkgName, *target)
                cls._sessions[self.deviceId] = session
        value = session.read(metric)
        self.last_sample_time = value.get("timestamp", 0) / 1000
        if metric == DataType.CPU:
            count = value.get("count") or 1
            return value["value"] / count, value["sys_value"] / count
        if metric == DataType.NETWORK:
            return value["downFlow"], value["upFlow"]
        return value["value"]

    @classmethod
    def clear(cls):
        with cls._lock:
            cls._generation += 1
            for session in cls._sessions.values():
                session.stop()
            cls._sessions.clear()

    @classmethod
    def begin_request(cls, process="", process_start="", network_process="", network_process_start=""):
        with cls._requests:
            if not cls._accepting:
                raise RuntimeError("iOS collection stopped")
            cls._active_requests += 1
            cls._request_context.generation = cls._generation
            cls._request_context.target = (process or "", process_start or "", network_process or "", network_process_start or "")

    @classmethod
    def end_request(cls):
        with cls._requests:
            cls._active_requests -= 1
            if hasattr(cls._request_context, "generation"):
                del cls._request_context.generation
            if hasattr(cls._request_context, "target"):
                del cls._request_context.target
            cls._requests.notify_all()

    @classmethod
    def stop_requests(cls):
        with cls._requests:
            cls._accepting = False
            cls.clear()
            if not cls._requests.wait_for(lambda: cls._active_requests == 0, timeout=25):
                raise TimeoutError("iOS requests still active; report creation deferred")

    @classmethod
    def resume_requests(cls):
        with cls._requests:
            cls._accepting = True


atexit.register(IOSPerformance.clear)
