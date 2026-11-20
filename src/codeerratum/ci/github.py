"""Post a review to a GitHub pull request — standard library only.

The output shape is dictated by what a PR reader can act on: at most five inline
comments, each on a line that really is part of the diff, plus one summary comment
carrying everything else. More inline comments than that and none of them get read.

Two failure modes are handled explicitly, because both turn a useful run into a
silent dud:

* GitHub rejects a whole review when one commented line is not in the diff, so
  findings are filtered against the hunks first (``split_by_anchor``) and a failed
  batch degrades to per-comment posting instead of losing all of them.
* A re-run on the same commit would duplicate every comment, so the summary carries
  a hidden marker and the poster refuses to post twice.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..models import Finding, FindingCollection, ReviewUnit

API_ROOT = "https://api.github.com"
MAX_INLINE = 5
INLINE_SEVERITIES = ("P0", "P1")
SUMMARY_MAX_ROWS = 20
TIMEOUT = 30


class CIError(Exception):
    """Anything the caller should surface as a one-line failure, not a traceback."""


@dataclass
class PullRequest:
    owner: str
    repo: str
    number: int
    base_sha: str
    head_sha: str

    @property
    def slug(self) -> str:
        return f"{self.owner}/{self.repo}"

    @property
    def diff_spec(self) -> str:
        """Three-dot diff: only what this branch added, not what base moved on."""
        return f"{self.base_sha}...{self.head_sha}"


# ---- event parsing ------------------------------------------------------


def load_event(path: str | Path | None = None) -> dict:
    raw = str(path) if path else os.environ.get("GITHUB_EVENT_PATH", "")
    if not raw:
        raise CIError("GITHUB_EVENT_PATH is not set; pass --event or run inside GitHub Actions")
    event_path = Path(raw)
    if not event_path.is_file():
        raise CIError(f"event file not found: {event_path}")
    try:
        return json.loads(event_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise CIError(f"event file is not valid JSON: {exc}") from exc


def parse_pull_request(event: dict) -> PullRequest:
    pr = event.get("pull_request") or {}
    if not pr:
        raise CIError("this event is not a pull_request event (nothing to comment on)")
    full = (event.get("repository") or {}).get("full_name", "")
    if "/" not in full:
        raise CIError("event has no repository.full_name")
    owner, name = full.split("/", 1)
    base = (pr.get("base") or {}).get("sha", "")
    head = (pr.get("head") or {}).get("sha", "")
    if not base or not head:
        raise CIError("event is missing base/head sha")
    return PullRequest(
        owner=owner,
        repo=name,
        number=int(pr["number"]),
        base_sha=base,
        head_sha=head,
    )


# ---- selection ----------------------------------------------------------


def _line_in_hunks(finding: Finding, units: list[ReviewUnit]) -> bool:
    """True when the anchor's end line is inside a diff hunk of the same file."""
    if not finding.anchors:
        return False
    anchor = finding.anchors[0]
    line = anchor.end_line or anchor.start_line
    for unit in units:
        if unit.primary_file != anchor.file:
            continue
        for hunk in unit.hunks:
            start, end = hunk.new_range
            if start <= line <= end:
                return True
    return False


def split_by_anchor(
    findings: list[Finding], units: list[ReviewUnit], limit: int = MAX_INLINE
) -> tuple[list[Finding], list[Finding]]:
    """Split into (inline candidates, summary only).

    GitHub refuses the whole review if one comment points outside the diff, so a
    finding that cannot be placed is not a candidate at all — it goes to the summary
    where it is still visible, instead of taking the other four comments down with it.
    """
    inline: list[Finding] = []
    rest: list[Finding] = []
    for f in findings:
        if f.severity in INLINE_SEVERITIES and _line_in_hunks(f, units) and len(inline) < limit:
            inline.append(f)
        else:
            rest.append(f)
    return inline, rest


# ---- rendering ----------------------------------------------------------


def inline_body(finding: Finding) -> str:
    parts = [f"**{finding.severity} · {finding.category}** — {finding.claim}", "", finding.detail]
    if finding.suggestion.explanation:
        parts += ["", f"**建议**：{finding.suggestion.explanation}"]
    if finding.suggestion.diff:
        parts += ["", "```diff", finding.suggestion.diff.strip(), "```"]
    if finding.suggestion.acceptance:
        parts += ["", f"**验收**：`{finding.suggestion.acceptance}`"]
    if finding.agreement == "unique" and finding.source_models:
        parts += ["", f"<sub>仅 {', '.join(finding.source_models)} 报出</sub>"]
    return "\n".join(parts)


def marker_for(head_sha: str) -> str:
    return f"<!-- codeerratum:review:{head_sha} -->"


def summary_body(
    collection: FindingCollection,
    inline: list[Finding],
    rest: list[Finding],
    marker: str,
) -> str:
    mode = collection.mode
    lines = [
        marker,
        f"## CodeErratum · {len(collection.findings)} 条发现",
        "",
        f"模式 `{mode}` · 模型 {', '.join(collection.reviewer_models) or '—'}"
        + (f" · 证伪 {collection.falsifier_model}" if collection.falsifier_model else ""),
    ]
    if collection.house_rules_sha:
        sha, chars = collection.house_rules_sha, collection.house_rules_chars
        lines.append(f"项目规范 `{sha}`（{chars} 字）")
    if inline:
        lines += ["", f"{len(inline)} 条已贴在具体代码行上（下表标注「行内」）。"]
    lines.append("")

    # One table for everything: the inline comments are visible in the diff, but a
    # reader skimming the summary should still see the whole picture in one place.
    rows = (inline + rest)[:SUMMARY_MAX_ROWS]
    if rows:
        inline_ids = {f.id for f in inline}
        lines += ["| 严重度 | 位置 | 结论 | 方式 |", "|---|---|---|---|"]
        for f in rows:
            anchor = f.anchors[0] if f.anchors else None
            where = f"`{anchor.file}:{anchor.start_line}`" if anchor else "—"
            claim = f.claim.replace("|", "\\|")
            how = "行内" if f.id in inline_ids else "摘要"
            lines.append(f"| {f.severity} | {where} | {claim} | {how} |")
        if len(inline) + len(rest) > len(rows):
            hidden = len(inline) + len(rest) - len(rows)
            lines += ["", f"_另有 {hidden} 条未列出，见运行产物 `findings.json`。_"]
    else:
        lines += ["本次变更未发现明确问题。"]

    if collection.rejected:
        lines += ["", f"<sub>{len(collection.rejected)} 条发现因定位校验未通过被丢弃。</sub>"]
    lines += ["", "<sub>AI 审查通过 ≠ 可以合入，请人工确认。</sub>"]
    return "\n".join(lines)


# ---- transport ----------------------------------------------------------


def _request(
    method: str,
    url: str,
    token: str,
    payload: dict | None = None,
    timeout: int = TIMEOUT,
) -> Any:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    req.add_header("User-Agent", "codeerratum")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - fixed host
            body = resp.read()
            return json.loads(body) if body.strip() else {}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:400]
        raise CIError(f"{method} {url} -> HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise CIError(f"{method} {url} failed: {exc.reason}") from exc


def already_posted(pr: PullRequest, token: str, marker: str, api: str = API_ROOT) -> bool:
    url = f"{api}/repos/{pr.slug}/issues/{pr.number}/comments?per_page=100"
    comments = _request("GET", url, token)
    return any(marker in (c.get("body") or "") for c in comments)


def _inline_payloads(pr: PullRequest, inline: list[Finding]) -> list[dict]:
    out = []
    for f in inline:
        anchor = f.anchors[0]
        out.append(
            {
                "path": anchor.file,
                "line": anchor.end_line or anchor.start_line,
                "side": "RIGHT",
                "body": inline_body(f),
            }
        )
    return out


def post_review(
    pr: PullRequest,
    token: str,
    collection: FindingCollection,
    inline: list[Finding],
    rest: list[Finding],
    api: str = API_ROOT,
    force: bool = False,
) -> dict:
    """Post one review (inline comments) plus one summary comment. Idempotent per commit."""
    marker = marker_for(pr.head_sha)
    if not force and already_posted(pr, token, marker, api):
        return {"skipped": "already posted for this commit", "marker": marker}

    result: dict = {"marker": marker, "inline_posted": 0, "degraded": False}
    payloads = _inline_payloads(pr, inline)

    if payloads:
        url = f"{api}/repos/{pr.slug}/pulls/{pr.number}/reviews"
        try:
            _request(
                "POST",
                url,
                token,
                {"event": "COMMENT", "body": "CodeErratum 行内发现见下。", "comments": payloads},
            )
            result["inline_posted"] = len(payloads)
        except CIError as batch_error:
            # One bad line kills the batch; retry individually so the rest survive.
            result["degraded"] = True
            result["batch_error"] = str(batch_error)
            single = f"{api}/repos/{pr.slug}/pulls/{pr.number}/comments"
            for payload in payloads:
                try:
                    _request("POST", single, token, payload)
                    result["inline_posted"] += 1
                except CIError as exc:
                    result.setdefault("inline_failed", []).append(str(exc))

    body = summary_body(collection, inline, rest, marker)
    comment = _request(
        "POST",
        f"{api}/repos/{pr.slug}/issues/{pr.number}/comments",
        token,
        {"body": body},
    )
    result["summary_url"] = comment.get("html_url", "")
    return result
