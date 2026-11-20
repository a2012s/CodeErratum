"""Spec level, abstain monitoring and model-profile feedback.

These three closed a specific kind of dishonesty: numbers in the report that looked
measured but were constants. `spec_level` was always L1, abstain was never counted,
and `measured_*` was never written — so routing never learned anything regardless of
how much history accumulated.
"""

from __future__ import annotations

import pytest

from codeerratum.models import Anchor, Finding, FindingCollection, ModelRegistry
from codeerratum.models.finding import Suggestion, Verdict
from codeerratum.pipeline import abstain_warning, apply_measured_profiles
from codeerratum.report.markdown import render_markdown
from codeerratum.spec import annotation_coverage, detect
from codeerratum.store import ReviewStore

SOURCE = ["def foo(items):", "    return items[0]"]


def collection(**kw) -> FindingCollection:
    base = {
        "run_id": "r1",
        "repo": "/tmp/repo",
        "commit": "c1",
        "model_id": "m",
        "spec_level": "L1",
    }
    return FindingCollection(**(base | kw))


def finding(claim: str, models: list[str], agreement: str = "unverified", **kw) -> Finding:
    anchor = Anchor.from_source("m.py", 2, 2, SOURCE)
    return Finding(
        id=Finding.content_id("u1", claim, ["m.py:2"]),
        unit_id="u1",
        lens="correctness",
        category="boundary",
        severity="P1",
        claim=claim,
        detail="当 items 为空时抛 IndexError。",
        anchors=[anchor],
        suggestion=Suggestion(explanation="判空", acceptance="pytest -q"),
        source_models=models,
        agreement=agreement,
        **kw,
    )


# ---- spec level ---------------------------------------------------------


def test_bare_repo_is_l1_and_says_why(tmp_path):
    (tmp_path / "m.py").write_text("def f(x):\n    return x\n", encoding="utf-8")
    level, reason = detect(tmp_path)
    assert level == "L1"
    assert "无规格来源" in reason or "仅" in reason


def test_spec_directory_lifts_to_l3(tmp_path):
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "spec.md").write_text("# spec\n", encoding="utf-8")
    level, reason = detect(tmp_path)
    assert level == "L3"
    assert "spec.md" in reason


def test_contracts_plus_tests_is_l4(tmp_path):
    (tmp_path / "contracts").mkdir()
    (tmp_path / "contracts" / "api.yaml").write_text("x: 1\n", encoding="utf-8")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_a.py").write_text("def test_a():\n    assert True\n")
    level, reason = detect(tmp_path)
    assert level == "L4"
    assert "contracts" in reason


def test_documented_code_is_l2(tmp_path):
    (tmp_path / "m.py").write_text(
        "def a(x: int) -> int:\n    return x\n\ndef b(y: str) -> str:\n    return y\n",
        encoding="utf-8",
    )
    level, reason = detect(tmp_path)
    assert level == "L2"
    assert "%" in reason


def test_coverage_ignores_private_functions(tmp_path):
    (tmp_path / "m.py").write_text(
        "def _hidden(x):\n    return x\n\ndef shown(x: int) -> int:\n    return x\n",
        encoding="utf-8",
    )
    assert annotation_coverage(tmp_path) == 1.0


def test_header_prints_the_reason_next_to_the_level():
    md = render_markdown(
        collection(spec_level="L2", spec_level_reason="80% 的公开符号带注解或 docstring"),
        "2026-01-01T00:00:00Z",
    )
    assert "规格等级=L2（80% 的公开符号带注解或 docstring）" in md


def test_header_without_reason_stays_short():
    md = render_markdown(collection(), "2026-01-01T00:00:00Z")
    assert "规格等级=L1" in md
    assert "规格等级=L1（" not in md


# ---- abstain monitoring -------------------------------------------------


def _with_verdicts(roles: list[str], model: str = "gpt-a") -> list[Finding]:
    """One finding the model asserted, plus another model's finding it judged.

    A pure falsifier has no assertions, and `apply_measured_profiles` skips those on
    purpose: precision only matters for a model that also takes reviewer seats.
    """
    own = finding("自己报的", [model])
    other = finding("别人报的", ["gpt-b"])
    other.verdicts = [Verdict(model_id=model, role=role, reason="r") for role in roles]
    return [own, other]


def test_no_verdicts_means_no_warning():
    assert abstain_warning([finding("问题", ["gpt-stub"])]) == ""


def test_low_abstain_rate_is_quiet():
    assert abstain_warning(_with_verdicts(["refute", "confirm", "confirm"])) == ""


def test_high_abstain_rate_is_surfaced():
    warning = abstain_warning(_with_verdicts(["abstain", "abstain", "confirm"]))
    assert "证据不足" in warning
    assert "2/3" in warning


# ---- model profile feedback --------------------------------------------


@pytest.fixture
def store(tmp_path) -> ReviewStore:
    return ReviewStore(tmp_path / "feedback.db")


def test_save_run_counts_assertions_and_uniqueness(store):
    store.save_run(
        collection(
            findings=[
                finding("甲", ["gpt-a"], agreement="unique"),
                finding("乙", ["gpt-a", "claude-b"], agreement="unanimous"),
            ]
        ),
        "2026-01-01T00:00:00Z",
    )
    profiles = store.model_profile_map()
    assert profiles["gpt-a"]["n_assert"] == 2
    assert profiles["gpt-a"]["n_unique"] == 1
    assert profiles["claude-b"]["n_assert"] == 1
    assert profiles["claude-b"]["n_unique"] == 0


def test_verdicts_count_toward_precision_inputs(store):
    store.save_run(
        collection(findings=_with_verdicts(["confirm", "refute", "abstain"])),
        "2026-01-01T00:00:00Z",
    )
    profile = store.model_profile_map()["gpt-a"]
    assert profile["n_confirmed"] == 1
    assert profile["n_refuted"] == 1
    assert profile["n_assert"] == 1


def test_accumulates_across_runs(store):
    for i in range(3):
        store.save_run(
            collection(run_id=f"r{i}", findings=[finding(f"问题{i}", ["gpt-a"])]),
            "2026-01-01T00:00:00Z",
        )
    assert store.model_profile_map()["gpt-a"]["n_assert"] == 3


def test_profiles_reach_the_registry(store):
    store.save_run(
        collection(findings=[finding("甲", ["gpt-a"], agreement="unique")] * 40),
        "2026-01-01T00:00:00Z",
    )
    # 40 identical findings share one id in the DB, but the profile tally is per run,
    # so pad the registry check with the accumulated count instead of the row count.
    registry = ModelRegistry.from_json('[{"model_id":"gpt-a","base_url":"http://x/v1"}]')
    apply_measured_profiles(registry, store)
    assert registry.models[0].measured_n >= 1
    assert registry.models[0].measured_unique_rate == 1.0


def test_precision_needs_five_decided_verdicts(store):
    registry = ModelRegistry.from_json('[{"model_id":"gpt-a","base_url":"http://x/v1"}]')
    store.save_run(
        collection(findings=_with_verdicts(["confirm", "refute"])),
        "2026-01-01T00:00:00Z",
    )
    apply_measured_profiles(registry, store)
    # Two verdicts is noise; a routing change driven by it is worse than none.
    assert registry.models[0].measured_precision is None


def test_precision_applies_once_enough_verdicts_exist(store):
    registry = ModelRegistry.from_json('[{"model_id":"gpt-a","base_url":"http://x/v1"}]')
    store.save_run(
        collection(findings=_with_verdicts(["confirm", "confirm", "confirm", "confirm", "refute"])),
        "2026-01-01T00:00:00Z",
    )
    apply_measured_profiles(registry, store)
    assert registry.models[0].measured_precision == pytest.approx(0.8)


def test_unknown_model_is_left_alone(store):
    registry = ModelRegistry.from_json('[{"model_id":"never-used","base_url":"http://x/v1"}]')
    assert apply_measured_profiles(registry, store) == 0
    assert registry.models[0].measured_n == 0
