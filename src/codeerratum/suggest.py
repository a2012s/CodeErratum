"""Close the loop one step further: rejected verdicts become prompt rules, not just routing scores.

``apply_measured_profiles`` already teaches the *router* which model asserts
junk. A human who rejects "n_plus_one in every test file, again" is also
teaching the *prompt* — and that lesson currently dies in feedback.db. This
module aggregates repeated human rejections by (directory, category) and offers
them back as candidate house rules.

Two contracts are non-negotiable, both inherited from how rules are loaded:

* **append-only.** ``.codeerratum/rules.md`` is hand-written project memory.
  Suggestions are added below the existing text, never reordered or rewritten.
* **the 4000-char budget is the user's problem, not a silent truncation.**
  The pipeline truncates at load time (and announces it), but writing a file
  whose tail will be cut off *by our own limit* is a trap. Over budget means:
  refuse, report the excess, let the user prune first.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .review import MAX_HOUSE_RULES_CHARS, sanitize_house_rules

# A rule that cites a directory is about a *pattern*; a rule that cites one
# deep path is usually about one incident. Two segments ("src/codeerratum")
# keep the aggregation coarse enough to survive refactors of individual files.
_SCOPE_SEGMENTS = 2


def scope_of(file: str) -> str:
    """Repo-relative first path segments: src/a/b.py -> src/a; solo.py -> solo.py."""
    parts = [p for p in (file or "").replace("\\", "/").strip("/").split("/") if p]
    if not parts:
        return ""
    return "/".join(parts[:_SCOPE_SEGMENTS])


def build_suggestions(rows: list[dict[str, Any]], min_count: int = 2) -> list[dict[str, Any]]:
    """Aggregate rejected findings into candidate rules, most-rejected first.

    ``rows`` are {category, file, n} triples from the store. One finding per
    (category, scope) bucket would be a taste, not a pattern — the default
    threshold of 2 is what separates "the reviewer misfired" from "this project
    keeps rejecting this class of claim in this corner".
    """
    buckets: dict[tuple[str, str], int] = {}
    for row in rows:
        category = str(row.get("category") or "")
        scope = scope_of(str(row.get("file") or ""))
        if not category or not scope:
            continue
        key = (scope, category)
        buckets[key] = buckets.get(key, 0) + int(row.get("n") or 0)
    out = [
        {
            "scope": scope,
            "category": category,
            "count": count,
            "rule": render_rule(scope, category, count),
        }
        for (scope, category), count in buckets.items()
        if count >= min_count
    ]
    out.sort(key=lambda s: (-s["count"], s["scope"], s["category"]))
    return out


def render_rule(scope: str, category: str, count: int) -> str:
    return (
        f"- {scope}/ 下的 {category} 类问题已被人工驳回 {count} 次："
        "除非附带可运行的复现证据，不要再报告该类问题。"
    )


def plan_append(existing: str, rules: list[str]) -> tuple[str, int]:
    """Return (new_file_text, overflow_chars). Overflow > 0 refuses the write.

    Duplicate rule lines are skipped so re-running the command is idempotent;
    existing text is kept verbatim, including anything after our own last block.
    """
    if not existing.strip():
        base = "# 项目规范\n"
    else:
        base = existing.rstrip("\n") + "\n"
    known = set(existing.splitlines())
    fresh = [r for r in rules if r.strip() and r not in known]
    if not fresh:
        return existing, 0
    block = "\n## suggest-rules 追加\n" + "\n".join(fresh) + "\n"
    combined = base + block
    if len(sanitize_house_rules(combined)) > MAX_HOUSE_RULES_CHARS:
        return existing, len(sanitize_house_rules(combined)) - MAX_HOUSE_RULES_CHARS
    return combined, 0


def append_rules(path: Path, rules: list[str]) -> tuple[int, int]:
    """Append new rule lines. Returns (added, overflow); 0 added means nothing changed.

    A refusal (overflow > 0) leaves the file untouched — never half a block.
    """
    existing = path.read_text(encoding="utf-8") if path.is_file() else ""
    combined, overflow = plan_append(existing, rules)
    if overflow:
        return 0, overflow
    if combined == existing:
        return 0, 0
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(combined, encoding="utf-8")
    before = set(existing.splitlines())
    added = sum(1 for r in rules if r not in before)
    return added, 0
