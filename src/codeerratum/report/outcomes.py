"""Read the work order's execution-result table back into the feedback store.

Until now the table was write-only: a downstream coding agent filled it in and
the acceptance rate stayed at whatever a human remembered to type into
``accept``/``reject``. That measured bookkeeping, not finding quality. This
module maps the table back to finding ids and turns the executor's vocabulary
into the store's three states.

The mapping uses the hidden ``<!-- finding:F-001=<id> -->`` marker emitted by
the renderer, with the visible ``问题编号`` line as a fallback for reports
generated before the marker existed. Unknown statuses are reported, never
guessed: silently accepting "看起来改了" would poison the one number the
product claims to improve.

Classification is deliberately three-stage, in this order:

1. Whole-cell negated verdicts (``_REJECTED_NEGATIONS`` / ``_ACCEPTED_NEGATIONS``)
   cover compositional English such as ``nothing to fix``. They match the
   entire cell so a later unresolved clause cannot be overridden.
2. Unresolved markers win over any positive word: the fixed phrase lists
   (``_UNRESOLVED`` / ``_UNRESOLVED_TRAILING``) hold only non-derivable
   vocabulary, while ``_NEG_PREFIXES`` / ``_EN_NEGATION_RE`` handle combinable
   *preceding* negation and ``_UNRESOLVED_TRAILING`` handles *following*
   qualifiers ("fixed without full verification").
3. Only then are accepted/rejected words matched, with the same negation
   checks applied at each match position.

Keeping fixed phrases and combinable rules separate matters: dumping every
negation variant into a flat list is how ``未通过`` used to be read as a
pass, and how a new qualifier silently inflates the acceptance rate.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Protocol

_MARKER_RE = re.compile(r"<!--\s*finding:(F-\d{3})=([0-9a-f]{6,})\s*-->", re.IGNORECASE)
_FINDING_HEADING_RE = re.compile(r"(?m)^##\s+(F-\d{3})\b[^\n]*$", re.IGNORECASE)
_PROSE_ID_RE = re.compile(
    r"(?:问题编号|问题\s*ID|Finding\s*ID|finding_id)\s*[:：]\s*`?([0-9a-f]{6,})`?",
    re.IGNORECASE,
)
_RESULT_HEADING_RE = re.compile(r"(?m)^##\s*(?:执行结果|Execution results?)[^\n]*$", re.IGNORECASE)
_NEXT_HEADING_RE = re.compile(r"(?m)^##\s+")
_HEX_RE = re.compile(r"^[0-9a-f]{6,}$", re.IGNORECASE)

# Fixed, non-derivable unresolved phrases only. Combinable negation belongs to
# the rules below; adding "not <positive>" variants here would duplicate them
# and hide future gaps in the rule path.
# Unresolved markers are checked first. "未修复" contains "修复" and "not fixed"
# contains "fixed"; without this pass they would be read as accepted.
_UNRESOLVED = (
    "未修复",
    "修复失败",
    "未完成",
    "修复中",
    "修复进行中",
    "正在修复",
    "未通过",
    "没通过",
    "不通过",
    "未测试",
    "未验证",
    "没验证",
    "待验证",
    "待测试",
    "待复测",
    "待回归",
    "待确认",
    "待合并",
    "测试失败",
    "回归失败",
    "失败",
    "阻塞",
    "无法修复",
    "无法解决",
    "not fixed",
    "not yet fixed",
    "not done",
    "not resolved",
    "not accepted",
    "not confirmed",
    "cannot be fixed",
    "can't be fixed",
    "cant be fixed",
    "non-fixed",
    "unfixed",
    "not passing",
    "did not pass",
    "didn't pass",
    "does not pass",
    "doesn't pass",
    "not tested",
    "tests fail",
    "test failure",
    "tests failure",
    "regression failure",
    "none of the tests",
    "not verified",
    "pending verification",
    "awaiting verification",
    "needs verification",
    "need verification",
    "requires verification",
    "to be verified",
    "pending review",
    "awaiting review",
    "needs review",
    "needs testing",
    "requires testing",
    "still broken",
    "still failing",
    "failed",
    "blocked",
    "incomplete",
)
_REJECTED = (
    "误报",
    "假阳性",
    "不适用",
    "已跳过",
    "跳过",
    "未复现",
    "无法复现",
    "不存在",
    "非问题",
    "不是问题",
    "不是缺陷",
    "非缺陷",
    "不算问题",
    "不构成问题",
    "没有问题",
    # "no fix needed" is a false-positive verdict, not an acceptance. Without
    # these, "无需修复" matched the accepted word "修复" and inflated the rate.
    "无需修复",
    "不需修复",
    "不需要修复",
    "不用修复",
    "不必修复",
    "没必要修复",
    "无须修复",
    "不须修复",
    "毋须修复",
    "勿需修复",
    "无需修改",
    "不需要修改",
    "无须修改",
    "不须修改",
    "无需改动",
    "不需要改动",
    "不用改",
    "无需改",
    "不必改",
    "不需要改",
    "没必要改",
    "无需处理",
    "无须处理",
    "不用处理",
    "不需要处理",
    "不必处理",
    "已忽略",
    "已驳回",
    "驳回",
    "falsepositive",
    "false positive",
    "falsealarm",
    "false alarm",
    "duplicate",
    "notapplicable",
    "not applicable",
    "n/a",
    "skipped",
    "skip",
    "notreproducible",
    "not reproducible",
    "cannot reproduce",
    "can't reproduce",
    "cant reproduce",
    "could not reproduce",
    "couldn't reproduce",
    "unable to reproduce",
    "无法重现",
    "未重现",
    "not a bug",
    "no bug",
    "not a problem",
    "no problem",
    "not a vulnerability",
    "no issue",
    "not an issue",
    "not a real issue",
    "isn't an issue",
    "is not an issue",
    "invalid",
    "wontfix",
    "won't fix",
    "no fix needed",
    "not needed",
    "not necessary",
    "not required",
)
_ACCEPTED = (
    "已修复",
    "部分修复",
    "修复",
    "已解决",
    "已处理",
    "已完成",
    "完成",
    "已通过",
    "通过",
    "成功",
    "已确认",
    "确认",
    "属实",
    "已采纳",
    "采纳",
    # "已复现" is the highest evidence grade the report itself hands the
    # executor (VERIFICATION_LABEL); leaving it out of the vocabulary made
    # exactly those rows read as "needs a human" and silently vanish from the
    # acceptance stats.
    "已复现",
    "复现成功",
    "reproduced",
    "partially fixed",
    "partial",
    "fixed",
    "merged",
    "已合并",
    "改完",
    "done",
    "resolved",
    "passed",
    "pass",
    "success",
    "succeeded",
    "accepted",
    "confirmed",
    "verified",
    "true positive",
)
# A short accepted word is a substring of its negative forms: "通过" lives in
# "未通过", "done" in "not done". Checking these prefixes at the match position
# is what keeps a negated verdict from being counted as an acceptance.
_NEG_PREFIXES = (
    "不是",
    "并非",
    "无法",
    "未能",
    "尚未",
    "没有",
    "未被",
    "不予",
    "无需",
    "不需",
    "不用",
    "不必",
    "未",
    "不",
    "无",
    "没",
    "待",
    "尚",
    "非",
)
# English negation prefixes need a word boundary: plain endswith("un") read
# "...tests run" (and "begun", "rerun") as a negation of the verdict that
# follows, silently dropping real acceptances.
_EN_PREFIX_RE = re.compile(r"(?:^|[\s\"'\(\[-])(?:not|no|non|un|pending|n't)[\s-]*$", re.IGNORECASE)

# Qualifiers that follow a positive verdict. "已修复"/"fixed" alone is evidence
# of acceptance, but "部分修复但仍有问题" and "fixed without full verification"
# explicitly withhold it. Checked alongside _UNRESOLVED before any positive word.
_UNRESOLVED_TRAILING = (
    "without full verification",
    "without verification",
    "without testing",
    "without regression test",
    "without a regression test",
    "not fully verified",
    "not fully tested",
    "but not verified",
    "but not tested",
    "but still",
    "但仍",
    "但尚",
    "但问题仍在",
    "仍有待",
    "仍待",
    "尚待",
)

_CLAUSE_BREAK_RE = re.compile(r"[，,。.;；:：!?！？\n]")
# Preceding negation inside one clause: "not a false positive", "not yet fixed".
_EN_NEGATION_RE = re.compile(
    r"(?:\b(?:not|no|none|nothing|neither|nor|never|cannot|can't|cant|isn't|"
    r"wasn't|weren't|hasn't|haven't|hadn't|won't|wouldn't|doesn't|didn't|don't)\b"
    r"[\s-]*(?:\w+[\s-]+){0,2})$",
    re.IGNORECASE,
)

# Compositional whole-cell verdicts. These are matched against the *entire*
# cell: "no test failed" is positive even though it contains "failed", while
# "no test failed but not fixed" must still fall through to the unresolved
# pass. Keeping them here also means "not a bug" no longer depends on a single
# flat-list entry (see the meta-test in test_outcomes.py).
_REJECTED_NEGATIONS = (
    re.compile(r"nothing\s+to\s+(?:fix|change|modify|do)\b", re.IGNORECASE),
    re.compile(
        r"no\s+(?:changes?|fix(?:es)?|modifications?|adjustments?)\s+"
        r"(?:needed|required|necessary)\b",
        re.IGNORECASE,
    ),
    re.compile(r"nothing\s+(?:needs?|requires?)\s+(?:fixing|changing)\b", re.IGNORECASE),
    re.compile(
        r"no\s+longer\s+(?:an?\s+)?(?:issue|bug|problem|vulnerability|concern)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"none\s+of\s+these\s+(?:are\s+|is\s+)?(?:issues|problems|bugs|defects)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?:not|isn't|is\s+not|wasn't|was\s+not|isnt|wasnt)\s+"
        r"(?:really\s+|actually\s+|real\s+)?(?:a\s+|an\s+)?"
        # "not a false positive" is deliberately absent: that is a *denial of
        # rejection*, which the clause-level negation below leaves open rather
        # than turning into a rejection of the finding itself.
        r"(?:bug|issue|problem|vulnerability)\b",
        re.IGNORECASE,
    ),
)
_ACCEPTED_NEGATIONS = (
    re.compile(r"no\s+tests?\s+(?:failed|fail|failing)\b", re.IGNORECASE),
    re.compile(r"no\s+(?:test|regression)\s+failures?\b", re.IGNORECASE),
    re.compile(r"no\s+failures?\b", re.IGNORECASE),
    re.compile(r"all\s+tests?\s+passed\b", re.IGNORECASE),
    re.compile(r"all\s+checks?\s+passed\b", re.IGNORECASE),
)


def _matches_whole_cell(patterns: tuple[re.Pattern[str], ...], text: str) -> bool:
    """True when a negation pattern describes the whole status cell."""
    cell = text.strip(" \t\"'`()[]{}.,;:!?。！，；：、")
    return any(pattern.fullmatch(cell) for pattern in patterns)


_PENDING = ("", "待处理", "待定", "处理中", "排队中", "pending", "todo", "in progress", "open")
_EMPTY_CELLS = {"", "-", "--", "—", "–", "n/a", "na", "none", "无"}


@dataclass
class OutcomeRow:
    """One row of the execution table, after reference resolution."""

    ref: str
    status_raw: str
    verification: str
    finding_id: str | None = None
    status: str | None = None
    reason: str = ""


@dataclass
class ParseResult:
    rows: list[OutcomeRow] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    found_table: bool = False


class StatusStore(Protocol):
    def statuses_for(self, finding_ids: list[str]) -> dict[str, dict]: ...

    def set_status(self, finding_id: str, status: str, reason: str = "") -> bool: ...


def finding_ids(markdown: str) -> dict[str, str]:
    """Map ``F-001`` to the full content hash, marker first, prose second."""
    out: dict[str, str] = {}
    for match in _MARKER_RE.finditer(markdown):
        out[match.group(1).upper()] = match.group(2).lower()
    headings = list(_FINDING_HEADING_RE.finditer(markdown))
    for i, match in enumerate(headings):
        ref = match.group(1).upper()
        if ref in out:
            continue
        end = headings[i + 1].start() if i + 1 < len(headings) else len(markdown)
        prose = _PROSE_ID_RE.search(markdown, match.end(), end)
        if prose:
            out[ref] = prose.group(1).lower()
    return out


def _execution_section(markdown: str) -> str | None:
    match = _RESULT_HEADING_RE.search(markdown)
    if match is None:
        return None
    rest = markdown[match.end() :]
    nxt = _NEXT_HEADING_RE.search(rest)
    return rest[: nxt.start()] if nxt else rest


def _split_row(line: str) -> list[str]:
    text = line.strip()
    if text.startswith("|"):
        text = text[1:]
    if text.endswith("|") and not text.endswith("\\|"):
        text = text[:-1]
    cells: list[str] = []
    buf: list[str] = []
    escaped = False
    for ch in text:
        if escaped:
            buf.append(ch)
            escaped = False
        elif ch == "\\":
            escaped = True
        elif ch == "|":
            cells.append("".join(buf).strip())
            buf = []
        else:
            buf.append(ch)
    cells.append("".join(buf).strip())
    return cells


def _is_separator(cells: list[str]) -> bool:
    filled = [c.strip() for c in cells if c.strip()]
    return (
        bool(filled) and all(set(c) <= set("-: ") for c in filled) and any("-" in c for c in filled)
    )


def _table_rows(section: str) -> list[list[str]]:
    rows: list[list[str]] = []
    for line in section.splitlines():
        if not line.strip().startswith("|"):
            continue
        cells = _split_row(line)
        if _is_separator(cells):
            continue
        rows.append(cells)
    return rows


def _header_key(text: str) -> str:
    return re.sub(r"[\s_`*]+", "", text.strip().lower())


_REF_HEADERS = {"findingid", "finding", "id", "编号", "问题编号"}
_STATUS_HEADERS = {"状态", "status", "结果", "result"}
_VERIFY_HEADERS = {
    "验证结果",
    "验证",
    "verification",
    "verify",
    "备注",
    "说明",
    "note",
    "notes",
    "details",
}


def _column_index(header: list[str]) -> dict[str, int]:
    idx: dict[str, int] = {}
    for i, cell in enumerate(header):
        key = _header_key(cell)
        if key in _REF_HEADERS and "ref" not in idx:
            idx["ref"] = i
        elif key in _STATUS_HEADERS and "status" not in idx:
            idx["status"] = i
        elif key in _VERIFY_HEADERS and "verify" not in idx:
            idx["verify"] = i
    return idx


def _negated_at(text: str, pos: int) -> bool:
    """Whether the match starting at ``pos`` is negated in its own clause.

    English can put a short phrase between the negator and the verdict ("not
    yet fixed", "cannot be fixed"), while a comma usually starts a new verdict
    ("not a false positive, fixed"). Stop at clause punctuation so the earlier
    negation cannot cancel the later positive evidence.
    """
    before = text[:pos]
    breaks = list(_CLAUSE_BREAK_RE.finditer(before))
    clause = before[breaks[-1].end() :] if breaks else before
    if any(clause.endswith(prefix) for prefix in _NEG_PREFIXES):
        return True
    if _EN_PREFIX_RE.search(clause):
        return True
    return bool(_EN_NEGATION_RE.search(clause))


def _word_pattern(word: str) -> str:
    """Whole-word regex for ASCII vocabulary; substring for Chinese.

    Chinese verdicts are compositional without separators ("未通过" contains
    "通过"), so substring matching is required there. English words glued into
    longer tokens must not match, or "bypassed" counts as a "pass".
    """
    if word.isascii():
        return r"(?<![A-Za-z])" + re.escape(word) + r"(?![A-Za-z])"
    return re.escape(word)


def _has_rejected_word(text: str) -> bool:
    """True when a rejection phrase is present without its own negation."""
    for word in _REJECTED:
        for match in re.finditer(_word_pattern(word), text):
            if not _negated_at(text, match.start()):
                return True
    return False


def _has_accepted_word(text: str) -> bool:
    """True when an accepted word appears without a negation prefix.

    A single negated match must not be allowed to green-light the row just
    because a different accepted word appears somewhere else in the cell.
    """
    for word in _ACCEPTED:
        for match in re.finditer(_word_pattern(word), text):
            if not _negated_at(text, match.start()):
                return True
    return False


def classify_status(raw: str) -> str | None:
    """Return ``accepted`` / ``rejected``, or ``None`` when the row needs a human.

    ``None`` covers both "still pending" and "tried but not fixed": the executor
    did not produce evidence that the finding was real or false, and flipping
    either way would corrupt the acceptance rate.
    """
    text = re.sub(r"[`*_]+", "", raw).strip().lower()
    if not text:
        return None
    compact = re.sub(r"\s+", " ", text)
    # Whole-cell compositional verdicts first: "no test failed" is positive
    # despite containing the unresolved word "failed". Matching the full cell
    # prevents a later qualifier ("... but not fixed") from being overruled.
    if _matches_whole_cell(_ACCEPTED_NEGATIONS, compact):
        return "accepted"
    if _matches_whole_cell(_REJECTED_NEGATIONS, compact):
        return "rejected"
    if any(marker in compact for marker in _UNRESOLVED):
        return None
    if any(marker in compact for marker in _UNRESOLVED_TRAILING):
        return None
    if _has_rejected_word(compact):
        return "rejected"
    if _has_accepted_word(compact):
        return "accepted"
    if compact in _PENDING:
        return None
    return None


def _cell(cells: list[str], index: int | None) -> str:
    if index is None or index < 0 or index >= len(cells):
        return ""
    return cells[index].strip()


def _reason(raw: str, verification: str) -> str:
    parts = [p for p in (raw.strip(), verification.strip()) if p and p.lower() not in _EMPTY_CELLS]
    return " | ".join(parts)[:1000]


def parse_outcomes(markdown: str) -> ParseResult:
    """Parse the execution table and resolve every ``F-###`` to a finding id."""
    result = ParseResult()
    section = _execution_section(markdown)
    if section is None:
        return result
    result.found_table = True
    rows = _table_rows(section)
    if not rows:
        return result

    ids = finding_ids(markdown)
    idx = _column_index(rows[0])
    if idx:
        data = rows[1:]
        ref_i, status_i, verify_i = idx.get("ref"), idx.get("status"), idx.get("verify")
    else:
        data = rows
        ref_i, status_i, verify_i = 0, 1, 2

    for cells in data:
        ref = _cell(cells, ref_i).strip("`* ")
        if not ref:
            continue
        status_raw = _cell(cells, status_i)
        verification = _cell(cells, verify_i)
        ref_key = ref.upper()
        finding_id = ids.get(ref_key)
        if finding_id is None and _HEX_RE.fullmatch(ref):
            finding_id = ref.lower()
        row = OutcomeRow(
            ref=ref,
            status_raw=status_raw,
            verification=verification,
            finding_id=finding_id,
            status=classify_status(status_raw),
            reason=_reason(status_raw, verification),
        )
        if finding_id is None:
            result.problems.append(f"unknown finding reference: {ref}")
        result.rows.append(row)
    return result


def apply_outcomes(store: StatusStore, rows: list[OutcomeRow], dry_run: bool = False) -> dict:
    """Write resolved rows; the last row for one finding wins.

    A downstream agent that corrects itself usually appends a second row rather
    than editing the first. Treating the last row as authoritative is the only
    reading that does not make the order of the table meaningless.
    """
    counts = {
        "rows": len(rows),
        "updated": 0,
        "accepted": 0,
        "rejected": 0,
        "skipped": 0,
        "unmatched": 0,
    }
    latest: dict[str, OutcomeRow] = {}
    for row in rows:
        if row.finding_id is None:
            counts["unmatched"] += 1
            continue
        latest[row.finding_id] = row

    known = store.statuses_for(list(latest))
    for finding_id, row in latest.items():
        if finding_id not in known:
            counts["unmatched"] += 1
            continue
        if row.status is None:
            counts["skipped"] += 1
            continue
        if not dry_run:
            # A concurrent delete between statuses_for() and set_status() makes
            # rowcount 0. Counting that as updated would report writes that
            # never landed.
            if not store.set_status(finding_id, row.status, row.reason):
                counts["unmatched"] += 1
                continue
        counts["updated"] += 1
        counts[row.status] += 1
    return counts
