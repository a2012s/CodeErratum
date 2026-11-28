"""Turn hard static diagnostics no model claimed into executable work orders.

The reviewer only attached a diagnostic to a finding whose anchor sat within
three lines of it. If no model mentioned the broken line, the diagnostic stayed
in ``static_diags.json`` while the merge gate reported "nothing blocking" —
the tool knew, the report did not. This module closes that gap without another
model call: it synthesizes a finding for each unclaimed error on a changed line.

Tool-produced findings are marked ``origin="static"`` and carry no
``source_models``, so eval cannot charge them to a model's precision.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from ..models import ReviewUnit
from ..models.finding import (
    Anchor,
    DefectCategory,
    Evidence,
    Finding,
    Lens,
    Suggestion,
)

# Only these two report an explicit error/warning level we trust for a gate.
# ruff/mypy findings stay advisory: they are useful context, but a lint nit
# must not block a merge just because it landed on a changed line.
_GATING_TOOLS = frozenset({"tsc", "eslint"})


def _same_file(a: str, b: str) -> bool:
    na = (a or "").replace("\\", "/").strip("/")
    nb = (b or "").replace("\\", "/").strip("/")
    return bool(na and nb) and (na == nb or na.endswith("/" + nb) or nb.endswith("/" + na))


def _changed_lines(unit: ReviewUnit) -> set[int]:
    """New-side line numbers added by the diff, excluding context and removals.

    Hunk bodies contain no ``@@`` header (``split_diff`` strips it), so the
    running new-side counter starts at ``hunk.new_start`` and advances on every
    context or addition line, never on a deletion.
    """
    out: set[int] = set()
    for hunk in unit.hunks:
        cursor = hunk.new_start
        for line in hunk.content.splitlines():
            if line.startswith("+") and not line.startswith("+++"):
                out.add(cursor)
                cursor += 1
            elif line.startswith(("-", "\\")):
                continue
            else:
                cursor += 1
    return out


def _claimed(diag: dict[str, Any], findings: list[Finding]) -> bool:
    """Whether a model finding already covers this diagnostic (±3 lines)."""
    line = int(diag.get("line") or 0)
    file = str(diag.get("file") or "")
    return any(
        _same_file(anchor.file, file) and anchor.start_line - 3 <= line <= anchor.end_line + 3
        for finding in findings
        for anchor in finding.anchors
    )


def _severity(diag: dict[str, Any]) -> str:
    level = str(diag.get("severity") or "error").lower()
    if diag.get("tool") in _GATING_TOOLS and level == "error":
        return "P1"
    return "P2"


def _acceptance(tool: str, file: str, repo_root: Path) -> str:
    """A command the reader can actually run to close the item."""
    if tool in ("tsc", "eslint"):
        local = repo_root / "node_modules" / ".bin" / tool
        exe = str(local) if local.exists() else tool
        return f"{exe} --noEmit" if tool == "tsc" else f"{exe} {file}"
    if tool == "ruff":
        return f"ruff check {file}"
    if tool == "mypy":
        return f"mypy {file}"
    return f"{tool} {file}"


def _source_line(repo_root: Path, file: str, line: int, message: str) -> list[str]:
    """Use the real source as the anchor so later verification still matches."""
    try:
        lines = (repo_root / file).read_text(encoding="utf-8").splitlines(keepends=True)
    except (OSError, UnicodeDecodeError):
        lines = []
    if 1 <= line <= len(lines):
        return [lines[line - 1]]
    return [message + "\n"]


def _make(diag: dict[str, Any], repo_root: Path) -> Finding:
    tool = str(diag.get("tool") or "static")
    file = str(diag.get("file") or "")
    line = int(diag.get("line") or 0)
    code = str(diag.get("code") or "")
    message = str(diag.get("message") or "").strip()
    raw = f"static::{tool}::{file}::{line}::{code}::{message}".encode()

    return Finding(
        id=hashlib.sha256(raw).hexdigest()[:16],
        unit_id="static",
        lens=Lens.CORRECTNESS,
        category=DefectCategory.SEMANTIC_DRIFT,
        severity=_severity(diag),
        claim=f"{tool} {code} 报错未闭环"[:60],
        detail=f"{message}（本次变更第 {line} 行；模型未提及，由 {tool} 直接确认）",
        anchors=[
            Anchor.from_source(file, line, line, _source_line(repo_root, file, line, message))
        ],
        evidence=[
            Evidence(
                kind="static_diag",
                summary=f"{tool}:{code}",
                payload=dict(diag),
                strength="hard",
            )
        ],
        suggestion=Suggestion(
            explanation=f"修复 {tool} 报出的 {code}：{message}",
            acceptance=_acceptance(tool, file, repo_root),
        ),
        source_models=[],
        origin="static",
        agreement="unverified",
        verification="suspected",
    )


def synthesize_static_findings(
    diags: list[dict[str, Any]],
    units: list[ReviewUnit],
    findings: list[Finding],
    repo_root: Path,
) -> list[Finding]:
    """Diagnostics on changed lines that no model finding already covers."""
    changed: dict[str, set[int]] = {}
    for unit in units:
        changed.setdefault(unit.primary_file, set()).update(_changed_lines(unit))
    if not changed:
        return []

    made: list[Finding] = []
    seen: set[tuple[str, str, int, str, str]] = set()
    for diag in diags:
        tool = str(diag.get("tool") or "")
        file = str(diag.get("file") or "")
        line = int(diag.get("line") or 0)
        level = str(diag.get("severity") or "error").lower()
        if line <= 0 or not file:
            continue
        # mypy "note:" lines are explanations, not defects.
        if tool == "mypy" and level == "note":
            continue
        lines = next((v for k, v in changed.items() if _same_file(k, file)), None)
        if lines is None or line not in lines:
            continue
        if _claimed(diag, findings):
            continue
        key = (
            tool,
            file,
            line,
            str(diag.get("code") or ""),
            str(diag.get("message") or ""),
        )
        if key in seen:
            continue
        seen.add(key)
        made.append(_make(diag, repo_root))
    return made
