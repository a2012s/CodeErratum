"""Context builder — enclose the changed symbol with the code the model needs.

Second-phase feature: the single biggest quality lever. A model given only a diff
cannot know the caller's invariant or the surrounding function's full body.
"""

from __future__ import annotations

import ast
from pathlib import Path

from ..models import ReviewUnit

# jedi parses the whole project on first use; cache per (file, mtime, symbol) so a
# dual-model run does not pay that cost once per model.
_CALLER_CACHE: dict[tuple[str, int, str], list[str]] = {}
_CACHE_LIMIT = 512


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


def callers_of(unit: ReviewUnit, repo_root: Path) -> list[str]:
    """Find direct callers using jedi (in-process, no LSP subprocess).

    The reference search must start at the symbol's own name token. Querying the
    hunk's first line at column 0 resolves to nothing and silently returns no
    callers at all.
    """
    if not unit.symbol or unit.symbol.startswith("<"):
        return []
    source_path = repo_root / unit.primary_file
    if not source_path.exists():
        return []
    try:
        mtime = source_path.stat().st_mtime_ns
    except OSError:
        return []
    key = (str(source_path), mtime, unit.symbol)
    cached = _CALLER_CACHE.get(key)
    if cached is not None:
        return list(cached)

    try:
        import jedi

        source = source_path.read_text(encoding="utf-8", errors="replace")
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


def _remember(key: tuple[str, int, str], value: list[str]) -> None:
    if len(_CALLER_CACHE) > _CACHE_LIMIT:
        _CALLER_CACHE.clear()
    _CALLER_CACHE[key] = list(value)


def build_context(unit: ReviewUnit, repo_root: Path) -> dict:
    """Assemble the enrichment block used by the review prompt."""
    source_path = repo_root / unit.primary_file
    source = (
        source_path.read_text(encoding="utf-8", errors="replace") if source_path.exists() else ""
    )
    span = enclosing_range(unit, source) if source else None
    return {
        "enclosing": enclosing_source(unit, source) if source else "",
        "enclosing_range": span,
        "callers": callers_of(unit, repo_root),
        "symbol": unit.symbol,
        "risk_tags": unit.risk_tags,
    }
