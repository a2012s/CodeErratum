"""Model endpoint probe — green tick / red cross relies on these contracts."""

from __future__ import annotations

import json

import pytest

from codeerratum.model_check import probe_model
from codeerratum.web import WebApp


class _Reply:
    def __init__(self, status: int, body: dict | None = None):
        self.status_code = status
        self._body = body or {}

    def json(self):
        return self._body


class _Client:
    """Context-manager stand-in returning a queued reply and recording the request."""

    def __init__(self, reply: _Reply):
        self.reply = reply
        self.sent: dict = {}

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def post(self, url, headers=None, json=None):
        self.sent = {"url": url, "headers": headers or {}, "json": json or {}}
        return self.reply


def _patch_client(monkeypatch, reply: _Reply) -> _Client:
    import httpx

    stub = _Client(reply)

    def factory(*_args, **_kwargs):
        return stub

    monkeypatch.setattr(httpx, "Client", factory)
    return stub


def test_ok_with_choices(monkeypatch):
    stub = _patch_client(monkeypatch, _Reply(200, {"choices": [{"message": {"content": "pong"}}]}))
    result = probe_model("gpt-x", "https://api.test/v1/", "sk-abc")
    assert result["ok"] is True
    assert result["status"] == 200
    assert "可用" in result["detail"]
    assert stub.sent["url"] == "https://api.test/v1/chat/completions"
    assert stub.sent["headers"]["Authorization"] == "Bearer sk-abc"
    assert stub.sent["json"]["max_tokens"] == 1


def test_no_key_sends_no_auth_header(monkeypatch):
    stub = _patch_client(monkeypatch, _Reply(200, {"choices": [{}]}))
    probe_model("m", "https://api.test", "")
    assert "Authorization" not in stub.sent["headers"]


@pytest.mark.parametrize(
    ("status", "fragment"),
    [
        (401, "鉴权失败"),
        (403, "鉴权失败"),
        (404, "404"),
        (429, "限流"),
        (500, "服务端错误"),
        (400, "请求被拒"),
    ],
)
def test_http_errors_are_red_crosses(monkeypatch, status, fragment):
    _patch_client(monkeypatch, _Reply(status))
    result = probe_model("m", "https://api.test")
    assert result["ok"] is False
    assert result["status"] == status
    assert fragment in result["detail"]


def test_empty_choices_is_failure(monkeypatch):
    _patch_client(monkeypatch, _Reply(200, {"choices": []}))
    result = probe_model("m", "https://api.test")
    assert result["ok"] is False
    assert "choices" in result["detail"]


def test_bad_base_url_never_hits_network(monkeypatch):
    import httpx

    def boom(*_args, **_kwargs):
        raise AssertionError("should not call network")

    monkeypatch.setattr(httpx, "Client", boom)
    result = probe_model("m", "file:///etc/passwd")
    assert result["ok"] is False
    assert "http" in result["detail"]


def test_missing_model_id(monkeypatch):
    import httpx

    monkeypatch.setattr(httpx, "Client", lambda *_a, **_k: pytest.fail("no network expected"))
    assert probe_model("", "https://api.test")["ok"] is False


def test_check_model_resolves_keep_sentinel(monkeypatch, tmp_path):
    """A saved key must be used when the row sends __KEEP__, else false red cross."""
    import httpx

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    (repo / ".codeerratum").mkdir()
    (repo / ".codeerratum" / "config.json").write_text(
        json.dumps(
            {
                "mode": "quick",
                "models": [
                    {
                        "model_id": "saved",
                        "base_url": "https://saved.test/v1",
                        "api_key": "sk-preserved",
                        "family": "openai",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    seen: dict = {}

    class Client:
        def __enter__(self):
            return self

        def __exit__(self, *_a):
            return False

        def post(self, url, headers=None, json=None):
            seen["auth"] = (headers or {}).get("Authorization")
            return _Reply(200, {"choices": [{}]})

    monkeypatch.setattr(httpx, "Client", lambda *_a, **_k: Client())
    app = WebApp(repo)
    result = app.check_model(
        {"model_id": "saved", "base_url": "https://saved.test/v1", "api_key": "__KEEP__"}
    )
    assert result["ok"] is True
    assert seen["auth"] == "Bearer sk-preserved"
