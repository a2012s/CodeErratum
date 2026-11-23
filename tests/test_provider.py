"""Provider tests: JSON Schema enforcement, parse retry, failure logging."""

import json

import httpx
import pytest

from codeerratum.providers.base import schema_to_json_schema
from codeerratum.providers.openai_compat import OpenAICompatProvider
from codeerratum.review.schema import LLMReview


def test_schema_keeps_defs_for_refs():
    schema = schema_to_json_schema(LLMReview)
    assert "$defs" in schema
    dumped = json.dumps(schema)
    assert "$ref" in dumped
    assert schema["title"] == "LLMReview"


@pytest.mark.asyncio
async def test_parse_retry_on_bad_json(monkeypatch):
    """First response is malformed, second is valid — retry must succeed."""
    provider = OpenAICompatProvider("http://localhost", "key")

    valid = LLMReview(summary="ok", findings=[])
    calls = {"n": 0}

    async def fake_post(payload):
        calls["n"] += 1
        if calls["n"] == 1:
            return {
                "choices": [{"message": {"content": "{not json"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            }
        return {
            "choices": [
                {
                    "message": {"content": valid.model_dump_json()},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        }

    monkeypatch.setattr(provider, "_post", fake_post)
    completion = await provider.complete("m", [{"role": "user", "content": "x"}], schema=LLMReview)
    assert isinstance(completion.parsed, LLMReview)
    assert calls["n"] == 2
    assert provider.parse_stats["retry"] >= 1
    assert len(provider.failure_log) == 1


@pytest.mark.asyncio
async def test_parse_hard_failure_logged(monkeypatch, tmp_path):
    provider = OpenAICompatProvider("http://localhost", "key")

    async def fake_post(payload):
        return {
            "choices": [{"message": {"content": "garbage"}, "finish_reason": "stop"}],
            "usage": {},
        }

    monkeypatch.setattr(provider, "_post", fake_post)
    with pytest.raises(ValueError, match="parse failed"):
        await provider.complete("m", [{"role": "user", "content": "x"}], schema=LLMReview)
    assert len(provider.failure_log) == 2
    out = tmp_path / "failures.json"
    provider.dump_failures(out)
    assert len(json.loads(out.read_text())) == 2


@pytest.mark.asyncio
async def test_post_reuses_one_client_until_closed(monkeypatch):
    """One TLS pool per provider, not one per unit or per transport retry."""
    provider = OpenAICompatProvider("http://localhost", "key")
    created: list[httpx.AsyncClient] = []
    real_client = httpx.AsyncClient

    def fake_client(*args, **kwargs):
        client = real_client(
            *args,
            transport=httpx.MockTransport(lambda req: httpx.Response(200, json={"ok": True})),
            **kwargs,
        )
        created.append(client)
        return client

    monkeypatch.setattr("codeerratum.providers.openai_compat.httpx.AsyncClient", fake_client)
    await provider._post({"n": 1})
    await provider._post({"n": 2})
    assert len(created) == 1
    await provider.aclose()
    assert created[0].is_closed
    # A closed client is rebuilt lazily instead of failing on the next unit.
    await provider._post({"n": 3})
    assert len(created) == 2
    await provider.aclose()


def test_read_window_is_sized_for_full_length_answers(monkeypatch):
    """120 s could never finish an 8192-token answer at ~50 tok/s.

    The single 120 s timeout silently capped answer *length*: a legitimate long
    review needed ~165 s of generation and was cut off mid-stream every time,
    which surfaced as ``ReadTimeout`` on every other unit.
    """
    monkeypatch.delenv("CODEERRATUM_READ_TIMEOUT", raising=False)
    provider = OpenAICompatProvider("http://localhost", "key")
    assert provider.read_timeout >= 240
    # A dead endpoint must fail fast instead of looking like a slow model.
    assert provider._timeout.connect == 10.0
    assert provider._timeout.read >= 240

    monkeypatch.setenv("CODEERRATUM_READ_TIMEOUT", "420")
    assert OpenAICompatProvider("http://localhost", "k").read_timeout == 420


@pytest.mark.asyncio
async def test_read_timeout_gets_one_retry_not_three(monkeypatch):
    """A read timeout means the endpoint went quiet, not that it was flaky.

    Three full windows would be 12 minutes per unit and a second billed input;
    the policy is one retry, then let the unit fail with an explanation.
    """
    import httpx

    provider = OpenAICompatProvider("http://localhost", "key", timeout=5)
    calls = {"n": 0}
    sleeps: list[float] = []

    async def fake_post(payload):
        calls["n"] += 1
        raise httpx.ReadTimeout("timed out")

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(provider, "_post", fake_post)
    monkeypatch.setattr("codeerratum.providers.openai_compat.asyncio.sleep", fake_sleep)

    with pytest.raises(httpx.ReadTimeout):
        await provider._post_with_retry({"model": "m"})

    assert calls["n"] == 2, "read timeout must not burn the full three attempts"
    assert provider.parse_stats["read_timeouts"] == 2
    assert sleeps and sleeps[0] >= 2.0


def test_retry_after_is_honoured_and_capped():
    """A provider asking for 20 s and getting 0.5 s just fails again."""
    import httpx

    provider_cls = OpenAICompatProvider
    request = httpx.Request("POST", "http://x")
    honoured = httpx.Response(429, headers={"retry-after": "7"}, request=request)
    assert (
        provider_cls._retry_after(
            httpx.HTTPStatusError("429", request=request, response=honoured), 0.5
        )
        == 7.0
    )
    capped = httpx.Response(429, headers={"retry-after": "600"}, request=request)
    assert (
        provider_cls._retry_after(
            httpx.HTTPStatusError("429", request=request, response=capped), 0.5
        )
        == 30.0
    )
    silent = httpx.Response(429, request=request)
    assert (
        provider_cls._retry_after(
            httpx.HTTPStatusError("429", request=request, response=silent), 0.5
        )
        == 0.5
    )
