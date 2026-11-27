"""Offline evaluation — the ground truth behind "how trustworthy was this review".

``stats()`` answers what a human *did* with the work order (adoption rate). This
module answers whether the review was **right**, which is a different number on
purpose: a report gets adopted because it is convenient and can still be wrong about
the code, and a correct finding nobody acted on is invisible to adoption rate.

Two sources of truth, because only two kinds exist:

* a **seeded manifest** — a repository whose defects are known by construction
  (``demo/seeded-bugs/bugs.json``). Here recall is measured instead of guessed, and
  because the baseline file is clean by design, a finding that matches no seeded bug
  *is* a false positive. This is the only place precision and recall are both real.
* a **label file** — exported from live runs (``--export``). It carries the verdicts a
  human already gave, plus the claims the falsifier killed. Those killed claims never
  appear in a report, so nobody has ever adjudicated them: without them the falsifier's
  false-kill rate is unmeasurable, and that rate is exactly what gates the clustering
  thresholds (see the Jaccard note in AGENTS.md).

Nothing here calls a model. It reads artifacts a review already produced.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .models.finding import Finding, FindingCollection

# A label file is hand-edited, so the accepted spellings are deliberately few and
# case-insensitive. Anything else is "unlabeled", never a guess.
_TRUE = {"true", "real", "yes", "y", "accepted", "accept", "已修复", "真", "是"}
_FALSE = {"false", "fake", "fp", "no", "n", "rejected", "reject", "误报", "假", "否"}


def _verdict_of(value: Any) -> str:
    """Map a label file's verdict field to "true" / "false" / "" (unlabeled)."""
    text = str(value or "").strip().lower()
    if text in _TRUE:
        return "true"
    if text in _FALSE:
        return "false"
    return ""


@dataclass(frozen=True)
class SeededBug:
    """One defect the manifest says is really in the reviewed code."""

    file: str
    start_line: int
    end_line: int
    category: str = ""
    note: str = ""

    @property
    def lines(self) -> str:
        return (
            str(self.start_line)
            if self.start_line == self.end_line
            else (f"{self.start_line}-{self.end_line}")
        )


def _same_file(a: str, b: str) -> bool:
    """Match repo-relative, absolute and manifest-written paths alike."""
    na = (a or "").replace("\\", "/").strip("/")
    nb = (b or "").replace("\\", "/").strip("/")
    if not na or not nb:
        return False
    return na == nb or na.endswith("/" + nb) or nb.endswith("/" + na)


def load_seeded_bugs(path: Path | str) -> list[SeededBug]:
    """Read a bug manifest (JSON list, or ``{"bugs": [...]}``).

    A malformed entry is an error, not a skip: a half-loaded manifest silently turns
    a missed bug into "not in the ground truth", which is the one failure mode an
    evaluation harness must not have.
    """
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    items = raw.get("bugs") if isinstance(raw, dict) else raw
    if not isinstance(items, list) or not items:
        raise ValueError(f"{path}: expected a non-empty list of bugs")
    bugs: list[SeededBug] = []
    for i, item in enumerate(items):
        if not isinstance(item, dict) or not item.get("file"):
            raise ValueError(f"{path}: bug #{i + 1} needs at least a 'file'")
        start = int(item.get("start_line") or 0)
        end = int(item.get("end_line") or start or 0)
        if start < 1 or end < start:
            raise ValueError(f"{path}: bug #{i + 1} has an invalid line range {start}-{end}")
        bugs.append(
            SeededBug(
                file=str(item["file"]),
                start_line=start,
                end_line=end,
                category=str(item.get("category") or ""),
                note=str(item.get("note") or ""),
            )
        )
    return bugs


def _hits(finding: Finding, bug: SeededBug) -> bool:
    """Anchor overlap, not line equality.

    A finding citing the enclosing statement and one citing the call site both count:
    what is being scored is whether the review *found the defect*, not whether it
    quoted the same line.
    """
    for anchor in finding.anchors:
        if _same_file(anchor.file, bug.file) and (
            anchor.start_line <= bug.end_line and bug.start_line <= anchor.end_line
        ):
            return True
    return False


def _all_findings(collections: list[FindingCollection]) -> list[Finding]:
    return [f for c in collections for f in c.findings]


def _all_refuted(collections: list[FindingCollection]) -> list[Finding]:
    return [f for c in collections for f in c.refuted_findings]


def score_seeded(collections: list[FindingCollection], bugs: list[SeededBug]) -> dict[str, Any]:
    """Score reported findings against a repository whose real defects are known.

    ``precision`` is honest only because the seeded baseline is clean: with a manifest
    over a real repository, unmatched findings include real defects the manifest never
    listed, so the caller should treat precision as an upper bound there. Recall holds
    in both cases.
    """
    reported = _all_findings(collections)
    refuted = _all_refuted(collections)

    per_model: dict[str, dict[str, int]] = {}

    def bump(model: str, key: str) -> None:
        per_model.setdefault(model, {"assert": 0, "hits": 0, "refuted": 0})[key] += 1

    for f in reported:
        hit = any(_hits(f, b) for b in bugs)
        for model in f.source_models or [""]:
            if not model:
                continue
            bump(model, "assert")
            if hit:
                bump(model, "hits")
    for f in refuted:
        for model in f.source_models or []:
            bump(model, "refuted")

    bug_rows: list[dict[str, Any]] = []
    for bug in bugs:
        matched = [f for f in reported if _hits(f, bug)]
        killed = [f for f in refuted if _hits(f, bug)]
        bug_rows.append(
            {
                "file": bug.file,
                "lines": bug.lines,
                "category": bug.category,
                "note": bug.note,
                "found": bool(matched),
                "only_refuted": bool(killed and not matched),
                "reported_by": sorted({m for f in matched for m in f.source_models}),
                "refuted_by": sorted({m for f in killed for m in f.source_models}),
                "finding_ids": [f.id for f in matched],
            }
        )

    found = sum(1 for b in bug_rows if b["found"])
    hits = sum(1 for f in reported if any(_hits(f, b) for b in bugs))
    for row in per_model.values():
        row["precision"] = round(row["hits"] / row["assert"], 3) if row["assert"] else None
        row["recall_share"] = round(row["hits"] / len(bugs), 3) if bugs else None

    return {
        "kind": "seeded",
        "bugs_total": len(bugs),
        "bugs_found": found,
        "recall": round(found / len(bugs), 3) if bugs else None,
        "findings_reported": len(reported),
        "findings_matched": hits,
        "precision": round(hits / len(reported), 3) if reported else None,
        # A seeded bug the only sighting of which the falsifier talked out of the
        # report. This is the number that says the refutation round costs real bugs.
        "false_kills": [b for b in bug_rows if b["only_refuted"]],
        "missed": [b for b in bug_rows if not b["found"] and not b["only_refuted"]],
        "bugs": bug_rows,
        "per_model": per_model,
        "clustering": _cluster_rollup(collections),
    }


def _cluster_rollup(collections: list[FindingCollection]) -> dict[str, Any]:
    """Raw -> clustered -> survived, summed over the runs actually scored.

    ``merge_rate`` is how much cross-model duplication clustering removed and
    ``kill_rate`` is what the falsifier then took out. Both are reported per stage
    rather than as one end-to-end ratio because they are tuned by different knobs.
    """
    raw = clustered = final = refuted = 0
    for c in collections:
        counts = c.comparison.get("raw_counts") or {}
        raw += sum(int(v) for v in counts.values())
        clustered += int(c.comparison.get("after_clustering") or 0)
        after = c.comparison.get("after_falsification")
        final += int(after) if after is not None else 0
        refuted += len(c.refuted_findings)
    return {
        "raw": raw,
        "after_clustering": clustered,
        "after_falsification": final,
        "merge_rate": round(1 - clustered / raw, 3) if raw else None,
        # Denominator is the clustered pool, not the raw one: the falsifier only ever
        # sees candidates that survived clustering.
        "kill_rate": round(1 - final / clustered, 3) if clustered else None,
        "refuted_findings": refuted,
    }


def run_collections(
    repo_root: Path, run_ids: list[str] | None = None, limit: int = 20
) -> tuple[list[FindingCollection], list[str]]:
    """Load run ledgers from ``<repo>/.codeerratum/runs/<run_id>/findings.json``.

    Returns (collections, warnings). A ledger that is missing or unparseable is a
    warning, not an exception: eval scores what the machine still has, and says which
    runs it could not read rather than quietly dropping them from the denominator.
    """
    from .resume import STATE_DIR

    base = Path(repo_root) / STATE_DIR / "runs"
    if not base.is_dir():
        return [], [f"no run artifacts under {base}"]
    if run_ids:
        wanted = list(run_ids)
    else:
        wanted = sorted((p.name for p in base.iterdir() if p.is_dir()), reverse=True)[:limit]
    out: list[FindingCollection] = []
    warnings: list[str] = []
    for run_id in wanted:
        path = base / run_id / "findings.json"
        if not path.is_file():
            warnings.append(f"{run_id}: findings.json missing (cleaned up?)")
            continue
        try:
            out.append(FindingCollection.model_validate(json.loads(path.read_text("utf-8"))))
        except (ValueError, json.JSONDecodeError, OSError) as exc:
            warnings.append(f"{run_id}: unreadable ledger ({type(exc).__name__})")
    return out, warnings


def export_labels(
    store: Any, repo_root: Path | None = None, run_id: str | None = None
) -> list[dict[str, Any]]:
    """Build the label file: adjudicated findings, plus the claims nobody adjudicated.

    ``kind="finding"`` lines arrive pre-labelled from the human verdicts already in the
    store. ``kind="refuted"`` lines are emitted with an empty ``human`` field on
    purpose: they were removed before reaching a report, so no verdict exists, and
    labelling them by hand is the only way to learn the falsifier's false-kill rate.
    """
    rows: list[dict[str, Any]] = []
    for f in store.adjudication_rows(run_id=run_id):
        rows.append(
            {
                "kind": "finding",
                "run_id": f.get("run_id") or "",
                "id": f.get("id") or "",
                "file": f.get("file") or "",
                "line": None,
                "category": f.get("category") or "",
                "severity": f.get("severity") or "",
                "claim": f.get("claim") or "",
                "models": f.get("models") or [],
                "human": _verdict_of(f.get("status")),
            }
        )
    collections, _ = (
        run_collections(repo_root, [run_id] if run_id else None) if repo_root else ([], [])
    )
    seen: set[str] = set()
    for c in collections:
        if run_id and c.run_id != run_id:
            continue
        for f in c.refuted_findings:
            if f.id in seen:
                continue
            seen.add(f.id)
            rows.append(
                {
                    "kind": "refuted",
                    "run_id": c.run_id,
                    "id": f.id,
                    "file": f.anchors[0].file if f.anchors else "",
                    "line": f.anchors[0].start_line if f.anchors else None,
                    "category": f.category.value,
                    "severity": f.severity,
                    "claim": f.claim,
                    "models": list(f.source_models),
                    # Empty means "your call": true = a real bug the falsifier killed,
                    # false = it was right to kill it.
                    "human": "",
                }
            )
    return rows


def score_labels(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Score a label file (the DB's own verdicts, or a hand-completed export)."""
    per_model: dict[str, dict[str, int]] = {}
    per_category: dict[str, dict[str, int]] = {}
    adjudicated = 0

    def table(bucket: dict[str, dict[str, int]], key: str, label: str) -> None:
        row = bucket.setdefault(key, {"true": 0, "false": 0, "unlabeled": 0})
        row["true" if label == "true" else "false" if label == "false" else "unlabeled"] += 1

    for row in rows:
        if row.get("kind") != "finding":
            continue
        label = _verdict_of(row.get("human"))
        if label:
            adjudicated += 1
        for model in row.get("models") or []:
            table(per_model, model, label)
        category = str(row.get("category") or "uncategorized")
        table(per_category, category, label)

    for bucket in (per_model, per_category):
        for row in bucket.values():
            decided = row["true"] + row["false"]
            row["precision"] = round(row["true"] / decided, 3) if decided else None

    refuted_rows = [r for r in rows if r.get("kind") == "refuted"]
    killed_true = sum(1 for r in refuted_rows if _verdict_of(r.get("human")) == "true")
    killed_labeled = sum(1 for r in refuted_rows if _verdict_of(r.get("human")))
    return {
        "kind": "labels",
        "findings": sum(1 for r in rows if r.get("kind") == "finding"),
        "adjudicated": adjudicated,
        "per_model": per_model,
        "per_category": per_category,
        # None rather than 0: an unlabelled refuted sample must not read as
        # "the falsifier never kills a real bug".
        "refuted_total": len(refuted_rows),
        "refuted_labeled": killed_labeled,
        "false_kill_rate": round(killed_true / killed_labeled, 3) if killed_labeled else None,
    }


def _sample_cluster(f: Finding) -> str:
    """Bucket similar defects by (lens, category, directory), not by exact line.

    A missed bug never has an anchor in the run that missed it, so file+line
    matching would make the corpus impossible to score. The bucket is coarse on
    purpose: it measures whether a model covers this *kind* of problem in this
    area, which is the signal routing profiles can use.
    """
    parent = ""
    if f.anchors:
        parent = str(Path(f.anchors[0].file).parent).replace("\\", "/")
    raw = f"{f.lens.value}|{f.category.value}|{parent}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]


def export_missed_corpus(store: Any, collections: list[FindingCollection]) -> list[dict[str, Any]]:
    """Turn accepted verdicts into missed-bug samples for models that stayed silent.

    An accepted finding is a human-confirmed real defect. If a run seated models
    A and B but only A reported it, B has a miss in that cluster. This is weaker
    evidence than a seeded manifest — an accepted finding may still be a false
    positive the maintainer waved through — so the output is a *sample corpus*,
    never ground truth. It exists to grow recall's denominator beyond two seeded
    bugs, and to give a future red-team agent something to work on.
    """
    verdicts = {str(r["id"]): str(r.get("status") or "") for r in store.adjudication_rows()}
    by_id: dict[str, tuple[FindingCollection, Finding]] = {
        f.id: (c, f) for c in collections for f in c.findings
    }
    samples: dict[str, dict[str, Any]] = {}
    for fid, status in verdicts.items():
        if status != "accepted":
            continue
        found = by_id.get(fid)
        if found is None:
            continue  # the verdict outlived its ledger; nothing to attribute
        collection, finding = found
        reporters = set(finding.source_models)
        if not reporters and collection.model_id:
            reporters = {m.strip() for m in collection.model_id.split(",") if m.strip()}
        roster = set(collection.reviewer_models) or reporters
        for model in sorted(roster - reporters):
            sample_id = hashlib.sha256(f"{fid}|{model}".encode()).hexdigest()[:16]
            samples[sample_id] = {
                "sample_id": sample_id,
                "run_id": collection.run_id,
                "finding_id": fid,
                "model": model,
                "lens": finding.lens.value,
                "category": finding.category.value,
                "file": finding.anchors[0].file if finding.anchors else "",
                "cluster": _sample_cluster(finding),
                "claim": finding.claim,
            }
    return [samples[key] for key in sorted(samples, key=lambda k: (samples[k]["cluster"], k))]


def score_missed(
    collections: list[FindingCollection], samples: list[dict[str, Any]]
) -> dict[str, Any]:
    """Score a new batch of runs against the adjudicated miss corpus."""
    covered: set[tuple[str, str]] = set()
    for collection in collections:
        for finding in collection.findings:
            cluster = _sample_cluster(finding)
            models = set(finding.source_models)
            if not models and collection.model_id:
                models = {m.strip() for m in collection.model_id.split(",") if m.strip()}
            for model in models:
                covered.add((cluster, model))

    rows: list[dict[str, Any]] = []
    per_model: dict[str, dict[str, int | float | None]] = {}
    for sample in samples:
        caught = (str(sample.get("cluster") or ""), str(sample.get("model") or "")) in covered
        rows.append({**sample, "caught": caught})
        bucket = per_model.setdefault(
            str(sample.get("model") or ""), {"samples": 0, "caught": 0, "recall": None}
        )
        bucket["samples"] += 1
        bucket["caught"] += int(caught)
    for bucket in per_model.values():
        n = bucket["samples"]
        bucket["recall"] = round(bucket["caught"] / n, 3) if n else None
    caught_total = sum(1 for r in rows if r["caught"])
    return {
        "kind": "missed",
        "samples_total": len(samples),
        "samples_caught": caught_total,
        "sample_recall": round(caught_total / len(samples), 3) if samples else None,
        "per_model": per_model,
        "samples": rows,
    }


def format_eval(
    seeded: dict[str, Any] | None = None,
    labels: dict[str, Any] | None = None,
    missed: dict[str, Any] | None = None,
) -> str:
    """Human-readable report. Same numbers as ``--json``, so the two cannot drift."""
    out: list[str] = []
    if seeded:
        out.append("Seeded ground truth")
        rate = seeded.get("recall")
        out.append(
            f"  recall      {seeded['bugs_found']}/{seeded['bugs_total']}"
            + (f" = {rate:.1%}" if rate is not None else "")
        )
        prec = seeded.get("precision")
        out.append(
            f"  precision   {seeded['findings_matched']}/{seeded['findings_reported']}"
            + (f" = {prec:.1%}" if prec is not None else "")
        )
        for bug in seeded["bugs"]:
            mark = "found " if bug["found"] else ("killed" if bug["only_refuted"] else "missed")
            who = ",".join(bug["reported_by"] or bug["refuted_by"]) or "-"
            out.append(f"  [{mark}] {bug['file']}:{bug['lines']} {bug['note']} ({who})")
        for model, row in sorted(seeded["per_model"].items()):
            p = row["precision"]
            out.append(
                f"  {model:<28} {row['hits']}/{row['assert']} on-target"
                + (f" = {p:.1%}" if p is not None else "")
                + (f", {row['refuted']} refuted" if row["refuted"] else "")
            )
        cl = seeded["clustering"]
        if cl["raw"]:
            out.append(
                f"  clustering  {cl['raw']} raw -> {cl['after_clustering']}"
                f" (merged {cl['merge_rate']:.0%}) -> {cl['after_falsification']} survived"
            )
    if labels:
        out.append("Live labels")
        out.append(f"  findings    {labels['findings']} ({labels['adjudicated']} adjudicated)")
        if labels["refuted_total"]:
            rate = labels["false_kill_rate"]
            out.append(
                f"  falsifier   {labels['refuted_total']} refuted, "
                f"{labels['refuted_labeled']} labelled, "
                + (f"false-kill {rate:.1%}" if rate is not None else "false-kill unmeasured")
            )
        for model, row in sorted(labels["per_model"].items()):
            p = row["precision"]
            out.append(
                f"  {model:<28} {row['true']}t/{row['false']}f"
                + (f" = {p:.1%}" if p is not None else " (no verdict)")
            )
        worst = sorted(
            (r for r in labels["per_category"].values() if r["false"]),
            key=lambda r: (-r["false"],),
        )
        if worst:
            out.append("  categories crying wolf:")
            for category, row in sorted(
                ((k, v) for k, v in labels["per_category"].items() if v["false"]),
                key=lambda kv: -kv[1]["false"],
            ):
                out.append(f"    {category:<28} {row['false']} rejected / {row['true']} accepted")
    if missed:
        out.append("Adjudicated miss corpus")
        rate = missed["sample_recall"]
        out.append(
            f"  samples     {missed['samples_caught']}/{missed['samples_total']} now covered"
            + (f" = {rate:.1%}" if rate is not None else "")
        )
        for model, row in sorted(missed["per_model"].items()):
            r = row["recall"]
            out.append(
                f"  {model:<28} {row['caught']}/{row['samples']} covered"
                + (f" = {r:.1%}" if r is not None else "")
            )
    return "\n".join(out) if out else "nothing to score yet"
