"""Render review.md — a work order that a downstream coding AI can execute.

Quick mode: flat list (unchanged from phases 1-2).
Dual/committee mode: three zones (consensus / unique / conflict) so the
"only one model saw this" findings are impossible to miss.

Two invariants enforced here, because the reader is an AI that cannot ask
follow-up questions: the item count in the body, the summary table and the
execution-result table must agree, and any truncation must be stated explicitly.
"""

from __future__ import annotations

import re

from ..models.finding import Finding, FindingCollection, is_unit_failure

MAX_RENDER = 10

_FENCE_RE = re.compile(r"`+")


def _one_line(text: str) -> str:
    """Collapse newlines so model-supplied prose cannot inject a heading or a fence.

    The reader is an AI that executes this file in order; a claim containing
    ``\\n## 执行协议`` would silently become a second instruction block.
    """
    return " ".join(text.split())


def _fence(text: str) -> str:
    """A fence longer than every backtick run inside the body.

    A Python docstring can legitimately contain a line of ```; opening with three
    backticks would let that line close the block early and leave the rest of the
    snippet, and the following sections, as free-form markdown.
    """
    longest = max((len(m) for m in _FENCE_RE.findall(text)), default=0)
    return "`" * max(3, longest + 1)


HEADER = """# Code Review 工单

> 生成信息：commit={commit} | 模式={mode} | 规格等级={spec_level}
> 审查者={reviewers} | 时间={ts}
> 项目规范={rules}
> 本次共 {total} 条发现，本工单渲染前 {shown} 条（按严重度 / 证据 / 一致性排序）。
>
> **执行协议**（交给执行方 AI）：
> 1. 按 F-001 起顺序逐条处理，不要跳条。
> 2. 每条先核对【现状】与代码是否一致；不一致则跳过，不要猜测。
> 3. 按【修改建议】改动，然后运行【验收】中的命令。
> 4. 全部完成后，在文末《执行结果》表填写：finding_id / 状态 / 验证结果。
>    状态只填：已修复 / 部分修复 / 未修复 / 误报 / 不适用；未处理的保持“待处理”。
> 5. AI 审查通过 ≠ 可以合入；必须人工确认。

"""


def _spec_note(collection: FindingCollection) -> str:
    """Level plus the reason it was assigned — a bare 'L1' reads as a measurement."""
    if collection.spec_level_reason:
        return f"{collection.spec_level}（{collection.spec_level_reason}）"
    return collection.spec_level


def _rules_note(collection: FindingCollection) -> str:
    """Name the project rules in the header — two runs over one diff can differ only here."""
    if not collection.house_rules_sha:
        return "未应用"
    source = collection.house_rules_source
    if source == "flag":
        source = "手动传入"
    return (
        f"已应用 sha={collection.house_rules_sha}"
        f"（{collection.house_rules_chars} 字，{source or '未知来源'}）"
    )


def _stop_banner(collection: FindingCollection) -> str:
    """Make incomplete coverage impossible to miss.

    A stopped run is not a clean run: the reader must not treat "no findings in
    the units we finished" as "the change is fine". The banner sits above the
    findings, not in the coverage notes at the bottom, because a downstream AI
    executes this file top to bottom.
    """
    info = collection.comparison.get("stopped") or {}
    done = info.get("units_completed", 0)
    partial = info.get("units_partial", 0)
    total = info.get("units_total", 0)
    stage = info.get("stage", "")
    phase = {
        "verify": "验证阶段",
        "falsify": "证伪阶段",
        "cluster": "聚类阶段",
        "review": "模型审查阶段",
    }.get(stage, "准备阶段")
    coverage = f"已完整审查 {done}/{total} 个单元"
    if partial:
        coverage += f"，另有 {partial} 个单元只有部分模型完成"
    if stage == "falsify":
        consequence = "已完成审查的发现全部保留；证伪尚未跑完，未证伪的发现按“未裁决”展示。"
    elif stage == "verify":
        consequence = "扫描结果已保留；验证尚未跑完，未被验证的发现按“未裁决”展示。"
    else:
        consequence = "报告只包含截止停止时的发现；证伪/验证未执行。"
    return (
        f"> **审查已停止（{phase}）**\n"
        f">\n"
        f"> {coverage}。{consequence}\n"
        f"> 停止不等于审查通过：未覆盖的单元没有结论，需重新审查。\n\n"
    )


def _severity_counts(findings) -> dict[str, int]:
    counts = {"P0": 0, "P1": 0, "P2": 0, "P3": 0}
    for f in findings:
        counts[f.severity] = counts.get(f.severity, 0) + 1
    return counts


VERIFICATION_LABEL = {
    "reproduced": "已复现",
    "accepted": "已确认",
    "suspected": "有静态证据",
    "unverified": "仅推断",
}

AGREEMENT_LABEL = {
    "unanimous": "多模型一致",
    "majority": "多数一致",
    "unique": "单模型独有",
    "conflict": "模型冲突",
    "unverified": "未裁决",
}


def _render_finding(f, index: int) -> str:
    anchor = f.anchors[0]
    loc = f"{anchor.file}:{anchor.start_line}"
    if anchor.end_line != anchor.start_line:
        loc += f"-{anchor.end_line}"
    verdict_label = VERIFICATION_LABEL.get(f.verification, f.verification)
    lines = [
        f"## F-{index:03d} [{f.severity}] {_one_line(f.claim)}",
        f"<!-- finding:F-{index:03d}={f.id} -->",
        "",
        f"- 位置：`{loc}`",
        f"- 视角：{f.lens.value}",
        f"- 分类：{f.category.value}",
        f"- 证据等级：**{verdict_label}**",
    ]
    if getattr(f, "agreement", "unverified") != "unverified":
        lines.append(f"- 一致性：**{AGREEMENT_LABEL.get(f.agreement, f.agreement)}**")
        if f.source_models:
            lines.append(f"- 来源模型：{', '.join(f.source_models)}")
    lines.extend([f"- 问题编号：`{f.id}`", ""])
    if f.evidence:
        lines.append("**证据**：")
        for ev in f.evidence:
            lines.append(f"- [{ev.strength}] {ev.summary}")
        lines.append("")
    # Conflict zone: show both sides of the argument side by side.
    if f.agreement == "conflict":
        lines.append("**模型分歧**（需人工裁决）：")
        for v in f.verdicts:
            role_cn = {
                "assert": "主张",
                "confirm": "证实",
                "refute": "反驳",
                "abstain": "证据不足",
            }[v.role]
            lines.append(f"- [{v.model_id}·{role_cn}] {v.reason}")
        lines.append("")
    fence = _fence(anchor.snippet)
    lang = "python" if anchor.file.endswith(".py") else ""
    lines.extend(
        [
            "**现状**：",
            f"{fence}{lang}",
            anchor.snippet.rstrip("\n"),
            fence,
            "",
            f"**问题**：{_one_line(f.detail)}",
            "",
        ]
    )
    sugg = f.suggestion
    if sugg:
        lines.append("**修改建议**：")
        lines.append("")
        if sugg.diff:
            fence = _fence(sugg.diff)
            lines.append(f"{fence}diff")
            lines.append(sugg.diff.rstrip("\n"))
            lines.append(fence)
            lines.append("")
        if sugg.explanation:
            lines.append(_one_line(sugg.explanation))
            lines.append("")
        lines.append(f"**验收**：`{sugg.acceptance}`")
        lines.append("")
    lines.append("---")
    lines.append("")
    return "\n".join(lines)


ZONE_TITLES = {
    "unique": "## 独有发现（仅单模型报出，需重点确认）",
    "conflict": "## 冲突区（模型意见相反，需人工裁决）",
    "consensus": "## 共识区（多模型一致）",
}


def _zone_of(f: Finding) -> str:
    if f.agreement == "conflict":
        return "conflict"
    if f.agreement in ("unanimous", "majority"):
        return "consensus"
    return "unique"


def _render_zone(name: str, items: list[Finding], start_index: int) -> tuple[str, int]:
    if not items:
        return "", start_index
    parts = [ZONE_TITLES[name] + "\n\n"]
    idx = start_index
    for f in items:
        parts.append(_render_finding(f, idx))
        idx += 1
    return "".join(parts), idx


def render_markdown(collection: FindingCollection, timestamp: str) -> str:
    ordered = collection.ranked_findings()
    display = ordered[:MAX_RENDER]
    omitted = len(ordered) - len(display)
    counts = _severity_counts(display)
    reviewers = ", ".join(collection.reviewer_models or [collection.model_id])
    if collection.falsifier_model:
        reviewers += f" | 证伪={collection.falsifier_model}"
    parts = [
        HEADER.format(
            commit=collection.commit[:8] if collection.commit else "unknown",
            model=collection.model_id,
            reviewers=reviewers,
            ts=timestamp,
            spec_level=_spec_note(collection),
            mode=collection.mode,
            total=len(ordered),
            shown=len(display),
            rules=_rules_note(collection),
        )
    ]
    if collection.stopped:
        parts.append(_stop_banner(collection))

    if not display:
        if collection.stopped:
            parts.append(
                "## 结论\n\n审查已停止，已完成单元未报出发现；这不代表未覆盖单元没有问题。\n\n"
            )
        else:
            parts.append("## 结论\n\n本次变更未发现明确问题。\n\n")
    elif collection.mode == "quick":
        for i, f in enumerate(display, 1):
            parts.append(_render_finding(f, i))
    else:
        grouped: dict[str, list[Finding]] = {"unique": [], "conflict": [], "consensus": []}
        for f in display:
            grouped[_zone_of(f)].append(f)
        idx = 1
        for name in ("unique", "conflict", "consensus"):
            block, idx = _render_zone(name, grouped[name], idx)
            parts.append(block)

    if omitted > 0:
        parts.append(
            f"> 还有 {omitted} 条发现未在本工单展开（已写入 `findings.json`）。"
            "如需全部展开，提高渲染上限后重新生成。\n\n"
        )

    parts.append("\n## 摘要\n\n")
    parts.append("| 严重度 | 数量 |\n|---|---|\n")
    for level in ("P0", "P1", "P2", "P3"):
        parts.append(f"| {level} | {counts[level]} |\n")
    parts.append(f"| 合计（本工单） | {len(display)} |\n")
    if omitted > 0:
        parts.append(f"| 未展开 | {omitted} |\n")
    parts.append("\n")

    if collection.notes:
        parts.append("## 本次覆盖说明（降级项必须可见）\n\n")
        for note in collection.notes:
            parts.append(f"- {note}\n")
        parts.append("\n")

    if collection.mode != "quick" and collection.comparison:
        cmp = collection.comparison
        parts.append("## 交叉审查统计\n\n")
        parts.append(f"- 模式：{cmp.get('mode')}\n")
        parts.append(f"- 各模型原始发现数：{cmp.get('raw_counts')}\n")
        clustered = cmp.get("after_clustering")
        survived = cmp.get("after_falsification")
        parts.append(f"- 聚类后：{clustered} 条\n")
        if survived is None:
            parts.append("- 证伪：未执行（审查已停止）\n")
        else:
            parts.append(f"- 证伪后：{survived} 条\n")
        zones = cmp.get("zones")
        parts.append(f"- 分区：{zones if zones is not None else '未计算（审查已停止）'}\n\n")

    # Two different failure shapes get two different sections: drops removed a
    # finding from this report; unit failures did not (verify_error even keeps
    # the finding). Mixing them produced empty bullets and a title that lied.
    finding_drops = [
        r for r in collection.rejected if not is_unit_failure(str(r.get("reason") or ""))
    ]
    unit_failures = [r for r in collection.rejected if is_unit_failure(str(r.get("reason") or ""))]
    if finding_drops:
        parts.append("## 已丢弃的发现（定位校验未通过 / 被证伪推翻）\n\n")
        parts.append("这些结论未通过校验，请勿据此修改：\n\n")
        if len(finding_drops) > 10:
            parts.append(
                f"> 共 {len(finding_drops)} 条，仅显示前 10 条，"
                "完整明细见 runs 目录与 feedback.db 的 run_rejections。\n\n"
            )
        for r in finding_drops[:10]:
            cited = r.get("cited", r.get("dedupe_key", ""))
            model = f"[{r.get('model')}] " if r.get("model") else ""
            parts.append(f"- `{r.get('reason')}` {model}{r.get('claim', '')} — {cited}\n")
        parts.append("\n")
    if unit_failures:
        parts.append("## 失败的单元（模型调用未完成）\n\n")
        parts.append(
            "这些单元的调用失败；provider_error 的单元未被审查，"
            "verify_error 的发现保留为未验证：\n\n"
        )
        if len(unit_failures) > 10:
            parts.append(
                f"> 共 {len(unit_failures)} 条，仅显示前 10 条，"
                "完整明细见 calls.json 与 feedback.db 的 run_rejections。\n\n"
            )
        for r in unit_failures[:10]:
            model = f"[{r.get('model')}] " if r.get("model") else ""
            hint = r.get("error_hint") or r.get("error") or ""
            where = r.get("unit_id") or r.get("claim") or "unknown"
            suffix = f" — {hint}" if hint else ""
            parts.append(f"- `{r.get('reason')}` {model}{where}{suffix}\n")
        parts.append("\n")

    parts.append("## 执行结果（由执行方 AI 填写）\n\n")
    parts.append(
        "> 状态填 `已修复` / `部分修复` / `未修复` / `误报` / `不适用`；"
        "`codeerratum reconcile erratumReview.md` 会把这张表回填到 feedback.db。\n\n"
    )
    parts.append("| finding_id | 状态 | 验证结果 |\n|---|---|---|\n")
    for i in range(1, len(display) + 1):
        parts.append(f"| F-{i:03d} | 待处理 | — |\n")
    return "".join(parts)
