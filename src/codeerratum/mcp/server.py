"""MCP server over stdio — lets a coding agent call the reviewer as a tool.

Design notes that are not obvious from the protocol spec:

* One JSON object per line, no batching and no concurrency. Every tool call blocks
  the client anyway, and a single loop means no interleaved writes.
* Nothing but protocol messages may reach stdout. Progress and warnings go to
  stderr, because the client parses stdout as JSON and one stray line kills the
  session.
* ``review_diff`` is allowed to take a minute. The caller is an agent that can wait,
  which is exactly why this interface is better than asking a human to run the CLI.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import IO, Any

from ..pipeline import (
    ConfigError,
    DiffFailed,
    NoChanges,
    NotARepository,
    execute_review,
    find_repo_root,
    store_for,
)

PROTOCOL_VERSION = "2024-11-05"

TOOLS: list[dict] = [
    {
        "name": "review_diff",
        "description": (
            "Review a git diff and return an executable work order (markdown). "
            "Use this after generating or changing code. Findings carry a verbatim "
            "anchor, an evidence grade and an acceptance command."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "diff": {
                    "type": "string",
                    "description": "Git revspec, e.g. HEAD~1, main...feature. Default HEAD~1.",
                },
                "repo": {
                    "type": "string",
                    "description": "Absolute path to the repository. Default: the server's cwd.",
                },
                "mode": {
                    "type": "string",
                    "enum": ["quick", "dual", "committee"],
                    "description": "quick = one model; dual = two vendors plus falsification.",
                },
                "max_units": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": 500,
                    "description": (
                        "Units to review. Omit for auto (every unit when the diff is "
                        "small, else the 60 highest-risk). Pass 0 for every unit."
                    ),
                },
                "house_rules": {
                    "type": "string",
                    "description": (
                        "Project conventions appended to the system prompt, e.g. "
                        "'we do not use type hints'. Cannot override output format."
                    ),
                },
                "format": {
                    "type": "string",
                    "enum": ["markdown", "summary"],
                    "description": (
                        "markdown (default) returns the work order; summary returns counts."
                    ),
                },
            },
        },
    },
    {
        "name": "list_runs",
        "description": "List previous review runs with counts and timestamps.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "repo": {"type": "string", "description": "Absolute path to the repository."},
                "limit": {"type": "integer", "minimum": 1, "maximum": 200, "default": 20},
            },
        },
    },
    {
        "name": "get_finding",
        "description": (
            "Fetch one finding by id (or id prefix) with its current accept/reject status."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "finding_id": {"type": "string", "description": "Finding id or unique prefix."},
                "repo": {"type": "string", "description": "Absolute path to the repository."},
            },
            "required": ["finding_id"],
        },
    },
    {
        "name": "check_docs_drift",
        "description": (
            "Compare the saved signature baseline against the current code and report "
            "modules/symbols whose public surface changed."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "repo": {"type": "string", "description": "Absolute path to the repository."}
            },
        },
    },
]


def _text(text: str) -> dict:
    return {"content": [{"type": "text", "text": text}], "isError": False}


def _error(text: str) -> dict:
    return {"content": [{"type": "text", "text": text}], "isError": True}


def _root(args: dict) -> Path:
    """Explicit argument wins; then --repo from the CLI; then the process cwd."""
    candidate = args.get("repo") or os.environ.get("CODEERRATUM_MCP_REPO") or "."
    return find_repo_root(Path(candidate))


def _tool_review_diff(args: dict) -> dict:
    try:
        outcome = execute_review(
            _root(args),
            diff_spec=str(args.get("diff") or "HEAD~1"),
            mode=str(args.get("mode") or ""),
            # Omission means auto, the same 60-unit gate the CLI and schema
            # promise; 0 still means "review everything". A hardcoded 10 here
            # silently reviewed a sixth of the diff through MCP only.
            max_units=(int(args["max_units"]) if args.get("max_units") is not None else None),
            house_rules=str(args.get("house_rules") or ""),
        )
    except NoChanges as exc:
        return _text(f"没有可审查的变更：{exc}")
    except (NotARepository, ConfigError, DiffFailed) as exc:
        return _error(str(exc))

    if (args.get("format") or "markdown") == "summary":
        return _text(json.dumps(outcome.summary(), ensure_ascii=False, indent=2))
    trailer = f"\n\n---\n运行产物：`{outcome.run_dir}`"
    return _text(outcome.markdown + trailer)


def _tool_list_runs(args: dict) -> dict:
    rows = store_for(_root(args)).list_runs(limit=int(args.get("limit") or 20))
    if not rows:
        return _text("还没有运行记录。")
    return _text(json.dumps(rows, ensure_ascii=False, indent=2))


def _tool_get_finding(args: dict) -> dict:
    finding_id = str(args.get("finding_id") or "")
    if not finding_id:
        return _error("finding_id is required")
    rows = store_for(_root(args)).find_finding(finding_id)
    if not rows:
        return _error(f"没有以 {finding_id} 开头的发现")
    return _text(json.dumps(rows[0], ensure_ascii=False, indent=2))


def _tool_check_docs_drift(args: dict) -> dict:
    from ..docs import build_index, collect, compare, load_index

    root = _root(args)
    scan = (root / "src") if (root / "src").is_dir() else None
    try:
        modules = collect(root, scan)
    except (NotADirectoryError, OSError) as exc:
        return _error(f"cannot scan sources: {exc}")
    baseline = load_index(root / ".codeerratum" / "docs-index.json")
    if baseline is None:
        return _text("尚无签名基线，先运行 `codeerratum docs`。")
    drifts = compare(baseline, build_index(modules))
    if not drifts:
        return _text("无签名漂移。")
    payload = [
        {
            "kind": d.kind,
            "path": d.path,
            "symbol": d.symbol,
            "detail": d.detail,
            "breaking": d.is_breaking,
        }
        for d in drifts
    ]
    return _text(json.dumps(payload, ensure_ascii=False, indent=2))


_HANDLERS = {
    "review_diff": _tool_review_diff,
    "list_runs": _tool_list_runs,
    "get_finding": _tool_get_finding,
    "check_docs_drift": _tool_check_docs_drift,
}


def _ok(request_id: Any, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _fail(request_id: Any, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def handle(request: dict) -> dict | None:
    """Return the response for one request, or None when nothing must be sent."""
    request_id = request.get("id")
    method = request.get("method")

    if request_id is None:
        # Notifications (initialize/initialized, cancelled) never get a response.
        return None

    if method == "initialize":
        from .. import __version__

        return _ok(
            request_id,
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "codeerratum", "version": __version__},
                "instructions": (
                    "Call review_diff after writing code. Findings are anchored to exact "
                    "lines and include an acceptance command; do not claim a fix without "
                    "running it."
                ),
            },
        )
    if method == "ping":
        return _ok(request_id, {})
    if method == "tools/list":
        return _ok(request_id, {"tools": TOOLS})
    if method == "tools/call":
        params = request.get("params") or {}
        name = params.get("name")
        handler = _HANDLERS.get(name)
        if handler is None:
            return _fail(request_id, -32602, f"unknown tool: {name}")
        try:
            return _ok(request_id, handler(params.get("arguments") or {}))
        except Exception as exc:  # noqa: BLE001 - a tool crash must not end the session
            return _ok(request_id, _error(f"{type(exc).__name__}: {exc}"))

    return _fail(request_id, -32601, f"method not supported: {method}")


def _write(stream: IO[str], payload: dict) -> None:
    stream.write(json.dumps(payload, ensure_ascii=False) + "\n")
    stream.flush()


def serve(stdin: IO[str] | None = None, stdout: IO[str] | None = None) -> int:
    """Read requests line by line until EOF. Returns a process exit code."""
    inp = stdin or sys.stdin
    out = stdout or sys.stdout
    for raw in inp:
        line = raw.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError as exc:
            _write(out, _fail(None, -32700, f"parse error: {exc}"))
            continue
        if not isinstance(request, dict):
            _write(out, _fail(None, -32600, "invalid request"))
            continue
        response = handle(request)
        if response is not None:
            _write(out, response)
    return 0
