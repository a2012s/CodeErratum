"""End-to-end test with a stubbed provider — proves the whole pipeline without an API key."""

import json
import subprocess

import pytest

from codeerratum.ingest import get_commit, get_diff, split_diff
from codeerratum.models import FindingCollection
from codeerratum.providers.base import Completion, Usage
from codeerratum.report import render_markdown
from codeerratum.review import LLMReview, review_unit
from codeerratum.review.schema import LLMAnchor, LLMFinding, LLMSuggestion
from codeerratum.store import ReviewStore


class StubProvider:
    """Returns a finding that cites a real line, plus a hallucinated one."""

    family = "stub"

    def __init__(self, real_line: int, snippet: str):
        self.real_line = real_line
        self.snippet = snippet

    async def complete(self, model_id, messages, schema, temperature=0.0, max_tokens=2048):
        review = LLMReview(
            summary="stub review",
            findings=[
                LLMFinding(
                    lens="correctness",
                    category="boundary",
                    severity="P1",
                    claim="真实问题：空列表取首元素",
                    detail="当 items 为空时抛 IndexError，接口返回 500。",
                    anchor=LLMAnchor(
                        file="m.py",
                        start_line=self.real_line,
                        end_line=self.real_line,
                        snippet=self.snippet,
                    ),
                    suggestion=LLMSuggestion(explanation="取首元素前判空", acceptance="pytest -q"),
                ),
                LLMFinding(
                    lens="correctness",
                    category="null_deref",
                    severity="P2",
                    claim="幻觉问题：引用不存在的行",
                    detail="行号不存在，应被定位校验丢弃。",
                    anchor=LLMAnchor(file="m.py", start_line=9999, end_line=9999, snippet="# fake"),
                    suggestion=LLMSuggestion(explanation="N/A", acceptance="N/A"),
                ),
            ],
        )
        return Completion(
            text=json.dumps(review.model_dump(mode="json")),
            parsed=review,
            usage=Usage(input_tokens=100, output_tokens=50),
            raw_request_hash="stub",
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
    # Modify line 2 to create a real diff
    (tmp_path / "m.py").write_text("def foo():\n    x = []\n    return x[0]\n", encoding="utf-8")
    return tmp_path


@pytest.mark.asyncio
async def test_full_pipeline(repo):
    diff = get_diff("HEAD", repo)  # unstaged changes vs HEAD
    units = split_diff(diff, repo)
    assert units, "should produce at least one unit"

    source_lines = (repo / "m.py").read_text().splitlines(keepends=True)
    real_line = next(i for i, line in enumerate(source_lines, 1) if "return x[0]" in line)
    snippet = source_lines[real_line - 1]

    provider = StubProvider(real_line=real_line, snippet=snippet)
    all_findings, all_rejected = [], []
    for unit in units:
        findings, rejected, _completion, _diags = await review_unit(
            provider, "stub-model", unit, repo, "run-e2e"
        )
        all_findings.extend(findings)
        all_rejected.extend(rejected)

    # The real finding survives; the hallucinated one is dropped by the anchor gate.
    assert len(all_findings) >= 1
    assert any("真实问题" in f.claim for f in all_findings)
    assert any(r.get("reason") == "anchor_out_of_range" for r in all_rejected)

    collection = FindingCollection(
        run_id="run-e2e",
        repo=str(repo),
        commit=get_commit(repo),
        model_id="stub-model",
        spec_level="L1",
        findings=all_findings,
        rejected=all_rejected,
    )
    md = render_markdown(collection, "2026-01-01T00:00:00Z")
    assert "# Code Review 工单" in md
    assert "执行协议" in md
    assert "真实问题" in md
    assert "已丢弃的发现" in md

    # Artifact round trip + store
    data = json.loads(collection.model_dump_json())
    restored = FindingCollection.model_validate(data)
    assert len(restored.findings) == len(all_findings)

    store = ReviewStore(repo / ".codeerratum" / "f.db")
    store.save_run(collection, "ts")
    fid = all_findings[0].id
    store.set_status(fid, "accepted")
    s = store.stats()
    assert s["accepted"] == 1
    assert s["acceptance_rate"] == 1.0
