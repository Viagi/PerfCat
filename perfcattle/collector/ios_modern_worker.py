"""Standalone pymobiledevice3 worker, run only by the isolated modern runtime."""
import asyncio
import json
import logging
import math
import sys
import threading
import time
from typing import Any

from pymobiledevice3.dtx import DTXService, dtx_method
from pymobiledevice3.dtx_service import DtxService
from pymobiledevice3.lockdown import create_using_usbmux
from pymobiledevice3.exceptions import InvalidServiceError, UnrecognizedSelectorError, UnsupportedCommandError
from pymobiledevice3.remote.userspace_tunnel import UserspaceRsdTunnel
from pymobiledevice3.services.dvt.instruments.dvt_provider import DvtProvider
from pymobiledevice3.services.dvt.instruments.device_info import DeviceInfo
from pymobiledevice3.services.dvt.instruments.energy_monitor import EnergyMonitor
from pymobiledevice3.services.dvt.instruments.graphics import Graphics
from pymobiledevice3.services.dvt.instruments.process_control import ProcessControl
from pymobiledevice3.services.dvt.instruments.sysmontap import Sysmontap
from pymobiledevice3.services.mobile_image_mounter import PersonalizedImageMounter, auto_mount


class PrerequisiteError(RuntimeError):
    pass


class ProcessNetworkService(DTXService):
    IDENTIFIER = "com.apple.xcode.debug-gauge-data-providers.NetworkStatistics"

    @dtx_method("startSamplingForPIDs:", expects_reply=False)
    async def start_sampling(self, pids: list[int]) -> None: ...

    @dtx_method("stopSamplingForPIDs:", expects_reply=False)
    async def stop_sampling(self, pids: list[int]) -> None: ...

    @dtx_method("sampleAttributes:forPIDs:")
    async def sample(self, attributes: dict, pids: list[int]) -> Any: ...


class ProcessNetwork(DtxService[ProcessNetworkService]):
    pass


def number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


class Sampler:
    def __init__(self, dvt, package, emit, process="", process_start=""):
        self.dvt, self.package, self.emit = dvt, package, emit
        self.pid = None
        self.process = process
        self.process_start = process_start
        self.next_identity_check = 0

    async def resolve_pid(self):
        if self.process:
            if not self.process_start:
                raise PrerequisiteError("Selected iOS process has no start time. Refresh the process list and select it again.")
            if self.pid and time.monotonic() < self.next_identity_check:
                return
            pid, _, name = self.process.partition(":")
            if not pid.isascii() or not pid.isdecimal() or not name or int(pid) <= 0:
                raise PrerequisiteError("Invalid iOS process selection")
            async with DeviceInfo(self.dvt) as info:
                rows = await info.proclist()
            row = next((row for row in rows if row.get("pid") == int(pid)), None)
            if row is None or name not in (row.get("name"), row.get("bundleIdentifier")):
                raise PrerequisiteError("Selected iOS process exited or changed. Refresh the process list and select it again.")
            start = str(row.get("startDate") or "")
            if self.process_start and start != self.process_start:
                raise PrerequisiteError("Selected iOS PID was reused. Refresh the process list and select it again.")
            self.process_start = start
            self.pid = int(pid)
            self.next_identity_check = time.monotonic() + 3
            return
        async with ProcessControl(self.dvt) as control:
            self.pid = await control.process_identifier_for_bundle_identifier(self.package)
        if not self.pid or self.pid < 0:
            raise PrerequisiteError("iOS target app is not running. Open the selected app and restart collection.")

    async def watch_target(self):
        while True:
            await asyncio.sleep(3)
            await self.resolve_pid()

    def sample(self, metric, **value):
        self.emit({"metric": metric, "value": {"timestamp": int(time.time() * 1000), **value}})

    async def cpu_memory(self):
        tap = await Sysmontap.create(self.dvt, interval=1000)
        system = None
        last_resolve = time.monotonic()
        async with tap:
            async for row in tap:
                # iOS 26 interleaves heartbeat dictionaries; Tap iterates their string keys.
                if not isinstance(row, dict):
                    continue
                if time.monotonic() - last_resolve > 3:
                    await self.resolve_pid()
                    last_resolve = time.monotonic()
                if "CPUCount" in row:
                    system = row
                attrs = row.get("Processes", {}).get(self.pid)
                if attrs is None:
                    attrs = row.get("Processes", {}).get(str(self.pid))
                if attrs is None:
                    continue
                process = dict(zip(tap.process_attributes_cls.__dataclass_fields__, attrs))
                memory = process.get("physFootprint")
                if number(memory):
                    self.sample("memory", value=memory / 1024 / 1024, pid=self.pid)
                cpu = process.get("cpuUsage")
                total = (system or {}).get("SystemCPUUsage", {}).get("CPU_TotalLoad")
                count = (system or {}).get("CPUCount")
                if number(cpu) and number(total) and number(count) and count > 0:
                    self.sample("cpu", value=cpu, sys_value=total, count=count, pid=self.pid)

    async def graphics(self):
        first = True
        async with Graphics(self.dvt) as graphics:
            async for row in graphics:
                if isinstance(row, (list, tuple)) and len(row) == 2 and isinstance(row[1], (list, tuple)):
                    row = next((item for item in row[1] if isinstance(item, dict)), None)
                if not isinstance(row, dict):
                    continue
                if first:
                    first = False
                    continue
                for metric, key in (("fps", "CoreAnimationFramesPerSecond"), ("gpu", "Device Utilization %")):
                    if number(row.get(key)):
                        self.sample(metric, value=row[key])

    async def network(self):
        await self.resolve_pid()
        # iOS 26 networking notifications redact PID as -2. Query the target's gauge instead.
        async with ProcessNetwork(self.dvt) as monitor:
            pid = self.pid
            previous = None
            await monitor.service.start_sampling([pid])
            try:
                # Prime the gauge before establishing a baseline; initial replies can be cached.
                await monitor.service.sample({}, [pid])
                last_resolve = time.monotonic()
                while True:
                    await asyncio.sleep(1)
                    now = time.monotonic()
                    if now - last_resolve > 3:
                        await self.resolve_pid()
                        last_resolve = now
                    if pid != self.pid:
                        await monitor.service.stop_sampling([pid])
                        pid = self.pid
                        await monitor.service.start_sampling([pid])
                        previous = None
                        await monitor.service.sample({}, [pid])
                    row = await monitor.service.sample({}, [pid])
                    value = row.get(pid, row.get(str(pid))) if isinstance(row, dict) else None
                    if not isinstance(value, dict):
                        continue
                    down, up = value.get("net.rx.bytes"), value.get("net.tx.bytes")
                    if not number(down) or not number(up) or down < 0 or up < 0:
                        continue
                    delta_down = delta_up = 0
                    if previous is not None and down >= previous[0] and up >= previous[1]:
                        delta_down, delta_up = down - previous[0], up - previous[1]
                    previous = down, up
                    self.sample("network", downFlow=delta_down / 1024, upFlow=delta_up / 1024)
            finally:
                await monitor.service.stop_sampling([pid])

    async def energy(self):
        async with EnergyMonitor(self.dvt, [self.pid]) as monitor:
            async for row in monitor:
                value = row.get(self.pid, row.get(str(self.pid))) if isinstance(row, dict) else None
                if isinstance(value, dict):
                    self.sample("energy", value=value)
                await asyncio.sleep(1)


async def prepare_device(device):
    async with await create_using_usbmux(serial=device) as lockdown:
        if not await lockdown.get_developer_mode_status():
            raise PrerequisiteError(
                "Enable Developer Mode in iPhone Settings > Privacy & Security, "
                "restart and confirm, then restart collection."
            )
        async with PersonalizedImageMounter(lockdown) as mounter:
            mounted = await mounter.is_image_mounted("Personalized")
        if not mounted:
            logging.info("Preparing personalized iOS developer image")
            await asyncio.wait_for(auto_mount(lockdown), timeout=120)


async def list_processes(device, package):
    await prepare_device(device)
    async with UserspaceRsdTunnel(serial=device) as rsd:
        async with DvtProvider(rsd) as dvt:
            async with DeviceInfo(dvt) as info:
                rows = await info.proclist()
    result = []
    for row in rows:
        main = row.get("bundleIdentifier") == package
        if not main and row.get("isApplication"):
            continue
        name = row.get("bundleIdentifier") or row.get("name")
        if not isinstance(row.get("pid"), int) or row["pid"] <= 0 or not name:
            continue
        result.append({"pid": row["pid"], "name": name,
                       "start": str(row.get("startDate") or ""),
                       "kind": "app" if main else "system"})
    return sorted(result, key=lambda row: (row["kind"] != "app", "WebKit" not in row["name"], -row["pid"]))


async def launch_app(device, package):
    await prepare_device(device)
    async with UserspaceRsdTunnel(serial=device) as rsd:
        async with DvtProvider(rsd) as dvt:
            async with ProcessControl(dvt) as control:
                try:
                    pid = await control.launch(package, kill_existing=False)
                except AssertionError:
                    raise PrerequisiteError(
                        "The device did not return a PID; the app may not be launchable"
                    )
    if not isinstance(pid, int) or pid <= 0:
        raise PrerequisiteError("The device did not return a valid PID")
    return pid


async def run(device, package, emit, process="", process_start="", network_process="", network_process_start=""):
    loop = asyncio.get_running_loop()
    commands = asyncio.Queue()

    def read_commands():
        try:
            for line in sys.stdin:
                loop.call_soon_threadsafe(commands.put_nowait, json.loads(line))
        finally:
            if not loop.is_closed():
                loop.call_soon_threadsafe(commands.put_nowait, None)

    threading.Thread(target=read_commands, daemon=True).start()

    async def collect():
        await prepare_device(device)
        async with UserspaceRsdTunnel(serial=device) as rsd:
            async with DvtProvider(rsd) as dvt:
                sampler = Sampler(dvt, package, emit, process, process_start)
                network_sampler = Sampler(dvt, package, emit, network_process, network_process_start) if network_process else sampler
                await sampler.resolve_pid()
                tasks = {}
                if process:
                    tasks["target"] = asyncio.create_task(sampler.watch_target())
                started = set()
                pending = asyncio.create_task(commands.get())
                groups = {"cpu": "cpu_memory", "memory": "cpu_memory", "fps": "graphics",
                          "gpu": "graphics", "network": "network", "energy": "energy"}
                try:
                    while True:
                        done, _ = await asyncio.wait([pending, *tasks.values()], return_when=asyncio.FIRST_COMPLETED)
                        for task in done - {pending}:
                            group = next(name for name, value in tasks.items() if value is task)
                            try:
                                task.result()
                                raise RuntimeError("iOS metric stream closed")
                            except (InvalidServiceError, UnrecognizedSelectorError, UnsupportedCommandError, PrerequisiteError) as exc:
                                if group == "target" or (isinstance(exc, PrerequisiteError) and (group != "network" or network_sampler is sampler)):
                                    raise
                                for metric, name in groups.items():
                                    if name == group:
                                        emit({"metric": metric, "metric_error": str(exc) or "iOS metric not supported"})
                                del tasks[group]
                        if pending in done:
                            command = pending.result()
                            if command is None:
                                return
                            group = groups[command["metric"]]
                            if group not in started:
                                started.add(group)
                                source = network_sampler if group == "network" else sampler
                                tasks[group] = asyncio.create_task(getattr(source, group)())
                            pending = asyncio.create_task(commands.get())
                finally:
                    pending.cancel()
                    for task in tasks.values():
                        task.cancel()
                    await asyncio.gather(pending, *tasks.values(), return_exceptions=True)

    await collect()


def main():
    output = sys.stdout
    sys.stdout = sys.stderr
    logging.basicConfig(level=logging.INFO)

    def emit(message):
        output.write(json.dumps(message, allow_nan=False) + "\n")
        output.flush()

    try:
        if sys.argv[1] == "--list-processes":
            emit({"status": 1, "processes": asyncio.run(list_processes(sys.argv[2], sys.argv[3]))})
        elif sys.argv[1] == "--launch-app":
            emit({"status": 1, "pid": asyncio.run(launch_app(sys.argv[2], sys.argv[3]))})
        else:
            asyncio.run(run(sys.argv[1], sys.argv[2], emit, *sys.argv[3:7]))
    except Exception as exc:
        if isinstance(exc, PrerequisiteError):
            logging.warning("Modern iOS prerequisite: %s", exc)
        else:
            logging.exception("Modern iOS sampling failed")
        emit({"error": str(exc) or type(exc).__name__, "fatal": isinstance(exc, PrerequisiteError)})


if __name__ == "__main__":
    main()
