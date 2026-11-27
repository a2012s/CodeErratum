"""Provider protocol and shared types."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Protocol

from pydantic import BaseModel, Field


class Usage(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0


class Completion(BaseModel):
    text: str
    parsed: BaseModel | None = None
    usage: Usage = Field(default_factory=Usage)
    raw_request_hash: str
    latency_ms: int
    finish_reason: str = "stop"
    # True when the result came from a previous run instead of a paid call. It
    # rides on the completion so every consumer that counts tokens, latency or
    # seats sees the same truth the caller does.
    cached: bool = False


class ParseFailure(BaseModel):
    """Persisted sample for diagnosing structured-output instability."""

    model_id: str
    raw_text: str
    error: str
    attempt: int
    timestamp: str


class Provider(Protocol):
    family: str

    async def complete(
        self,
        model_id: str,
        messages: list[dict[str, str]],
        schema: type[BaseModel] | None,
        temperature: float = 0.0,
        max_tokens: int = 2048,
    ) -> Completion: ...


def schema_to_json_schema(schema: type[BaseModel]) -> dict[str, Any]:
    """Get JSON Schema from a pydantic model.

    Keep $defs — StrEnum fields are emitted as $ref pointers into it. Stripping
    definitions breaks strict json_schema mode on OpenAI-compatible endpoints.
    Also drop titles only at the root; provider needs a stable schema name.
    """
    raw = schema.model_json_schema()
    if "title" not in raw:
        raw["title"] = schema.__name__
    return raw


def to_strict_json_schema(node: Any) -> Any:
    """Rewrite a pydantic schema into the subset OpenAI strict mode accepts.

    Pydantic emits neither ``additionalProperties: false`` nor a ``required`` list
    covering every property, and expresses ``str | None`` as ``anyOf`` — all three
    are rejected by OpenAI with HTTP 400. Missing this made the primary target
    provider unusable while lenient compatible endpoints still worked.
    """
    if isinstance(node, list):
        return [to_strict_json_schema(item) for item in node]
    if not isinstance(node, dict):
        return node

    out = {key: to_strict_json_schema(value) for key, value in node.items()}

    branches = out.get("anyOf")
    if isinstance(branches, list) and len(branches) == 2:
        nulls = [b for b in branches if isinstance(b, dict) and b.get("type") == "null"]
        others = [b for b in branches if isinstance(b, dict) and b.get("type") != "null"]
        if len(nulls) == 1 and len(others) == 1 and isinstance(others[0].get("type"), str):
            merged = dict(others[0])
            merged["type"] = [merged["type"], "null"]
            for key in ("description", "title", "default"):
                if key in out and key not in merged:
                    merged[key] = out[key]
            return merged

    if out.get("type") == "object" or "properties" in out:
        properties = out.get("properties", {})
        out["additionalProperties"] = False
        out["required"] = list(properties.keys())
    return out


def request_hash(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:16]


def cache_key(
    model_id: str,
    messages: list[dict[str, str]],
    schema: type[BaseModel] | None = None,
    temperature: float = 0.0,
    max_tokens: int = 2048,
) -> str:
    """Identify a request exactly enough to reuse its answer across runs.

    This is the whole cache: a hit means the model, the prompt, the output budget
    and the output schema are all byte-identical, so only the reply is being
    replayed. ``request_hash`` is not usable here — it hashes the payload *as the
    provider assembled it*, which differs once a repair retry or a json_schema
    fallback has mutated it, and a short 16-hex digest is a bad primary key for a
    table that is meant to survive for months.
    """
    payload: dict[str, Any] = {
        "model": model_id,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "schema": schema.model_json_schema() if schema is not None else None,
    }
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()
