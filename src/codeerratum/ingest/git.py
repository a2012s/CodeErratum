"""Parse git diff into ReviewUnits split by modified symbol."""

from __future__ import annotations

import ast
import re
import subprocess
from pathlib import Path

from ..models import Hunk, ReviewUnit

DIFF_HEADER = re.compile(r"^diff --git a/(?P<a>.+?) b/(?P<b>.+)$")
HUNK_HEADER = re.compile(
    r"^@@ -(?P<old>\d+)(?:,(?P<old_n>\d+))? \+(?P<new>\d+)(?:,(?P<new_n>\d+))? @@"
)


class DiffError(RuntimeError):
    """Raised when git cannot produce the requested diff."""


def normalize_pathspec(paths: str) -> str:
    """Validate whitespace-separated git pathspecs from UI/CLI input.

    Values are passed to git as separate arguments after ``--`` (so no shell and
    no option injection), but a user can still type an absolute path or ``..``
    and accidentally review files outside the intended tree. Reject those early.
    """
    parts = paths.split()
    for value in parts:
        # Treat backslashes as separators too: Path on POSIX does not, so
        # "..\\secret" would otherwise slip through as a literal filename.
        normalized = value.replace("\\", "/")
        if normalized.startswith("/") or ".." in Path(normalized).parts:
            raise DiffError("路径必须是仓库内的相对路径（不接受绝对路径或 ..）")
    return paths.strip()


def get_diff(spec: str, repo_root: Path, paths: str = "") -> str:
    """Resolve a diff spec through git. Supports ranges and optional pathspecs.

    ``paths`` is split on whitespace and appended after ``--`` as separate git
    arguments, so each token is treated as a path, never as a flag.
    """
    pathspec = normalize_pathspec(paths)
    cmd = ["git", "diff", "--no-color", "--unified=3", spec]
    if pathspec:
        cmd += ["--", *pathspec.split()]
    result = subprocess.run(
        cmd,
        cwd=repo_root,
        capture_output=True,
        text=True,
        errors="replace",
        check=False,
    )
    if result.returncode != 0:
        raise DiffError(result.stderr.strip() or f"git diff {spec} failed")
    return result.stdout


def get_commit(repo_root: Path) -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else "unknown"


def _iter_files(diff_text: str):
    """Yield (path, hunks, is_deleted) per file from a unified diff.

    The trailing hunk body must be attached before crossing a file boundary,
    otherwise every file except the last one loses its final hunk content.
    """
    current: str | None = None
    hunks: list[Hunk] = []
    body: list[str] = []
    deleted = False

    def flush_body() -> None:
        if body and hunks:
            hunks[-1].content = "\n".join(body)
            body.clear()

    def flush_file():
        flush_body()
        if current is not None and hunks:
            return current, hunks, deleted
        return None

    for line in diff_text.splitlines():
        if line.startswith("diff --git "):
            done = flush_file()
            if done:
                yield done
            m = DIFF_HEADER.match(line)
            current = m.group("b") if m else None
            hunks = []
            body = []
            deleted = False
            continue
        if line.startswith("deleted file mode"):
            deleted = True
            continue
        if line.startswith("+++ ") and not hunks:
            if line[4:].strip() == "/dev/null":
                deleted = True
            continue
        if current is None:
            continue
        m = HUNK_HEADER.match(line)
        if m:
            flush_body()
            hunks.append(
                Hunk(
                    old_start=int(m.group("old")),
                    old_lines=int(m.group("old_n") or 1),
                    new_start=int(m.group("new")),
                    new_lines=int(m.group("new_n") or 1),
                    content="",
                )
            )
            continue
        if hunks:
            body.append(line)
    done = flush_file()
    if done:
        yield done


def _build_symbol_index(source: str) -> list[tuple[int, int, str, str]]:
    """Return (start_line, end_line, qualified_name, kind) for top-level defs."""
    index: list[tuple[int, int, str, str]] = []
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return index
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            kind = "class" if isinstance(node, ast.ClassDef) else "function"
            name = node.name
            if isinstance(node, ast.ClassDef):
                for child in node.body:
                    if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        index.append(
                            (
                                child.lineno,
                                child.end_lineno or child.lineno,
                                f"{name}.{child.name}",
                                "function",
                            )
                        )
            index.append((node.lineno, node.end_lineno or node.lineno, name, kind))
    index.sort(key=lambda x: x[0])
    return index


def _symbol_for_line(index: list[tuple[int, int, str, str]], line: int) -> tuple[str, str]:
    best: tuple[int, str, str] | None = None
    for start, end, name, kind in index:
        if start <= line <= end:
            span = end - start
            if best is None or span < best[0]:
                best = (span, name, kind)
    if best:
        return best[1], best[2]
    return "<module>", "module"


def _changed_lines(hunk: Hunk):
    """Yield (new_line_no, text) for every added/modified line in a hunk.

    A single hunk can span multiple symbols, so we must map each changed line
    individually instead of assigning the whole hunk by its first line.
    """
    new = hunk.new_start
    for line in hunk.content.splitlines():
        if line.startswith("+") and not line.startswith("+++"):
            yield new, line[1:]
            new += 1
        elif line.startswith("-") and not line.startswith("---"):
            continue
        elif line.startswith("\\"):
            continue
        else:
            new += 1


def _risk_tags(content: str) -> list[str]:
    tags: set[str] = set()
    lowered = content.lower()
    for keyword, tag in (
        ("thread", "concurrency"),
        ("asyncio", "concurrency"),
        ("lock", "concurrency"),
        ("auth", "auth"),
        ("token", "auth"),
        ("password", "auth"),
        ("migration", "migration"),
        ("transaction", "data"),
    ):
        if keyword in lowered:
            tags.add(tag)
    return sorted(tags)


class _SymbolGroup:
    """Hunks plus the true count of changed lines, accumulated per symbol."""

    __slots__ = ("hunks", "changed")

    def __init__(self) -> None:
        self.hunks: list[Hunk] = []
        self.changed = 0

    def add_hunk(self, hunk: Hunk) -> None:
        if all(h is not hunk for h in self.hunks):
            self.hunks.append(hunk)


LANGUAGE_BY_SUFFIX = {
    ".py": "python",
    ".pyi": "python",
    ".ts": "typescript",
    ".tsx": "tsx",
    ".js": "javascript",
    ".jsx": "jsx",
    ".mjs": "javascript",
    ".go": "go",
    ".rs": "rust",
    ".java": "java",
    ".kt": "kotlin",
    ".rb": "ruby",
    ".php": "php",
    ".c": "c",
    ".h": "c",
    ".cc": "cpp",
    ".cpp": "cpp",
    ".hpp": "cpp",
    ".cs": "csharp",
    ".swift": "swift",
    ".sh": "bash",
    ".sql": "sql",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".toml": "toml",
    ".json": "json",
    ".ini": "ini",
    ".md": "markdown",
    ".css": "css",
    ".html": "html",
}


def detect_language(file: str) -> str:
    """Map a path to a language tag. Unknown extensions become 'text'.

    Only Python gets symbol-level splitting; everything else is reviewed per file.
    That is a real limitation, not a silent claim of support: the tag reaches the
    prompt so the model knows what it is reading, and `language` is recorded on the
    unit so the report can show it.
    """
    return LANGUAGE_BY_SUFFIX.get(Path(file).suffix.lower(), "text")


def split_diff(
    diff_text: str,
    repo_root: Path,
    language: str | None = None,
    notices: list[str] | None = None,
) -> list[ReviewUnit]:
    """Split a unified diff into per-symbol ReviewUnits.

    Files that no longer exist in the working tree (deleted files, or reviewing
    a branch you are not on) are skipped: an anchor cannot be verified against
    source that is not there, and sending them downstream only yields a crash
    or a fabricated location. Every skip is appended to ``notices`` so the
    degradation stays visible.

    ``language`` forces a single language for every file; by default it is inferred
    per file from the extension.
    """
    units: list[ReviewUnit] = []
    for file, hunks, is_deleted in _iter_files(diff_text):
        if file == "/dev/null" or file.endswith((".lock", ".sum")):
            continue
        source_path = repo_root / file
        if is_deleted or not source_path.exists():
            if notices is not None:
                reason = "deleted" if is_deleted else "not present in working tree"
                notices.append(f"skipped {file} ({reason})")
            continue
        lang = language or detect_language(file)
        source = source_path.read_text(encoding="utf-8", errors="replace")
        index = _build_symbol_index(source) if lang == "python" and source else []

        by_symbol: dict[tuple[str, str], _SymbolGroup] = {}
        for hunk in hunks:
            changed_lines = list(_changed_lines(hunk))
            if not changed_lines:
                symbol, kind = _symbol_for_line(index, hunk.new_start)
                by_symbol.setdefault((symbol, kind), _SymbolGroup()).add_hunk(hunk)
                continue
            for new_line, _text in changed_lines:
                symbol, kind = _symbol_for_line(index, new_line)
                group = by_symbol.setdefault((symbol, kind), _SymbolGroup())
                group.add_hunk(hunk)
                group.changed += 1

        for (symbol, kind), group in by_symbol.items():
            content = "\n".join(h.content for h in group.hunks)
            is_config = kind == "module" and file.endswith(
                (".yaml", ".yml", ".toml", ".json", ".ini")
            )
            if is_config:
                kind = "config"
                symbol = "<config>"
            units.append(
                ReviewUnit(
                    unit_id=ReviewUnit.make_id(file, symbol, group.hunks),
                    primary_file=file,
                    symbol=symbol,
                    kind=kind,  # type: ignore[arg-type]
                    language=lang,
                    hunks=group.hunks,
                    changed_lines=group.changed,
                    is_public=not symbol.startswith("_") and symbol != "<module>",
                    risk_tags=_risk_tags(content),
                )
            )
    return units
