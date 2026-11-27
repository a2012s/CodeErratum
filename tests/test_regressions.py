"""Regression tests for defects found in the code audit.

Each test pins one concrete failure mode that used to pass silently. Keep the
comment on top of each test: it states what breaks if the fix is reverted.
"""

import json
import subprocess
from pathlib import Path

import httpx
import pytest

from codeerratum import pipeline
from codeerratum.context import build_context
from codeerratum.evidence import collect_static
from codeerratum.ingest.git import DiffError, _risk_tags, get_diff, new_side_sources, split_diff
from codeerratum.models import (
    Anchor,
    Evidence,
    Finding,
    FindingCollection,
    Hunk,
    ReviewUnit,
    Suggestion,
)
from codeerratum.models.finding import DefectCategory, Lens, Verdict, snippet_hash
from codeerratum.providers.base import (
    Completion,
    Usage,
    schema_to_json_schema,
    to_strict_json_schema,
)
from codeerratum.providers.openai_compat import OpenAICompatProvider
from codeerratum.report import render_markdown
from codeerratum.review.executor import (
    build_messages,
    convert_findings,
    render_callers,
    review_unit,
)
from codeerratum.review.schema import LLMReview
from codeerratum.store import ReviewStore


def _init_repo(path: Path) -> Path:
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.io"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=path, check=True)
    return path


class _CiteProvider:
    """Always cites line 2 of a.py with the given snippet; used to test the anchor gate."""

    family = "stub"

    def __init__(self, snippet: str):
        self.snippet = snippet

    async def complete(self, model_id, messages, schema, temperature=0.0, max_tokens=2048):
        review = LLMReview.model_validate(
            {
                "summary": "stub",
                "findings": [
                    {
                        "lens": "correctness",
                        "category": "boundary",
                        "severity": "P2",
                        "claim": "空列表取首元素",
                        "detail": "items 为空时抛 IndexError",
                        "anchor": {
                            "file": "a.py",
                            "start_line": 2,
                            "end_line": 2,
                            "snippet": self.snippet,
                        },
                        "suggestion": {"explanation": "判空", "acceptance": "pytest -q"},
                    }
                ],
            }
        )
        return Completion(
            text="{}",
            parsed=review,
            usage=Usage(input_tokens=1, output_tokens=1),
            raw_request_hash="stub",
            latency_ms=1,
        )


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


def test_risk_tags_match_identifiers_not_substrings():
    """Reverting: substring matching tagged `author_name` as auth and
    `blocking_call` as a lock, so the unit budget went to unrelated code."""
    assert _risk_tags("author_name = x") == []
    assert _risk_tags("blocking_call()") == []
    assert _risk_tags("clock.now()") == []
    assert _risk_tags("threading.Lock()") == ["concurrency"]
    assert _risk_tags("acquire_lock()") == ["concurrency"]
    assert _risk_tags("verify_token(t)") == ["auth"]
    assert _risk_tags("run_migration()") == ["migration"]


def test_diff_from_unchecked_out_revision_is_skipped(tmp_path):
    """Reverting: without the new side's blob, hunks describing commit B were sliced
    against the checked-out commit A, so the model reviewed the wrong region while
    the anchor hash confirmed it."""
    repo = _init_repo(tmp_path)
    (repo / "a.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "one"], cwd=repo, check=True)
    sha1 = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()

    (repo / "a.py").write_text("def f():\n    return 2\n", encoding="utf-8")
    subprocess.run(["git", "commit", "-qam", "two"], cwd=repo, check=True)
    sha2 = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()

    diff = get_diff(f"{sha1}..{sha2}", repo)
    sources = new_side_sources(f"{sha1}..{sha2}", repo)
    assert sources["a.py"] == "def f():\n    return 2\n"

    subprocess.run(["git", "checkout", "-q", sha1], cwd=repo, check=True)

    # With the new side's blob, B is reviewable while A is checked out.
    notices: list[str] = []
    units = split_diff(diff, repo, notices=notices, sources=sources)
    assert [u.symbol for u in units] == ["f"]
    assert notices == []

    # Without it, the working tree is used and the mismatch is refused rather than
    # reviewed at the wrong offsets.
    notices = []
    assert split_diff(diff, repo, notices=notices) == []
    assert any("a.py" in n and "diff revision" in n for n in notices)


@pytest.mark.asyncio
async def test_anchor_is_verified_against_the_diff_revision(tmp_path):
    """Reverting: anchors were checked against the working tree, so a model citing
    the checked-out revision passed while the finding described the diff revision."""
    repo = _init_repo(tmp_path)
    (repo / "a.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "one"], cwd=repo, check=True)
    sha1 = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()

    (repo / "a.py").write_text("def f():\n    return 2\n", encoding="utf-8")
    subprocess.run(["git", "commit", "-qam", "two"], cwd=repo, check=True)
    sha2 = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()

    diff = get_diff(f"{sha1}..{sha2}", repo)
    sources = new_side_sources(f"{sha1}..{sha2}", repo)
    subprocess.run(["git", "checkout", "-q", sha1], cwd=repo, check=True)
    unit = split_diff(diff, repo, sources=sources)[0]

    findings, rejected, _completion, _diags = await review_unit(
        _CiteProvider("    return 2\n"), "stub", unit, repo, "run", sources=sources
    )
    assert [f.anchors[0].snippet for f in findings] == ["    return 2\n"]
    assert rejected == []

    # The same citation, but with the checked-out revision's text, must fail.
    findings, rejected, _completion, _diags = await review_unit(
        _CiteProvider("    return 1\n"), "stub", unit, repo, "run", sources=sources
    )
    assert findings == []
    assert [r["reason"] for r in rejected] == ["anchor_mismatch"]


def test_diff_spec_cannot_be_parsed_as_a_git_option(tmp_path):
    """Reverting: `--diff '--output=/path HEAD~1'` made git write the diff to a
    caller-chosen file and return empty stdout — indistinguishable from "no changes",
    with an arbitrary file written as a side effect."""
    repo = _init_repo(tmp_path)
    (repo / "a.py").write_text("x = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "one"], cwd=repo, check=True)
    (repo / "a.py").write_text("x = 2\n", encoding="utf-8")
    subprocess.run(["git", "commit", "-qam", "two"], cwd=repo, check=True)

    escaped = tmp_path / "pwned.txt"
    with pytest.raises(DiffError, match="option"):
        get_diff(f"--output={escaped} HEAD~1", repo)
    assert not escaped.exists()


def test_diff_spec_with_pathspec_still_works(tmp_path):
    """Reverting: inserting --end-of-options must not break `revspec -- paths`,
    which is the documented way to review one directory without staging."""
    repo = _init_repo(tmp_path)
    (repo / "a.py").write_text("x = 1\n", encoding="utf-8")
    (repo / "b.py").write_text("y = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=repo, check=True)
    (repo / "a.py").write_text("x = 2\n", encoding="utf-8")
    (repo / "b.py").write_text("y = 2\n", encoding="utf-8")

    diff = get_diff("HEAD", repo, paths="a.py")
    assert "a.py" in diff
    assert "b.py" not in diff


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


def test_caller_fence_adapts_to_backticks_in_snippet():
    """A fixed ``` fence closes early on a docstring containing ```, leaving the
    rest of the caller source as free-form text in the review prompt."""
    snippet = '"""doc\n```\nstill source\n"""'
    rendered = render_callers(["app.py:5"], [{"file": "app.py", "line": 5, "snippet": snippet}])
    assert "````python\n" in rendered  # opening fence is four ticks, not three
    assert rendered.rstrip().endswith("````")  # and it closes with four
    assert "still source" in rendered  # snippet body stays inside the block


def test_main_excerpt_fence_adapts_to_backticks_in_source(tmp_path):
    """The reviewed source goes into the prompt inside a fence too. Today every
    excerpt line is numbered, so no line can start with ``` — but the fence must
    not depend on that formatting detail staying true."""
    unit = _unit(primary_file="app.py", symbol="fn_0", language="python")
    source = ["def fn_0():", '    """doc', "    ```", "    still source"]
    messages = build_messages(unit, source, enclosing_span=(1, 4))
    user = messages[1]["content"]
    assert "````python\n" in user  # opening fence is four ticks, not three
    assert "````" in user.split("\n\n", 1)[1]  # and it closes with four
    assert "still source" in user


def test_caller_backslash_paths_are_not_listed_twice():
    """callers use Path.relative_to() (backslash on Windows) while details use
    "/"; unnormalised comparison defeats the "max 5 callers" limit."""
    details = [{"file": "pkg/app.py", "line": 5, "snippet": "target(1)"}]
    rendered = render_callers([r"pkg\app.py:5", "other.py:9"], details)
    assert "Also referenced at:" in rendered
    assert r"pkg\app.py:5" not in rendered
    assert "other.py:9" in rendered


def test_caller_snippets_show_the_call_expression(tmp_path):
    """Reverting: the prompt listed ``app.py:5`` only, so a signature or
    return-value change was reviewed without the line that consumes it."""
    (tmp_path / "lib.py").write_text("def target(x):\n    return x + 1\n", encoding="utf-8")
    (tmp_path / "app.py").write_text(
        "from lib import target\n\n\ndef main():\n    return target(1)\n", encoding="utf-8"
    )
    unit = _unit(
        primary_file="lib.py",
        symbol="target",
        hunks=[
            Hunk(
                old_start=2,
                old_lines=1,
                new_start=2,
                new_lines=1,
                content="+    return x + 1",
            )
        ],
    )
    source_lines = (tmp_path / "lib.py").read_text(encoding="utf-8").splitlines(keepends=True)

    ctx = build_context(unit, tmp_path)
    details = ctx["caller_snippets"]
    assert details and details[0]["file"] == "app.py" and details[0]["line"] == 5
    assert "return target(1)" in details[0]["snippet"]

    messages = build_messages(
        unit,
        source_lines,
        enclosing_span=ctx["enclosing_range"],
        callers=ctx["callers"],
        caller_details=details,
    )
    user = messages[1]["content"]
    assert "## app.py:5" in user
    assert "return target(1)" in user
    assert "never" in user and "context" in user


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


def test_profile_counts_once_and_credits_only_asserting_models(tmp_path):
    """Reverting: save_comparison had a second profile writer, so every assertion
    was counted twice (n_assert doubled on every dual run) and the falsifier's
    ``confirm`` was credited to the falsifier, handing a model that never reports
    anything a perfect precision — the exact inversion 0.4.1 set out to fix."""
    store = ReviewStore(tmp_path / "f.db")
    finding = _finding(1)
    finding.source_models = ["gpt-x"]
    finding.verdicts = [
        Verdict(model_id="gpt-x", role="assert", reason="r"),
        Verdict(model_id="claude-y", role="confirm", reason="ok"),
    ]
    collection = FindingCollection(
        run_id="run1",
        repo=".",
        commit="c",
        model_id="gpt-x,claude-y",
        spec_level="L1",
        mode="dual",
        reviewer_models=["gpt-x", "claude-y"],
        falsifier_model="claude-y",
        comparison={
            "mode": "dual",
            "raw_counts": {"gpt-x": 1},
            "after_clustering": 1,
            "after_falsification": 1,
            "zones": {"unique": 1},
        },
        findings=[finding],
    )
    store.save_run(collection, "t1")
    store.save_comparison(collection, "t1")
    store.save_run(collection, "t1")  # a retried save must be idempotent

    profiles = store.model_profile_map()
    assert profiles["gpt-x"]["n_assert"] == 1
    assert profiles["gpt-x"]["n_unique"] == 1
    assert profiles["gpt-x"]["n_confirmed"] == 1
    assert "claude-y" not in profiles, "a falsifier must not gain a reviewer profile"


def test_quick_parse_only_comparison_is_not_written_as_a_cross_model_row(tmp_path):
    """Reverting: `codeerratum compare` listed every quick run with raw={} and
    after_clustering=0, because its parse snapshot was stored as comparison data."""
    store = ReviewStore(tmp_path / "f.db")
    quick = FindingCollection(
        run_id="quick1",
        repo=".",
        commit="c",
        model_id="m",
        spec_level="L1",
        comparison={"parse": {"calls": 2}, "parse_failures": []},
    )
    store.save_comparison(quick, "t1")
    assert store.comparison_history() == []

    tiered = FindingCollection(
        run_id="tiered1",
        repo=".",
        commit="c",
        model_id="m",
        spec_level="L1",
        comparison={"mode": "tiered", "parse": {"calls": 2}, "parse_failures": []},
    )
    store.save_comparison(tiered, "t2")
    assert [row["run_id"] for row in store.comparison_history()] == ["tiered1"]


def test_pipeline_reads_the_diff_revisions_blobs(tmp_path, monkeypatch):
    """Reverting: execute_review passed no sources to split_diff / run_review, so a
    committed range was reviewed against whatever happened to be checked out."""
    repo = _init_repo(tmp_path)
    (repo / "a.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "one"], cwd=repo, check=True)
    sha1 = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()

    (repo / "a.py").write_text("def f():\n    return 2\n", encoding="utf-8")
    subprocess.run(["git", "commit", "-qam", "two"], cwd=repo, check=True)
    sha2 = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()
    subprocess.run(["git", "checkout", "-q", sha1], cwd=repo, check=True)

    monkeypatch.setenv("CODEERRATUM_BASE_URL", "http://x")
    monkeypatch.setenv("CODEERRATUM_MODEL", "m")
    monkeypatch.delenv("CODEERRATUM_MODELS", raising=False)
    captured: dict = {}

    async def fake_run(
        registry,
        mode,
        units,
        repo_root,
        run_id,
        author_model=None,
        rejected_category_counts=None,
        house_rules="",
        sources=None,
        progress=None,
        should_stop=None,
        pinned=None,
        on_findings=None,
        output_lang="",
        tiered=False,
        on_checkpoint=None,
        unit_cache=None,
        token_budget=None,
    ):
        captured["sources"] = sources
        return (
            FindingCollection(
                run_id=run_id, repo=str(repo_root), commit="", model_id="m", spec_level="L1"
            ),
            [],
            [],
        )

    monkeypatch.setattr(pipeline, "run_review", fake_run)
    outcome = pipeline.execute_review(repo, diff_spec=f"{sha1}..{sha2}")
    assert captured["sources"]["a.py"] == "def f():\n    return 2\n"
    assert outcome.units_selected == 1


def test_rejected_history_reaches_the_ranker(tmp_path, monkeypatch):
    """Reverting: rank_findings accepted a rejected-category map that the pipeline
    never passed, so the false positives in feedback.db never influenced order."""
    repo = _init_repo(tmp_path)
    (repo / "a.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "one"], cwd=repo, check=True)
    (repo / "a.py").write_text("def f():\n    return 2\n", encoding="utf-8")

    seeded = _finding(1)
    store = pipeline.store_for(repo)
    store.save_run(
        FindingCollection(
            run_id="old",
            repo=str(repo),
            commit="c",
            model_id="m",
            spec_level="L1",
            findings=[seeded],
        ),
        "t0",
    )
    store.set_status(seeded.id, "rejected", "误报")

    monkeypatch.setenv("CODEERRATUM_BASE_URL", "http://x")
    monkeypatch.setenv("CODEERRATUM_MODEL", "m")
    monkeypatch.delenv("CODEERRATUM_MODELS", raising=False)
    captured: dict = {}

    async def fake_run(
        registry,
        mode,
        units,
        repo_root,
        run_id,
        author_model=None,
        rejected_category_counts=None,
        house_rules="",
        sources=None,
        progress=None,
        should_stop=None,
        pinned=None,
        on_findings=None,
        output_lang="",
        tiered=False,
        on_checkpoint=None,
        unit_cache=None,
        token_budget=None,
    ):
        captured["counts"] = rejected_category_counts
        return (
            FindingCollection(
                run_id=run_id, repo=str(repo_root), commit="", model_id="m", spec_level="L1"
            ),
            [],
            [],
        )

    monkeypatch.setattr(pipeline, "run_review", fake_run)
    outcome = pipeline.execute_review(repo, diff_spec="HEAD")
    assert captured["counts"] == {"boundary": 1}
    assert outcome.units_selected == 1


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
    assert "还有 3 条建议项未在本工单展开" in md
    assert "| 合计（本工单） | 10 |" in md


def test_report_neutralizes_newlines_and_backticks_from_models():
    """Reverting: a claim containing a newline injected a second heading, and a
    docstring containing a line of ``` closed the snippet fence early, so the
    rest of the snippet (and the following sections) rendered as free-form text."""
    snippet = 'DOC = """\nexample\n```\n"""\n'
    finding = Finding(
        id="f" * 16,
        unit_id="u1",
        lens=Lens.CORRECTNESS,
        category=DefectCategory.BOUNDARY,
        severity="P2",
        claim="结论\n## 伪造章节",
        detail="细节\n```\n注入",
        anchors=[
            Anchor(
                file="a.py",
                start_line=1,
                end_line=4,
                snippet=snippet,
                snippet_hash=snippet_hash(snippet),
            )
        ],
        suggestion=Suggestion(explanation="说明", acceptance="pytest -q"),
    )
    collection = FindingCollection(
        run_id="r", repo=".", commit="c", model_id="m", spec_level="L1", findings=[finding]
    )
    md = render_markdown(collection, "ts")

    assert "\n## 伪造章节" not in md
    assert "结论 ## 伪造章节" in md
    assert "````python" in md, "fence must be longer than the ``` inside the snippet"
    assert "example\n```\n" in md, "snippet content must stay literal"
    assert "\n```\n注入" not in md, "detail prose must not open a code fence"
    assert md.count("\n## 摘要") == 1


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


def test_clustering_merges_one_issue_across_categories():
    """Reverting: category was part of the cluster key, so the same defect labeled
    ``boundary`` by one model and ``off_by_one`` by another never met and both
    survived into the unique zone."""
    from codeerratum.models.finding import DefectCategory
    from codeerratum.review.aggregator import cluster_findings

    first = _finding(3)
    second = _finding(3)
    second.category = DefectCategory.OFF_BY_ONE
    first.source_models, second.source_models = [], []
    merged, _ = cluster_findings({"m1": [first], "m2": [second]})
    assert len(merged) == 1
    assert set(merged[0].source_models) == {"m1", "m2"}


def test_clustering_keeps_distinct_same_line_defects_with_shared_wording():
    """Reverting the strict cross-category threshold silently deletes a defect.

    Two models can describe different bugs on the same line with near-identical
    prefixes ("返回值未…", "循环未…"). At jaccard 0.35-0.43 the old rule merged
    them and one bug vanished from the work order.
    """
    from codeerratum.review.aggregator import cluster_findings

    first = _finding(10)
    first.claim = "返回值未判空导致崩溃"
    first.category = DefectCategory.NULL_DEREF
    second = _finding(10)
    second.claim = "返回值未校验导致注入"
    second.category = DefectCategory.INJECTION
    first.source_models, second.source_models = [], []
    merged, _ = cluster_findings({"m1": [first], "m2": [second]})
    assert len(merged) == 2


def test_clustering_still_merges_same_defect_across_categories():
    """The strict threshold must not undo cross-category merging of one issue."""
    from codeerratum.review.aggregator import cluster_findings

    first = _finding(3)
    second = _finding(3)
    second.category = DefectCategory.OFF_BY_ONE
    first.source_models, second.source_models = [], []
    merged, _ = cluster_findings({"m1": [first], "m2": [second]})
    assert len(merged) == 1


def test_clustering_keeps_two_distinct_issues_on_the_same_line():
    """Reverting ``gap == 0 -> True`` matters most here: two unrelated bugs on one
    line used to be merged, silently deleting one from the work order."""
    from codeerratum.models.finding import DefectCategory
    from codeerratum.review.aggregator import cluster_findings

    first = _finding(3)
    second = _finding(3)
    second.category = DefectCategory.SEMANTIC_DRIFT
    second.claim = "返回类型不一致"
    first.source_models, second.source_models = [], []
    merged, _ = cluster_findings({"m1": [first], "m2": [second]})
    assert len(merged) == 2


def test_review_with_records_a_finished_unit_before_stop(monkeypatch):
    """Reverting the sink means cancellation throws away the only paid work."""
    import asyncio

    import codeerratum.review.orchestrator as orch
    from codeerratum.models import ModelSpec
    from codeerratum.review import ReviewCancelled

    finding = _finding(1)

    async def fake_review_unit(*_args, **_kwargs):
        return (
            [finding],
            [],
            Completion(
                text="{}",
                usage=Usage(input_tokens=1, output_tokens=1),
                raw_request_hash="stub",
                latency_ms=1,
            ),
            [],
        )

    monkeypatch.setenv("CODEERRATUM_UNIT_CONCURRENCY", "1")
    monkeypatch.setattr(orch, "review_unit", fake_review_unit)
    partial = orch._PartialRun()
    seen: list[str] = []

    def progress(message: str) -> None:
        seen.append(message)
        if len(seen) == 2:  # unit_start + unit_done for the first unit
            raise ReviewCancelled("stop")

    with pytest.raises(ReviewCancelled):
        asyncio.run(
            orch._review_with(
                object(),
                ModelSpec(model_id="m", base_url="http://x"),
                [_unit(), _unit(unit_id="u2", primary_file="b.py")],
                Path("."),
                "run",
                progress=progress,
                sink=partial,
            )
        )
    assert partial.units_done == {"u1"}
    assert partial.calls[0]["unit_id"] == "u1"
    assert [f.id for f in partial.per_unit["m"]["u1"]] == [finding.id]


def test_run_review_returns_partial_collection_when_one_unit_finished(monkeypatch, tmp_path):
    """A stop after a completed unit must keep that unit's findings."""
    import asyncio

    import codeerratum.review.orchestrator as orch
    from codeerratum.models import ModelSpec

    class Registry:
        def route(self, mode, author_model=None, pinned=None):
            return [ModelSpec(model_id="m", base_url="http://x")], None

    finding = _finding(1)

    async def fake_review_with(
        provider,
        spec,
        units,
        repo_root,
        run_id,
        house_rules="",
        sources=None,
        progress=None,
        on_findings=None,
        output_lang="",
        sink=None,
        on_unit_complete=None,
        cache=None,
    ):
        assert sink is not None
        sink.per_unit.setdefault(spec.model_id, {}).setdefault(units[0].unit_id, []).append(finding)
        sink.calls.append({"model": spec.model_id, "unit_id": units[0].unit_id, "usage": {}})
        sink.units_done.add(units[0].unit_id)
        raise asyncio.CancelledError()

    monkeypatch.setattr(orch, "_make_provider", lambda _spec: object())
    monkeypatch.setattr(orch, "_review_with", fake_review_with)
    collection, calls, _diags = asyncio.run(
        orch.run_review(Registry(), "quick", [_unit()], tmp_path, "run")
    )
    assert collection.stopped is True
    assert [f.id for f in collection.findings] == [finding.id]
    assert collection.comparison["stopped"]["units_completed"] == 1
    assert collection.comparison["stopped"]["units_total"] == 1
    assert calls[0]["unit_id"] == "u1"


def test_run_review_still_raises_when_stopped_before_any_unit(monkeypatch, tmp_path):
    """No completed unit means there is no partial report worth writing."""
    import asyncio

    import codeerratum.review.orchestrator as orch
    from codeerratum.models import ModelSpec
    from codeerratum.review import ReviewCancelled

    class Registry:
        def route(self, mode, author_model=None, pinned=None):
            return [ModelSpec(model_id="m", base_url="http://x")], None

    async def fake_review_with(*_args, **_kwargs):
        raise asyncio.CancelledError()

    monkeypatch.setattr(orch, "_make_provider", lambda _spec: object())
    monkeypatch.setattr(orch, "_review_with", fake_review_with)
    with pytest.raises(ReviewCancelled):
        asyncio.run(orch.run_review(Registry(), "quick", [_unit()], tmp_path, "run"))


def test_stopped_pipeline_writes_partial_report_and_skips_profiles(tmp_path, monkeypatch):
    """Stopping must persist what was found, without teaching routing from it."""
    repo = _init_repo(tmp_path)
    (repo / "a.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "one"], cwd=repo, check=True)
    (repo / "a.py").write_text("def f():\n    return 2\n", encoding="utf-8")

    monkeypatch.setenv("CODEERRATUM_BASE_URL", "http://x")
    monkeypatch.setenv("CODEERRATUM_MODEL", "m")
    monkeypatch.delenv("CODEERRATUM_MODELS", raising=False)
    finding = _finding(1)
    partial = FindingCollection(
        run_id="partial",
        repo=str(repo),
        commit="",
        model_id="m",
        spec_level="L1",
        mode="quick",
        reviewer_models=["m"],
        findings=[finding],
        stopped=True,
        comparison={
            "parse": {},
            "parse_failures": [],
            "stopped": {
                "stage": "review",
                "units_completed": 1,
                "units_partial": 0,
                "units_total": 3,
                "calls": 1,
            },
        },
    )

    async def fake_run(*_args, **_kwargs):
        return partial, [], []

    monkeypatch.setattr(pipeline, "run_review", fake_run)
    outcome = pipeline.execute_review(repo, diff_spec="HEAD")
    markdown = (outcome.run_dir / "review.md").read_text(encoding="utf-8")
    assert "审查已停止" in markdown
    assert "已完整审查 1/3 个单元" in markdown
    assert "None 条" not in markdown
    data = json.loads((outcome.run_dir / "findings.json").read_text(encoding="utf-8"))
    assert data["stopped"] is True
    assert pipeline.store_for(repo).model_profiles() == []
