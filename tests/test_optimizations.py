"""Guardrails for the three quick-review optimizations.

1. docs/config/i18n units are skipped unless explicitly requested;
2. oversized units are split by symbol (or line window) before a model call;
3. quick mode scans with the light model and re-reviews hit units with the
   trusted one, without charging a second full sweep.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from codeerratum import pipeline
from codeerratum.ingest import get_diff, split_diff
from codeerratum.models import FindingCollection, ModelRegistry, ModelSpec
from codeerratum.providers.base import Completion, Usage
from codeerratum.review import orchestrator as orch
from codeerratum.review import run_review
from codeerratum.review.falsification import LLMFalsify as FalsifySchema
from codeerratum.review.schema import LLMAnchor, LLMFinding, LLMReview, LLMSuggestion


def _init_repo(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.io"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=root, check=True)
    return root


def _stub_run(monkeypatch, seen: dict) -> None:
    monkeypatch.setenv("CODEERRATUM_BASE_URL", "http://stub.invalid/v1")
    monkeypatch.setenv("CODEERRATUM_MODEL", "gpt-stub")
    monkeypatch.delenv("CODEERRATUM_MODELS", raising=False)

    async def fake_run(registry, mode, units, repo_root, run_id, **kwargs):
        seen["units"] = len(units)
        seen["files"] = [u.primary_file for u in units]
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


def test_non_code_units_are_skipped_by_default(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path / "repo")
    files = {
        "m.py": 'def f():\n    return "old"\n',
        "README.md": "# Title\n\nold\n",
        "package.json": '{"name": "old"}\n',
        "src/i18n/zh-CN.ts": "export const t = { a: 'old' }\n",
    }
    for name, text in files.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=repo, check=True)
    for name, text in files.items():
        (repo / name).write_text(text.replace("old", "new"), encoding="utf-8")

    seen: dict = {}
    notes: list[str] = []
    _stub_run(monkeypatch, seen)
    pipeline.execute_review(repo, diff_spec="HEAD", progress=notes.append)

    assert seen["files"] == ["m.py"], seen["files"]
    assert any("非代码单元" in n for n in notes), notes

    seen_all: dict = {}
    _stub_run(monkeypatch, seen_all)
    pipeline.execute_review(
        repo, diff_spec="HEAD", include_non_code=True, progress=notes.append
    )
    assert len(seen_all["files"]) == 4, seen_all["files"]


def test_oversized_unit_is_split_by_symbol(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    target = repo / "big.ts"

    def source(n: int) -> str:
        chunks = []
        for name in ("alpha", "beta", "gamma"):
            body = "\n".join(f"  const v{i} = {i};" for i in range(n))
            chunks.append(f"export function {name}() {{\n{body}\n}}\n")
        return "\n".join(chunks)

    target.write_text(source(5), encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=repo, check=True)
    target.write_text(source(120), encoding="utf-8")

    units = split_diff(get_diff("HEAD", repo), repo)
    assert len(units) >= 3, [u.symbol for u in units]
    from codeerratum.ingest.git import MAX_UNIT_CHANGED_LINES

    assert all(u.changed_lines <= MAX_UNIT_CHANGED_LINES for u in units)
    assert {"alpha", "beta", "gamma"} <= {u.symbol for u in units}


def test_route_quick_tier_uses_light_scanner_and_trusted_verifier():
    registry = ModelRegistry(
        models=[
            ModelSpec(model_id="qwen-max-preview", base_url="http://a"),
            ModelSpec(model_id="qwen3-flash", base_url="http://b"),
            ModelSpec(model_id="deepseek-mini", base_url="http://c"),
        ]
    )
    scanner, verifier = registry.route_quick_tier(pinned=["qwen-max-preview"])
    assert verifier is not None and verifier.model_id == "qwen-max-preview"
    assert "flash" in scanner.model_id or "mini" in scanner.model_id


def test_route_quick_tier_degrades_with_one_model():
    registry = ModelRegistry(models=[ModelSpec(model_id="only-model", base_url="http://a")])
    scanner, verifier = registry.route_quick_tier()
    assert scanner.model_id == "only-model"
    assert verifier is None


class TierStub:
    """Returns the same real finding for every unit unless asked to stay silent."""

    family = "stub"

    def __init__(self, real_line: int, snippet: str, report: bool = True):
        self.real_line = real_line
        self.snippet = snippet
        self.report = report
        self.calls = 0

    def dump_failures(self, path):  # noqa: ANN001
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("[]", encoding="utf-8")

    async def complete(self, model_id, messages, schema, temperature=0.0, max_tokens=2048):
        self.calls += 1
        if schema is FalsifySchema:
            raise AssertionError("tiered quick must not run the falsifier")
        findings = []
        if self.report:
            findings.append(
                LLMFinding(
                    lens="correctness",
                    category="boundary",
                    severity="P1",
                    claim="空列表取首元素崩溃",
                    detail="x 为空时抛 IndexError。",
                    anchor=LLMAnchor(
                        file="m.py",
                        start_line=self.real_line,
                        end_line=self.real_line,
                        snippet=self.snippet,
                    ),
                    suggestion=LLMSuggestion(explanation="判空", acceptance="pytest -q"),
                )
            )
        review = LLMReview(summary="stub", findings=findings)
        return Completion(
            text=json.dumps(review.model_dump(mode="json")),
            parsed=review,
            usage=Usage(input_tokens=100, output_tokens=50),
            raw_request_hash="stub-review",
            latency_ms=1,
        )


@pytest.fixture
def tier_repo(tmp_path):
    repo = _init_repo(tmp_path)
    (repo / "m.py").write_text("def foo():\n    return 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=repo, check=True)
    (repo / "m.py").write_text("def foo():\n    x = []\n    return x[0]\n", encoding="utf-8")
    return repo


@pytest.mark.asyncio
async def test_quick_tier_reviews_hit_units_only_once_each(tier_repo, monkeypatch):
    from codeerratum.ingest import split_diff

    diff = get_diff("HEAD", tier_repo)
    units = split_diff(diff, tier_repo)
    lines = (tier_repo / "m.py").read_text().splitlines(keepends=True)
    line_no = next(i for i, line in enumerate(lines, 1) if "return x[0]" in line)
    snippet = lines[line_no - 1]

    registry = ModelRegistry(
        models=[
            ModelSpec(model_id="qwen-max-preview", base_url="http://a"),
            ModelSpec(model_id="qwen3-flash", base_url="http://b"),
        ]
    )
    stubs = {
        "qwen3-flash": TierStub(line_no, snippet),
        "qwen-max-preview": TierStub(line_no, snippet),
    }
    monkeypatch.setattr(orch, "_make_provider", lambda spec: stubs[spec.model_id])

    collection, calls, _ = await run_review(
        registry,
        "quick",
        units,
        tier_repo,
        "run-tier",
        pinned=["qwen-max-preview"],
        tiered=True,
    )

    assert len(collection.findings) == 1
    assert set(collection.findings[0].source_models) == {"qwen3-flash", "qwen-max-preview"}
    assert collection.findings[0].agreement == "unanimous"
    assert collection.comparison["mode"] == "tiered"
    assert collection.comparison["verified_units"] == 1
    assert sum(1 for c in calls if c.get("role") == "verify") == 1
    # Exactly one scan per unit: >= 1 would also pass a regression that sweeps
    # the whole queue twice and doubles the token bill.
    assert stubs["qwen3-flash"].calls == len(units)
    assert stubs["qwen-max-preview"].calls == 1


@pytest.mark.asyncio
async def test_quick_tier_clean_scan_skips_verifier(tier_repo, monkeypatch):
    from codeerratum.ingest import split_diff

    units = split_diff(get_diff("HEAD", tier_repo), tier_repo)
    registry = ModelRegistry(
        models=[
            ModelSpec(model_id="qwen-max-preview", base_url="http://a"),
            ModelSpec(model_id="qwen3-flash", base_url="http://b"),
        ]
    )
    stubs = {
        "qwen3-flash": TierStub(1, "", report=False),
        "qwen-max-preview": TierStub(1, "", report=False),
    }
    monkeypatch.setattr(orch, "_make_provider", lambda spec: stubs[spec.model_id])

    collection, calls, _ = await run_review(
        registry,
        "quick",
        units,
        tier_repo,
        "run-clean",
        pinned=["qwen-max-preview"],
        tiered=True,
    )

    assert not collection.findings
    assert not any(c.get("role") == "verify" for c in calls)
    assert stubs["qwen-max-preview"].calls == 0
