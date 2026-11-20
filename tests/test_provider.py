"""Provider tests: JSON Schema enforcement, parse retry, failure logging."""

import json

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
