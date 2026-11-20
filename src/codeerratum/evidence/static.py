"""Static tool orchestration — run deterministic checks first.

Second-phase feature: lint/type findings are facts, not opinions. Feed them to the
model as "already known" so it does not waste output re-reporting them, and convert
them into hard Evidence that upgrades a finding's verification level.

Results are cached per file (path + mtime + size): in dual mode the same file used
to be linted twice, once per model, and mypy is the slow part.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

_CACHE: dict[tuple[str, int, int], list[StaticDiag]] = {}
_CACHE_LIMIT = 256


@dataclass
class StaticDiag:
    tool: str
    file: str
    line: int
    code: str
    message: str

    def to_dict(self) -> dict:
        return {
            "tool": self.tool,
            "file": self.file,
            "line": self.line,
            "code": self.code,
            "message": self.message,
        }


def _run(cmd: list[str], cwd) -> subprocess.CompletedProcess | None:
    """Run a tool, returning None when it is missing or times out."""
    try:
        return subprocess.run(
            cmd,
            cwd=cwd,
            capture_output=True,
            text=True,
            errors="replace",
            check=False,
            timeout=120,
        )
    except (subprocess.TimeoutExpired, OSError):
        # A hung or missing tool must degrade to "no facts", never abort the review.
        return None


def _ruff_base() -> list[str]:
    exe = shutil.which("ruff")
    return [exe] if exe else [sys.executable, "-m", "ruff"]


def _mypy_base() -> list[str]:
    exe = shutil.which("mypy")
    return [exe] if exe else [sys.executable, "-m", "mypy"]


def run_ruff(path: str, cwd) -> list[StaticDiag]:
    result = _run([*_ruff_base(), "check", "--output-format", "json", str(path)], cwd)
    if result is None or not result.stdout.strip():
        return []
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        return []
    if not isinstance(data, list):
        return []
    diags = []
    for item in data:
        loc = item.get("location", {})
        diags.append(
            StaticDiag(
                tool="ruff",
                file=item.get("filename", ""),
                line=loc.get("row", 0),
                code=item.get("code") or "",
                message=item.get("message", ""),
            )
        )
    return diags


def run_mypy(path: str, cwd) -> list[StaticDiag]:
    result = _run([*_mypy_base(), "--no-error-summary", str(path)], cwd)
    if result is None:
        return []
    diags = []
    for line in result.stdout.splitlines():
        parts = line.split(":", 3)
        if len(parts) >= 4 and parts[1].isdigit():
            diags.append(
                StaticDiag(
                    tool="mypy",
                    file=parts[0],
                    line=int(parts[1]),
                    code=parts[2].strip(),
                    message=parts[3].strip(),
                )
            )
    return diags


def collect_static(file: str, cwd) -> list[StaticDiag]:
    """Collect all static diagnostics for a file (best-effort, tools optional)."""
    if not file.endswith(".py"):
        return []
    path = Path(cwd) / file
    try:
        stat = path.stat()
    except OSError:
        return []
    key = (str(path), stat.st_mtime_ns, stat.st_size)
    cached = _CACHE.get(key)
    if cached is not None:
        return list(cached)
    diags = run_ruff(str(file), cwd) + run_mypy(str(file), cwd)
    if len(_CACHE) > _CACHE_LIMIT:
        _CACHE.clear()
    _CACHE[key] = diags
    return list(diags)


def known_facts_block(diags: list[StaticDiag]) -> str:
    """Render diagnostics as a prompt block telling the model what is already known."""
    if not diags:
        return ""
    lines = [
        "# 已知静态工具诊断（已由 ruff/mypy 检出，禁止重复报告为你的发现）",
        "以下问题是已知事实。除非你能指出静态工具漏掉的更深层的语义 bug，",
        "否则不要把它们列为 finding。你可以引用它们作为证据。",
        "",
    ]
    for d in sorted(diags, key=lambda d: d.line)[:20]:
        lines.append(f"- {d.file}:{d.line} [{d.tool}:{d.code}] {d.message}")
    return "\n".join(lines)
