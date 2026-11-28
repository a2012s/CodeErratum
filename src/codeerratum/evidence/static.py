"""Static tool orchestration — run deterministic checks first.

Second-phase feature: lint/type findings are facts, not opinions. Feed them to the
model as "already known" so it does not waste output re-reporting them, and convert
them into hard Evidence that upgrades a finding's verification level.

Results are cached per file (path + mtime + size): in dual mode the same file used
to be linted twice, once per model, and mypy is the slow part.

TypeScript/JavaScript support: tsc --noEmit + eslint, discovered repo-local
first (node_modules/.bin), then PATH, gracefully degraded when missing.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path

from ..i18n import t

_CACHE: dict[tuple[str, str, int, int], list[StaticDiag]] = {}
_CACHE_LIMIT = 256
_TS_EXTENSIONS: frozenset[str] = frozenset({".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"})
_TSC_TIMEOUT = 180  # tsc is slower than linters; still bounded well under 120 s baseline

# tsc compiles the *project*, so its results are cached per repo, not per file.
# See tsc_project_diags for why the per-file shape was wrong.
_TSC_PROJECT: dict[str, tuple[tuple, list[StaticDiag]]] = {}
_TSC_LOCKS: dict[str, threading.Lock] = {}
_TSC_LOCKS_GUARD = threading.Lock()
_TSC_SKIP_DIRS: frozenset[str] = frozenset(
    {"node_modules", ".git", "dist", "build", "out", ".next", "coverage", "__pycache__"}
)
_TSC_STAMP_FILE_CAP = 20_000


@dataclass
class StaticDiag:
    tool: str
    file: str
    line: int
    code: str
    message: str
    # tsc and eslint distinguish errors from warnings; a warning must not become
    # a merge-blocking work order when diagnostics are synthesized.
    severity: str = "error"

    def to_dict(self) -> dict:
        return {
            "tool": self.tool,
            "file": self.file,
            "line": self.line,
            "code": self.code,
            "message": self.message,
            "severity": self.severity,
        }


def _run(
    cmd: list[str],
    cwd,
    timeout: float = 120,
) -> subprocess.CompletedProcess | None:
    """Run a tool, returning None when it is missing or times out.

    *timeout* defaults to 120 s to preserve existing behaviour; tsc gets a
    slightly larger budget because it compiles the whole project.
    """
    try:
        return subprocess.run(
            cmd,
            cwd=cwd,
            capture_output=True,
            text=True,
            errors="replace",
            check=False,
            timeout=timeout,
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
                    severity=parts[2].strip() or "error",
                )
            )
    return diags


# ── TypeScript / JavaScript ──────────────────────────────────────────────────


def _find_node_tool(name: str, repo_root: Path) -> list[str] | None:
    """Discover a Node.js CLI tool: repo-local node_modules/.bin first, then PATH."""
    local = repo_root / "node_modules" / ".bin" / name
    if local.is_file() and os.access(str(local), os.X_OK):
        return [str(local)]
    exe = shutil.which(name)
    return [exe] if exe else None


def _normalize_tool_path(raw: str, repo: Path) -> str:
    """Make a tool-reported path repo-relative so it matches ReviewUnit.primary_file."""
    candidate = raw.strip()
    # Only a "./" prefix, never lstrip("./"): that character class also eats the
    # leading slash of an absolute path and turns /Users/x/y into Users/x/y.
    while candidate.startswith("./"):
        candidate = candidate[2:]
    path = Path(candidate)
    if path.is_absolute():
        try:
            return str(path.relative_to(repo))
        except ValueError:
            return candidate
    return candidate


def _source_stamp(repo: Path) -> tuple:
    """Fingerprint the project's TS/JS sources.

    ``tsconfig.json`` alone is not enough. In the desktop app and the Web server
    the process outlives a single review, so an *edit to a source file* between
    two runs has to invalidate the cached type-check, or the next report would
    feed the model the previous run's errors. The walk skips vendored/build
    directories and is capped, because it replaces a subprocess that costs
    minutes; a few thousand ``stat`` calls is noise next to that.

    A (count, newest mtime) pair is not enough either: swapping one old file for
    another old file keeps both numbers identical while changing what tsc sees.
    The digest over every (relative path, mtime, size) triple catches renames and
    equal-count replacements as well as ordinary edits.
    """
    count = 0
    newest = 0
    visited = 0
    digests: list[str] = []
    for dirpath, dirnames, filenames in os.walk(repo):
        dirnames[:] = [d for d in dirnames if d not in _TSC_SKIP_DIRS]
        for name in filenames:
            visited += 1
            if visited > _TSC_STAMP_FILE_CAP:
                return (-1, -1, "")
            if Path(name).suffix in _TS_EXTENSIONS:
                count += 1
                full = Path(dirpath) / name
                try:
                    stat_info = full.stat()
                except OSError:
                    continue
                newest = max(newest, stat_info.st_mtime_ns)
                rel = full.relative_to(repo).as_posix()
                digests.append(f"{rel}|{stat_info.st_mtime_ns}|{stat_info.st_size}")
    digest = hashlib.sha256("\n".join(sorted(digests)).encode("utf-8")).hexdigest()
    return (count, newest, digest)


def _run_tsc_project(repo: Path) -> list[StaticDiag]:
    """One whole-project ``tsc --noEmit``, parsed for every file at once."""
    base = _find_node_tool("tsc", repo)
    if base is None:
        return []
    # --pretty false ensures no ANSI colour codes; output stays machine-parseable.
    result = _run([*base, "--noEmit", "--pretty", "false"], repo, timeout=_TSC_TIMEOUT)
    if result is None:
        return []
    diags: list[StaticDiag] = []
    for line in result.stdout.splitlines():
        # Expected format: path(line,col): error TSxxxx: message
        m = re.match(r"^(.+?)\((\d+),(\d+)\):\s+(error|warning)\s+(TS\d+):\s+(.+)$", line)
        if m:
            diags.append(
                StaticDiag(
                    tool="tsc",
                    file=_normalize_tool_path(m.group(1), repo),
                    line=int(m.group(2)),
                    code=m.group(5),
                    message=m.group(6),
                    severity=m.group(4),
                )
            )
    return diags


def tsc_project_diags(repo_root: Path) -> list[StaticDiag]:
    """Run tsc once per project and share the result across all of its files.

    Caching this per file, the way ruff and mypy are cached, was wrong twice
    over: a review of 40 changed ``.ts`` files paid for 40 whole-project
    compiles, and because units run in parallel worker threads those 40 compiles
    started *at the same time*, each holding a full program in memory. One run
    per repo, guarded by a per-repo lock so the threads queue instead of
    racing, is the shape that actually fits.
    """
    repo = Path(repo_root)
    key = str(repo)
    with _tsc_lock(key):
        # Probe before the tree walk: without tsc there is nothing to memoise, and
        # stamping the sources would cost a walk per file for no reason.
        if _find_node_tool("tsc", repo) is None:
            return []
        stamp = (
            _source_stamp(repo),
            *[_stat_pair(repo / name) for name in ("tsconfig.json", "package.json")],
        )
        cached = _TSC_PROJECT.get(key)
        if cached is not None and cached[0] == stamp:
            return cached[1]
        diags = _run_tsc_project(repo)
        _TSC_PROJECT[key] = (stamp, diags)
        return diags


def _stat_pair(path: Path) -> tuple[int, int]:
    try:
        st = path.stat()
    except OSError:
        return (0, 0)
    return (st.st_mtime_ns, st.st_size)


def _tsc_lock(repo_key: str) -> threading.Lock:
    with _TSC_LOCKS_GUARD:
        lock = _TSC_LOCKS.get(repo_key)
        if lock is None:
            lock = threading.Lock()
            _TSC_LOCKS[repo_key] = lock
        return lock


def run_tsc(path: str, repo_root: Path) -> list[StaticDiag]:
    """tsc diagnostics belonging to one file. The compile itself is project-wide."""
    return [d for d in tsc_project_diags(repo_root) if d.file == path]


def reset_tsc_cache() -> None:
    """Drop the project-level type-check memo (tests, or a repo path reuse)."""
    with _TSC_LOCKS_GUARD:
        _TSC_PROJECT.clear()
        _TSC_LOCKS.clear()


def run_eslint(path: str, repo_root: Path) -> list[StaticDiag]:
    """Run eslint --format json and parse structured JSON output."""
    base = _find_node_tool("eslint", repo_root)
    if base is None:
        return []
    result = _run([*base, "--format", "json", str(path)], repo_root)
    if result is None or not result.stdout.strip():
        return []
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        return []
    if not isinstance(data, list):
        return []
    diags: list[StaticDiag] = []
    for item in data:
        file_path = item.get("filePath", "")
        # eslint reports an absolute filePath; downstream consumers match on the
        # repo-relative name carried by the review unit.
        file_path = _normalize_tool_path(str(file_path), Path(repo_root))
        for msg in item.get("messages", []):
            diags.append(
                StaticDiag(
                    tool="eslint",
                    file=file_path,
                    line=msg.get("line", 0),
                    code=msg.get("ruleId") or "eslint",
                    message=msg.get("message", ""),
                    # eslint's JSON uses 1 = warning, 2 = error.
                    severity="error" if msg.get("severity") == 2 else "warning",
                )
            )
    return diags


# ── Cache helpers ────────────────────────────────────────────────────────────


def _cache_prune() -> None:
    """Trim the oldest half of cached entries when the cache grows too large."""
    if len(_CACHE) >= _CACHE_LIMIT:
        for stale in list(_CACHE)[: _CACHE_LIMIT // 2]:
            del _CACHE[stale]


# ── Main entry point ─────────────────────────────────────────────────────────


def collect_static(path: str, repo_root) -> list[StaticDiag]:
    """Collect all static diagnostics for a file (best-effort, tools optional).

    Dispatch is by file extension:

    * .py  — ruff + mypy (unchanged from the original implementation)
    * .ts / .tsx / .js / .jsx / .mjs / .cjs — tsc + eslint (only when
      package.json exists in *repo_root*)
    * everything else — empty list
    """
    # ── Python path (unchanged semantics) ───────────────────────────────────
    if path.endswith(".py"):
        return _collect_py(path, repo_root)

    # ── TypeScript / JavaScript path ────────────────────────────────────────
    ext = Path(path).suffix
    if ext in _TS_EXTENSIONS:
        return _collect_ts(path, repo_root)

    return []


def _collect_py(path: str, repo_root) -> list[StaticDiag]:
    """Python path: ruff + mypy (unchanged behaviour)."""
    file_path = Path(repo_root) / path
    try:
        stat_info = file_path.stat()
    except OSError:
        return []
    key = (str(file_path), "ruff+mypy", stat_info.st_mtime_ns, stat_info.st_size)
    cached = _CACHE.get(key)
    if cached is not None:
        return list(cached)
    diags = run_ruff(str(path), repo_root) + run_mypy(str(path), repo_root)
    _cache_prune()
    _CACHE[key] = diags
    return list(diags)


def _collect_ts(path: str, repo_root) -> list[StaticDiag]:
    """TypeScript / JavaScript path: tsc + eslint.

    tsc must go through ``run_tsc`` on *every* call: its result is project-wide, so
    changing an imported types.ts changes a.ts's diagnostics while a.ts itself is
    byte-identical. Caching it per file on (path, mtime, size) skipped the
    project-level fingerprint entirely and fed the model last run's type errors.
    eslint is genuinely per-file, so only that half is memoised here.
    """
    file_path = Path(repo_root) / path
    try:
        stat_info = file_path.stat()
    except OSError:
        return []
    repo = Path(repo_root)
    # Only proceed when the project declares itself as a JS/TS project.
    if not (repo / "package.json").exists():
        return []
    tsc_diags = run_tsc(str(path), repo)
    key = (str(file_path), "eslint", stat_info.st_mtime_ns, stat_info.st_size)
    cached = _CACHE.get(key)
    if cached is not None:
        eslint_diags = list(cached)
    else:
        eslint_diags = run_eslint(str(path), repo)
        _cache_prune()
        _CACHE[key] = eslint_diags
    return tsc_diags + eslint_diags


# ── Prompt rendering ─────────────────────────────────────────────────────────


def known_facts_block(diags: list[StaticDiag]) -> str:
    """Render diagnostics as a prompt block telling the model what is already known."""
    if not diags:
        return ""
    # The header follows the run's output language: a block of Chinese
    # instructions inside an English prompt contradicts the language directive
    # and nudges the model to answer in the wrong language.
    shown = sorted(diags, key=lambda d: d.line)[:20]
    # The tools are named from what is actually in the block. Hard-coding
    # "ruff/mypy" told a TypeScript reviewer that tsc/eslint findings were
    # somebody else's work, which is exactly the kind of wrong fact a prompt
    # cannot be corrected from inside.
    tools = ", ".join(sorted({d.tool for d in shown}))
    lines = [t("evidence.known_facts", tools=tools), ""]
    for d in shown:
        lines.append(f"- {d.file}:{d.line} [{d.tool}:{d.code}] {d.message}")
    return "\n".join(lines)
