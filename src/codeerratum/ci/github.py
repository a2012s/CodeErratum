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
import re
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

# Shown next to the marker when the summary could not be posted but the inline
# comments already landed. The marker alone is an HTML comment and renders as an
# empty bubble on the PR — a reviewer would see a mystery blank comment.
FALLBACK_NOTE = (
    "CodeErratum：行内评论已发布，但摘要发送失败。已记下本次提交的审查标记，"
    "重跑不会重复发布行内评论。"
)


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


def labels_comment(labels: dict[str, str]) -> str:
    """A machine-readable map from the work order's ``F-001`` labels to finding ids.

    The PR is the only artifact guaranteed to reach whoever does the adjudicating,
    so it carries the ids with it: collection then works from any machine, including
    one that never ran the review. It is an HTML comment, so it stays invisible to a
    human reading the page.
    """
    if not labels:
        return ""
    pairs = ",".join(f"{label}={fid}" for label, fid in sorted(labels.items()))
    return f"<!-- codeerratum:labels:{pairs} -->"


def summary_body(
    collection: FindingCollection,
    inline: list[Finding],
    rest: list[Finding],
    marker: str,
    labels: dict[str, str] | None = None,
) -> str:
    mode = collection.mode
    lines = [
        marker,
        labels_comment(labels or {}),
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

    if rows and labels:
        # The reply syntax has to be written down where the verdict is given, or the
        # loop never closes: a maintainer who agrees in prose says "对，改一下", which
        # no parser can turn into an adoption rate.
        lines += [
            "",
            "<sub>裁决回收：回复一行 `F-001 accept` 或 `F-001 reject: 原因`，"
            "`codeerratum pr-collect` 会写回反馈库并回灌模型路由。</sub>",
        ]

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


def list_issue_comments(pr: PullRequest, token: str, api: str = API_ROOT) -> list[dict]:
    """Every conversation comment on the PR, newest page first not assumed.

    GitHub paginates via the Link header, which ``_request`` does not surface, so
    walk the implicit ``page`` param until a short page arrives. The caller needs
    the *whole* thread twice over: an idempotency check that misses a marker on
    page 2 re-posts the entire review, and a verdict that lives one page down is
    silently dropped from the adoption rate.
    """
    base = f"{api}/repos/{pr.slug}/issues/{pr.number}/comments?per_page=100"
    out: list[dict] = []
    page = 1
    while True:
        url = base if page == 1 else f"{base}&page={page}"
        comments = _request("GET", url, token)
        out.extend(comments)
        if len(comments) < 100:
            return out
        page += 1


def already_posted(pr: PullRequest, token: str, marker: str, api: str = API_ROOT) -> bool:
    """True when this commit's review marker is already on the PR."""
    return any(marker in (c.get("body") or "") for c in list_issue_comments(pr, token, api))


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
    labels: dict[str, str] | None = None,
) -> dict:
    """Post one review (inline comments) plus one summary comment. Idempotent per commit.

    ``labels`` is the ``F-001`` -> finding id map from the work order. It rides along
    inside the summary comment so a verdict collected later resolves to the finding the
    maintainer actually read, from any machine.
    """
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

    body = summary_body(collection, inline, rest, marker, labels)
    comments_url = f"{api}/repos/{pr.slug}/issues/{pr.number}/comments"
    try:
        comment = _request("POST", comments_url, token, {"body": body})
    except CIError:
        # The idempotency marker lives only in the summary. If inline comments
        # already landed and the summary fails, a rerun would post them all
        # again — leave a marker-only comment so the next run skips this commit.
        if result["inline_posted"]:
            try:
                _request(
                    "POST",
                    comments_url,
                    token,
                    {"body": f"{marker}\n\n{FALLBACK_NOTE}"},
                )
            except CIError:
                pass  # best effort; the original failure is the one to report
        raise
    result["summary_url"] = comment.get("html_url", "")
    return result


# ---- verdict collection -------------------------------------------------
#
# The adoption rate is the number this whole system is built on, and until now it
# could only be written on the machine that ran the review. A pull request is where
# a finding actually gets judged, so the judgement is read back from there: the
# summary comment carries the label -> finding id map, a maintainer replies one
# directive line, and `pr-collect` turns it into the same status the Web UI writes.


_LABEL_RE = re.compile(r"\bF-(\d{1,4})\b", re.IGNORECASE)
# Anything we posted. Our own summary contains the literal words "accept" and
# "reject" (it teaches the syntax), so parsing it back would manufacture verdicts.
_OURS_RE = re.compile(r"<!--\s*codeerratum:")
_LABELS_MAP_RE = re.compile(r"<!--\s*codeerratum:labels:([^\s>]+)\s*-->")

_ACCEPT_WORDS = {"accept", "accepted", "approve", "approved", "agree", "confirm", "confirmed"}
_REJECT_WORDS = {"reject", "rejected", "refuse", "refused", "decline", "declined", "wontfix"}
_ACCEPT_CJK = ("采纳", "确认", "同意", "已修", "认可")
_REJECT_CJK = ("驳回", "误报", "不修", "不采纳", "拒绝")


def parse_verdict_line(line: str) -> tuple[str, str, str] | None:
    """``(label, status, note)`` when one line is a verdict directive, else None.

    Deliberately narrow: a label token plus a verdict word. "对，改一下" is not a
    verdict, and guessing at it would put words in the maintainer's mouth.
    """
    label_match = _LABEL_RE.search(line)
    if not label_match:
        return None
    label = f"F-{int(label_match.group(1)):03d}"
    status = ""
    tail = ""
    for match in re.finditer(r"[A-Za-z]+|[\u4e00-\u9fff]+", line):
        word = match.group(0)
        key = word.lower()
        if key in _ACCEPT_WORDS:
            status = "accepted"
            tail = line[match.end() :]
            break
        if key in _REJECT_WORDS:
            status = "rejected"
            tail = line[match.end() :]
            break
        if any(alias in word for alias in _ACCEPT_CJK):
            status = "accepted"
            tail = line[match.end() :]
            break
        if any(alias in word for alias in _REJECT_CJK):
            status = "rejected"
            tail = line[match.end() :]
            break
    if not status:
        return None
    # The reason a rejected finding is useful to model routing rather than just a
    # lost point is the note, and only an explicit colon after the verdict word
    # introduces one: "F-005：已修" explains nothing, while "F-007 这条我同意，改一下"
    # would otherwise turn trailing prose into a justification nobody wrote.
    note_match = re.search(r"[:：]\s*(.+)$", tail)
    note = note_match.group(1).strip() if note_match else ""
    return label, status, note


def extract_verdicts(comments: list[dict]) -> list[dict]:
    """Verdict directives in posting order, our own comments excluded.

    A comment can carry several lines ("F-001 accept / F-002 reject: 误报"), and one
    finding can be revisited later in the thread; the caller applies last-write-wins
    per label, in chronological order.
    """
    out: list[dict] = []
    for comment in sorted(comments, key=lambda c: c.get("created_at") or ""):
        body = comment.get("body") or ""
        # The whole comment, not just the marker line: the summary *teaches* the
        # syntax with a literal "F-001 accept", and reading our own instructions
        # back as a maintainer's verdict would fabricate an adoption rate.
        if _OURS_RE.search(body):
            continue
        author = ((comment.get("user") or {}).get("login")) or "unknown"
        for line in body.splitlines():
            parsed = parse_verdict_line(line)
            if parsed is None:
                continue
            label, status, note = parsed
            out.append(
                {
                    "label": label,
                    "status": status,
                    "note": note,
                    "author": author,
                    "created_at": comment.get("created_at") or "",
                }
            )
    return out


def labels_from_comments(comments: list[dict]) -> dict[str, str]:
    """The label map from the newest summary comment that carries one.

    Older comments stay in the thread: two runs on one PR both use ``F-001``, so
    taking the union would map a label to two ids and adjudicate the wrong finding.
    """
    for comment in sorted(comments, key=lambda c: c.get("created_at") or "", reverse=True):
        match = _LABELS_MAP_RE.search(comment.get("body") or "")
        if not match:
            continue
        labels: dict[str, str] = {}
        for pair in match.group(1).split(","):
            if "=" not in pair:
                continue
            label, fid = pair.split("=", 1)
            if _LABEL_RE.fullmatch(label.strip()):
                labels[label.strip()] = fid.strip()
        if labels:
            return labels
    return {}


def collect_verdicts(
    pr: PullRequest,
    token: str,
    store: Any,
    labels: dict[str, str] | None = None,
    api: str = API_ROOT,
    dry_run: bool = False,
) -> dict:
    """Read maintainer verdicts off the PR and write them into the feedback store.

    ``store`` is duck-typed on purpose (``statuses_for`` / ``set_status``) so a test
    can hand in a fake. Idempotent: a verdict already recorded stays put.
    """
    comments = list_issue_comments(pr, token, api)
    embedded = labels_from_comments(comments)
    # The PR is what the maintainer was replying to, so its map wins; the local run
    # ledger only fills gaps left by reviews posted before the map existed.
    resolved = dict(labels or {})
    resolved.update(embedded)

    latest: dict[str, dict] = {}
    for verdict in extract_verdicts(comments):
        latest[verdict["label"]] = verdict

    previous = store.statuses_for(sorted(set(resolved.values()))) if resolved else {}
    result: dict[str, Any] = {
        "applied": [],
        "unchanged": 0,
        "unknown": [],
        "not_found": [],
        "labels_source": "pr" if embedded else ("local" if resolved else "none"),
    }
    for label in sorted(latest):
        verdict = latest[label]
        fid = resolved.get(label)
        if not fid:
            result["unknown"].append(label)
            continue
        reason = f"github:@{verdict['author']}"
        if verdict["note"]:
            reason = f"{reason}：{verdict['note']}"
        current = previous.get(fid) or {}
        if current.get("status") == verdict["status"]:
            result["unchanged"] += 1
            continue
        if not dry_run and not store.set_status(fid, verdict["status"], reason):
            result["not_found"].append(label)
            continue
        result["applied"].append(
            {
                "label": label,
                "finding_id": fid,
                "status": verdict["status"],
                "author": verdict["author"],
                "reason": reason,
                "dry_run": dry_run,
            }
        )
    return result
