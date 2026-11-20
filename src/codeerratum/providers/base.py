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
