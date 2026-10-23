"""Unity arm/wait/disarm HTTP endpoints."""
from unittest.mock import patch

from flask import Flask

from perfcattle.view.unity_api import unity_api


def make_client():
    app = Flask(__name__)
    app.register_blueprint(unity_api)
    return app.test_client()


def test_wait_endpoint_arms_then_waits():
    client = make_client()

    with patch(
        "perfcattle.view.unity_api.d.getIdbyDevice", return_value="device-id"
    ), patch(
        "perfcattle.view.unity_api.unity_capture.arm",
        return_value={"status": 1, "available": False, "arming": True},
    ) as arm, patch(
        "perfcattle.view.unity_api.unity_capture.wait_connected",
        return_value={"status": 1, "available": True},
    ) as wait:
        response = client.get(
            "/apm/unity/wait",
            query_string={
                "platform": "Android",
                "device": "Pixel",
                "pkgname": "com.example.game",
                "process": "123:game",
                "deep_profiling": "1",
                "timeout": "3",
            },
        )

    assert response.json["available"] is True
    arm.assert_called_once_with(
        deviceId="device-id",
        pkgName="com.example.game",
        pid="123",
        deep_profiling=True,
        areas=None,
        record=False,
    )
    wait.assert_called_once_with(3.0)


def test_arm_endpoint_rejects_ios():
    client = make_client()

    response = client.get("/apm/unity/arm", query_string={"platform": "iOS"})

    assert response.json["available"] is False
    assert "android" in response.json["msg"]


def test_start_endpoint_records_by_default():
    client = make_client()

    with patch(
        "perfcattle.view.unity_api.d.getIdbyDevice", return_value="device-id"
    ), patch(
        "perfcattle.view.unity_api.unity_capture.start",
        return_value={"status": 1, "available": True},
    ) as start:
        response = client.get(
            "/apm/unity/start",
            query_string={
                "platform": "Android",
                "device": "Pixel",
                "pkgname": "com.example.game",
            },
        )

    assert response.json["available"] is True
    assert start.call_args.kwargs["record"] is True


def test_arm_endpoint_can_start_recording_mid_run():
    client = make_client()

    with patch(
        "perfcattle.view.unity_api.d.getIdbyDevice", return_value="device-id"
    ), patch(
        "perfcattle.view.unity_api.unity_capture.arm",
        return_value={"status": 1, "available": True, "recording": True},
    ) as arm:
        response = client.get(
            "/apm/unity/arm",
            query_string={
                "platform": "Android",
                "device": "Pixel",
                "pkgname": "com.example.game",
                "record": "1",
            },
        )

    assert response.json["recording"] is True
    assert arm.call_args.kwargs["record"] is True


def test_arm_endpoint_requires_device_and_app():
    client = make_client()

    response = client.get(
        "/apm/unity/arm",
        query_string={"platform": "Android", "device": "", "pkgname": ""},
    )

    assert response.json["available"] is False
    assert "device and app" in response.json["msg"]


def test_arm_endpoint_keeps_serial_when_device_disconnected():
    client = make_client()

    with patch(
        "perfcattle.view.unity_api.d.getIdbyDevice",
        side_effect=Exception("no device found"),
    ), patch(
        "perfcattle.view.unity_api.unity_capture.arm",
        return_value={"status": 1, "available": False, "arming": True},
    ) as arm:
        response = client.get(
            "/apm/unity/arm",
            query_string={
                "platform": "Android",
                "device": "R5CX21L23NE(Pixel 8)",
                "pkgname": "com.example.game",
            },
        )

    assert response.json["arming"] is True
    assert arm.call_args.kwargs["deviceId"] == "R5CX21L23NE"


def test_disarm_endpoint_forwards_stop_and_discard():
    client = make_client()

    with patch(
        "perfcattle.view.unity_api.unity_capture.disarm",
        return_value={"status": 1, "available": False},
    ) as disarm:
        response = client.get(
            "/apm/unity/disarm", query_string={"stop": "1", "discard": "1"}
        )

    assert response.json["status"] == 1
    disarm.assert_called_once_with(stop=True, discard=True)
