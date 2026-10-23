"""Consent-driven installation of optional iOS tooling in an isolated runtime."""
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import threading

import psutil
from logzero import logger


INDEX = "https://pypi.org/simple"
BUSY = {"creating", "installing", "verifying"}
PROFILES = {
    "legacy": {"py-ios-device": "2.4.25"},
    "modern": {"pymobiledevice3": "11.10.4"},
}


class IOSDependencies:
    def __init__(self, profile):
        self.profile = profile
        self.packages = PROFILES[profile]
        self.root = Path.cwd() / ".runtime" / ("ios-" + profile)
        self.python = self.root / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        self.log = Path.cwd() / "logs" / ("ios-" + profile + "-install.log")
        self.lock = threading.RLock()
        self.state = "idle"
        self.error = ""
        self.thread = None

    @staticmethod
    def device_version(device, timeout=12):
        if not isinstance(device, str) or not re.fullmatch(r"[A-Fa-f0-9-]{16,64}", device):
            raise ValueError("Invalid iOS device ID")
        result = subprocess.run(
            [sys.executable, "-c", "import sys,json,tidevice; "
             "print(json.dumps(tidevice.Device(sys.argv[1]).get_value('ProductVersion')))", device],
            capture_output=True, text=True, encoding="utf-8", timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if result.returncode:
            raise RuntimeError("Cannot read iOS version. Unlock the device and trust this computer.")
        version = json.loads(result.stdout.strip())
        if not isinstance(version, str) or not re.fullmatch(r"\d+(?:\.\d+){1,2}", version):
            raise RuntimeError("Invalid iOS version returned by device")
        return version

    def _probe(self):
        if not self.python.is_file() or not (self.root / "pyvenv.cfg").is_file():
            return False
        try:
            modules = (
                "import ios_device.main"
                if self.profile == "legacy" else
                "import pymobiledevice3.services.dvt.instruments.sysmontap; "
                "import pymobiledevice3.services.mobile_image_mounter; "
                "import pymobiledevice3.remote.userspace_tunnel; import pymobiledevice3.cli"
            )
            result = subprocess.run(
                [str(self.python), "-I", "-c", "import importlib.metadata as m; " + modules + "; " +
                 "; ".join("assert m.version({!r}) == {!r}".format(p, v) for p, v in self.packages.items())],
                capture_output=True, timeout=20,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            return result.returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            return False

    def snapshot(self):
        with self.lock:
            return {"state": self.state, "error": self.error, "profile": self.profile,
                    "packages": self.packages, "source": INDEX, "environment": str(self.root),
                    "collection_supported": True}

    def check(self):
        with self.lock:
            if self.state not in BUSY:
                self.state = "ready" if self._probe() else "missing"
                if self.state == "ready":
                    self.error = ""
            return self.snapshot()

    def start(self):
        with self.lock:
            if self.state in BUSY:
                return self.snapshot()
            if getattr(sys, "frozen", False):
                raise RuntimeError("This packaged runtime cannot create a Python environment.")
            if self._probe():
                self.state = "ready"
                return self.snapshot()
            self.state, self.error = "creating", ""
            self.thread = threading.Thread(target=self._install, name="ios-dependency-install", daemon=True)
            try:
                self.thread.start()
            except RuntimeError as exc:
                self.state, self.error = "failed", str(exc)
                raise
            return self.snapshot()

    def _run(self, args, output, timeout):
        process = subprocess.Popen(
            args, stdout=output, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        try:
            if process.wait(timeout=timeout):
                raise RuntimeError("Installation command failed; see the installation log.")
        except subprocess.TimeoutExpired:
            # pip/build helpers must not survive the bounded installation job.
            try:
                children = psutil.Process(process.pid).children(recursive=True)
                for child in children:
                    try:
                        child.kill()
                    except psutil.Error:
                        pass
            except psutil.Error:
                pass
            finally:
                try:
                    process.kill()
                    process.wait(timeout=5)
                except (OSError, subprocess.TimeoutExpired):
                    logger.warning("Could not reap iOS installer process %s", process.pid)
            raise RuntimeError("Dependency installation timed out. Check network access and retry.")

    def _install(self):
        try:
            self.log.parent.mkdir(parents=True, exist_ok=True)
            with self.log.open("w", encoding="utf-8") as output:
                self._run([sys.executable, "-m", "venv", str(self.root)], output, 120)
                with self.lock:
                    self.state = "installing"
                self._run(
                    [str(self.python), "-m", "pip", "--isolated", "install", "--no-input",
                     "--disable-pip-version-check", "--index-url", INDEX, "--timeout", "30",
                     "--retries", "2", "--prefer-binary"] +
                    [p + "==" + v for p, v in self.packages.items()], output, 600,
                )
            with self.lock:
                self.state = "verifying"
            if not self._probe():
                raise RuntimeError("Dependency import verification failed. Check the installation log.")
            with self.lock:
                self.state = "ready"
            logger.info("iOS optional dependencies installed: %s in %s", self.packages, self.root)
        except Exception as exc:
            logger.exception("iOS dependency installation failed")
            with self.lock:
                self.state, self.error = "failed", str(exc)

    def log_tail(self):
        try:
            with self.log.open("rb") as stream:
                stream.seek(0, 2)
                stream.seek(max(0, stream.tell() - 6000))
                value = stream.read().decode("utf-8", errors="replace")
            return re.sub(r"(https?://)[^\s/@]+:[^\s/@]+@", r"\1[redacted]@", value)
        except OSError:
            return ""


ios_dependencies = {profile: IOSDependencies(profile) for profile in PROFILES}
