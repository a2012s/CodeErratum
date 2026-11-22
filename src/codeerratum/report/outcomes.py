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

# Unresolved markers are checked first. "未修复" contains "修复" and "not fixed"
# contains "fixed"; without this pass they would be read as accepted.
_UNRESOLVED = (
    "未修复",
    "修复失败",
    "未完成",
    "失败",
    "阻塞",
    "无法修复",
    "无法解决",
    "not fixed",
    "unfixed",
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
    # "no fix needed" is a false-positive verdict, not an acceptance. Without
    # these, "无需修复" matched the accepted word "修复" and inflated the rate.
    "无需修复",
    "不需修复",
    "不需要修复",
    "不用修复",
    "不必修复",
    "没必要修复",
    "无需修改",
    "不需要修改",
    "已驳回",
    "驳回",
    "falsepositive",
    "false positive",
    "notapplicable",
    "not applicable",
    "n/a",
    "skipped",
    "skip",
    "notreproducible",
    "not reproducible",
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
    "partially fixed",
    "partial",
    "fixed",
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
    "not ",
    "n't ",
    "no ",
    "non",
    "un",
    "pending ",
)

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
        bool(filled)
        and all(set(c) <= set("-: ") for c in filled)
        and any("-" in c for c in filled)
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
    """Whether the match starting at ``pos`` is immediately preceded by a negation."""
    before = text[:pos]
    return any(before.endswith(prefix) for prefix in _NEG_PREFIXES)


def _has_accepted_word(text: str) -> bool:
    """True when an accepted word appears without a negation prefix.

    A single negated match must not be allowed to green-light the row just
    because a different accepted word appears somewhere else in the cell.
    """
    for word in _ACCEPTED:
        for match in re.finditer(re.escape(word), text):
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
    if any(marker in compact for marker in _UNRESOLVED):
        return None
    if any(word in compact for word in _REJECTED):
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
