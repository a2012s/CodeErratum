"""The unit budget: how many of the split units a run actually reviews.

The default of 10 is a cost guard for the common case. It turned into a wall
the moment someone asked for a full sweep: 332 units were split, 10 were
reviewed, and there was no value that meant "all of them".
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from codeerratum import pipeline
from codeerratum.ingest import get_diff, split_diff
from codeerratum.models import FindingCollection


def _dirty_repo(root: Path, files: int = 12) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.io"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=root, check=True)
    for i in range(files):
        (root / f"mod_{i:02d}.py").write_text(f"def f_{i}():\n    return {i}\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=root, check=True)
    for i in range(files):
        (root / f"mod_{i:02d}.py").write_text(
            f"def f_{i}():\n    return {i} + 1\n", encoding="utf-8"
        )
    return root


def _stub_run(monkeypatch, seen: dict) -> None:
    monkeypatch.setenv("CODEERRATUM_BASE_URL", "http://stub.invalid/v1")
    monkeypatch.setenv("CODEERRATUM_MODEL", "gpt-stub")
    monkeypatch.delenv("CODEERRATUM_MODELS", raising=False)

    async def fake_run(registry, mode, units, repo_root, run_id, **kwargs):
        seen["units"] = len(units)
        return (
            FindingCollection(
                run_id=run_id,
                repo=str(repo_root),
                commit="",
                model_id="m",
                spec_level="L1",
            ),
            [],
            [],
        )

    monkeypatch.setattr(pipeline, "run_review", fake_run)


def test_zero_budget_reviews_every_unit(tmp_path, monkeypatch):
    """0 means "no budget": every unit the diff produced reaches the reviewer."""
    repo = _dirty_repo(tmp_path / "repo")
    expected = len(split_diff(get_diff("HEAD", repo), repo))
    assert expected > 10, "the fixture must exceed the default budget to prove anything"

    seen: dict = {}
    _stub_run(monkeypatch, seen)
    notes: list[str] = []
    outcome = pipeline.execute_review(repo, diff_spec="HEAD", max_units=0, progress=notes.append)

    assert seen["units"] == expected
    assert outcome.units_selected == expected
    assert not [n for n in notes if "仅审查" in n], "nothing was cut, so no cap notice"


def test_a_positive_budget_still_caps_and_says_so(tmp_path, monkeypatch):
    """The guard is not gone — it just cannot be the only answer."""
    repo = _dirty_repo(tmp_path / "repo")
    expected = len(split_diff(get_diff("HEAD", repo), repo))

    seen: dict = {}
    _stub_run(monkeypatch, seen)
    notes: list[str] = []
    outcome = pipeline.execute_review(repo, diff_spec="HEAD", max_units=5, progress=notes.append)

    assert seen["units"] == 5
    assert outcome.units_selected == 5
    assert any("仅审查 5/" in n for n in notes), notes
    assert expected > 5


def test_auto_budget_covers_small_diffs_and_caps_large_ones():
    """None means "decide for me": everything when it is small, else a top slice.

    The default used to be a flat 10, which cut a 44-unit diff down to a quarter
    without explanation. Auto is a single constant the report can name.
    """
    from codeerratum.pipeline import AUTO_UNIT_BUDGET, _unit_budget

    assert _unit_budget(None, 12) == (12, "auto")
    assert _unit_budget(None, AUTO_UNIT_BUDGET) == (AUTO_UNIT_BUDGET, "auto")
    assert _unit_budget(None, 332) == (AUTO_UNIT_BUDGET, "auto")
    assert _unit_budget(0, 332) == (332, "all")
    assert _unit_budget(7, 332) == (7, "fixed")


def test_default_call_reviews_every_unit_of_a_normal_diff(tmp_path, monkeypatch):
    """No max_units at all — the CLI/UI default — must not silently drop units."""
    repo = _dirty_repo(tmp_path / "repo")
    expected = len(split_diff(get_diff("HEAD", repo), repo))
    assert expected <= 60, "fixture must stay under the auto budget"

    seen: dict = {}
    _stub_run(monkeypatch, seen)
    outcome = pipeline.execute_review(repo, diff_spec="HEAD")
    assert seen["units"] == expected
    assert outcome.units_selected == expected


def test_negative_budget_raises_instead_of_reviewing_everything():
    """-1 used to fall into the ``<= 0`` branch and review the whole diff.

    A negative cap looks like a smaller budget but meant "no budget" — the
    exact opposite of what the flag promises. Now it is a config error, and
    only 0 keeps the full-sweep meaning.
    """
    import pytest

    from codeerratum.pipeline import ConfigError, _unit_budget

    with pytest.raises(ConfigError):
        _unit_budget(-1, 12)
    with pytest.raises(ConfigError):
        _unit_budget(-99, 12)
    # the other three meanings are untouched
    assert _unit_budget(0, 12) == (12, "all")
    assert _unit_budget(None, 12) == (12, "auto")
    assert _unit_budget(3, 12) == (3, "fixed")


def test_machine_drops_are_summarized_in_the_notes(tmp_path, monkeypatch):
    """Every rejected entry is a machine drop; the report must say why counts shrank."""
    repo = _dirty_repo(tmp_path / "repo")
    monkeypatch.setenv("CODEERRATUM_BASE_URL", "http://stub.invalid/v1")
    monkeypatch.setenv("CODEERRATUM_MODEL", "gpt-stub")
    monkeypatch.delenv("CODEERRATUM_MODELS", raising=False)

    async def fake_run(registry, mode, units, repo_root, run_id, **kwargs):
        return (
            FindingCollection(
                run_id=run_id,
                repo=str(repo_root),
                commit="",
                model_id="m",
                spec_level="L1",
                rejected=[
                    {"reason": "refuted", "model": "falsifier", "unit_id": "u1"},
                    {"reason": "refuted", "model": "falsifier", "unit_id": "u2"},
                    {"reason": "verify_error", "model": "verifier", "unit_id": "u3"},
                ],
            ),
            [],
            [],
        )

    monkeypatch.setattr(pipeline, "run_review", fake_run)
    outcome = pipeline.execute_review(repo, diff_spec="HEAD")

    notes = outcome.collection.notes
    assert any("refuted×2" in n for n in notes), notes
    assert any("verify_error×1" in n for n in notes), notes
