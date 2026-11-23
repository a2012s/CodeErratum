"""Context builder — enclose the changed symbol with the code the model needs.

Second-phase feature: the single biggest quality lever. A model given only a diff
cannot know the caller's invariant or the surrounding function's full body.
"""

from __future__ import annotations

import ast
import hashlib
from pathlib import Path

from ..models import ReviewUnit

# jedi parses the whole project on first use; cache per (file, content stamp, symbol)
# so a dual-model run does not pay that cost once per model. The stamp is the mtime
# for the working tree and a content hash for a blob, so two revisions of the same
# file cannot collide in the cache.
_CALLER_CACHE: dict[tuple[str, str, str], list[str]] = {}
_CACHE_LIMIT = 512

# Five call sites is enough to show the shape of the contract without turning a
# review unit into a project-wide read. Two lines of padding catches the common
# multi-line call (opening paren, arguments on the next line).
CALLER_PAD = 2
MAX_CALLER_SNIPPETS = 5
MAX_CALLER_LINE_CHARS = 240
_SNIPPET_CACHE: dict[tuple[str, str, int, int], str | None] = {}


def _find_definition(source: str, symbol: str) -> tuple[int, int] | None:
    """Locate the (line, column) of the symbol's own name token — 1-based line."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None
    name = symbol.split(".")[-1]
    want_method = "." in symbol
    lines = source.splitlines()
    fallback: tuple[int, int] | None = None
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        if node.name != name:
            continue
        line_text = lines[node.lineno - 1] if node.lineno <= len(lines) else ""
        col = line_text.find(name)
        position = (node.lineno, col if col >= 0 else node.col_offset)
        is_function = isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        if want_method and is_function:
            return position
        if fallback is None:
            fallback = position
    return fallback


def enclosing_range(unit: ReviewUnit, source: str) -> tuple[int, int] | None:
    """Return the (start_line, end_line) of the enclosing def/class, 1-based."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None
    target = unit.symbol.split(".")[0]
    if target in ("<module>", "<config>") or target.startswith("<"):
        return (1, len(source.splitlines()))
    for node in ast.walk(tree):
        if getattr(node, "name", None) != target:
            continue
        end = getattr(node, "end_lineno", None)
        if end:
            return (node.lineno, end)
    return None


def enclosing_source(unit: ReviewUnit, source: str) -> str:
    """Return the full text of the enclosing function/class for the changed symbol."""
    span = enclosing_range(unit, source)
    if span is None:
        return ""
    lines = source.splitlines()
    return "\n".join(lines[span[0] - 1 : span[1]])


def callers_of(
    unit: ReviewUnit, repo_root: Path, sources: dict[str, str] | None = None
) -> list[str]:
    """Find direct callers using jedi (in-process, no LSP subprocess).

    The reference search must start at the symbol's own name token. Querying the
    hunk's first line at column 0 resolves to nothing and silently returns no
    callers at all.

    ``sources`` supplies the file text of the diff revision; without it the working
    tree is used. The cache key carries a content hash in that case, so a review of
    another revision cannot be answered from the working tree's cache entry.
    """
    if not unit.symbol or unit.symbol.startswith("<"):
        return []
    source_path = repo_root / unit.primary_file
    blob = (sources or {}).get(unit.primary_file)
    if blob is None and not source_path.exists():
        return []
    if blob is not None:
        stamp = hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]
    else:
        try:
            stamp = str(source_path.stat().st_mtime_ns)
        except OSError:
            return []
    key = (str(source_path), stamp, unit.symbol)
    cached = _CALLER_CACHE.get(key)
    if cached is not None:
        return list(cached)

    try:
        import jedi

        source = (
            blob if blob is not None else source_path.read_text(encoding="utf-8", errors="replace")
        )
        position = _find_definition(source, unit.symbol)
        if position is None:
            _remember(key, [])
            return []
        script = jedi.Script(
            code=source,
            path=str(source_path),
            # Scope the project to this repo: without it jedi resolves same-named
            # modules from whatever directory it saw first in the process.
            project=jedi.Project(str(repo_root)),
        )
        refs = script.get_references(line=position[0], column=position[1])
        callers: set[str] = set()
        for ref in refs:
            if ref.is_definition() or not ref.module_path:
                continue
            try:
                rel = Path(ref.module_path).relative_to(repo_root)
            except ValueError:
                continue
            callers.add(f"{rel}:{ref.line}")
        result = sorted(callers)[:10]
        _remember(key, result)
        return result
    except Exception:  # noqa: BLE001 - jedi is best-effort enrichment
        return []


def _line_window(text: str, line_no: int, pad: int) -> tuple[int, int, str] | None:
    lines = text.splitlines()
    if not 1 <= line_no <= len(lines):
        return None
    start = max(1, line_no - pad)
    end = min(len(lines), line_no + pad)
    body = "\n".join(
        f"{i:4d} | {lines[i - 1].rstrip()[:MAX_CALLER_LINE_CHARS]}" for i in range(start, end + 1)
    )
    return start, end, body


def caller_snippets(
    callers: list[str],
    repo_root: Path,
    sources: dict[str, str] | None = None,
    pad: int = CALLER_PAD,
    limit: int = MAX_CALLER_SNIPPETS,
) -> list[dict]:
    """Read the call expression at each caller coordinate, not just its address.

    A coordinate tells the model that a symbol is used; it does not show *how*.
    The highest-value bug class this review catches is a signature or return-value
    change whose caller was not updated, and that is invisible from ``app.py:5``.

    Best-effort by design: a caller outside the working tree, a binary file, or a
    line number that the revision moved drops that one snippet instead of failing
    the review.
    """
    out: list[dict] = []
    for coord in callers[:limit]:
        file, sep, line_text = coord.rpartition(":")
        if not sep:
            continue
        file = file.replace("\\", "/")
        try:
            line_no = int(line_text)
        except ValueError:
            continue
        blob = (sources or {}).get(file)
        path = repo_root / file
        if blob is not None:
            stamp = hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]
        else:
            try:
                stamp = str(path.stat().st_mtime_ns)
            except OSError:
                continue
        key = (str(path), stamp, line_no, pad)
        if key in _SNIPPET_CACHE:
            window = _SNIPPET_CACHE[key]
        else:
            text = blob
            if text is None:
                try:
                    text = path.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    text = None
            window = _line_window(text, line_no, pad) if text is not None else None
            if len(_SNIPPET_CACHE) > _CACHE_LIMIT:
                _SNIPPET_CACHE.clear()
            _SNIPPET_CACHE[key] = window
        if window is None:
            continue
        start, end, body = window
        out.append(
            {
                "file": file,
                "line": line_no,
                "start_line": start,
                "end_line": end,
                "snippet": body,
            }
        )
    return out


def _remember(key: tuple[str, str, str], value: list[str]) -> None:
    if len(_CALLER_CACHE) > _CACHE_LIMIT:
        _CALLER_CACHE.clear()
    _CALLER_CACHE[key] = list(value)


def build_context(unit: ReviewUnit, repo_root: Path, sources: dict[str, str] | None = None) -> dict:
    """Assemble the enrichment block used by the review prompt."""
    source_path = repo_root / unit.primary_file
    blob = (sources or {}).get(unit.primary_file)
    if blob is not None:
        source = blob
    else:
        source = (
            source_path.read_text(encoding="utf-8", errors="replace")
            if source_path.exists()
            else ""
        )
    span = enclosing_range(unit, source) if source else None
    callers = callers_of(unit, repo_root, sources)
    return {
        "enclosing": enclosing_source(unit, source) if source else "",
        "enclosing_range": span,
        "callers": callers,
        "caller_snippets": caller_snippets(callers, repo_root, sources),
        "symbol": unit.symbol,
        "risk_tags": unit.risk_tags,
    }
