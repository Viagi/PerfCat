"""Device API launch endpoint tests."""
from unittest.mock import patch

from flask import Flask

from perfcattle.view.device_api import device_api


def make_client():
    app = Flask(__name__)
    app.register_blueprint(device_api)
    return app.test_client()


def test_launch_package_endpoint_launches_selected_app():
    client = make_client()

    with patch(
        "perfcattle.view.device_api.d.getIdbyDevice", return_value="abc123"
    ), patch(
        "perfcattle.view.device_api.d.launchApp", return_value=True
    ) as launch:
        response = client.get(
            "/package/launch",
            query_string={
                "platform": "Android",
                "device": "abc123",
                "pkgname": "com.example.app",
            },
        )

    assert response.json == {"status": 1, "msg": "App launched"}
    launch.assert_called_once_with("abc123", "com.example.app", "Android")


def test_launch_package_endpoint_requires_device_and_package():
    client = make_client()

    response = client.get(
        "/package/launch",
        query_string={"platform": "Android", "device": "", "pkgname": ""},
    )

    assert response.json == {"status": 0, "msg": "Select a device and app first"}


def test_launch_package_endpoint_reports_launch_failure():
    client = make_client()

    with patch(
        "perfcattle.view.device_api.d.getIdbyDevice", return_value="abc123"
    ), patch(
        "perfcattle.view.device_api.d.launchApp",
        side_effect=Exception("No launcher activity found for this package"),
    ):
        response = client.get(
            "/package/launch",
            query_string={
                "platform": "Android",
                "device": "abc123",
                "pkgname": "com.example.app",
            },
        )

    assert response.json == {
        "status": 0,
        "msg": "No launcher activity found for this package",
    }
