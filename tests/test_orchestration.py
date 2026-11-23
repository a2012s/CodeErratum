"""End-to-end phase-3 orchestration with stubbed heterogeneous providers.

Proves: two models review in parallel -> findings clustered -> a different-vendor
model falsifies -> zones split -> comparison data produced. No API key needed.
"""

from __future__ import annotations

import json
import subprocess

import pytest

from codeerratum.models import ModelRegistry, ModelSpec
from codeerratum.providers.base import Completion, Usage
from codeerratum.review import LLMReview, run_review
from codeerratum.review import orchestrator as orch
from codeerratum.review.falsification import LLMFalsify as FalsifySchema
from codeerratum.review.schema import LLMAnchor, LLMFinding, LLMSuggestion


class ReviewStub:
    """Returns a finding citing a real line. Behavior depends on model_role."""

    family = "stub"

    def __init__(self, model_id: str, family: str, real_line: int, snippet: str, claim: str):
        self.model_id = model_id
        self.family = family
        self.real_line = real_line
        self.snippet = snippet
        self.claim = claim
        self.parse_stats = {"success": 0, "retry": 0, "failed": 0}

    def dump_failures(self, path):  # noqa: ANN001
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("[]", encoding="utf-8")

    async def complete(self, model_id, messages, schema, temperature=0.0, max_tokens=2048):
        if schema is FalsifySchema:
            # Falsifier refutes the second finding, confirms the first.
            result = FalsifySchema(
                verdict="refuted", reason="有判空保护", counterexample="x=[] 时有 guard"
            )
            return Completion(
                text=json.dumps(result.model_dump(mode="json")),
                parsed=result,
                usage=Usage(input_tokens=10, output_tokens=5),
                raw_request_hash="stub-falsify",
                latency_ms=1,
            )
        # Review call.
        review = LLMReview(
            summary="stub",
            findings=[
                LLMFinding(
                    lens="correctness",
                    category="boundary",
                    severity="P1",
                    claim=self.claim,
                    detail="当 x 为空时抛 IndexError。",
                    anchor=LLMAnchor(
                        file="m.py",
                        start_line=self.real_line,
                        end_line=self.real_line,
                        snippet=self.snippet,
                    ),
                    suggestion=LLMSuggestion(explanation="判空", acceptance="pytest -q"),
                )
            ],
        )
        return Completion(
            text=json.dumps(review.model_dump(mode="json")),
            parsed=review,
            usage=Usage(input_tokens=100, output_tokens=50),
            raw_request_hash="stub-review",
            latency_ms=1,
        )


@pytest.fixture
def repo(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.io"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path, check=True)
    (tmp_path / "m.py").write_text("def foo():\n    return 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=tmp_path, check=True)
    (tmp_path / "m.py").write_text("def foo():\n    x = []\n    return x[0]\n", encoding="utf-8")
    return tmp_path


@pytest.mark.asyncio
async def test_dual_orchestration_end_to_end(repo, monkeypatch):
    from codeerratum.ingest import get_diff, split_diff

    diff = get_diff("HEAD", repo)
    units = split_diff(diff, repo)
    assert units

    source_lines = (repo / "m.py").read_text().splitlines(keepends=True)
    real_line = next(i for i, line in enumerate(source_lines, 1) if "return x[0]" in line)
    snippet = source_lines[real_line - 1]

    registry = ModelRegistry(
        models=[
            ModelSpec(model_id="gpt-4o-x", base_url="http://a", family="openai"),
            ModelSpec(model_id="claude-3-x", base_url="http://b", family="anthropic"),
            ModelSpec(model_id="deepseek-chat", base_url="http://c", family="deepseek"),
        ]
    )

    # Both reviewers report the SAME issue with the same wording -> clustered into
    # one consensus finding; falsifier (deepseek, 3rd vendor) refutes it -> conflict.
    stubs = {}
    for mid, fam, claim in [
        ("gpt-4o-x", "openai", "空列表取首元素崩溃"),
        ("claude-3-x", "anthropic", "空列表取首元素崩溃"),
        ("deepseek-chat", "deepseek", "空列表取首元素崩溃"),
    ]:
        stubs[mid] = ReviewStub(mid, fam, real_line, snippet, claim)

    monkeypatch.setattr(orch, "_make_provider", lambda spec: stubs[spec.model_id])

    collection, calls, diags = await run_review(
        registry, "dual", units, repo, "run-phase3", author_model=None
    )

    assert collection.mode == "dual"
    assert len(collection.reviewer_models) == 2
    assert collection.falsifier_model == "deepseek-chat"
    # Two identical findings clustered into one, then falsifier refuted -> conflict.
    assert len(collection.findings) == 1
    f = collection.findings[0]
    assert set(f.source_models) == {"gpt-4o-x", "claude-3-x"}
    assert f.agreement == "conflict"
    assert any(v.role == "refute" and v.model_id == "deepseek-chat" for v in f.verdicts)
    assert collection.comparison["raw_counts"] == {"gpt-4o-x": 1, "claude-3-x": 1}
    assert collection.comparison["after_clustering"] == 1
    # A falsify call was recorded from the third vendor.
    assert any(c.get("role") == "falsify" for c in calls)


@pytest.mark.asyncio
async def test_parse_health_is_rolled_up_for_the_run(repo, monkeypatch):
    """A hard gate that only lives in memory cannot be audited after a run."""
    from codeerratum.ingest import get_diff, split_diff

    diff = get_diff("HEAD", repo)
    units = split_diff(diff, repo)
    source_lines = (repo / "m.py").read_text().splitlines(keepends=True)
    real_line = next(i for i, line in enumerate(source_lines, 1) if "return x[0]" in line)
    snippet = source_lines[real_line - 1]
    registry = ModelRegistry(
        models=[
            ModelSpec(model_id="gpt-4o-x", base_url="http://a"),
            ModelSpec(model_id="claude-3-x", base_url="http://b"),
            ModelSpec(model_id="deepseek-chat", base_url="http://c"),
        ]
    )
    stubs = {}
    for mid in ("gpt-4o-x", "claude-3-x", "deepseek-chat"):
        stub = ReviewStub(mid, "stub", real_line, snippet, "空列表取首元素崩溃")
        stub.parse_stats = {"calls": 4, "first_try": 3, "repaired": 1, "failed": 0}
        stub.failure_log = []
        stubs[mid] = stub
    monkeypatch.setattr(orch, "_make_provider", lambda spec: stubs[spec.model_id])

    collection, _, _ = await run_review(registry, "dual", units, repo, "run-parse")
    parse = collection.comparison["parse"]
    assert parse["calls"] == 12  # 2 reviewers + 1 falsifier
    assert parse["first_try"] == 9
    assert parse["repaired"] == 3
    assert parse["success_rate"] == 1.0
    assert collection.comparison["parse_failures"] == []


@pytest.mark.asyncio
async def test_quick_parse_health_is_rolled_up_before_the_early_return(repo, monkeypatch):
    """Plain quick returns before the multi-model block; its parser health still counts."""
    from codeerratum.ingest import get_diff, split_diff

    units = split_diff(get_diff("HEAD", repo), repo)
    source_lines = (repo / "m.py").read_text().splitlines(keepends=True)
    real_line = next(i for i, line in enumerate(source_lines, 1) if "return x[0]" in line)
    snippet = source_lines[real_line - 1]
    registry = ModelRegistry(models=[ModelSpec(model_id="mini-scanner", base_url="http://a")])
    stub = ReviewStub("mini-scanner", "stub", real_line, snippet, "空列表取首元素崩溃")
    stub.parse_stats = {"calls": 4, "first_try": 3, "repaired": 1, "failed": 0}
    stub.failure_log = []
    monkeypatch.setattr(orch, "_make_provider", lambda spec: stub)

    collection, _, _ = await run_review(registry, "quick", units, repo, "run-quick-parse")

    parse = collection.comparison["parse"]
    assert collection.mode == "quick"
    assert parse["calls"] == 4
    assert parse["first_try"] == 3
    assert parse["repaired"] == 1
    assert parse["success_rate"] == 1.0
    assert collection.comparison["parse_failures"] == []


@pytest.mark.asyncio
async def test_tiered_parse_health_combines_scanner_and_verifier(repo, monkeypatch):
    """Both calls in a tiered run belong to one run-level parse snapshot."""
    from codeerratum.ingest import get_diff, split_diff

    units = split_diff(get_diff("HEAD", repo), repo)
    source_lines = (repo / "m.py").read_text().splitlines(keepends=True)
    real_line = next(i for i, line in enumerate(source_lines, 1) if "return x[0]" in line)
    snippet = source_lines[real_line - 1]
    registry = ModelRegistry(
        models=[
            ModelSpec(model_id="mini-scanner", base_url="http://a"),
            ModelSpec(model_id="strong-verifier", base_url="http://b"),
        ]
    )
    scanner = ReviewStub("mini-scanner", "stub", real_line, snippet, "空列表取首元素崩溃")
    scanner.parse_stats = {"calls": 5, "first_try": 4, "repaired": 1, "failed": 0}
    scanner.failure_log = []
    verifier = ReviewStub("strong-verifier", "stub", real_line, snippet, "空列表取首元素崩溃")
    verifier.parse_stats = {"calls": 2, "first_try": 1, "repaired": 0, "failed": 1}
    verifier.failure_log = [{"model": "strong-verifier", "stage": "review", "error": "bad json"}]
    stubs = {"mini-scanner": scanner, "strong-verifier": verifier}
    monkeypatch.setattr(orch, "_make_provider", lambda spec: stubs[spec.model_id])

    collection, calls, _ = await run_review(
        registry,
        "quick",
        units,
        repo,
        "run-tiered-parse",
        pinned=["strong-verifier"],
        tiered=True,
    )

    comparison = collection.comparison
    assert comparison["mode"] == "tiered"
    assert comparison["scanner"] == "mini-scanner"
    assert comparison["verifier"] == "strong-verifier"
    assert any(c.get("role") == "verify" for c in calls)
    parse = comparison["parse"]
    assert parse["calls"] == 7
    assert parse["first_try"] == 5
    assert parse["repaired"] == 1
    assert parse["failed"] == 1
    assert parse["success_rate"] == 0.857
    assert comparison["parse_failures"] == [
        {"model": "strong-verifier", "stage": "review", "error": "bad json"}
    ]


@pytest.mark.asyncio
async def test_live_findings_callback_fires_per_unit_before_the_run_ends(repo, monkeypatch):
    """Findings must be observable while reviewing, not only in the final report.

    The UI streams them one by one; that only works if the orchestrator reports
    them per unit instead of returning them in a batch at the end.
    """
    from codeerratum.ingest import get_diff, split_diff

    units = split_diff(get_diff("HEAD", repo), repo)
    source_lines = (repo / "m.py").read_text().splitlines(keepends=True)
    real_line = next(i for i, line in enumerate(source_lines, 1) if "return x[0]" in line)
    snippet = source_lines[real_line - 1]

    registry = ModelRegistry(
        models=[
            ModelSpec(model_id="gpt-4o-x", base_url="http://a", family="openai"),
            ModelSpec(model_id="claude-3-x", base_url="http://b", family="anthropic"),
            ModelSpec(model_id="deepseek-chat", base_url="http://c", family="deepseek"),
        ]
    )
    stubs = {
        mid: ReviewStub(mid, fam, real_line, snippet, "空列表取首元素崩溃")
        for mid, fam in [
            ("gpt-4o-x", "openai"),
            ("claude-3-x", "anthropic"),
            ("deepseek-chat", "deepseek"),
        ]
    }
    monkeypatch.setattr(orch, "_make_provider", lambda spec: stubs[spec.model_id])

    live: list[tuple[str, str, str]] = []
    order: list[str] = []

    def on_findings(unit, findings, model_id):  # noqa: ANN001
        order.append("finding")
        for f in findings:
            live.append((model_id, f.claim, f.anchors[0].file))

    collection, _calls, _diags = await run_review(
        registry,
        "dual",
        units,
        repo,
        "run-live",
        author_model=None,
        on_findings=on_findings,
    )
    order.append("done")

    assert live, "the callback must see the raw per-unit findings"
    assert {m for m, _c, _f in live} == {"gpt-4o-x", "claude-3-x"}
    assert all(claim for _m, claim, _f in live)
    assert order[0] == "finding" and order[-1] == "done"
    # The report still comes from the full collection, not from the stream.
    assert len(collection.findings) == 1


@pytest.mark.asyncio
async def test_falsify_progress_keeps_full_claim(repo, monkeypatch):
    """The SSE progress line must show the whole claim, not a 40-char prefix."""
    from codeerratum.ingest import get_diff, split_diff

    diff = get_diff("HEAD", repo)
    units = split_diff(diff, repo)
    source_lines = (repo / "m.py").read_text().splitlines(keepends=True)
    real_line = next(i for i, line in enumerate(source_lines, 1) if "return x[0]" in line)
    snippet = source_lines[real_line - 1]
    claim = "verify uses npm install despite pnpm packageManager pin"
    assert 40 < len(claim) <= 60  # regression truncated at 40; model schema caps at 60

    registry = ModelRegistry(
        models=[
            ModelSpec(model_id="gpt-4o-x", base_url="http://a", family="openai"),
            ModelSpec(model_id="claude-3-x", base_url="http://b", family="anthropic"),
            ModelSpec(model_id="deepseek-chat", base_url="http://c", family="deepseek"),
        ]
    )
    stubs = {
        mid: ReviewStub(mid, fam, real_line, snippet, claim)
        for mid, fam in [
            ("gpt-4o-x", "openai"),
            ("claude-3-x", "anthropic"),
            ("deepseek-chat", "deepseek"),
        ]
    }
    monkeypatch.setattr(orch, "_make_provider", lambda spec: stubs[spec.model_id])

    messages: list[str] = []
    await run_review(
        registry,
        "dual",
        units,
        repo,
        "run-full-claim",
        author_model=None,
        progress=messages.append,
    )

    falsify_lines = [m for m in messages if m.startswith("证伪 ")]
    assert falsify_lines
    assert any(claim in m for m in falsify_lines)


def test_timeout_failure_says_what_expired_not_just_the_class_name():
    """`单元 3/29 调用失败：ReadTimeout` answers none of the real questions."""
    import httpx

    from codeerratum.providers import OpenAICompatProvider
    from codeerratum.review.orchestrator import _failure_text

    provider = OpenAICompatProvider("http://localhost", "k", timeout=240)
    text = _failure_text(httpx.ReadTimeout("x"), provider)
    assert text != "ReadTimeout"
    assert "240" in text, "the window that expired is the actionable number"

    request = httpx.Request("POST", "http://x")
    unauthorized = httpx.Response(401, request=request)
    assert "API key" in _failure_text(
        httpx.HTTPStatusError("401", request=request, response=unauthorized), provider
    )


class ExplodingStub(ReviewStub):
    """A verifier endpoint that fails on every call (timeout, 5xx, ...)."""

    def __init__(self, model_id: str):
        super().__init__(model_id, "stub", 0, "", "")
        self.parse_stats = {"calls": 0, "first_try": 0, "repaired": 0, "failed": 0}
        self.failure_log = []

    async def complete(self, model_id, messages, schema, temperature=0.0, max_tokens=2048):
        raise RuntimeError("connection reset by peer")


@pytest.mark.asyncio
async def test_tiered_verifier_failure_becomes_a_machine_drop(repo, monkeypatch):
    """A verifier crash must show up in run_rejections, not only in calls.json.

    Previously the error was recorded solely as calls verdict=error, so
    dropped_by_reason and the report notes both under-counted — the run looked
    like the verifier simply agreed with everything.
    """
    from codeerratum.ingest import get_diff, split_diff

    units = split_diff(get_diff("HEAD", repo), repo)
    source_lines = (repo / "m.py").read_text().splitlines(keepends=True)
    real_line = next(i for i, line in enumerate(source_lines, 1) if "return x[0]" in line)
    snippet = source_lines[real_line - 1]
    registry = ModelRegistry(
        models=[
            ModelSpec(model_id="mini-scanner", base_url="http://a"),
            ModelSpec(model_id="strong-verifier", base_url="http://b"),
        ]
    )
    scanner = ReviewStub("mini-scanner", "stub", real_line, snippet, "空列表取首元素崩溃")
    scanner.parse_stats = {"calls": 1, "first_try": 1, "repaired": 0, "failed": 0}
    scanner.failure_log = []
    stubs = {
        "mini-scanner": scanner,
        "strong-verifier": ExplodingStub("strong-verifier"),
    }
    monkeypatch.setattr(orch, "_make_provider", lambda spec: stubs[spec.model_id])

    collection, calls, _ = await run_review(
        registry,
        "quick",
        units,
        repo,
        "run-verify-error",
        pinned=["strong-verifier"],
        tiered=True,
    )

    verify_errors = [r for r in collection.rejected if r.get("reason") == "verify_error"]
    assert verify_errors, "verifier crash must land in rejected"
    assert all(r["model"] == "strong-verifier" for r in verify_errors)
    assert all(r.get("unit_id") for r in verify_errors)
    # the raw error stays visible in calls.json for whoever debugs the endpoint
    assert any(
        c.get("role") == "verify" and c.get("verdict") == "error" for c in calls
    )
    # attempted vs failed: cross-review stats must not look more complete than it was
    assert collection.comparison["verify_failed_units"] == len(verify_errors)
    assert collection.comparison["verified_units"] >= len(verify_errors)
    # the scanner's work survives: a broken verifier must not erase its findings
    assert collection.findings, "scanner findings must not be dropped when the verifier fails"
