"""Spec level — how much ground truth exists besides the code itself.

The report used to print a constant `L1`, which reads like a measurement and is
actually a placeholder. This module makes the number mean something and returns the
reason next to it, so a reader can disagree with the judgement instead of trusting a
value that never changes.

Levels (highest first):

* L4 executable spec — contracts, or an OpenAPI/proto schema paired with tests
* L3 external spec    — docs/spec*, ADR/RFC, openapi/swagger, .proto
* L2 self-documented  — most public symbols carry annotations or a docstring
* L1 code only        — the default when nothing above holds
"""

from __future__ import annotations

import ast
from pathlib import Path

from .models.finding import SpecLevel

SPEC_GLOBS = (
    "docs/spec*",
    "docs/rfc*",
    "docs/adr*",
    "adr",
    "rfcs",
    "specs",
    "proto",
    "openapi.*",
    "swagger.*",
)
CONTRACT_GLOBS = ("contracts", "tests/contracts", "**/tests/contracts")
TEST_GLOBS = ("tests/**/test_*.py", "**/tests/test_*.py", "**/*_test.py")
ANNOTATION_THRESHOLD = 0.6


def _first_match(root: Path, globs: tuple[str, ...]) -> str:
    """First existing path matching any pattern. Existence is the signal, not content."""
    for pattern in globs:
        for path in root.glob(pattern):
            return path.relative_to(root).as_posix()
    return ""


def _has_tests(root: Path) -> bool:
    return any(root.glob(pattern) for pattern in TEST_GLOBS)


def annotation_coverage(root: Path, scan: Path | None = None) -> float:
    """Share of public functions/methods that document themselves.

    A cheap proxy for "is the intent written down anywhere". Counting only public
    symbols keeps private helpers from dragging the number down.
    """
    base = scan or root
    documented = 0
    total = 0
    for path in base.rglob("*.py"):
        # Judge only the path below the scan root: an absolute path can contain a
        # dot-directory higher up (tmp dirs, CI workspaces) and would skip everything.
        try:
            rel_parts = path.relative_to(base).parts
        except ValueError:
            rel_parts = path.parts
        if any(
            part.startswith(".") or part in {"__pycache__", ".venv", "build", "dist"}
            for part in rel_parts
        ):
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        except (SyntaxError, ValueError):
            continue
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if node.name.startswith("_") and not node.name.startswith("__"):
                continue
            total += 1
            annotated = node.returns is not None or any(
                arg.annotation is not None for arg in node.args.args
            )
            if annotated or ast.get_docstring(node):
                documented += 1
    return documented / total if total else 0.0


def detect(repo_root: Path, *, scan: Path | None = None) -> tuple[SpecLevel, str]:
    """Return the level plus a short reason suitable for a report header."""
    root = repo_root.resolve()
    spec_hit = _first_match(root, SPEC_GLOBS)
    contract_hit = _first_match(root, CONTRACT_GLOBS)
    has_tests = _has_tests(root)

    if contract_hit and (spec_hit or has_tests):
        return "L4", f"可执行规格 {contract_hit}"
    if spec_hit:
        return "L3", f"外部规格 {spec_hit}"
    coverage = annotation_coverage(root, scan)
    if coverage >= ANNOTATION_THRESHOLD:
        return "L2", f"{coverage:.0%} 的公开符号带注解或 docstring"
    if coverage > 0:
        return "L1", f"仅 {coverage:.0%} 的公开符号有说明"
    return "L1", "无规格来源，仅凭代码判断"
