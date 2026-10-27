"""页面语言与平台：URL 显式指定优先，否则沿用本地缓存的选择（默认中文/Android）。"""
import pytest

from perfcattle.core.settings_cache import ApmSettingsCache
from perfcattle.web import app, startup_url


@pytest.fixture(autouse=True)
def isolated_settings(tmp_path, monkeypatch):
    monkeypatch.setattr(
        ApmSettingsCache, "CACHE_PATH", str(tmp_path / "apm_settings.json")
    )


def _index_body(query=""):
    client = app.test_client()
    response = client.get("/?" + query if query else "/")
    assert response.status_code == 200
    return response.get_data(as_text=True)


def _remember(**values):
    client = app.test_client()
    response = client.get("/apm/settings", query_string=values)
    assert response.json["status"] == 1


def test_index_defaults_to_chinese():
    body = _index_body("platform=Android")
    assert 'lang="zh-CN"' in body
    assert "性能牛" in body
    assert "PERFCATTLE" not in body


def test_index_honours_explicit_chinese():
    assert "性能牛" in _index_body("platform=Android&lan=cn")


def test_index_honours_explicit_english():
    assert "PERFCATTLE" in _index_body("platform=Android&lan=en")


def test_index_falls_back_to_chinese_for_unknown_language():
    assert "性能牛" in _index_body("platform=Android&lan=fr")


def test_index_reuses_remembered_language():
    _remember(language="en")
    assert "PERFCATTLE" in _index_body("platform=Android")


def test_index_ignores_unknown_remembered_language():
    _remember(language="fr")
    assert "性能牛" in _index_body("platform=Android")


def test_explicit_language_wins_over_remembered():
    _remember(language="en")
    assert "性能牛" in _index_body("platform=Android&lan=cn")


def test_index_reuses_remembered_platform():
    _remember(platform="iOS")
    assert "var platform = 'iOS'" in _index_body()


def test_explicit_platform_wins_over_remembered():
    _remember(platform="iOS")
    assert "var platform = 'Android'" in _index_body("platform=Android")


def test_startup_url_uses_remembered_preferences():
    _remember(language="en", platform="iOS")
    assert startup_url("127.0.0.1", 50003).endswith(
        "/?platform=iOS&lan=en"
    )


def test_startup_url_falls_back_to_defaults():
    assert startup_url("127.0.0.1", 50003).endswith(
        "/?platform=Android&lan=cn"
    )
