"""Phase-3 tests: registry heterogeneity, falsification, aggregation, orchestration."""

from __future__ import annotations

import json

import pytest

from codeerratum.config import Config
from codeerratum.models import ModelRegistry, ModelSpec
from codeerratum.models.finding import (
    Anchor,
    DefectCategory,
    Finding,
    Lens,
    Suggestion,
    Verdict,
    snippet_hash,
)
from codeerratum.report import render_markdown
from codeerratum.review.aggregator import (
    agreement,
    cluster_findings,
    jaccard,
    rank_findings,
)
from codeerratum.review.falsification import LLMFalsify, should_falsify


def _finding(claim: str, line: int = 3, category=DefectCategory.BOUNDARY, severity="P1") -> Finding:
    snippet = "    return x[0]\n"
    return Finding(
        id=Finding.content_id("u1", claim, [f"m.py:{line}:{category.value}"]),
        unit_id="u1",
        lens=Lens.CORRECTNESS,
        category=category,
        severity=severity,
        claim=claim,
        detail="当 x 为空时抛 IndexError。",
        anchors=[
            Anchor(
                file="m.py",
                start_line=line,
                end_line=line,
                snippet=snippet,
                snippet_hash=snippet_hash(snippet),
            )
        ],
        suggestion=Suggestion(explanation="判空后返回", acceptance="pytest -q"),
    )


# --- Registry ---------------------------------------------------------------


def test_derive_family():
    from codeerratum.models.registry import derive_family

    assert derive_family("gpt-4o-2024-08-06") == "openai"
    assert derive_family("claude-3-5-sonnet") == "anthropic"
    assert derive_family("deepseek-chat") == "deepseek"
    assert derive_family("my-custom-model") == "my-custom-model"


def test_registry_dual_requires_heterogeneous():
    registry = ModelRegistry(
        models=[
            ModelSpec(model_id="gpt-4o-x", base_url="http://a"),
            ModelSpec(model_id="gpt-3.5-x", base_url="http://b"),
        ]
    )
    with pytest.raises(ValueError, match="heterogeneous"):
        registry.route("dual")


def test_registry_route_with_third_vendor():
    registry = ModelRegistry(
        models=[
            ModelSpec(model_id="gpt-4o-x", base_url="http://a"),
            ModelSpec(model_id="claude-3-x", base_url="http://b"),
            ModelSpec(model_id="deepseek-chat", base_url="http://c"),
        ]
    )
    reviewers, falsifier = registry.route("dual")
    assert len(reviewers) == 2
    assert {m.resolved_family() for m in reviewers} == {"openai", "anthropic"}
    assert falsifier is not None and falsifier.resolved_family() == "deepseek"


def test_registry_excludes_author():
    registry = ModelRegistry(
        models=[
            ModelSpec(model_id="gpt-4o-x", base_url="http://a"),
            ModelSpec(model_id="claude-3-x", base_url="http://b"),
            ModelSpec(model_id="deepseek-chat", base_url="http://c"),
        ]
    )
    reviewers, _ = registry.route("dual", author_model="gpt-4o-x")
    # Author model must never appear in a reviewer seat (product principle #1).
    assert "gpt-4o-x" not in [m.model_id for m in reviewers]
    assert len(reviewers) == 2
    # Excluding the author leaves too few heterogeneous reviewers -> hard error.
    with pytest.raises(ValueError, match="heterogeneous"):
        ModelRegistry(
            models=[
                ModelSpec(model_id="gpt-4o-x", base_url="http://a"),
                ModelSpec(model_id="gpt-3.5-x", base_url="http://b"),
            ]
        ).route("dual", author_model="gpt-4o-x")


def test_config_backward_compatible(monkeypatch):
    monkeypatch.setenv("CODEERRATUM_BASE_URL", "http://localhost/v1")
    monkeypatch.setenv("CODEERRATUM_MODEL", "local-model")
    monkeypatch.delenv("CODEERRATUM_MODELS", raising=False)
    monkeypatch.delenv("CODEERRATUM_MODE", raising=False)
    cfg = Config.from_env()
    assert cfg.mode == "quick"
    assert cfg.model == "local-model"
    assert len(cfg.registry.models) == 1


def test_config_multi_model(monkeypatch):
    monkeypatch.setenv(
        "CODEERRATUM_MODELS",
        json.dumps(
            [
                {"model_id": "gpt-4o-x", "base_url": "http://a"},
                {"model_id": "claude-3-x", "base_url": "http://b"},
            ]
        ),
    )
    monkeypatch.setenv("CODEERRATUM_MODE", "dual")
    cfg = Config.from_env()
    assert cfg.mode == "dual"
    assert len(cfg.registry.models) == 2


# --- Aggregation ------------------------------------------------------------


def test_agreement_logic():
    two_asserts = [
        Verdict(model_id="a", role="assert", reason="x"),
        Verdict(model_id="b", role="assert", reason="x"),
    ]
    assert agreement(two_asserts) == "unanimous"
    assert agreement([two_asserts[0]]) == "unique"
    assert agreement(two_asserts + [Verdict(model_id="c", role="refute", reason="y")]) == "conflict"
    assert agreement([]) == "unverified"


def test_agreement_ignores_falsifier_confirm_and_duplicate_asserts():
    """A confirm is not a second reviewer; neither is the same model twice.

    Reverting either rule puts every surviving finding in the consensus zone and
    empties the unique zone — the one place the product shows heterogeneous value.
    """
    one = [Verdict(model_id="a", role="assert", reason="x")]
    confirmed = one + [Verdict(model_id="falsifier", role="confirm", reason="still true")]
    assert agreement(confirmed) == "unique"
    repeated = one + [Verdict(model_id="a", role="assert", reason="x again")]
    assert agreement(repeated) == "unique"


def test_jaccard_and_cluster():
    assert jaccard("空列表取首元素崩溃", "空列表取首元素崩溃") == 1.0
    assert jaccard("abc", "xyz") == 0.0
    f1 = _finding("空列表取首元素崩溃")
    f2 = _finding("空列表访问首元素崩溃")
    merged, counts = cluster_findings({"modelA": [f1], "modelB": [f2]})
    assert counts == {"modelA": 1, "modelB": 1}
    assert len(merged) == 1  # clustered into one issue
    assert set(merged[0].source_models) == {"modelA", "modelB"}
    assert merged[0].agreement == "unanimous"


def test_conflict_zone_after_refute():
    f1 = _finding("空列表取首元素崩溃")
    f2 = _finding("空列表取首元素崩溃")  # same issue reported by both
    merged, _ = cluster_findings({"modelA": [f1], "modelB": [f2]})
    merged[0].verdicts.append(Verdict(model_id="modelC", role="refute", reason="有判空"))
    merged[0].agreement = agreement(merged[0].verdicts)
    assert merged[0].agreement == "conflict"


def test_rank_prefers_severity_and_unique():
    p0 = _finding("严重问题", severity="P0")
    p0.agreement = "unique"
    p3 = _finding("轻微问题", severity="P3")
    p3.agreement = "unanimous"
    ranked = rank_findings([p3, p0])
    assert ranked[0].claim == "严重问题"


# --- Falsification ----------------------------------------------------------


def test_should_falsify_gate():
    f = _finding("空列表取首元素崩溃", severity="P1")
    f.agreement = "unique"
    assert should_falsify(f) is True
    f2 = _finding("小问题", severity="P3")
    f2.agreement = "unanimous"
    assert should_falsify(f2) is False
    f3 = _finding("已复现问题", severity="P1")
    f3.verification = "reproduced"
    assert should_falsify(f3) is False


def test_llm_falsify_schema():
    good = LLMFalsify(verdict="refuted", reason="有判空", counterexample="x=[] 时有 guard")
    assert good.verdict == "refuted"
    with pytest.raises(ValueError):
        LLMFalsify(verdict="refuted", reason="有判空")  # missing counterexample
    with pytest.raises(ValueError):
        LLMFalsify(verdict="maybe", reason="x")


# --- Report three zones -----------------------------------------------------

from codeerratum.models import FindingCollection  # noqa: E402


def test_report_three_zones():
    unique = _finding("独有发现", severity="P1")
    unique.source_models = ["modelA"]
    unique.agreement = "unique"
    conflict = _finding("冲突发现", severity="P0")
    conflict.source_models = ["modelA", "modelB"]
    conflict.agreement = "conflict"
    conflict.verdicts = [
        Verdict(model_id="modelA", role="assert", reason="有问题"),
        Verdict(model_id="modelC", role="refute", reason="无问题"),
    ]
    consensus = _finding("共识发现", severity="P2")
    consensus.source_models = ["modelA", "modelB"]
    consensus.agreement = "unanimous"

    collection = FindingCollection(
        run_id="r1",
        repo="/repo",
        commit="abc1234",
        model_id="modelA,modelB",
        spec_level="L1",
        mode="dual",
        reviewer_models=["modelA", "modelB"],
        findings=[unique, conflict, consensus],
    )
    md = render_markdown(collection, "ts")
    assert "独有发现" in md
    assert "冲突区" in md
    assert "共识区" in md
    assert "来源模型：modelA" in md
    assert "模型分歧" in md


def test_report_quick_mode_flat():
    f = _finding("普通发现")
    collection = FindingCollection(
        run_id="r1",
        repo="/repo",
        commit="abc",
        model_id="m",
        spec_level="L1",
        mode="quick",
        findings=[f],
    )
    md = render_markdown(collection, "ts")
    assert "独有发现（仅单模型" not in md
    assert "## F-001" in md
