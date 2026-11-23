# CodeErratum 0.6.5 复核报告

复核日期：2026-11-23

## 结论摘要

0.6.5 的方向是对的：tiered 验证器异常不再只留在 `calls.json`，报告 notes 开始汇总机器丢弃，Web 标签从“被拒”改为“机器丢弃”，负数 `--max-units` 也从“静默全量”改为明确报错。测试没有新增回归，桌面应用确实是 0.6.5。

但这一版还有一个影响可信度的口径矛盾：`verify_error` 被写进 `collection.rejected`，因此被统计为“机器丢弃的发现”，可同一版新增的测试又明确要求验证器失败时 scanner 的 finding 必须保留。结果是同一条 finding 既在 `findings` 里，又被计入 `n_dropped` / `dropped_total` / “机器丢弃 N 条”，指标会说谎。

建议把 0.6.5 视为“方向正确、可继续迭代”，在 0.6.6 里优先修掉下面三个 P2：验证失败与发现丢弃分离、报告 Markdown 口径同步、`units_failed` 提示语区分“未审查”和“验证失败但发现保留”。

本轮只做复核并形成本文档，没有修改业务代码，也没有提交代码。

## 事实源与验证

| 项目 | 结果 |
|---|---|
| HEAD | `412a865 fix(report): 机器丢弃口径贯通展示层，负数预算报错（0.6.5）` |
| 上一版 | `fe65791 feat(mcp): 补齐闭环工具与 quick/tiered 解析健康度（0.6.4）` |
| 版本号 | `src/codeerratum/__init__.py` 中 `__version__ = "0.6.5"` |
| 开始复核时工作树 | 干净 |
| 全量测试 | `490 passed, 5 failed in 64.59s` |
| ruff | `All checks passed!` |
| 桌面应用 | `--selftest` 输出 `version: 0.6.5 | frozen: True`，`SELFTEST OK` |

5 个失败全部是 `AGENTS.md` 已声明的既有基线，不是 0.6.5 新回归：

- `tests/test_desktop.py::test_seeding_a_repo_keeps_the_projects_already_saved`
- `tests/test_hub.py::test_add_dedupes_and_validates`
- `tests/test_hub.py::test_add_and_remove_project_over_http`
- `tests/test_snapshot.py::test_commit_relative_specs_mean_everything_only_without_history`
- `tests/test_web.py::test_pipeline_refuses_an_unknown_revision_in_a_plain_folder`

验证命令：

```bash
mkdir -p .pytest_tmp/local
.venv/bin/python -m pytest -q --basetemp=.pytest_tmp/local
.venv/bin/ruff check src tests
/Applications/CodeErratum.app/Contents/MacOS/CodeErratum --selftest
```

## 0.6.5 改动逐项核验

| 变更 | 是否落实 | 证据 |
|---|---|---|
| tiered verifier 抛异常时写入 `rejected`，reason 为 `verify_error` | 已落实 | `review/orchestrator.py:487-500` |
| 报告 notes 汇总机器丢弃总数和前三个原因 | 已落实 | `pipeline.py:610-627`，`i18n.py:348-351` |
| Web 运行详情“被拒”改为“机器丢弃” | 已落实 | `web/static/index.html:699, 1157, 1177-1180` |
| `list_runs` 新增 `n_dropped`，取自 `run_rejections` | 已落实 | `store/sqlite.py:320-340` |
| `--max-units` 负数抛 `ConfigError`，0 才表示全量 | 已落实 | `pipeline.py:237-250` |
| 新增 verifier 失败、负数预算、`n_dropped`、notes 汇总测试 | 已落实 | `tests/test_orchestration.py:390`、`tests/test_outcomes.py:368`、`tests/test_units_budget.py:116,137` |
| 桌面应用重打包并自检 | 已核实 | selftest 返回 0.6.5 |

## 发现

### P2-1　`verify_error` 被统计为“丢弃的发现”，但 finding 实际仍保留

证据链：

1. 验证器异常时，代码把一条 `verify_error` 写进 `all_rejected`：`src/codeerratum/review/orchestrator.py:487-500`。
2. 但 `FindingCollection.rejected` 的字段定义仍是“Findings dropped by anchor check”：`src/codeerratum/models/finding.py:234-236`。
3. 同一版新增测试明确断言 scanner 的 finding 不能因为验证器失败而消失：`tests/test_orchestration.py:436-437`。
4. 保存运行时，`collection.rejected` 会把 `n_rejected` 和 `run_rejections` 一起写大：`src/codeerratum/store/sqlite.py:108-157`。
5. `list_runs.n_dropped` 直接聚合 `run_rejections`：`src/codeerratum/store/sqlite.py:325-340`。
6. `stats()` 的 `dropped_total` 也来自 `run_rejections`：`src/codeerratum/store/sqlite.py:391-408`。
7. 报告 notes 把 `collection.rejected` 全部加总为“机器丢弃 N 条”：`src/codeerratum/pipeline.py:610-627`。

影响：

- 一个“scanner 找到 1 条、verifier 崩了、finding 保留为未验证”的运行，会同时显示“1 条发现”和“机器丢弃 1 条”。
- `dropped_total`、`n_dropped`、Web 的“N 条机器丢弃”都会被这类运行污染。
- 用户会以为 finding 被删掉了，实际它仍在工单里，只是没有完成复核。

建议：

- 把“单元级验证失败”和“finding 级机器丢弃”分开建模。可以在 `collection` 增加 `verification_failures`，或给 rejected 行增加 `kind`（`finding_drop` / `unit_failure`）。
- 如果仍保留在 `run_rejections` 以便观测，统计 `n_dropped` / `dropped_total` 时只计 `kind=finding_drop`，另给 `verify_failed_units` 单独计数。
- 报告和 Web 不应把验证器失败放进“已丢弃的发现”列表。
- 补测试：验证器失败时 `findings` 保留、`n_dropped` 不增加、`dropped_total` 不增加。

### P2-2　`review.md` 仍使用旧的“丢弃”标题和说明，且验证/模型失败会渲染成空条目

位置：`src/codeerratum/report/markdown.py:267-273`。

当前渲染：

```markdown
## 已丢弃的发现（定位校验未通过 / 被证伪推翻）

这些结论未通过校验，请勿据此修改：

- `provider_error` [m]  —
- `verify_error` [v]  —
- `refuted` [f] claim here — m.py:3
```

问题：

- `provider_error` / `verify_error` 不是“定位校验未通过”，也不是“被证伪推翻”。
- 这两类条目没有 `claim` / `cited`，实际渲染为没有内容的空 bullet，读者无法判断是哪个单元、什么错误。
- 说明文字“这些结论未通过校验”不适用于模型调用失败。
- 0.6.5 改了 Web 和 notes，但报告 Markdown 这一层没有同步，属于口径贯通遗漏。

建议：

- 标题改为通用表述，例如“机器丢弃与验证失败”。
- 按 `reason` 分组渲染：定位失败、证伪、模型调用失败、验证器失败。
- 没有 `claim` 时显示 `unit_id` 和 `error_hint`，不要输出空 bullet；Web 已经在用 `r.claim || r.unit_id` 兜底（`web/static/index.html:1182`），Markdown 可以直接对齐。
- 增加报告渲染测试，覆盖 `provider_error` / `verify_error` 两种条目，而不是只测 notes 字符串。

### P2-3　`units_failed` 把“验证器失败”误报成“相关文件未被审查”

位置：`src/codeerratum/pipeline.py:593-608`，文案在 `src/codeerratum/i18n.py:343-346`。

当前逻辑把 `provider_error` 和 `verify_error` 合并：

```python
failed = [
    r for r in collection.rejected
    if r.get("reason") in ("provider_error", "verify_error")
]
```

然后统一提示“N 个单元调用模型失败，相关文件未被审查”。

问题：

- `provider_error` 通常表示 reviewer 调用失败，相关单元确实没有完成审查。
- `verify_error` 表示 reviewer 已经产出 finding，只是 verifier 失败；文件已经被审查，finding 也保留了。
- 两种失败混在一句话里，会让用户低估已覆盖范围，也会和 P2-1 的“丢弃”口径互相矛盾。

建议：

- 拆成两条提示：`provider_error` 用“未审查”，`verify_error` 用“验证失败，发现保留为未验证”。
- 失败原因提示分别聚合，避免把 verifier 的异常混进 reviewer 失败原因。
- 补一条 tiered 验证器失败的 notes 测试，断言文案不再声称文件未审查。

### P3-1　CLI 和 MCP 的 summary 仍把机器丢弃叫 `Rejected`

位置：`src/codeerratum/cli.py:138-140`，`src/codeerratum/pipeline.py:127-137`。

CLI 仍然打印：

```text
Findings: N | Rejected: M
```

MCP 的 `format=summary` 也直接返回 `summary()["rejected"]`。这与 0.6.5 已经把 Web 改成“机器丢弃”不一致，也容易和人工驳回混淆。

建议：

- CLI 改为 `Machine-dropped: M` 或中文“机器丢弃”。
- `summary()` 增加 `dropped` 字段，保留 `rejected` 作为兼容别名，并在文档中标注旧字段只表示机器丢弃。

### P3-2　Web 详情与 `list_runs` 的丢弃数来源不同，历史库还可能显示 0

位置：`src/codeerratum/web/server.py:560-565` 与 `src/codeerratum/store/sqlite.py:325-340`。

- Web 详情的 `totals.rejected` 仍来自 `len(collection.get("rejected", []))`。
- `list_runs.n_dropped` 来自 `run_rejections` 聚合。
- `run_rejections` 是 0.6.3（`fcbb84b`）才加入的表；更早的运行只有 `runs.n_rejected`，没有明细行，因此 `n_dropped` 会是 0，而 `n_rejected` 不是 0。

影响：

- 新旧运行、详情页和列表 API 可能给出不同的丢弃数。
- 外部调用方按 0.6.5 文档改用 `n_dropped` 后，历史运行会突然变成“0 丢弃”。

建议：

- 明确定义唯一权威来源；详情页也读同一个聚合值。
- 对旧库做回填，或在 SQL 中用 `CASE WHEN EXISTS (...)` 回退到 `r.n_rejected`。
- 给“旧库无 `run_rejections` 明细”补一条迁移测试。

### P3-3　报告只列前 10 条丢弃，但 notes 声称“明细见报告末尾”

位置：`src/codeerratum/report/markdown.py:270` 只遍历 `collection.rejected[:10]`；`i18n.py:348-350` 的 notes 写“明细见报告末尾与 run_rejections”。

当丢弃超过 10 条时，报告末尾并不包含全部明细，也没有“还有 N 条未展开”的提示。建议复用发现列表的截断提示方式，明确写出未展开数量。

### P3-4　`comparison.verified_units` 统计的是“尝试验证的单元”，不是“验证成功的单元”

位置：`src/codeerratum/review/orchestrator.py:519-525`。

`verified_units` 直接取 `len(verify_units)`，即使其中部分 verifier 调用失败也会计入。交叉审查统计会因此看起来比实际更完整。建议增加 `verify_failed_units`，或把 `verified_units` 改成成功数并另设 `verify_attempted_units`。

### P3-5　notes 对同一批失败会重复出现

`provider_error` / `verify_error` 目前会同时进入：

- `pipeline.units_failed` / `pipeline.units_failed_reason`
- `pipeline.machine_dropped`

信息侧重点不同，不算错误，但同一批失败在报告开头出现两次。完成 P2-1 / P2-3 的拆分后，可以顺手合并成一条“失败与丢弃摘要”，减少噪声。

## 测试覆盖缺口

0.6.5 新增的 4 条测试覆盖了主路径，但没有覆盖这次改动最容易出错的地方：

- 没有测试 `review.md` 在 `provider_error` / `verify_error` 下的标题、条目内容和空 bullet。
- 没有测试 CLI summary 的用户可见标签。
- 没有测试 Web 详情 `totals.rejected` 与 `list_runs.n_dropped` 是否一致。
- 没有测试旧库没有 `run_rejections` 明细时 `n_dropped` 的回退行为。
- 没有测试“验证器失败但 finding 保留”时 `n_dropped` / `dropped_total` 不应增加。
- 没有测试 `comparison.verified_units` 在部分验证失败时的口径。

建议至少补前两项和第四项，这三项直接对应本报告里的 P2/P3。

## 建议修复顺序

1. 先修 P2-1 / P2-3：把单元级验证失败与 finding 级丢弃分开；同步修 notes 文案。这是指标可信度问题，优先级高于展示层措辞。
2. 再修 P2-2：重写 `review.md` 的丢弃/验证失败区块，按 reason 分组，空字段用 `unit_id` / `error_hint` 兜底。
3. 最后处理 P3：CLI/MCP 命名、`n_dropped` 的数据源与旧库回退、报告截断提示、`verified_units` 口径。
4. 修完后按 `AGENTS.md` 升到 0.6.6，更新 CHANGELOG，跑全量测试和 ruff，重新打包并替换 `/Applications/CodeErratum.app`。

## 本轮边界

- 只复核 0.6.5，没有修改源码。
- 没有升版本、没有改 CHANGELOG、没有重新打包。
- 没有提交代码。
