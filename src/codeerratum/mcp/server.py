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
                "author_model": {
                    "type": "string",
                    "description": (
                        "Model id that wrote the diff. It is excluded from every review "
                        "seat (scanner, verifier and falsifier)."
                    ),
                },
                "paths": {
                    "type": "string",
                    "description": "Git pathspec narrowing the diff, e.g. src/api.",
                },
                "tiered": {
                    "type": "boolean",
                    "default": True,
                    "description": (
                        "quick only: scan every unit with the lightest model, then "
                        "re-review hit units with the strong verifier."
                    ),
                },
                "include_non_code": {
                    "type": "boolean",
                    "default": False,
                    "description": "Include docs/config/i18n units in the review.",
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
        "name": "accept_finding",
        "description": (
            "Mark a finding accepted after its fix and acceptance command were verified. "
            "Idempotent: accepting an already accepted finding succeeds."
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
        "name": "reject_finding",
        "description": (
            "Mark a finding rejected with a reason (false positive, duplicate, invalid). "
            "Idempotent: rejecting an already rejected finding succeeds."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "finding_id": {"type": "string", "description": "Finding id or unique prefix."},
                "reason": {
                    "type": "string",
                    "description": "Why it is not a real defect. Required for stats.",
                },
                "repo": {"type": "string", "description": "Absolute path to the repository."},
            },
            "required": ["finding_id", "reason"],
        },
    },
    {
        "name": "reconcile_report",
        "description": (
            "Read a report's 执行结果 table and write its accepted/rejected verdicts back "
            "into the feedback store. Re-resolved by finding id; the last row wins."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "report": {
                    "type": "string",
                    "description": (
                        "Report path. Relative paths resolve against the repository. "
                        "Default erratumReview.md."
                    ),
                },
                "repo": {"type": "string", "description": "Absolute path to the repository."},
                "dry_run": {
                    "type": "boolean",
                    "default": False,
                    "description": "Report what would change without writing statuses.",
                },
            },
        },
    },
    {
        "name": "get_stats",
        "description": (
            "Read acceptance rate, per-category counts, per-model assertions and "
            "machine-drop reasons for this repository."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "repo": {"type": "string", "description": "Absolute path to the repository."}
            },
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


def _as_bool(value: Any, default: bool) -> bool:
    """Coerce a schema-declared boolean without trusting the client's typing.

    ``"false"`` is a truthy string in Python; a client that sends JSON strings
    for booleans would silently flip tiered/include_non_code to True.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in ("true", "1", "yes"):
            return True
        if lowered in ("false", "0", "no"):
            return False
    return default


def _as_int(
    value: Any, default: int, minimum: int | None = None, maximum: int | None = None
) -> int:
    """Coerce an integer argument and clamp it to the advertised schema range.

    ``LIMIT -1`` in SQLite means "unbounded", so an unclamped negative limit
    silently returned every row instead of the promised page.
    """
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    if minimum is not None:
        number = max(minimum, number)
    if maximum is not None:
        number = min(maximum, number)
    return number


def _tool_review_diff(args: dict) -> dict:
    try:
        outcome = execute_review(
            _root(args),
            diff_spec=str(args.get("diff") or "HEAD~1"),
            mode=str(args.get("mode") or ""),
            # Omission means auto, the same 60-unit gate the CLI and schema
            # promise; 0 still means "review everything". A hardcoded 10 here
            # silently reviewed a sixth of the diff through MCP only.
            max_units=(
                _as_int(args["max_units"], 0, minimum=0, maximum=500)
                if args.get("max_units") is not None
                else None
            ),
            house_rules=str(args.get("house_rules") or ""),
            author_model=str(args.get("author_model") or "") or None,
            paths=str(args.get("paths") or ""),
            tiered=_as_bool(args.get("tiered"), True),
            include_non_code=_as_bool(args.get("include_non_code"), False),
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
    limit = _as_int(args.get("limit"), 20, minimum=1, maximum=200)
    rows = store_for(_root(args)).list_runs(limit=limit)
    if not rows:
        return _text("还没有运行记录。")
    return _text(json.dumps(rows, ensure_ascii=False, indent=2))


def _resolve_finding(store, raw_id: str) -> tuple[dict | None, str | None]:
    """Resolve an exact id or a unique prefix without guessing between matches."""
    finding_id = raw_id.strip().lower()
    if not finding_id:
        return None, "finding_id is required"
    rows = store.find_finding(finding_id)
    exact = [row for row in rows if row["id"] == finding_id]
    if exact:
        return exact[0], None
    if len(rows) == 1:
        return rows[0], None
    if not rows:
        return None, f"没有以 {raw_id} 开头的发现"
    ids = ", ".join(row["id"] for row in rows)
    return None, f"前缀 {raw_id} 匹配到多条发现，请给出更长的 id：{ids}"


def _tool_get_finding(args: dict) -> dict:
    finding_id = str(args.get("finding_id") or "")
    if not finding_id:
        return _error("finding_id is required")
    row, problem = _resolve_finding(store_for(_root(args)), finding_id)
    if problem:
        return _error(problem)
    return _text(json.dumps(row, ensure_ascii=False, indent=2))


def _set_finding_status(args: dict, status: str, reason: str = "") -> dict:
    finding_id = str(args.get("finding_id") or "")
    if not finding_id:
        return _error("finding_id is required")
    store = store_for(_root(args))
    row, problem = _resolve_finding(store, finding_id)
    if problem:
        return _error(problem)
    previous = row["status"]
    if not store.set_status(row["id"], status, reason):
        return _error(f"finding {row['id']} disappeared before the status update")
    payload = {
        "id": row["id"],
        "status": status,
        "previous_status": previous,
        "reason": reason,
    }
    return _text(json.dumps(payload, ensure_ascii=False, indent=2))


def _tool_accept_finding(args: dict) -> dict:
    return _set_finding_status(args, "accepted")


def _tool_reject_finding(args: dict) -> dict:
    reason = str(args.get("reason") or "").strip()
    if not reason:
        return _error("reason is required: a rejection without a reason pollutes the stats")
    return _set_finding_status(args, "rejected", reason)


def _tool_reconcile_report(args: dict) -> dict:
    from ..report.outcomes import apply_outcomes, parse_outcomes

    root = _root(args)
    raw_path = str(args.get("report") or "erratumReview.md")
    report = Path(raw_path)
    if not report.is_absolute():
        report = root / report
    if not report.is_file():
        return _error(f"报告不存在：{report}")
    parsed = parse_outcomes(report.read_text(encoding="utf-8", errors="replace"))
    if not parsed.found_table:
        return _error("报告里没有 `## 执行结果` 表")
    dry_run = bool(args.get("dry_run", False))
    counts = apply_outcomes(store_for(root), parsed.rows, dry_run=dry_run)
    payload = {
        "report": str(report),
        "dry_run": dry_run,
        "counts": counts,
        "problems": parsed.problems,
    }
    if counts["rows"] == 0:
        return _error(json.dumps(payload | {"error": "执行结果表里没有数据行"}, ensure_ascii=False))
    if not dry_run and counts["updated"] == 0 and counts["unmatched"]:
        # A stale or mistyped work order must not look like a closed loop.
        return _error(
            json.dumps(
                payload | {"error": "没有任何编号匹配到本仓库的 finding，请检查报告编号"},
                ensure_ascii=False,
            )
        )
    return _text(json.dumps(payload, ensure_ascii=False, indent=2))


def _tool_get_stats(args: dict) -> dict:
    return _text(json.dumps(store_for(_root(args)).stats(), ensure_ascii=False, indent=2))


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
    "accept_finding": _tool_accept_finding,
    "reject_finding": _tool_reject_finding,
    "reconcile_report": _tool_reconcile_report,
    "get_stats": _tool_get_stats,
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
        # params extraction lives inside the try: a client that sends
        # params as a string/list must yield a protocol error, not an
        # AttributeError that kills the whole stdio session.
        try:
            params = request.get("params") or {}
            if not isinstance(params, dict):
                return _fail(request_id, -32602, "params must be an object")
            name = params.get("name")
            handler = _HANDLERS.get(name)
            if handler is None:
                return _fail(request_id, -32602, f"unknown tool: {name}")
            arguments = params.get("arguments") or {}
            if not isinstance(arguments, dict):
                return _fail(request_id, -32602, "arguments must be an object")
            return _ok(request_id, handler(arguments))
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
