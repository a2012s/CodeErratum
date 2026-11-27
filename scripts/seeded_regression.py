"""Nightly seeded regression: run a fixed fixture, append one metric row.

This is deliberately not a per-PR CI step. ``eval --seeded`` scores a *real*
review run, which needs model credentials and costs money; burning that on every
PR is both insecure and wasteful. A scheduled job instead runs the same fixture
with a pinned roster and appends recall / false-kill / precision to a curve, so
prompt and threshold changes can be compared before and after.

Only model *ids* are written to the curve. The roster file (which carries keys)
lives in the throwaway fixture repo and is deleted with it.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_FIXTURE = REPO_ROOT / "demo" / "seeded-bugs"


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def prepare_fixture(fixture: Path, config: Path, work_root: Path) -> Path:
    """Materialise the two-commit fixture repo with the pinned roster installed."""
    missing = [
        p
        for p in ("base_refunds.py", "buggy_refunds.py", "bugs.json")
        if not (fixture / p).is_file()
    ]
    if missing:
        raise ValueError(f"{fixture}: missing fixture files: {', '.join(missing)}")
    if not config.is_file():
        raise ValueError(f"pinned roster not found: {config}")

    repo = work_root / "seeded-fixture"
    repo.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(fixture / "base_refunds.py", repo / "refunds.py")
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "seeded@codeerratum.dev")
    _git(repo, "config", "user.name", "seeded regression")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "baseline: refund helpers")
    shutil.copyfile(fixture / "buggy_refunds.py", repo / "refunds.py")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "seeded defects")

    (repo / "bugs.json").write_bytes((fixture / "bugs.json").read_bytes())
    state = repo / ".codeerratum"
    state.mkdir(exist_ok=True)
    shutil.copyfile(config, state / "config.json")
    return repo


def pinned_models(config: Path) -> list[str]:
    """Model ids only — never copy keys into the curve or the logs."""
    data = json.loads(config.read_text(encoding="utf-8"))
    models = data.get("models") if isinstance(data, dict) else None
    if not isinstance(models, list):
        raise ValueError(f"{config}: models must be a non-empty array")
    return [str(m["model_id"]) for m in models if isinstance(m, dict) and m.get("model_id")]


def metric_row(
    payload: dict[str, Any], commit: str, date: str, models: list[str]
) -> dict[str, Any]:
    """Flatten ``eval --json`` into one curve row; absent metrics stay null."""
    seeded = payload.get("seeded") or {}
    labels = payload.get("labels") or {}
    return {
        "date": date,
        "commit": commit,
        "recall": seeded.get("recall"),
        "precision": seeded.get("precision"),
        "false_kill_rate": labels.get("false_kill_rate"),
        "models": models,
    }


def append_metric(curve: Path, row: dict[str, Any]) -> bool:
    """Append one row; return False when an identical (date, commit, roster) exists.

    A nightly job may be re-run by hand after a transient failure. Duplicating the
    same day's point would make the curve look like a trend when nothing changed.
    """
    curve.parent.mkdir(parents=True, exist_ok=True)
    key = (row.get("date"), row.get("commit"), tuple(row.get("models") or []))
    if curve.is_file():
        for line in curve.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                old = json.loads(line)
            except json.JSONDecodeError:
                continue  # a hand-edited bad line must not block new points
            if (
                old.get("date"),
                old.get("commit"),
                tuple(old.get("models") or []),
            ) == key:
                return False
    with curve.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    return True


def run_regression(
    *,
    fixture: Path,
    config: Path,
    curve: Path,
    binary: str,
    commit: str,
    date: str,
    mode: str,
    work_root: Path | None = None,
) -> tuple[dict[str, Any], bool]:
    models = pinned_models(config)
    temp = tempfile.TemporaryDirectory(prefix="ce-seeded-") if work_root is None else None
    root = Path(temp.name) if temp else work_root
    assert root is not None
    try:
        repo = prepare_fixture(fixture, config, root)
        env = dict(os.environ)
        # The fixture's own config wins; a developer's ~/.codeerratum must not
        # leak a different roster into a scheduled run.
        env["CODEERRATUM_HOME"] = str(root / "home")
        subprocess.run(
            [binary, "review", "--diff", "HEAD~1", "--mode", mode, "--out", "erratumReview.md"],
            cwd=repo,
            env=env,
            check=True,
        )
        result = subprocess.run(
            [binary, "eval", "--seeded", "bugs.json", "--json"],
            cwd=repo,
            env=env,
            check=True,
            capture_output=True,
            text=True,
        )
        payload = json.loads(result.stdout)
        row = metric_row(payload, commit, date, models)
        added = append_metric(curve, row)
        return row, added
    finally:
        if temp is not None:
            temp.cleanup()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", type=Path, default=DEFAULT_FIXTURE)
    parser.add_argument(
        "--config", type=Path, required=True, help="Pinned roster JSON (contains keys)"
    )
    parser.add_argument("--curve", type=Path, default=REPO_ROOT / "metrics" / "seeded.jsonl")
    parser.add_argument("--bin", default="codeerratum", help="codeerratum executable")
    parser.add_argument("--commit", default=os.environ.get("GITHUB_SHA", "local"))
    parser.add_argument("--date", default=datetime.now(UTC).strftime("%Y-%m-%d"))
    parser.add_argument("--mode", default="dual", choices=("quick", "dual", "committee"))
    args = parser.parse_args(argv)

    try:
        row, added = run_regression(
            fixture=args.fixture,
            config=args.config,
            curve=args.curve,
            binary=args.bin,
            commit=args.commit,
            date=args.date,
            mode=args.mode,
        )
    except (OSError, ValueError, subprocess.CalledProcessError, json.JSONDecodeError) as exc:
        print(f"seeded regression failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    status = "appended" if added else "already present"
    print(
        f"seeded regression {status}: recall={row['recall']} "
        f"false_kill_rate={row['false_kill_rate']} precision={row['precision']} -> {args.curve}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
