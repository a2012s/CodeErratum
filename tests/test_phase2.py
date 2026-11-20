"""Second-phase tests: context enrichment, static evidence, feedback store."""

import subprocess
from pathlib import Path

from codeerratum.context import enclosing_source
from codeerratum.evidence import StaticDiag, known_facts_block
from codeerratum.models import Anchor, Finding, FindingCollection
from codeerratum.models.finding import DefectCategory, Lens, Suggestion, snippet_hash
from codeerratum.store import ReviewStore


def make_repo(tmp_path: Path) -> Path:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.io"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path, check=True)
    (tmp_path / "m.py").write_text("def foo():\n    return 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=tmp_path, check=True)
    return tmp_path


def test_enclosing_source():
    src = "def outer():\n    x = 1\n    return x\n\ndef other():\n    pass\n"
    from codeerratum.models import Hunk, ReviewUnit

    unit = ReviewUnit(
        unit_id="u",
        primary_file="m.py",
        symbol="outer",
        kind="function",
        hunks=[Hunk(old_start=2, old_lines=1, new_start=2, new_lines=1, content="+")],
        changed_lines=1,
    )
    result = enclosing_source(unit, src)
    assert "return x" in result
    assert "other" not in result


def test_known_facts_block():
    d = StaticDiag(tool="ruff", file="m.py", line=3, code="E501", message="line too long")
    block = known_facts_block([d])
    assert "禁止重复报告" in block
    assert "E501" in block
    assert known_facts_block([]) == ""


def make_collection(run_id="run1") -> FindingCollection:
    snippet = "x[0]\n"
    f = Finding(
        id="finding_abc123",
        unit_id="u",
        lens=Lens.CORRECTNESS,
        category=DefectCategory.BOUNDARY,
        severity="P1",
        claim="空索引崩溃",
        detail="空列表取首元素",
        anchors=[
            Anchor(
                file="m.py",
                start_line=1,
                end_line=1,
                snippet=snippet,
                snippet_hash=snippet_hash(snippet),
            )
        ],
        suggestion=Suggestion(explanation="判空", acceptance="pytest -q"),
    )
    return FindingCollection(
        run_id=run_id, repo=".", commit="abc", model_id="m", spec_level="L1", findings=[f]
    )


def test_store_roundtrip(tmp_path):
    store = ReviewStore(tmp_path / "f.db")
    store.save_run(make_collection(), "2026-01-01T00:00:00Z")
    assert store.find_finding("finding_abc")[0]["claim"] == "空索引崩溃"
    assert store.set_status("finding_abc123", "accepted") is True
    s = store.stats()
    assert s["accepted"] == 1
    assert s["acceptance_rate"] == 1.0


def test_store_reject_reason(tmp_path):
    store = ReviewStore(tmp_path / "f.db")
    store.save_run(make_collection("run2"), "ts")
    store.set_status("finding_abc123", "rejected", "误报")
    row = store.find_finding("finding_abc")[0]
    assert row["status"] == "rejected"
    assert row["rejection_reason"] == "误报"
