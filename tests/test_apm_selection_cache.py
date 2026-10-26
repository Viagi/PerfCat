# -*- encoding: utf-8 -*-
"""APM selection cache tests."""
import json
import importlib
from unittest.mock import patch

import pytest
from flask import Flask

from perfcattle.core.selection_cache import ApmSelectionCache
from perfcattle.core.settings_cache import ApmSettingsCache
from perfcattle.view.apm_api import apm_api

apm_api_module = importlib.import_module("perfcattle.view.apm_api")


@pytest.fixture
def cache_path(tmp_path):
    return tmp_path / "apm_selection.json"


def test_save_selection_handles_target_cache_lifecycle(cache_path):
    with patch.object(ApmSelectionCache, "CACHE_PATH", str(cache_path)):
        cache = ApmSelectionCache.save_selection(
            platform="Android",
            device="serial(Model)",
            package_name="com.example.app",
            process_name="com.example.app:sub",
        )
        selection = cache["Android"]["devices"]["serial(Model)"]
        assert cache["Android"]["lastDevice"] == "serial(Model)"
        assert selection == {
            "packageName": "com.example.app",
            "processName": "com.example.app:sub",
        }

        cache = ApmSelectionCache.save_selection(
            platform="Android",
            device="serial(Model)",
        )
        selection = cache["Android"]["devices"]["serial(Model)"]
        assert selection["packageName"] == "com.example.app"
        assert selection["processName"] == "com.example.app:sub"

        cache = ApmSelectionCache.save_selection(
            platform="Android",
            device="serial(Model)",
            package_name="",
            process_name="",
        )

        selection = cache["Android"]["devices"]["serial(Model)"]
        assert selection["packageName"] == "com.example.app"
        assert selection["processName"] == "com.example.app:sub"

        cache = ApmSelectionCache.save_selection(
            platform="Android",
            device="serial(Model)",
            package_name="com.example.app",
            process_name="",
        )

        selection = cache["Android"]["devices"]["serial(Model)"]
        assert selection["packageName"] == "com.example.app"
        assert selection["processName"] == "com.example.app:sub"

        cache = ApmSelectionCache.save_selection(
            platform="Android",
            device="serial(Model)",
            package_name="com.new.app",
            process_name="",
        )

        selection = cache["Android"]["devices"]["serial(Model)"]
        assert selection["packageName"] == "com.new.app"
        assert selection["processName"] == ""
        assert json.loads(cache_path.read_text(encoding="utf-8")) == cache


def test_apm_selection_endpoint_preserves_cache_on_empty_target(cache_path):
    app = Flask(__name__)
    app.register_blueprint(apm_api)

    with patch.object(ApmSelectionCache, "CACHE_PATH", str(cache_path)):
        client = app.test_client()
        response = client.post(
            "/apm/selection",
            data={
                "platform": "Android",
                "device": "serial(Model)",
                "packageName": "com.tencent.mm",
                "processName": "com.tencent.mm:appbrand1",
            },
        )

        assert response.json["status"] == 1
        client.post(
            "/apm/selection",
            data={
                "platform": "Android",
                "device": "",
                "packageName": "",
                "processName": "",
            },
        )
        client.post(
            "/apm/selection",
            data={
                "platform": "Android",
                "device": "serial(Model)",
                "packageName": "",
                "processName": "",
            },
        )
        response = client.get("/apm/selection")

        assert response.json["status"] == 1
        selection = response.json["cache"]["Android"]["devices"]["serial(Model)"]
        assert selection["packageName"] == "com.tencent.mm"
        assert selection["processName"] == "com.tencent.mm:appbrand1"
        assert json.loads(cache_path.read_text(encoding="utf-8")) == response.json["cache"]


def test_apm_settings_endpoint_persists_to_local_file(tmp_path):
    cache_path = tmp_path / "apm_settings.json"
    app = Flask(__name__)
    app.register_blueprint(apm_api)

    with patch.object(ApmSettingsCache, "CACHE_PATH", str(cache_path)):
        response = app.test_client().get(
            "/apm/settings",
            query_string={
                "cpuWarning": "80",
                "duration": "15",
                "perfcattle_host": "127.0.0.1:6006",
                "host_switch": "1",
                "recordMaxSize": "720",
                "recordMaxFps": "60",
                "recordVideoBitRate": "4M",
                "recordNoAudio": "1",
                "defaultPackageName": "com.tencent.mm",
                "defaultProcessName": "com.tencent.mm:appbrand1",
                "collectCpu": "0",
                "collectNetwork": "1",
                "collectRecordScreen": "1",
                "collectThermal": "1",
            },
        )

        assert response.json["status"] == 1
        settings = json.loads(cache_path.read_text(encoding="utf-8"))
        assert settings["cpuWarning"] == "80"
        assert settings["duration"] == "15"
        assert settings["perfcattle_host"] == "127.0.0.1:6006"
        assert settings["defaultPackageName"] == "com.tencent.mm"
        assert settings["defaultProcessName"] == "com.tencent.mm:appbrand1"
        assert settings["collectCpu"] == "0"
        assert settings["collectNetwork"] == "1"
        assert settings["collectRecordScreen"] == "1"
        assert settings["collectThermal"] == "1"
        assert "Set-Cookie" not in response.headers


def test_file_io_endpoints_default_to_normal_model(monkeypatch):
    class FakeFileIO:
        last_source = "storaged"
        fallback_reason = None
        last_sample_time = 1715752800.0

        def setAndroidFileIO(self):
            return 0, 0

        def getFileIOData(self, noLog=False):
            return 1, 2

    fake_file_io = FakeFileIO()
    app = Flask(__name__)
    app.register_blueprint(apm_api)

    monkeypatch.setattr(
        apm_api_module.d,
        "getIdbyDevice",
        lambda device, platform: "device-1",
    )
    monkeypatch.setattr(
        apm_api_module.FileIO,
        "getObject",
        classmethod(lambda cls, *args, **kwargs: fake_file_io),
    )

    client = app.test_client()
    base_query = {
        "platform": "Android",
        "pkgname": "com.tencent.mm",
        "device": "10.0.10.110:39527(23127PN0CC)",
        "process": "10729:com.tencent.mm:appbrand1",
    }

    set_response = client.get("/apm/set/file_io", query_string=base_query)
    get_response = client.get("/apm/file_io", query_string=base_query)

    assert set_response.status_code == 200
    assert set_response.json["status"] == 1
    assert get_response.status_code == 200
    assert get_response.json["status"] == 1
    assert get_response.json["read"] == 1
    assert get_response.json["write"] == 2
    assert get_response.json["sample_time"] == 1715752800.0


def test_thermal_endpoint_returns_sensor_list(monkeypatch):
    class FakeThermal:
        def getThermalTemp(self, noLog=True):
            return [{"type": "cpu-thermal", "temp": "45000"}]

    app = Flask(__name__)
    app.register_blueprint(apm_api)

    monkeypatch.setattr(
        apm_api_module.d,
        "getIdbyDevice",
        lambda device, platform: "device-1",
    )
    monkeypatch.setattr(
        apm_api_module.ThermalSensor,
        "getObject",
        classmethod(lambda cls, *args, **kwargs: FakeThermal()),
    )

    response = app.test_client().get(
        "/apm/thermal",
        query_string={"platform": "Android", "device": "serial(Model)"},
    )

    assert response.status_code == 200
    assert response.json == {
        "status": 1,
        "available": True,
        "sensors": [{"type": "cpu-thermal", "temp": "45000"}],
    }
