from pathlib import Path

import pytest
from pydantic import ValidationError

from codeerratum.models.finding import (
    Anchor,
    DefectCategory,
    Finding,
    Lens,
    Suggestion,
    snippet_hash,
)
from codeerratum.models.unit import Hunk, ReviewUnit


def make_finding(tmp_path: Path, claim: str = "空列表取首元素导致崩溃") -> Finding:
    code = "items = []\nfirst = items[0]\n"
    (tmp_path / "demo.py").write_text(code, encoding="utf-8")
    anchor = Anchor(
        file="demo.py",
        start_line=2,
        end_line=2,
        snippet="first = items[0]\n",
        snippet_hash=snippet_hash("first = items[0]\n"),
    )
    return Finding(
        id=Finding.content_id("u1", claim, ["demo.py:2:null_deref"]),
        unit_id="u1",
        lens=Lens.CORRECTNESS,
        category=DefectCategory.BOUNDARY,
        severity="P1",
        claim=claim,
        detail="当 items 为空时，items[0] 抛出 IndexError，导致接口返回 500。",
        anchors=[anchor],
        suggestion=Suggestion(
            diff=(
                "--- a/demo.py\n+++ b/demo.py\n@@ -1,2 +1,4 @@\n items = []\n"
                "-first = items[0]\n+if not items:\n+    raise ValueError('empty')\n"
                "+first = items[0]\n"
            ),
            explanation="在取首元素前检查空列表。",
            acceptance="pytest tests/test_demo.py -q 通过",
        ),
        first_seen_run="run1",
        last_seen_run="run1",
    )


def test_finding_roundtrip(tmp_path):
    f = make_finding(tmp_path)
    data = f.model_dump(mode="json")
    restored = Finding.model_validate(data)
    assert restored.id == f.id
    assert restored.anchors[0].snippet_hash == f.anchors[0].snippet_hash


def test_anchor_rejects_wrong_hash():
    with pytest.raises(ValidationError):
        Anchor(file="a.py", start_line=1, end_line=1, snippet="x = 1\n", snippet_hash="wrong")


def test_anchor_verify_detects_changed_file(tmp_path):
    f = make_finding(tmp_path)
    assert f.anchors[0].verify(tmp_path) is True
    (tmp_path / "demo.py").write_text("items = []\nfirst = items[0]  # changed\n", encoding="utf-8")
    assert f.anchors[0].verify(tmp_path) is False


def test_claim_length_limit(tmp_path):
    with pytest.raises(ValidationError):
        make_finding(tmp_path, "很长" * 40)


def test_suggestion_requires_acceptance():
    with pytest.raises(ValidationError):
        Suggestion(diff="--- a\n+++ b", explanation="change", acceptance="")


def test_review_unit_id_stable():
    h = Hunk(old_start=1, old_lines=2, new_start=1, new_lines=2, content="@@ -1,2 +1,2 @@")
    a = ReviewUnit.make_id("a.py", "foo", [h])
    b = ReviewUnit.make_id("a.py", "foo", [h])
    assert a == b
    assert len(a) == 16


def _registry(**extra):
    from codeerratum.models.registry import ModelRegistry

    rows = [
        {"model_id": "qwen-a", "base_url": "http://x/v1", "api_key": "k"},
        {"model_id": "qwen-b", "base_url": "http://x/v1", "api_key": "k"},
        {"model_id": "ds4.1", "base_url": "http://y/v1", "api_key": "k"},
    ]
    return ModelRegistry.model_validate({"models": rows, **extra})


def test_route_uses_the_pinned_model_for_a_single_seat():
    """quick + pinned is how "review with my ds4.1" is expressed."""
    reviewers, falsifier = _registry().route("quick", pinned=["ds4.1"])
    assert [m.model_id for m in reviewers] == ["ds4.1"]
    assert falsifier is None  # quick has no falsification round


def test_route_keeps_the_user_order_and_picks_a_spare_falsifier():
    reviewers, falsifier = _registry().route("dual", pinned=["ds4.1", "qwen-a"])
    assert [m.model_id for m in reviewers] == ["ds4.1", "qwen-a"]
    assert falsifier is not None and falsifier.model_id == "qwen-b"


def test_route_refuses_an_unknown_model_id():
    with pytest.raises(ValueError, match="unknown model id"):
        _registry().route("quick", pinned=["gpt-nope"])


def test_route_refuses_a_seat_count_that_does_not_match_the_mode():
    with pytest.raises(ValueError, match="takes 1 model"):
        _registry().route("quick", pinned=["qwen-a", "qwen-b"])


def test_route_never_seats_the_same_model_twice():
    with pytest.raises(ValueError, match="takes 2 model"):
        _registry().route("dual", pinned=["qwen-a", "qwen-a"])
