"""Aggregator — cluster cross-model findings, judge agreement, split zones, rank.

Design constraints (from internal tech doc §5):
  - Two-level rule clustering, NOT embedding: position bucket + Jaccard token overlap.
  - agreement() is computed from verdict roles, never from a majority vote — LLM
    errors are ~60% correlated, so consensus is not evidence of correctness.
  - A finding reported by one model but surviving falsification is the single
    most valuable output ('unique'), and must be labeled with its source model.
"""

from __future__ import annotations

import re

from ..models.finding import Agreement, Finding, Verdict

# Same line, different category: two genuinely distinct defects often share a
# line and a long descriptive prefix ("返回值未…" / "循环未…"). The generic 0.35
# threshold merges them and silently drops one from the work order. Cross-label
# agreement on one line needs near-identical wording instead.
SAME_LINE_CROSS_CATEGORY_THRESHOLD = 0.8

_TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*|[\u4e00-\u9fff]")


def _tokens(text: str) -> set[str]:
    return set(_TOKEN_RE.findall(text.lower()))


def jaccard(a: str, b: str) -> float:
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def cluster_key(f: Finding) -> str:
    """Level 1: candidate group = same file.

    Bucketing by ``line // 3`` looked equivalent to "±3 lines" but split pairs
    that straddle a bucket boundary (line 2 vs line 4), so near-identical findings
    were never compared. Group first, then bound by real line distance.

    Category deliberately stays out of the key. It is a label the model picked,
    and two models describing one defect can disagree on it (``boundary`` vs
    ``off_by_one``). With category in the key those copies never met, so both
    survived into the unique zone — inflating exactly the signal the three-zone
    report exists to show. File + line distance + wording is the evidence;
    category only buys a shortcut when two reports land on the same line.
    """
    return f.anchors[0].file


def same_issue(a: Finding, b: Finding, threshold: float = 0.35, max_line_gap: int = 3) -> bool:
    """Level 2: same file AND (same line + same label, or nearby + wording overlap).

    Same line with different categories still needs wording overlap: two distinct
    bugs can share a line, and the old ``gap == 0 -> True`` merged them into one
    finding, silently deleting one of the two.
    """
    if cluster_key(a) != cluster_key(b):
        return False
    gap = abs(a.anchors[0].start_line - b.anchors[0].start_line)
    if gap > max_line_gap:
        return False
    if gap == 0 and a.category == b.category:
        # Same anchor, same label: models agree on the issue even when the
        # wording differs. Only within one category, though.
        return True
    if gap == 0:
        # Different labels on one line. A shared prefix is not enough — "崩溃"
        # vs "注入" need to stay as two findings. Require near-identical claims.
        return jaccard(a.claim, b.claim) >= SAME_LINE_CROSS_CATEGORY_THRESHOLD
    return jaccard(a.claim, b.claim) >= threshold


def agreement(verdicts: list[Verdict]) -> Agreement:
    """Derive agreement from *independent asserting models*, never a vote.

    Two traps this avoids:
      - A falsifier's ``confirm`` is a verdict on someone else's finding, not a
        second reviewer. Counting it made every confirmed finding "unanimous",
        so the unique zone — the whole point of heterogeneous review — stayed
        permanently empty.
      - The same model re-reporting a finding across several review units must
        count once. Counting verdicts instead of model ids inflated a single
        model's repeated output into "consensus".
    """
    assert_models = {v.model_id for v in verdicts if v.role == "assert"}
    refute_models = {v.model_id for v in verdicts if v.role == "refute"}
    if assert_models and refute_models:
        return "conflict"
    if len(assert_models) >= 2:
        return "unanimous"
    if len(assert_models) == 1:
        return "unique"
    return "unverified"


def _merge(group: list[Finding], model_ids: dict[int, str]) -> Finding:
    """Merge one cluster into a single Finding, unioning verdicts and sources."""
    # Keep the highest-severity, most-evidenced representative as the base.
    sev = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}
    base = sorted(group, key=lambda f: (sev[f.severity], -len(f.evidence)))[0]
    for other in group:
        if other is base:
            continue
        base.evidence.extend(other.evidence)
        base.verdicts.extend(other.verdicts)
        for src in other.source_models:
            if src not in base.source_models:
                base.source_models.append(src)

    # First-round assert verdicts: one per model that reported this issue.
    for f in group:
        mid = model_ids.get(id(f), "")
        if mid and not any(v.model_id == mid and v.role == "assert" for v in base.verdicts):
            base.verdicts.append(Verdict(model_id=mid, role="assert", reason=f.claim))

    # De-duplicate verdicts by (model_id, role).
    seen: set[tuple[str, str]] = set()
    unique_verdicts: list[Verdict] = []
    for v in base.verdicts:
        k = (v.model_id, v.role)
        if k not in seen:
            seen.add(k)
            unique_verdicts.append(v)
    base.verdicts = unique_verdicts

    for src in base.source_models:
        if not any(v.model_id == src and v.role == "assert" for v in base.verdicts):
            base.verdicts.insert(0, Verdict(model_id=src, role="assert", reason=base.claim))

    base.agreement = agreement(base.verdicts)
    return base


def cluster_findings(
    per_model: dict[str, list[Finding]],
) -> tuple[list[Finding], dict[str, int]]:
    """Cluster findings across models. Returns (merged findings, per-model raw counts)."""
    model_ids: dict[int, str] = {}
    all_findings: list[Finding] = []
    raw_counts: dict[str, int] = {}
    for model_id, findings in per_model.items():
        raw_counts[model_id] = len(findings)
        for f in findings:
            if not f.source_models:
                f.source_models = [model_id]
            model_ids[id(f)] = model_id
            all_findings.append(f)

    # Union-find style grouping by cluster key, then pairwise same_issue within bucket.
    buckets: dict[str, list[Finding]] = {}
    for f in all_findings:
        buckets.setdefault(cluster_key(f), []).append(f)

    merged: list[Finding] = []
    for group in buckets.values():
        clusters: list[list[Finding]] = []
        for f in group:
            placed = False
            for cluster in clusters:
                if any(same_issue(f, rep) for rep in cluster):
                    cluster.append(f)
                    placed = True
                    break
            if not placed:
                clusters.append([f])
        for cluster in clusters:
            merged.append(_merge(cluster, model_ids))
    return merged, raw_counts


def rank_findings(
    findings: list[Finding], rejected_category_counts: dict[str, int] | None = None
) -> list[Finding]:
    """Score by severity + evidence + agreement, minus historical FP penalty."""
    rejected_category_counts = rejected_category_counts or {}
    sev_w = {"P0": 4, "P1": 3, "P2": 2, "P3": 1}
    agree_w = {"unanimous": 2, "majority": 1, "unique": 1.5, "conflict": 0, "unverified": 0}

    def score(f: Finding) -> float:
        hard = 3 if any(e.strength == "hard" for e in f.evidence) else (1 if f.evidence else 0)
        fp_penalty = min(rejected_category_counts.get(f.category.value, 0), 5) * 0.5
        return sev_w[f.severity] * 10 + hard + agree_w.get(f.agreement, 0) - fp_penalty

    return sorted(findings, key=lambda f: (-score(f), f.id))
