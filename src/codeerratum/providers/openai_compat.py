"""OpenAI-compatible provider — covers OpenAI / DeepSeek / Qwen / Ollama / vLLM.

Structured output strategy (in order):
  1. provider-native json_schema via response_format, rewritten to the strict subset
     OpenAI actually accepts (additionalProperties / required / no bare anyOf)
  2. if the endpoint rejects that with HTTP 400, latch to json_object mode
  3. one repair retry with the validation error fed back to the model

Transient failures (429/5xx/transport) retry with backoff; every parse failure is
persisted. Parse success rate is the v0.1 hard gate, so it is counted per call: a
call that succeeded on the repair attempt is a success, not a failure.
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
from pydantic import BaseModel, ValidationError

from .base import (
    Completion,
    ParseFailure,
    Usage,
    request_hash,
    schema_to_json_schema,
    to_strict_json_schema,
)

_MAX_TRANSPORT_ATTEMPTS = 3


class OpenAICompatProvider:
    family = "openai_compat"

    def __init__(self, base_url: str, api_key: str, timeout: float = 120.0):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        self.parse_stats: dict[str, int] = {
            "calls": 0,
            "first_try": 0,
            "repaired": 0,
            "retry": 0,  # alias of repaired, kept for existing callers
            "failed": 0,
            "schema_fallbacks": 0,
        }
        self.failure_log: list[ParseFailure] = []
        self._use_json_schema = True

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    async def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.post(
                f"{self.base_url}/chat/completions",
                headers=self._headers(),
                json=payload,
            )
            response.raise_for_status()
            return response.json()

    async def _post_with_retry(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Retry transient failures instead of dropping the whole review unit."""
        delay = 0.5
        last_error: Exception | None = None
        for attempt in range(_MAX_TRANSPORT_ATTEMPTS):
            try:
                return await self._post(payload)
            except httpx.HTTPStatusError as exc:
                status = exc.response.status_code
                if status == 429 or 500 <= status < 600:
                    last_error = exc
                    if attempt < _MAX_TRANSPORT_ATTEMPTS - 1:
                        await asyncio.sleep(delay)
                        delay *= 3
                        continue
                raise
            except httpx.TransportError as exc:
                last_error = exc
                if attempt < _MAX_TRANSPORT_ATTEMPTS - 1:
                    await asyncio.sleep(delay)
                    delay *= 3
                    continue
                raise
        raise last_error or RuntimeError("request failed without a recorded error")

    def _build_payload(
        self,
        model_id: str,
        messages: list[dict[str, str]],
        temperature: float,
        max_tokens: int,
        js: dict[str, Any] | None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": model_id,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if js:
            if self._use_json_schema:
                payload["response_format"] = {
                    "type": "json_schema",
                    "json_schema": {
                        "name": js.get("title", "structured_output"),
                        "strict": True,
                        "schema": to_strict_json_schema(js),
                    },
                }
            else:
                payload["response_format"] = {"type": "json_object"}
        return payload

    async def complete(
        self,
        model_id: str,
        messages: list[dict[str, str]],
        schema: type[BaseModel] | None = None,
        temperature: float = 0.0,
        max_tokens: int = 4096,
    ) -> Completion:
        t0 = time.monotonic()
        self.parse_stats["calls"] += 1
        js = schema_to_json_schema(schema) if schema else None
        payload = self._build_payload(model_id, messages, temperature, max_tokens, js)

        rhash = request_hash(payload)
        raw_text = ""
        parsed: BaseModel | None = None
        usage = Usage()
        finish_reason = "stop"
        last_error = ""

        try:
            data = await self._post_with_retry(payload)
        except httpx.HTTPStatusError as exc:
            if js and self._use_json_schema and exc.response.status_code == 400:
                # Endpoint does not accept json_schema: latch to json_object so the
                # remaining units do not pay for another 400.
                self._use_json_schema = False
                self.parse_stats["schema_fallbacks"] += 1
                payload = self._build_payload(model_id, messages, temperature, max_tokens, js)
                data = await self._post_with_retry(payload)
            else:
                raise

        for attempt in range(2):
            choice = data.get("choices", [{}])[0]
            raw_text = choice.get("message", {}).get("content", "") or ""
            finish_reason = choice.get("finish_reason", "stop")
            u = data.get("usage", {}) or {}
            usage = Usage(
                input_tokens=u.get("prompt_tokens", 0),
                output_tokens=u.get("completion_tokens", 0),
                cached_tokens=(u.get("prompt_tokens_details") or {}).get("cached_tokens", 0),
            )
            if schema is None:
                parsed = None
                self.parse_stats["first_try"] += 1
                break
            try:
                parsed = schema.model_validate_json(raw_text)
                if attempt == 0:
                    self.parse_stats["first_try"] += 1
                else:
                    self.parse_stats["repaired"] += 1
                    self.parse_stats["retry"] += 1
                break
            except ValidationError as exc:
                last_error = str(exc)[:1000]
                self.failure_log.append(
                    ParseFailure(
                        model_id=model_id,
                        raw_text=raw_text[:4000],
                        error=last_error,
                        attempt=attempt,
                        timestamp=datetime.now(UTC).isoformat(),
                    )
                )
                if attempt == 0:
                    repair = json.dumps(
                        {
                            "error": "Your previous output failed schema validation.",
                            "validation_error": last_error,
                            "instruction": (
                                "Return ONLY a valid JSON object matching the schema. "
                                "No markdown fences, no commentary."
                            ),
                        },
                        ensure_ascii=False,
                    )
                    payload = dict(payload)
                    payload["messages"] = messages + [
                        {"role": "assistant", "content": raw_text},
                        {"role": "user", "content": repair},
                    ]
                    if finish_reason == "length":
                        # Output was truncated, not malformed: give the retry room.
                        payload["max_tokens"] = max(payload["max_tokens"] * 2, 8192)
                    data = await self._post_with_retry(payload)

        if schema is not None and parsed is None:
            self.parse_stats["failed"] += 1
            raise ValueError(
                f"Structured output parse failed after 2 attempts "
                f"(model={model_id}): {last_error[:300]}"
            )

        return Completion(
            text=raw_text,
            parsed=parsed,
            usage=usage,
            raw_request_hash=rhash,
            latency_ms=int((time.monotonic() - t0) * 1000),
            finish_reason=finish_reason,
        )

    @property
    def success_rate(self) -> float | None:
        """Share of calls that yielded a parsed object (repairs counted as success)."""
        calls = self.parse_stats["calls"]
        if not calls:
            return None
        return (self.parse_stats["first_try"] + self.parse_stats["repaired"]) / calls

    def dump_failures(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps([f.model_dump() for f in self.failure_log], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
