"""Lightweight endpoint probe for the settings drawer's green tick / red cross.

One minimal chat completion (``max_tokens=1``) answers three questions at once:
is the endpoint reachable, is the key accepted, and does the model id resolve?
Unlike the review provider there is no retry, no schema negotiation and no
persisted state — a check must be fast and side-effect free. The API key is used
for the request only and never appears in the result.
"""

from __future__ import annotations

import time

import httpx

DEFAULT_TIMEOUT = 10.0
MAX_TIMEOUT = 30.0


def _explain(exc: Exception, timeout: float) -> str:
    if isinstance(exc, httpx.TimeoutException):
        return f"连接超时（{timeout:g}s）"
    if isinstance(exc, httpx.ConnectError):
        reason = str(exc).strip().replace("\n", " ")[:160]
        return f"无法连接端点（地址或网络不可达）{f'：{reason}' if reason else ''}"
    if isinstance(exc, httpx.TransportError):
        return f"网络错误：{type(exc).__name__}"
    return f"请求失败：{type(exc).__name__}"


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
        result["detail"] = "缺少 model_id"
        return result
    if not base_url.startswith(("http://", "https://")):
        result["detail"] = "base_url 必须以 http:// 或 https:// 开头"
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
        if response.status_code == 401 or response.status_code == 403:
            result["detail"] = f"鉴权失败（{response.status_code}）：检查 api_key"
            return result
        if response.status_code == 404:
            result["detail"] = "404：检查 base_url 与 model_id"
            return result
        if response.status_code == 429:
            result["detail"] = "429：端点已限流，稍后重试"
            return result
        if response.status_code >= 500:
            result["detail"] = f"服务端错误（{response.status_code}）"
            return result
        if response.status_code >= 400:
            result["detail"] = f"请求被拒（{response.status_code}）"
            return result
        data = response.json()
        if not data.get("choices"):
            result["detail"] = "响应无 choices，端点可能不兼容 OpenAI 协议"
            return result
    except (ValueError, TypeError) as exc:
        # A non-JSON 200 response is a compatibility problem, not a crash.
        result["latency_ms"] = int((time.monotonic() - t0) * 1000)
        result["detail"] = f"响应不是合法 JSON：{type(exc).__name__}"
        return result
    except Exception as exc:  # noqa: BLE001 - transport errors are the common case
        result["latency_ms"] = int((time.monotonic() - t0) * 1000)
        result["detail"] = _explain(exc, timeout)
        return result

    result["ok"] = True
    result["detail"] = f"可用 · {result['latency_ms']}ms"
    return result
