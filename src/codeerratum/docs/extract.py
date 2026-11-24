"""Extract a symbol map from Python source — no model, no guessing.

Documentation written by a model drifts the moment the code moves. This module only
records what the AST actually says, which makes the later `--check` able to answer a
question a model cannot: did the public surface change after the docs were written?
"""

from __future__ import annotations

import ast
import hashlib
from dataclasses import dataclass, field
from pathlib import Path

SKIP_DIRS = {
    ".git",
    ".venv",
    "venv",
    "__pycache__",
    ".codeerratum",
    "build",
    "dist",
    "node_modules",
    ".tox",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".idea",
    ".vscode",
}

DOCSTRING_LIMIT = 240


@dataclass
class SymbolDoc:
    qualname: str
    kind: str
    line: int
    signature: str
    docstring: str
    is_public: bool
    children: list[SymbolDoc] = field(default_factory=list)

    @property
    def sig_hash(self) -> str:
        """Identity of the API shape: the same signature means nothing changed."""
        return hashlib.sha256(self.signature.encode("utf-8")).hexdigest()[:12]


@dataclass
class ModuleDoc:
    path: str
    module: str
    docstring: str
    symbols: list[SymbolDoc]

    def flatten(self) -> list[SymbolDoc]:
        out: list[SymbolDoc] = []
        for symbol in self.symbols:
            out.append(symbol)
            out.extend(symbol.children)
        return out


def _is_public(name: str) -> bool:
    if name.startswith("__") and name.endswith("__"):
        return True  # dunder methods are part of the contract
    return not name.startswith("_")


def _doc_summary(node: ast.AST) -> str:
    raw = ast.get_docstring(node) or ""
    paragraph = raw.strip().split("\n\n")[0].replace("\n", " ")
    return paragraph[:DOCSTRING_LIMIT]


def _signature(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
    returns = f" -> {ast.unparse(node.returns)}" if node.returns else ""
    try:
        args = ast.unparse(node.args)
    except Exception:  # noqa: BLE001 - unparse can fail on exotic syntax
        args = "..."
    return f"{prefix} {node.name}({args}){returns}"


def _class_signature(node: ast.ClassDef) -> str:
    bases = ", ".join(ast.unparse(b) for b in node.bases)
    keywords = ", ".join(f"{kw.arg}={ast.unparse(kw.value)}" for kw in node.keywords)
    inner = ", ".join(p for p in (bases, keywords) if p)
    return f"class {node.name}({inner})" if inner else f"class {node.name}"


def _walk(body: list[ast.stmt], prefix: str, parent_kind: str) -> list[SymbolDoc]:
    out: list[SymbolDoc] = []
    for node in body:
        if isinstance(node, ast.ClassDef):
            symbol = SymbolDoc(
                qualname=f"{prefix}{node.name}",
                kind="class",
                line=node.lineno,
                signature=_class_signature(node),
                docstring=_doc_summary(node),
                is_public=_is_public(node.name),
            )
            symbol.children = _walk(node.body, f"{prefix}{node.name}.", "class")
            out.append(symbol)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            kind = "method" if parent_kind == "class" else "function"
            symbol = SymbolDoc(
                qualname=f"{prefix}{node.name}",
                kind=kind,
                line=node.lineno,
                signature=_signature(node),
                docstring=_doc_summary(node),
                is_public=_is_public(node.name),
            )
            symbol.children = _walk(node.body, f"{prefix}{node.name}.", "function")
            out.append(symbol)
    return out


def module_name(path: Path, root: Path) -> str:
    rel = path.relative_to(root).with_suffix("")
    parts = list(rel.parts)
    if parts and parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts) or path.stem


def extract_module(path: Path, root: Path, module_root: Path | None = None) -> ModuleDoc | None:
    """Parse one file. Unparseable sources are skipped, not fatal.

    ``root`` decides the recorded file path (stable, repo-relative); ``module_root``
    decides the dotted name, so scanning ``src/`` yields ``codeerratum.cli`` rather
    than ``src.codeerratum.cli``.
    """
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    except (SyntaxError, ValueError):
        return None
    return ModuleDoc(
        path=path.relative_to(root).as_posix(),
        module=module_name(path, module_root or root),
        docstring=_doc_summary(tree),
        symbols=_walk(tree.body, "", "module"),
    )


def collect(root: Path, src: Path | None = None) -> list[ModuleDoc]:
    """All Python modules under ``src`` (default: repo root), skipping build clutter."""
    root = root.resolve()
    base = (src or root).resolve()
    if not base.is_dir():
        raise NotADirectoryError(f"not a directory: {base}")
    modules: list[ModuleDoc] = []
    for path in sorted(base.rglob("*.py")):
        # Skip build clutter BEFORE parsing: ast.parse on every .py under
        # node_modules / .venv dominated docs_overview latency on real trees.
        try:
            rel_parts = path.relative_to(root).parts
            anchor, module_root = root, base
        except ValueError:
            # src passed as an absolute path outside the repo: key everything by the
            # scan base so the index stays stable.
            rel_parts = path.relative_to(base).parts
            anchor = module_root = base
        if any(part in SKIP_DIRS for part in rel_parts):
            continue
        doc = extract_module(path, anchor, module_root)
        if doc is not None:
            modules.append(doc)
    return modules
