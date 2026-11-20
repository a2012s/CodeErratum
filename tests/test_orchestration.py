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
