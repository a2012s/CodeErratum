"""Regression tests for defects found in the code audit.

Each test pins one concrete failure mode that used to pass silently. Keep the
comment on top of each test: it states what breaks if the fix is reverted.
"""

import json
import subprocess
from pathlib import Path

import httpx
import pytest

from codeerratum.context import build_context
from codeerratum.evidence import collect_static
from codeerratum.ingest.git import DiffError, get_diff, split_diff
from codeerratum.models import (
    Anchor,
    Evidence,
    Finding,
    FindingCollection,
    Hunk,
    ReviewUnit,
    Suggestion,
)
from codeerratum.models.finding import DefectCategory, Lens, snippet_hash
from codeerratum.providers.base import schema_to_json_schema, to_strict_json_schema
from codeerratum.providers.openai_compat import OpenAICompatProvider
from codeerratum.report import render_markdown
from codeerratum.review.executor import build_messages, convert_findings
from codeerratum.review.schema import LLMReview
from codeerratum.store import ReviewStore


def _init_repo(path: Path) -> Path:
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.io"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=path, check=True)
    return path


def _unit(**overrides) -> ReviewUnit:
    defaults = dict(
        unit_id="u1",
        primary_file="a.py",
        symbol="f",
        kind="function",
        hunks=[Hunk(old_start=1, old_lines=2, new_start=1, new_lines=2, content="+x")],
        changed_lines=1,
    )
    defaults.update(overrides)
    return ReviewUnit(**defaults)


def _llm_item(**overrides) -> dict:
    item = {
        "lens": "correctness",
        "category": "boundary",
        "severity": "P1",
        "claim": "空列表取首元素",
        "detail": "items 为空时抛 IndexError",
        "anchor": {
            "file": "a.py",
            "start_line": 2,
            "end_line": 2,
            "snippet": "    return items[0]\n",
        },
        "suggestion": {"diff": None, "explanation": "先判空", "acceptance": "pytest -q"},
    }
    item.update(overrides)
    return item


SOURCE = ["def f(items):\n", "    return items[0]\n"]


# --- diff parsing -----------------------------------------------------------


def test_last_hunk_content_is_kept_for_every_file(tmp_path):
    """Reverting: files except the last lose their trailing hunk body, which
    silently disables risk tags and per-symbol attribution."""
    repo = _init_repo(tmp_path)
    (repo / "a.py").write_text("def a():\n    return 1\n", encoding="utf-8")
    (repo / "b.py").write_text("def b():\n    return 2\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=repo, check=True)
    (repo / "a.py").write_text("def a():\n    return 1 + 1\n", encoding="utf-8")
    (repo / "b.py").write_text("def b():\n    return 2 + 2\n", encoding="utf-8")

    units = split_diff(get_diff("HEAD", repo), repo)
    assert units, "expected units for both files"
    for unit in units:
        assert all(h.content for h in unit.hunks), f"empty hunk content in {unit.primary_file}"


def test_deleted_file_is_not_reviewable(tmp_path):
    """Reverting: a deleted file becomes a ReviewUnit whose source read raises
    FileNotFoundError, reported as a provider error."""
    repo = _init_repo(tmp_path)
    (repo / "gone.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=repo, check=True)
    subprocess.run(["git", "rm", "-q", "gone.py"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "rm"], cwd=repo, check=True)

    notices: list[str] = []
    units = split_diff(get_diff("HEAD~1", repo), repo, notices=notices)
    assert units == []
    assert any("gone.py" in note for note in notices)


def test_changed_lines_counts_only_added_lines(tmp_path):
    """Reverting: changed_lines reported the whole hunk size (context included)."""
    repo = _init_repo(tmp_path)
    (repo / "a.py").write_text("def f():\n    x = 1\n    return x\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=repo, check=True)
    (repo / "a.py").write_text("def f():\n    x = 2\n    return x\n", encoding="utf-8")

    units = split_diff(get_diff("HEAD", repo), repo)
    assert len(units) == 1
    assert units[0].changed_lines == 1


def test_bad_diff_spec_raises_diff_error(tmp_path):
    """Reverting: a raw RuntimeError traceback reaches the user."""
    repo = _init_repo(tmp_path)
    (repo / "a.py").write_text("x = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=repo, check=True)
    with pytest.raises(DiffError):
        get_diff("HEAD~1", repo)


# --- finding conversion -----------------------------------------------------


def test_one_invalid_finding_does_not_discard_the_batch():
    """Reverting: an over-long claim raised out of convert_findings, so the whole
    unit's findings were lost and surfaced as a provider error."""
    review = LLMReview.model_validate(
        {
            "summary": "x",
            "findings": [
                _llm_item(claim="这条结论特别特别长" * 10),
                _llm_item(claim="另一条有效结论", category="null_deref"),
            ],
        }
    )
    accepted, rejected = convert_findings(review, _unit(), SOURCE, "r1")
    assert len(accepted) == 1
    assert accepted[0].claim == "另一条有效结论"
    assert [r["reason"] for r in rejected] == ["invalid_finding"]
    assert "claim" in rejected[0]["error"]


def test_cross_file_anchor_is_rejected():
    """Reverting: a snippet from the primary file could be attached to a
    completely different file name, producing a fabricated location."""
    review = LLMReview.model_validate(
        {
            "summary": "x",
            "findings": [_llm_item(anchor={**_llm_item()["anchor"], "file": "elsewhere.py"})],
        }
    )
    accepted, rejected = convert_findings(review, _unit(), SOURCE, "r1")
    assert accepted == []
    assert rejected[0]["reason"] == "anchor_file_mismatch"


def test_model_lens_is_preserved():
    """Reverting: lens was hard-coded to correctness, so a security finding was
    reported as a correctness finding."""
    review = LLMReview.model_validate({"summary": "x", "findings": [_llm_item(lens="security")]})
    accepted, _ = convert_findings(review, _unit(), SOURCE, "r1")
    assert accepted[0].lens is Lens.SECURITY


# --- prompt construction ----------------------------------------------------


def test_prompt_sends_excerpt_not_whole_file(tmp_path):
    """Reverting: every unit sent the whole file plus a duplicated enclosing body,
    multiplying token cost by file size on every model."""
    lines = [f"def fn_{i}(x):\n    return x + {i}\n" for i in range(200)]
    path = tmp_path / "big.py"
    path.write_text("\n".join(lines), encoding="utf-8")
    source_lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    unit = _unit(
        primary_file="big.py",
        symbol="fn_0",
        hunks=[Hunk(old_start=2, old_lines=1, new_start=2, new_lines=1, content="+    return x")],
    )

    ctx = build_context(unit, tmp_path)
    messages = build_messages(
        unit, source_lines, enclosing_span=ctx["enclosing_range"], callers=ctx["callers"]
    )
    user = messages[1]["content"]
    assert "def fn_150" not in user, "unrelated function leaked into the prompt"
    assert user.count("def fn_0(") == 1, "enclosing function duplicated"
    assert len(user) < 3000, f"prompt too large: {len(user)} chars"


def test_callers_are_found(tmp_path):
    """Reverting: jedi was queried at the hunk line / column 0 and always
    returned no callers, silently disabling the enrichment."""
    (tmp_path / "lib.py").write_text("def target(x):\n    return x + 1\n", encoding="utf-8")
    (tmp_path / "app.py").write_text(
        "from lib import target\n\n\ndef main():\n    return target(1)\n", encoding="utf-8"
    )
    unit = _unit(
        primary_file="lib.py",
        symbol="target",
        hunks=[Hunk(old_start=2, old_lines=1, new_start=2, new_lines=1, content="+x")],
    )
    callers = build_context(unit, tmp_path)["callers"]
    assert "app.py:5" in callers


# --- provider ---------------------------------------------------------------


def test_strict_schema_is_accepted_by_openai():
    """Reverting: schema declared strict:true without additionalProperties:false,
    a full required list, or anyOf-to-type-array rewriting — OpenAI answers 400."""
    strict = to_strict_json_schema(schema_to_json_schema(LLMReview))
    dumped = json.dumps(strict)
    assert "anyOf" not in dumped

    def walk(node, out):
        if isinstance(node, dict):
            if "properties" in node:
                out.append(node)
            for value in node.values():
                walk(value, out)
        elif isinstance(node, list):
            for value in node:
                walk(value, out)
        return out

    for obj in walk(strict, []):
        assert obj["additionalProperties"] is False
        assert sorted(obj["required"]) == sorted(obj["properties"])


@pytest.mark.asyncio
async def test_parse_stats_count_calls_not_attempts(monkeypatch):
    """Reverting: a call that succeeded after one repair was reported as
    success=0/failed=1, so the 'parse success' gate looked broken."""
    provider = OpenAICompatProvider("http://x", "")
    calls = {"n": 0}

    async def fake_post(payload):
        calls["n"] += 1
        content = "not json" if calls["n"] == 1 else json.dumps({"summary": "ok", "findings": []})
        return {
            "choices": [{"message": {"content": content}, "finish_reason": "stop"}],
            "usage": {},
        }

    monkeypatch.setattr(provider, "_post", fake_post)
    await provider.complete("m", [{"role": "user", "content": "x"}], schema=LLMReview)

    assert provider.parse_stats["calls"] == 1
    assert provider.parse_stats["failed"] == 0
    assert provider.success_rate == 1.0


@pytest.mark.asyncio
async def test_schema_rejection_falls_back_to_json_object(monkeypatch):
    """Reverting: endpoints that reject json_schema failed the whole call."""
    provider = OpenAICompatProvider("http://x", "")
    modes: list[str | None] = []

    async def fake_post(payload):
        modes.append(payload.get("response_format", {}).get("type"))
        if payload.get("response_format", {}).get("type") == "json_schema":
            raise httpx.HTTPStatusError(
                "bad request",
                request=httpx.Request("POST", "http://x"),
                response=httpx.Response(400, request=httpx.Request("POST", "http://x")),
            )
        return {
            "choices": [
                {
                    "message": {"content": json.dumps({"summary": "ok", "findings": []})},
                    "finish_reason": "stop",
                }
            ],
            "usage": {},
        }

    monkeypatch.setattr(provider, "_post", fake_post)
    completion = await provider.complete("m", [{"role": "user", "content": "x"}], schema=LLMReview)
    assert isinstance(completion.parsed, LLMReview)
    assert modes == ["json_schema", "json_object"]


# --- store / report ---------------------------------------------------------


def _finding(index: int, severity: str = "P2", agreement: str = "unique") -> Finding:
    snippet = f"x{index} = 1\n"
    return Finding(
        id=f"{index:016d}",
        unit_id="u1",
        lens=Lens.CORRECTNESS,
        category=DefectCategory.BOUNDARY,
        severity=severity,
        claim=f"问题{index}",
        detail="d",
        anchors=[
            Anchor(
                file="a.py",
                start_line=index,
                end_line=index,
                snippet=snippet,
                snippet_hash=snippet_hash(snippet),
            )
        ],
        agreement=agreement,
        source_models=["m1"],
        suggestion=Suggestion(diff=None, explanation="e", acceptance="pytest -q"),
    )


def test_rerun_does_not_reset_human_verdict(tmp_path):
    """Reverting: INSERT OR REPLACE reset status to 'open' and erased the
    acceptance rate whenever the same code was reviewed again."""
    store = ReviewStore(tmp_path / "f.db")
    first = FindingCollection(
        run_id="run1", repo=".", commit="c1", model_id="m", spec_level="L1", findings=[_finding(1)]
    )
    store.save_run(first, "t1")
    store.set_status(first.findings[0].id, "accepted")

    second = FindingCollection(
        run_id="run2", repo=".", commit="c2", model_id="m", spec_level="L1", findings=[_finding(1)]
    )
    store.save_run(second, "t2")

    row = store.find_finding(first.findings[0].id)[0]
    assert row["status"] == "accepted"
    assert row["run_id"] == "run2"
    assert store.stats()["acceptance_rate"] == 1.0


def test_report_counts_match_rendered_items():
    """Reverting: the report rendered 13 items while the summary said 10 and the
    execution table had 10 rows — the executing AI saw an inconsistent work order."""
    findings = [_finding(i) for i in range(1, 14)]
    collection = FindingCollection(
        run_id="r",
        repo=".",
        commit="abcdef12",
        model_id="m1,m2",
        spec_level="L1",
        mode="dual",
        reviewer_models=["m1", "m2"],
        findings=findings,
    )
    md = render_markdown(collection, "ts")
    rendered = md.count("## F-")
    assert rendered == 10
    assert md.count("| 待处理 |") == rendered
    assert "还有 3 条发现未在本工单展开" in md
    assert "| 合计（本工单） | 10 |" in md


def test_rank_prefers_evidence_backed_findings():
    """Reverting: sorting by (severity, id) put a hash order first, discarding
    the evidence weighting computed by the aggregator."""
    plain = _finding(7, severity="P2", agreement="unverified")
    backed = _finding(8, severity="P2", agreement="unverified")
    backed.evidence = [Evidence(kind="static_diag", summary="ruff:E1", strength="hard")]
    collection = FindingCollection(
        run_id="r", repo=".", commit="c", model_id="m", spec_level="L1", findings=[plain, backed]
    )
    assert collection.ranked_findings()[0].id == backed.id


# --- static tools -----------------------------------------------------------


def test_static_diagnostics_are_cached_per_file(tmp_path, monkeypatch):
    """Reverting: ruff+mypy ran once per unit per model, so a dual run paid for
    every file twice."""
    from codeerratum.evidence import static as static_mod

    path = tmp_path / "a.py"
    path.write_text("x = 1\n", encoding="utf-8")
    calls = {"ruff": 0, "mypy": 0}

    def fake_ruff(file, cwd):
        calls["ruff"] += 1
        return []

    def fake_mypy(file, cwd):
        calls["mypy"] += 1
        return []

    monkeypatch.setattr(static_mod, "run_ruff", fake_ruff)
    monkeypatch.setattr(static_mod, "run_mypy", fake_mypy)
    static_mod._CACHE.clear()

    collect_static("a.py", tmp_path)
    collect_static("a.py", tmp_path)
    assert calls == {"ruff": 1, "mypy": 1}


# --- aggregation ------------------------------------------------------------


def test_clustering_merges_across_bucket_boundary():
    """Reverting: clustering bucketed by ``line // 3``, so two reports of the same
    issue at lines 2 and 4 landed in different buckets and never merged."""
    from codeerratum.review.aggregator import cluster_findings

    first = _finding(2, severity="P1", agreement="unique")
    second = _finding(4, severity="P1", agreement="unique")
    second.claim = first.claim  # same wording, adjacent line
    first.source_models, second.source_models = [], []
    merged, _ = cluster_findings({"m1": [first], "m2": [second]})
    assert len(merged) == 1
    assert set(merged[0].source_models) == {"m1", "m2"}
