"""CI integrations — posting review results where the code is actually discussed."""

from .github import (
    CIError,
    PullRequest,
    already_posted,
    collect_verdicts,
    extract_verdicts,
    inline_body,
    labels_comment,
    labels_from_comments,
    list_issue_comments,
    load_event,
    marker_for,
    parse_pull_request,
    parse_verdict_line,
    post_review,
    split_by_anchor,
    summary_body,
)

__all__ = [
    "CIError",
    "PullRequest",
    "already_posted",
    "collect_verdicts",
    "extract_verdicts",
    "inline_body",
    "labels_comment",
    "labels_from_comments",
    "list_issue_comments",
    "parse_verdict_line",
    "load_event",
    "marker_for",
    "parse_pull_request",
    "post_review",
    "split_by_anchor",
    "summary_body",
]
