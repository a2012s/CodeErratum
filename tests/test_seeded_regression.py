"""The nightly regression harness: one fixed fixture, one append-only curve.

Two things matter here: the curve must never carry a key (only model ids), and a
re-run of the same date/commit/roster must not add a second point that looks like
a trend. The integration test drives a fake codeerratum binary so no model is
called and no money is spent.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location(
    "seeded_regression", _REPO_ROOT / "scripts" / "seeded_regression.py"
)
assert _SPEC is not None and _SPEC.loader is not None
sr = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(sr)


def _config(tmp_path: Path) -> Path:
    path = tmp_path / "roster.json"
    path.write_text(
        json.dumps(
            {
                "mode": "dual",
                "models": [
                    {"model_id": "verifier-a", "api_key": "super-secret", "base_url": "http://x"},
                    {"model_id": "verifier-b", "api_key": "another-secret", "base_url": "http://x"},
                ],
            }
        ),
        encoding="utf-8",
    )
    return path


def test_metric_row_keeps_only_ids_and_nullable_metrics():
    row = sr.metric_row(
        {"seeded": {"recall": 0.5, "precision": None}, "labels": {"false_kill_rate": 0.1}},
        "abc123",
        "2026-11-28",
        ["m1", "m2"],
    )
    assert row == {
        "date": "2026-11-28",
        "commit": "abc123",
        "recall": 0.5,
        "precision": None,
        "false_kill_rate": 0.1,
        "models": ["m1", "m2"],
    }
    assert sr.metric_row({}, "x", "d", [])["recall"] is None


def test_pinned_models_returns_ids_without_keys(tmp_path: Path):
    assert sr.pinned_models(_config(tmp_path)) == ["verifier-a", "verifier-b"]


def test_append_metric_dedupes_and_tolerates_bad_lines(tmp_path: Path):
    curve = tmp_path / "metrics" / "seeded.jsonl"
    row = {
        "date": "2026-11-28",
        "commit": "abc",
        "recall": 0.5,
        "precision": None,
        "false_kill_rate": None,
        "models": ["m1"],
    }
    assert sr.append_metric(curve, row) is True
    assert sr.append_metric(curve, row) is False
    curve.write_text("{not json}\n" + curve.read_text(encoding="utf-8"), encoding="utf-8")
    other = {**row, "commit": "def"}
    assert sr.append_metric(curve, other) is True
    assert len(curve.read_text(encoding="utf-8").splitlines()) == 3


def test_full_run_with_a_fake_binary(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    config = _config(tmp_path)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "codeerratum"
    fake.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = review ]; then\n'
        "  mkdir -p .codeerratum/runs/fake\n"
        "  printf '{}' > .codeerratum/runs/fake/findings.json\n"
        "  exit 0\n"
        "fi\n"
        'if [ "$1" = eval ]; then\n'
        "  printf '%s' \"$FAKE_EVAL_JSON\"\n"
        "  exit 0\n"
        "fi\n"
        "exit 9\n",
        encoding="utf-8",
    )
    fake.chmod(0o755)
    monkeypatch.setenv(
        "FAKE_EVAL_JSON",
        json.dumps({"seeded": {"recall": 0.5, "precision": 0.25}, "labels": {}}),
    )

    curve = tmp_path / "metrics" / "seeded.jsonl"
    row, added = sr.run_regression(
        fixture=sr.DEFAULT_FIXTURE,
        config=config,
        curve=curve,
        binary=str(fake),
        commit="deadbeef",
        date="2026-11-28",
        mode="dual",
        work_root=tmp_path / "work",
    )
    assert added is True
    assert row["recall"] == 0.5
    saved = json.loads(curve.read_text(encoding="utf-8").splitlines()[0])
    assert saved == row
    assert "super-secret" not in curve.read_text(encoding="utf-8")
