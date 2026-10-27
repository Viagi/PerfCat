"""页面语言默认中文，显式传入 lan=en 时才切换英文。"""
from perfcattle.web import app


def _index_body(query):
    client = app.test_client()
    response = client.get("/?" + query)
    assert response.status_code == 200
    return response.get_data(as_text=True)


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
