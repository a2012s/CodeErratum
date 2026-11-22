"""Lightweight endpoint probe for the settings drawer's green tick / red cross.

One minimal chat completion (``max_tokens=1``) answers three questions at once:
is the endpoint reachable, is the key accepted, and does the model id resolve?
Unlike the review provider there is no retry, no schema negotiation and no
persisted state — a check must be fast and side-effect free. The API key is used
for the request only and never appears in the result.
"""

from __future__ import annotations

import time
from urllib.parse import urlsplit

import httpx

from .i18n import t

DEFAULT_TIMEOUT = 10.0
MAX_TIMEOUT = 30.0


def _explain(exc: Exception, timeout: float) -> str:
    if isinstance(exc, httpx.TimeoutException):
        return t("model.timeout", seconds=timeout)
    if isinstance(exc, httpx.ConnectError):
        reason = str(exc).strip().replace("\n", " ")[:160]
        suffix = t("model.reason_suffix", reason=reason) if reason else ""
        return t("model.unreachable", suffix=suffix)
    if isinstance(exc, httpx.TransportError):
        return t("model.network_error", error=type(exc).__name__)
    return t("model.request_failed", error=type(exc).__name__)


def probe_model(
    model_id: str,
    base_url: str,
    api_key: str = "",
    timeout: float = DEFAULT_TIMEOUT,
) -> dict:
    """Probe one model; return ``{ok, status, latency_ms, detail}``.

    Never raises for endpoint problems — a red cross with a reason is the whole
    point. Only programming errors (non-string inputs) are left to the caller.
    """
    result: dict = {"ok": False, "status": None, "latency_ms": 0, "detail": ""}
    model_id = str(model_id or "").strip()
    base_url = str(base_url or "").strip().rstrip("/")
    if not model_id:
        result["detail"] = t("model.missing_id")
        return result
    if not base_url.startswith(("http://", "https://")):
        result["detail"] = t("model.bad_base_url")
        return result

    timeout = max(1.0, min(float(timeout), MAX_TIMEOUT))
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    payload = {
        "model": model_id,
        "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": 1,
        "temperature": 0,
    }

    t0 = time.monotonic()
    try:
        with httpx.Client(timeout=timeout) as client:
            response = client.post(f"{base_url}/chat/completions", headers=headers, json=payload)
        result["status"] = response.status_code
        result["latency_ms"] = int((time.monotonic() - t0) * 1000)
        if 300 <= response.status_code < 400:
            # A console page answers POST with a redirect to a 404 page. We do
            # not follow it (that would leak the Authorization header to a
            # different host); name the likely misconfiguration instead of
            # failing later as an opaque JSONDecodeError.
            location = getattr(response, "headers", {}).get("location", "")
            target = ""
            if location:
                try:
                    parsed = urlsplit(location)
                    target = parsed.netloc or parsed.path
                except ValueError:
                    target = ""
            result["detail"] = t(
                "model.redirect",
                status=response.status_code,
                suffix=t("model.target_suffix", target=target) if target else "",
            )
            return result
        if response.status_code == 401 or response.status_code == 403:
            detail = t("model.auth", status=response.status_code)
            # sk-sp- keys belong to Aliyun Token Plan / Coding Plan and are
            # rejected by the pay-as-you-go dashscope endpoint. Name the
            # mismatch instead of leaving the user guessing at a valid key.
            if api_key.startswith("sk-sp-") and "dashscope.aliyuncs.com" in base_url:
                detail += t("model.token_plan_hint")
            result["detail"] = detail
            return result
        if response.status_code == 404:
            result["detail"] = t("model.not_found")
            return result
        if response.status_code == 429:
            result["detail"] = t("model.rate_limited")
            return result
        if response.status_code >= 500:
            result["detail"] = t("model.server_error", status=response.status_code)
            return result
        if response.status_code >= 400:
            result["detail"] = t("model.rejected", status=response.status_code)
            return result
        data = response.json()
        if not data.get("choices"):
            result["detail"] = t("model.no_choices")
            return result
    except (ValueError, TypeError) as exc:
        # A non-JSON 200 response is a compatibility problem, not a crash.
        result["latency_ms"] = int((time.monotonic() - t0) * 1000)
        result["detail"] = t("model.bad_json", error=type(exc).__name__)
        return result
    except Exception as exc:  # noqa: BLE001 - transport errors are the common case
        result["latency_ms"] = int((time.monotonic() - t0) * 1000)
        result["detail"] = _explain(exc, timeout)
        return result

    result["ok"] = True
    result["detail"] = t("model.ok", ms=result["latency_ms"])
    return result
