# CodeErratum 代码检查报告（codex 0.6.0 改动）

- **检查对象**：`/Users/gugu/Downloads/codes/agentsCodes/codeReviewAgent`（仅此项目）
- **检查时间**：2026-11-23 03:40 GMT+8
- **检查范围**：工作区未提交改动（`git status`），基线 HEAD 为 `f969599`（0.5.3）
- **检查方式**：逐文件读 diff + 关键函数实际调用验证 + 全量 pytest + ruff；只读，未修改任何源码
- **结论**：改动方向正确、价值明确，但有 **3 个 P1 级问题需在发布前处理**，另有 3 个 P2 建议项

---

## 一、这轮改动做了什么

codex 把版本从 0.5.3 升到 **0.6.0**（`__init__.py:4`、`CHANGELOG.md:3`），主线是「工单可执行化闭环」，共三条：

| 主线 | 内容 | 主要文件 |
|---|---|---|
| 执行结果回收 | 新增 `codeerratum reconcile erratumReview.md`，把执行方 AI 填的《执行结果》表回填 `feedback.db`；工单每条发现注入隐藏标记 `<!-- finding:F-001=<id> -->` 供解析 | `report/outcomes.py`（新）、`report/markdown.py`、`cli.py`、`i18n.py`、`report/__init__.py` |
| caller 上下文升级 | 从只给坐标 `app.py:5` 升级为读取调用点 ±2 行代码片段，review 与证伪两轮共用 | `context/builder.py`、`review/executor.py`、`review/orchestrator.py` |
| 聚类修复 | `cluster_key` 去掉 category；同行不同分类需措辞重叠才合并 | `review/aggregator.py` |

配套：`README.md` / `README.zh-CN.md` / `CHANGELOG.md` 文档，新增 `tests/test_outcomes.py`（7 项）+ `tests/test_regressions.py` 3 项回归。

---

## 二、发现清单

### P1-1　状态分类把否定/待办语义误判为 accepted，污染采纳率

- **位置**：`src/codeerratum/report/outcomes.py:237-256`（`classify_status`），词表在 `:34-105`
- **证据**（实际调用 `classify_status` 的结果）：

  | 输入 | 返回 | README/CHANGELOG 声明 |
  |---|---|---|
  | `未通过` / `未确认` / `不通过` / `未采纳` | `accepted` | 应保持 open |
  | `not done` / `not passed` / `not resolved` / `not accepted` | `accepted` | 应保持 open |
  | `无需修复` | `accepted` | 语义应为 rejected（误报） |
  | `待修复` / `无法确认` | `accepted` | 应保持 open（跳过） |

- **根因**：`_ACCEPTED` 词表含短词 `通过`/`确认`/`采纳`/`修复`/`done`/`passed`，匹配用 `word in compact` 子串判断（`:252`），而 `_UNRESOLVED` 只挡住 `未修复`/`未解决` 等有限几种前缀组合。README 声明的 7 个规范状态（`已修复`/`部分修复`/`误报`/`不适用`/`跳过`/`未修复`/`待处理`）映射**全部正确**——问题只在执行方 AI 写自由文本时的否定变体。
- **影响**：本功能唯一要度量的采纳率被直接抬高（正向误判）或扭曲（`无需修复` 算成采纳）。`tests/test_outcomes.py:105-121` 只覆盖 `未修复`/`not fixed`，未覆盖上述否定形式。
- **建议**：先识别否定前缀（`未|不|无|没|待|尚|无法` + 正向词，英文 `not |un|no |pending`）再判正向；或改为白名单精确匹配，其余全部进 `ParseResult.problems` 并计 skipped。补参数化回归用例。

### P1-2　聚类改动引入回归：同行不同分类的「不同缺陷」被吞并

- **位置**：`src/codeerratum/review/aggregator.py:31-64`（`cluster_key` / `same_issue`）
- **证据**（构造真实 `Finding` 调用 `same_issue`，同一行、不同 category）：

  | claim A | claim B | Jaccard | 结果 |
  |---|---|---|---|
  | 返回值未判空导致崩溃（`null_deref`） | 返回值未校验导致注入（`injection`） | 0.43 | **合并=True** |
  | 循环未设上限导致死循环（`missing_timeout`） | 循环未加锁导致竞态（`race_condition`） | 0.38 | **合并=True** |

  两条描述的是完全不同的缺陷（崩溃 vs 注入、死循环 vs 竞态），会被 `_merge()` 合成一条，另一条从工单里静默消失。
- **根因**：改动删除了 `cluster_key` 中的 category，且 `same_issue` 在 `gap==0` 且 category 不同时退化为 `jaccard >= 0.35`。中文按单字切分（`_TOKEN_RE` 的 `[\u4e00-\u9fff]`），0.35 阈值极易达到，而「共享描述前缀 + 不同后果」的中文句式恰恰是常态。
- **影响**：正是 CHANGELOG:30-31 声称要修的「同一行不同问题被吞并」被新逻辑重新引入——旧代码至少因 category 不同而分桶。`tests/test_regressions.py:840-856` 的用例用 `问题3` vs `返回类型不一致`（jaccard=0），无法覆盖此情形。
- **建议**：`gap==0` 且 category 不同时要求更高相似度（如 ≥0.8）或要求 `anchor.snippet_hash` 相同；或保留 category 作为 `gap==0` 时的分桶键，仅 `gap>0` 时允许跨分类合并。补充「同行相似但不同缺陷」的回归用例。

### P1-3　新增 CLI 测试在标准 `--basetemp` 布局下失败，且断言存在假阳性

- **位置**：`tests/test_outcomes.py:163-179`（`test_cli_reconcile_defaults_to_erratum_review_and_honours_dry_run`）
- **证据**：
  - 全量跑 `.venv/bin/python -m pytest -q --basetemp=.pytest_tmp/audit_verify2` → **6 failed / 390 passed**，新增失败即此测试：`assert 'open' == 'accepted'`（:179）。
  - 同一测试改用仓库外 `--basetemp=/tmp/ce_audit_ext1` → **1 passed**（已实测）。
  - 根因链：`find_repo_root()`（`pipeline.py:155-161`）向上找含 `.git` 的祖先；`--basetemp` 在项目内时，`tmp_path` 位于仓库树下，`reconcile` 的 `_store(_resolve_repo_or_exit(repo))`（`cli.py:622`）解析到**项目的** `.codeerratum/feedback.db`，而非测试检查的 `tmp_path/.codeerratum/feedback.db`，因此零写入。
- **影响**：测试对 tmp 目录是否位于 git 仓库内隐式依赖，标准项目内 basetemp 下失败（CI/本地都可能踩到）。更隐蔽的是：`.codeerratum/feedback.db` 零写入时 `applied.exit_code == 0` 仍成立（:178），测试前半段无法真正证明「正式执行落库」。
- **建议**：测试内把 `tmp_path` 变成独立 git 根（建 `(tmp_path/".git")`）或 monkeypatch `codeerratum.cli._store`；dry-run 与 applied 都断言目标库实际状态与返回 counts，而非仅看 exit_code。

### P2-1　`apply_outcomes` 丢弃 `set_status()` 的失败返回值

- **位置**：`report/outcomes.py:344-347`；`store/sqlite.py:254-260`
- **证据**：`StatusStore` 协议（`outcomes.py:128-131`）声明 `set_status` 返回 `bool`，实现为 `cur.rowcount > 0`；调用方不接收返回值，无条件 `counts["updated"] += 1; counts[row.status] += 1`。若 `statuses_for` 与 `set_status` 之间行被并发删除（CLI 与 UI 同时写），汇总会报「已回填 N」但实际未落库。
- **建议**：按返回值计入 `unmatched`/`problems`，仅成功时累加。

### P2-2　`reconcile` 默认工单路径按 CWD 解析，与 store 按 repo 根解析不一致

- **位置**：`cli.py:608-612`（`report` 默认 `Path("erratumReview.md")`、`--repo` 默认 `Path(".")`），`:622` 用解析后的 repo 根开库
- **证据**：从仓库子目录执行时，`find_repo_root(".")` 返回仓库根用于 DB，但 `report.is_file()` 检查 `<子目录>/erratumReview.md`。工单默认生成在仓库根（`review -o erratumReview.md`）。README.md:139 / README.zh-CN.md:121 均写「所有命令在仓库任意子目录执行都指向同一个数据库」。
- **建议**：相对 `report` 路径基于解析后的 repo 根拼接，或在帮助/README 明确路径按 CWD 解析。

### P2-3　caller 片段的 prompt 围栏不安全 + Windows 路径重复列出

- **位置**：`review/executor.py:221-227`（`render_callers`）；对照 `report/markdown.py:32-40` 已有的自适应 `_fence()`
- **证据**：
  - `render_callers` 用固定三反引号包裹原始源码片段，未检查片段内是否含 ```；调用方文件若是带 ``` 的 docstring 或内嵌 Markdown，会提前闭合代码块，后续内容变成自由文本污染 prompt。同仓的 markdown renderer 已专门实现自适应围栏但未复用。
  - `covered` 用 `builder.py:173` 归一化后的 `/` 路径，而 `callers` 来自 `builder.py:141` 的 `Path.relative_to()`，Windows 上是 `\`，两者不相等，`rest` 会重复列出同一调用点，使「最多 5 处」限制失效。
- **建议**：复用自适应 fence（取片段中最长反引号串 +1）；构造 `covered` 前对 `callers` 同样 `replace("\\", "/")`。

---

## 三、验证结果

| 项 | 结果 |
|---|---|
| 全量 pytest（`--basetemp=.pytest_tmp/audit_verify2`） | **6 failed / 390 passed** |
| 其中既有基线失败（改动前已存在） | 5 项：`test_desktop::test_seeding_a_repo_keeps_the_projects_already_saved`、`test_hub` ×2、`test_snapshot::test_commit_relative_specs_mean_everything_only_without_history`、`test_web::test_pipeline_refuses_an_unknown_revision_in_a_plain_folder` |
| 新增失败 | 1 项：`test_outcomes.py::test_cli_reconcile_...`（P1-3，仓库外 basetemp 下通过） |
| 用例数变化 | 基线 372 passed → 现 390 passed（新增 19 项：test_outcomes 16 + test_regressions 3，扣 1 项失败） |
| ruff check src tests | **All checks passed** |
| 工作区 | 审查期间文件 mtime 停在 03:24，未见并发写入；代码未被本次检查修改 |

---

## 四、结论与建议优先级

改动本身价值明确：`reconcile` 补上了「工单写了没人读」的真实闭环，caller 片段切中最高频的签名/返回值漂移回归，聚类去 category 也修复了跨分类漏合并。三条主线的设计意图在 CHANGELOG 与代码中一致，README 声明的状态映射本身正确。

发布前建议按此顺序处理：

1. **P1-1 状态分类否定词**——直接决定采纳率可信度，且修复面小（词表 + 前缀判断 + 参数化测试）。
2. **P1-2 聚类吞并**——会让不同缺陷从工单静默消失，是审查工具最不该有的失效模式；加严同行跨分类合并条件。
3. **P1-3 测试 basetemp 依赖**——让新增测试在标准布局下稳定通过，并消除零写入假阳性。
4. **P2-1 / P2-2 / P2-3**——并发写入统计、子目录路径一致性、prompt 围栏与 Windows 路径，按迭代排期处理。

以上所有发现均基于实际代码与实际调用验证；未对 `ai-novel-generator` 或其他项目作任何结论。
