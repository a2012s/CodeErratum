"""Reading maintainer verdicts back off a pull request.

The adoption rate is the number every other part of this system is tuned against,
and until now it could only be written on the machine that ran the review. A PR is
where a finding actually gets judged, so the judgement has to come back from there —
which means the label map has to travel with the comment, and the parser must not
invent verdicts from prose.
"""

from __future__ import annotations

import json

import pytest

from codeerratum.ci import github
from codeerratum.ci.github import (
    PullRequest,
    collect_verdicts,
    extract_verdicts,
    labels_comment,
    labels_from_comments,
    parse_verdict_line,
    post_review,
    summary_body,
)
from codeerratum.models import Anchor, Finding, FindingCollection
from codeerratum.models.finding import Suggestion
from codeerratum.report import finding_labels, labels_from_markdown
from codeerratum.report.markdown import render_markdown

PR = PullRequest(
    owner="a2012s", repo="CodeErratum", number=7, base_sha="base111", head_sha="head222"
)

SOURCE = ["def foo(items):", "    x = items", "    return x[0]", "", "def bar():", "    pass"]


def _finding(claim: str, agreement: str = "unique") -> Finding:
    return Finding(
        id=f"{'0' * 15}{len(claim)}",
        unit_id="u1",
        lens="correctness",
        category="boundary",
        severity="P1",
        claim=claim,
        detail="d",
        anchors=[Anchor.from_source("m.py", 3, 3, SOURCE)],
        agreement=agreement,
        suggestion=Suggestion(explanation="e", acceptance="pytest -q"),
    )


def _comment(body: str, at: str, who: str = "alice") -> dict:
    return {"body": body, "created_at": at, "user": {"login": who}}


class _Store:
    """The two methods collect_verdicts uses, plus a write log."""

    def __init__(self, known: dict[str, dict] | None = None, accepts: bool = True):
        self.known = known or {}
        self.accepts = accepts
        self.writes: list[tuple[str, str, str]] = []

    def statuses_for(self, ids):
        return {i: self.known[i] for i in ids if i in self.known}

    def set_status(self, finding_id, status, reason=""):
        self.writes.append((finding_id, status, reason))
        if not self.accepts:
            return False
        self.known[finding_id] = {"status": status, "reason": reason}
        return True


# ---- the directive grammar ---------------------------------------------


@pytest.mark.parametrize(
    "line,expected",
    [
        ("F-001 accept", ("F-001", "accepted", "")),
        ("**F-012** reject: 误报，测试已覆盖", ("F-012", "rejected", "误报，测试已覆盖")),
        ("f-1 ACCEPT", ("F-001", "accepted", "")),
        ("> F-003 rejected", ("F-003", "rejected", "")),
        ("- F-004 驳回", ("F-004", "rejected", "")),
        ("F-005：已修", ("F-005", "accepted", "")),
        ("F-006 wontfix", ("F-006", "rejected", "")),
        ("F-007 这条我同意，改一下", ("F-007", "accepted", "")),
        # A negation inverts the verdict. Before the fix these were all recorded
        # as acceptances, silently inverting the adoption rate and routing scores.
        ("F-001 不采纳", ("F-001", "rejected", "")),
        ("F-002 不认可", ("F-002", "rejected", "")),
        ("F-003 不确认", ("F-003", "rejected", "")),
        ("F-004 不同意", ("F-004", "rejected", "")),
        ("F-005 not accepted", ("F-005", "rejected", "")),
        ("F-006 do not approve", ("F-006", "rejected", "")),
        ("F-007 这条我不同意，别改", ("F-007", "rejected", "")),
    ],
)
def test_verdict_lines_parse(line, expected):
    assert parse_verdict_line(line) == expected


@pytest.mark.parametrize(
    "line",
    [
        "",
        "这里没问题，可以合了",  # a verdict in spirit, but on nothing addressable
        "F-009 看着有点奇怪",  # a label with no verdict word
        "已修 F-",  # not a label at all
        "reject",  # a verdict with no label
        "acceptance 测试要补",  # 'accept' must not match inside a longer word
        "F-003 这个同意吗",  # asking is not deciding
        "F-004 你觉得呢",
    ],
)
def test_prose_is_not_a_verdict(line):
    assert parse_verdict_line(line) is None


def test_multi_line_comment_yields_each_verdict():
    body = "整体 LGTM。\n\nF-001 accept\nF-002 reject: 已有回归测试\nF-003 讨论下"
    verdicts = extract_verdicts([_comment(body, "2026-11-25T10:00:00Z")])
    assert [(v["label"], v["status"]) for v in verdicts] == [
        ("F-001", "accepted"),
        ("F-002", "rejected"),
    ]


def test_our_own_summary_is_not_a_verdict():
    """The summary *teaches* the syntax, so parsing it back would fabricate采纳率."""
    body = (
        f"<!-- codeerratum:review:{PR.head_sha} -->\n"
        "<!-- codeerratum:labels:F-001=aaa -->\n"
        "## CodeErratum · 1 条发现\n\n"
        "裁决回收：回复一行 `F-001 accept` 或 `F-001 reject: 原因`\n"
    )
    assert extract_verdicts([_comment(body, "2026-11-25T10:00:00Z", who="bot")]) == []


def test_last_reply_wins_per_label():
    comments = [
        _comment("F-001 accept", "2026-11-25T10:00:00Z"),
        _comment("F-001 reject: 想清楚了，是误报", "2026-11-26T09:00:00Z", who="bob"),
    ]
    verdicts = extract_verdicts(comments)
    assert verdicts[-1] == {
        "label": "F-001",
        "status": "rejected",
        "note": "想清楚了，是误报",
        "author": "bob",
        "created_at": "2026-11-26T09:00:00Z",
    }


def test_created_at_order_is_not_the_api_order():
    """GitHub returns issue comments oldest first, but the map is built by walking pages."""
    comments = [
        _comment("F-001 reject: 后来确认是误报", "2026-11-27T10:00:00Z", who="carol"),
        _comment("F-001 accept", "2026-11-25T10:00:00Z"),
    ]
    verdicts = extract_verdicts(comments)
    assert [v["created_at"] for v in verdicts] == [
        "2026-11-25T10:00:00Z",
        "2026-11-27T10:00:00Z",
    ]


# ---- the label map -----------------------------------------------------


def test_labels_comment_round_trips():
    labels = {"F-001": "aaaa", "F-002": "bbbb"}
    text = labels_comment(labels)
    assert labels_from_comments([_comment(text, "2026-11-25T10:00:00Z")]) == labels


def test_newest_map_wins_when_two_runs_share_a_pr():
    """Two runs share F-001; taking the union would adjudicate the wrong finding."""
    older = _comment(labels_comment({"F-001": "old"}), "2026-11-25T10:00:00Z", who="bot")
    newer = _comment(labels_comment({"F-001": "new"}), "2026-11-26T10:00:00Z", who="bot")
    assert labels_from_comments([older, newer]) == {"F-001": "new"}
    assert labels_from_comments([newer, older]) == {"F-001": "new"}


def test_work_order_and_label_map_cannot_drift():
    """The headings the maintainer reads and the ids we write verdicts against."""
    for mode, findings in (
        ("quick", [_finding("甲"), _finding("乙")]),
        ("dual", [_finding("甲", "conflict"), _finding("乙", "unanimous")]),
    ):
        collection = FindingCollection(
            run_id="r1",
            repo="/x",
            commit="",
            model_id="m",
            spec_level="L1",
            mode=mode,
            findings=findings,
        )
        text = render_markdown(collection, "2026-11-28 10:00")
        assert finding_labels(collection) == labels_from_markdown(text), mode


def test_summary_carries_the_map_and_the_reply_syntax():
    collection = FindingCollection(
        run_id="r1",
        repo="/x",
        commit="",
        model_id="m",
        spec_level="L1",
        mode="quick",
        findings=[_finding("甲")],
    )
    body = summary_body(collection, [], collection.findings, "M", {"F-001": "abc123"})
    assert "codeerratum:labels:F-001=abc123" in body
    assert "F-001 accept" in body


def test_summary_without_labels_stays_unchanged():
    collection = FindingCollection(
        run_id="r1", repo="/x", commit="", model_id="m", spec_level="L1", findings=[]
    )
    body = summary_body(collection, [], [], "M")
    assert "codeerratum:labels" not in body


# ---- collection --------------------------------------------------------


def _stub_comments(monkeypatch, comments):
    monkeypatch.setattr(github, "_request", lambda method, url, token, **kw: list(comments))


def test_pr_review_embeds_the_map_in_the_posted_summary(monkeypatch):
    collection = FindingCollection(
        run_id="r1",
        repo="/x",
        commit="",
        model_id="m",
        spec_level="L1",
        mode="quick",
        findings=[_finding("甲")],
    )
    posted: list[dict] = []

    def fake(method, url, token, payload=None, **kw):
        if method == "POST" and "issues" in url:
            posted.append(payload)
            return {"html_url": "https://x/7#1"}
        if method == "POST":
            return {}
        return []

    monkeypatch.setattr(github, "_request", fake)
    post_review(
        PR,
        "t",
        collection,
        [],
        collection.findings,
        labels={"F-001": "abc123"},
    )
    assert "codeerratum:labels:F-001=abc123" in posted[0]["body"]


def test_collect_applies_both_verdicts_with_provenance(monkeypatch):
    comments = [
        _comment(labels_comment({"F-001": "aaa", "F-002": "bbb"}), "2026-11-25T10:00:00Z"),
        _comment("F-001 accept", "2026-11-25T11:00:00Z", who="alice"),
        _comment("F-002 reject: 空列表在调用方已保证", "2026-11-25T12:00:00Z", who="bob"),
    ]
    _stub_comments(monkeypatch, comments)
    store = _Store()

    result = collect_verdicts(PR, "t", store)

    assert sorted(store.writes) == sorted(
        [
            ("aaa", "accepted", "github:@alice"),
            ("bbb", "rejected", "github:@bob：空列表在调用方已保证"),
        ]
    )
    assert result["labels_source"] == "pr"
    assert [a["label"] for a in result["applied"]] == ["F-001", "F-002"]


def test_collect_falls_back_to_the_local_ledger_map(monkeypatch):
    """Reviews posted before 0.7.4 have no map on the PR; the run's work order does."""
    _stub_comments(monkeypatch, [_comment("F-003 accept", "2026-11-25T11:00:00Z")])
    store = _Store()

    result = collect_verdicts(PR, "t", store, {"F-003": "ccc"})

    assert store.writes == [("ccc", "accepted", "github:@alice")]
    assert result["labels_source"] == "local"


def test_verdict_for_an_unknown_label_is_reported_not_guessed(monkeypatch):
    _stub_comments(
        monkeypatch,
        [
            _comment(labels_comment({"F-001": "aaa"}), "2026-11-25T10:00:00Z"),
            _comment("F-099 accept", "2026-11-25T11:00:00Z"),
        ],
    )
    store = _Store()
    result = collect_verdicts(PR, "t", store)
    assert result["unknown"] == ["F-099"]
    assert store.writes == []


def test_a_finding_deleted_from_the_store_is_counted_as_missing(monkeypatch):
    _stub_comments(
        monkeypatch,
        [
            _comment(labels_comment({"F-001": "aaa"}), "2026-11-25T10:00:00Z"),
            _comment("F-001 accept", "2026-11-25T11:00:00Z"),
        ],
    )
    store = _Store(accepts=False)
    result = collect_verdicts(PR, "t", store)
    assert result["not_found"] == ["F-001"]
    assert result["applied"] == []


def test_re_running_is_idempotent(monkeypatch):
    """CI re-collecting on every push must not churn the reason column."""
    comments = [
        _comment(labels_comment({"F-001": "aaa"}), "2026-11-25T10:00:00Z"),
        _comment("F-001 accept", "2026-11-25T11:00:00Z"),
    ]
    _stub_comments(monkeypatch, comments)
    store = _Store(known={"aaa": {"status": "accepted", "reason": "github:@alice"}})

    result = collect_verdicts(PR, "t", store)
    assert result["applied"] == []
    assert result["unchanged"] == 1
    assert store.writes == []


def test_changing_one_s_mind_rewrites_it(monkeypatch):
    comments = [
        _comment(labels_comment({"F-001": "aaa"}), "2026-11-25T10:00:00Z"),
        _comment("F-001 reject: 复核后确认是误报", "2026-11-26T11:00:00Z", who="bob"),
    ]
    _stub_comments(monkeypatch, comments)
    store = _Store(known={"aaa": {"status": "accepted", "reason": "github:@alice"}})

    result = collect_verdicts(PR, "t", store)
    assert [a["status"] for a in result["applied"]] == ["rejected"]
    assert store.writes == [("aaa", "rejected", "github:@bob：复核后确认是误报")]


def test_dry_run_writes_nothing(monkeypatch):
    comments = [
        _comment(labels_comment({"F-001": "aaa"}), "2026-11-25T10:00:00Z"),
        _comment("F-001 accept", "2026-11-25T11:00:00Z"),
    ]
    _stub_comments(monkeypatch, comments)
    store = _Store()

    result = collect_verdicts(PR, "t", store, dry_run=True)
    assert store.writes == []
    assert [a["finding_id"] for a in result["applied"]] == ["aaa"]
    assert all(a["dry_run"] for a in result["applied"])


def test_pagination_reaches_a_verdict_on_page_two(monkeypatch):
    """The same one-page bug that made the idempotency marker miss before."""
    first = [_comment("x", f"2026-11-2{i}T10:00:00Z") for i in range(1, 10)] * 12
    first = first[:100]  # a full page: anything shorter and the walk stops at page one
    second = [_comment(labels_comment({"F-001": "aaa"}), "2026-11-25T10:00:00Z")]
    third = [_comment("F-001 accept", "2026-11-26T10:00:00Z")]

    def fake(method, url, token, **kw):
        if "page=2" in url:
            return second + third
        return first

    monkeypatch.setattr(github, "_request", fake)
    store = _Store()
    result = collect_verdicts(PR, "t", store)
    assert store.writes == [("aaa", "accepted", "github:@alice")]
    assert result["labels_source"] == "pr"


def test_no_directives_at_all_reports_the_source(monkeypatch):
    _stub_comments(monkeypatch, [_comment("整体看起来不错", "2026-11-25T10:00:00Z")])
    result = collect_verdicts(PR, "t", _Store())
    assert result["applied"] == []
    assert result["labels_source"] == "none"


def test_cli_command_exists_with_the_right_options():
    from typer.main import get_command

    from codeerratum.cli import app

    commands = get_command(app).commands
    assert "pr-collect" in commands
    opts = {o for p in commands["pr-collect"].params for o in p.opts}
    assert {"--pr", "--run-id", "--dry-run", "--json", "--repo"} <= opts


def _write_run(runs_dir, stamp, slug, number):
    run = runs_dir / stamp
    run.mkdir(parents=True)
    (run / "pr-comments.json").write_text(
        json.dumps({"pull_request": {"slug": slug, "number": number}}), encoding="utf-8"
    )
    return run


def test_the_local_ledger_finds_the_newest_run_for_that_pr(tmp_path):
    """Guessing the run is safe only because it is keyed on slug + number."""
    from codeerratum.cli import _latest_pr_run

    runs = tmp_path / "runs"
    runs.mkdir()
    _write_run(runs, "20261125-101010", "a2012s/CodeErratum", 7)
    newest = _write_run(runs, "20261128-090000", "a2012s/CodeErratum", 7)
    _write_run(runs, "20261129-090000", "a2012s/CodeErratum", 8)  # another PR
    _write_run(runs, "20261130-090000", "someoneelse/fork", 7)  # another repo
    (runs / "20261201-090000").mkdir()  # a run that was never posted
    broken = runs / "20261202-090000"
    broken.mkdir()
    (broken / "pr-comments.json").write_text("{not json", encoding="utf-8")

    assert _latest_pr_run(runs, PR) == newest.name


def test_the_local_ledger_is_empty_when_nothing_was_posted(tmp_path):
    from codeerratum.cli import _latest_pr_run

    assert _latest_pr_run(tmp_path / "runs", PR) == ""
