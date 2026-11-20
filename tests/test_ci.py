"""PR commenting tests — pure logic plus a stubbed transport, no network.

Two behaviours carry the value here. First, a finding whose line is outside the diff
must never become an inline comment: GitHub rejects the *whole* review for one bad
line, so a single misplaced anchor would silently take four good comments with it.
Second, a re-run on the same commit must not repost.
"""

from __future__ import annotations

import pytest

from codeerratum.ci import github
from codeerratum.ci.github import (
    CIError,
    PullRequest,
    already_posted,
    inline_body,
    load_event,
    marker_for,
    parse_pull_request,
    post_review,
    split_by_anchor,
    summary_body,
)
from codeerratum.models import Anchor, Finding, FindingCollection, ReviewUnit
from codeerratum.models.finding import Suggestion
from codeerratum.models.unit import Hunk

SOURCE = ["def foo(items):", "    x = items", "    return x[0]", "", "def bar():", "    pass"]

PR_EVENT = {
    "repository": {"full_name": "a2012s/CodeErratum"},
    "pull_request": {
        "number": 7,
        "base": {"sha": "base111"},
        "head": {"sha": "head222"},
    },
}


def _anchor(file: str = "m.py", line: int = 3) -> Anchor:
    return Anchor.from_source(file, line, line, SOURCE)


def _finding(claim: str, severity: str = "P1", line: int = 3, file: str = "m.py", **kw) -> Finding:
    return Finding(
        id=Finding.content_id("u1", claim, [f"{file}:{line}"]),
        unit_id="u1",
        lens="correctness",
        category="boundary",
        severity=severity,
        claim=claim,
        detail="items 为空时抛 IndexError，接口返回 500。",
        anchors=[_anchor(file, line)],
        suggestion=Suggestion(explanation="先判空再取首元素", acceptance="pytest -q"),
        **kw,
    )


def _unit(hunks: list[Hunk], file: str = "m.py") -> ReviewUnit:
    return ReviewUnit(
        unit_id="u1",
        primary_file=file,
        symbol="foo",
        kind="function",
        hunks=hunks,
        changed_lines=1,
    )


def _hunk(new_start: int, new_lines: int) -> Hunk:
    return Hunk(
        old_start=new_start,
        old_lines=new_lines,
        new_start=new_start,
        new_lines=new_lines,
        content="+    return x[0]\n",
    )


def _collection(findings: list[Finding], **kw) -> FindingCollection:
    base = {
        "run_id": "r1",
        "repo": "/tmp/repo",
        "commit": "head222",
        "model_id": "m",
        "spec_level": "L1",
        "mode": "quick",
        "reviewer_models": ["gpt-stub"],
        "findings": findings,
    }
    return FindingCollection(**(base | kw))


# ---- event parsing ------------------------------------------------------


def test_parse_pull_request_extracts_the_three_dot_range():
    pr = parse_pull_request(PR_EVENT)
    assert (pr.owner, pr.repo, pr.number) == ("a2012s", "CodeErratum", 7)
    assert pr.slug == "a2012s/CodeErratum"
    # Three dots: only what this branch added, not what base moved on since.
    assert pr.diff_spec == "base111...head222"


def test_parse_rejects_non_pull_request_events():
    with pytest.raises(CIError, match="not a pull_request"):
        parse_pull_request({"repository": {"full_name": "a/b"}})


def test_parse_rejects_missing_sha():
    event = {"repository": {"full_name": "a/b"}, "pull_request": {"number": 1, "base": {}}}
    with pytest.raises(CIError, match="base/head"):
        parse_pull_request(event)


def test_load_event_without_env_is_a_clear_error(monkeypatch):
    monkeypatch.delenv("GITHUB_EVENT_PATH", raising=False)
    with pytest.raises(CIError, match="GITHUB_EVENT_PATH"):
        load_event()


def test_load_event_reports_a_bad_path(tmp_path):
    with pytest.raises(CIError, match="not found"):
        load_event(tmp_path / "nope.json")


# ---- selection ----------------------------------------------------------


def test_only_findings_inside_a_hunk_become_inline():
    """A comment on a line outside the diff would make GitHub reject the whole review."""
    units = [_unit([_hunk(2, 2)])]  # lines 2..3
    inside = _finding("空列表取首元素会崩", line=3)
    outside = _finding("远处的空指针", line=99)
    inline, rest = split_by_anchor([inside, outside], units)
    assert inline == [inside]
    assert rest == [outside]


def test_only_p0_p1_are_inline():
    units = [_unit([_hunk(1, 6)])]
    p2 = _finding("健壮性不足", severity="P2", line=3)
    p0 = _finding("数据丢失", severity="P0", line=3)
    inline, rest = split_by_anchor([p2, p0], units)
    assert inline == [p0]
    assert rest == [p2]


def test_inline_cap_moves_the_excess_to_the_summary():
    units = [_unit([_hunk(1, 6)])]
    findings = [_finding(f"问题 {i}", line=3) for i in range(7)]
    inline, rest = split_by_anchor(findings, units, limit=5)
    assert len(inline) == 5
    assert len(rest) == 2


def test_anchor_in_a_different_file_is_not_inline():
    units = [_unit([_hunk(1, 6)], file="other.py")]
    f = _finding("空列表", line=3, file="m.py")
    inline, rest = split_by_anchor([f], units)
    assert inline == []
    assert rest == [f]


# ---- rendering ----------------------------------------------------------


def test_inline_body_carries_fix_and_acceptance():
    body = inline_body(_finding("空列表取首元素会崩"))
    assert "P1" in body
    assert "空列表取首元素会崩" in body
    assert "先判空再取首元素" in body
    assert "pytest -q" in body


def test_summary_states_counts_and_hides_the_marker():
    inline = [_finding("甲")]
    rest = [_finding("乙"), _finding("丙")]
    body = summary_body(_collection(inline + rest), inline, rest, marker_for("head222"))
    assert marker_for("head222") in body
    assert "3 条发现" in body
    assert "`m.py:3`" in body
    # The summary is the index: inline findings are listed there too, marked as such.
    assert "| 行内 |" in body
    assert "| 摘要 |" in body


def test_summary_truncates_a_long_list_and_says_so():
    rest = [_finding(f"问题 {i}") for i in range(30)]
    body = summary_body(_collection(rest), [], rest, "<!-- x -->")
    assert "另有 10 条未列出" in body


def test_summary_mentions_dropped_findings():
    body = summary_body(
        _collection([], rejected=[{"claim": "幻觉", "reason": "anchor_file_mismatch"}]),
        [],
        [],
        "<!-- x -->",
    )
    assert "1 条发现因定位校验未通过被丢弃" in body


def test_summary_reports_project_rules_when_used():
    body = summary_body(
        _collection([], house_rules_sha="abc123456789", house_rules_chars=42),
        [],
        [],
        "<!-- x -->",
    )
    assert "abc123456789" in body
    assert "42 字" in body


# ---- transport (stubbed) ------------------------------------------------


@pytest.fixture
def pr() -> PullRequest:
    return parse_pull_request(PR_EVENT)


def test_rerun_on_the_same_commit_is_skipped(pr, monkeypatch):
    monkeypatch.setattr(
        github, "_request", lambda *a, **k: [{"body": f"x {marker_for(pr.head_sha)}"}]
    )
    assert already_posted(pr, "t", marker_for(pr.head_sha)) is True
    result = post_review(pr, "t", _collection([]), [], [])
    assert result["skipped"]


def test_posting_one_review_then_one_summary(pr, monkeypatch):
    calls: list[tuple[str, str, dict | None]] = []

    def fake(method, url, token, payload=None, timeout=30):
        calls.append((method, url, payload))
        if method == "GET":
            return []
        return {"html_url": "https://github.com/x/y/pull/7#c1"}

    monkeypatch.setattr(github, "_request", fake)
    inline = [_finding("空列表")]
    rest = [_finding("乙")]
    result = post_review(pr, "t", _collection(inline + rest), inline, rest)

    assert result["inline_posted"] == 1
    assert result["summary_url"].endswith("#c1")
    review_calls = [c for c in calls if c[1].endswith("/reviews")]
    assert len(review_calls) == 1
    assert review_calls[0][2]["comments"][0]["path"] == "m.py"
    assert review_calls[0][2]["comments"][0]["line"] == 3
    assert review_calls[0][2]["comments"][0]["side"] == "RIGHT"
    issue_calls = [c for c in calls if "/issues/7/comments" in c[1] and c[0] == "POST"]
    assert len(issue_calls) == 1


def test_failed_batch_degrades_instead_of_losing_every_comment(pr, monkeypatch):
    """One unplaceable line must not cost the other comments."""
    posted: list[str] = []

    def fake(method, url, token, payload=None, timeout=30):
        if method == "GET":
            return []
        if url.endswith("/reviews"):
            raise CIError("HTTP 422: line not part of the diff")
        posted.append(payload["body"])
        return {"html_url": "u"}

    monkeypatch.setattr(github, "_request", fake)
    inline = [_finding("甲"), _finding("乙")]
    result = post_review(pr, "t", _collection(inline), inline, [])

    assert result["degraded"] is True
    assert result["inline_posted"] == 2  # both survived via the per-comment path
    # Summary also mentions them, so count only the bodies that carry a suggestion.
    inline_posts = [b for b in posted if "**建议**" in b]
    assert len(inline_posts) == 2


def test_single_comment_failure_is_collected_not_raised(pr, monkeypatch):
    def fake(method, url, token, payload=None, timeout=30):
        if method == "GET":
            return []
        if url.endswith("/reviews"):
            raise CIError("HTTP 422")
        if url.endswith("/comments") and payload and payload.get("line") == 3:
            raise CIError("HTTP 422 still")
        return {"html_url": "u"}

    monkeypatch.setattr(github, "_request", fake)
    result = post_review(pr, "t", _collection([]), [_finding("甲")], [])
    assert result["inline_posted"] == 0
    assert result["inline_failed"]
