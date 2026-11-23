# AGENTS.md — CodeErratum 协作约定

给所有写代码 agent：这里只写**读代码发现不了**的约定。产品功能、设计底线、CLI/UI 用法见 `README.zh-CN.md`，不要在此重复。

## 事实源（接到任务先做这一步）

| 要确认 | 权威来源 | 禁止 |
|---|---|---|
| 当前版本 | `git log -1` + `src/codeerratum/__init__.py` 的 `__version__` | 引用审查报告 md 里的版本/基线字段 |
| 代码状态 | `git status --short` | 假设审查文档描述的“未提交改动”仍然存在 |
| 测试结果 | 自己跑一遍（命令见文末） | 引用审查文档声称的 passed 数 |

已有事故：`OPTIMIZATION_REVIEW.md` 把基线写成 `e0d3e3e` + 未提交 0.6.2，实际 0.6.2 早已提交为 `cf690a8`；它写“469 passed”，真实是 464 passed / 5 failed（469 是 collected 总数）。

## 主链路

`pipeline.execute_review` → `review/orchestrator.run_review`（多模型并行 → 聚类 → 证伪）→ 锚点校验 → `report/markdown` → `store/sqlite` → `report/outcomes.reconcile` 回填人工裁决 → `apply_measured_profiles` 回灌路由分数。

## 硬约定

1. **版本号末位 +0.1**（0.6.2 → 0.6.3），同时改 `__init__.py` 与 `CHANGELOG.md` 顶部小节标题。
2. **改完必须打包并替换桌面应用**：先把旧 `dist/` 移走，再 `.venv/bin/python packaging/build.py`；构建脚本内含 `--selftest`（必须过，包括 webview 后端导入）；然后 `ditto dist/CodeErratum.app /Applications/CodeErratum.app`。用户按桌面应用使用，只改源码不算交付。
3. **提交前跑全量测试 + ruff**。有 5 项既有基线失败，不算新回归：`test_desktop::test_seeding_a_repo_keeps_the_projects_already_saved`、`test_hub::test_add_dedupes_and_validates`、`test_hub::test_add_and_remove_project_over_http`、`test_snapshot::test_commit_relative_specs_mean_everything_only_without_history`、`test_web::test_pipeline_refuses_an_unknown_revision_in_a_plain_folder`。

## 禁区与刻意权衡（改前先读原因）

| 约定 | 原因 |
|---|---|
| pywebview 的 `events.closing` 里禁止 `create_confirmation_dialog()` | macOS Cocoa 后端在主线程 runloop 里自等信号量，0.5.0 造成 Cmd+Q 挂死。改用官方 `window.confirm_close` 标志或页面内 DOM 弹窗 |
| 聚类同行跨分类 Jaccard 阈值 `0.8` 是刻意保留 | `0.35` 会把“缓冲区越界读”和“缓冲区未释放”这类不同缺陷吞并；没有标注集前不要调 |
| falsifier 不接收 `house_rules` | 吸收了项目惯例的证伪者会为错误理由表示同意，破坏证伪轮的独立性 |
| 作者模型不坐**任何**席位（reviewer / verifier / falsifier） | pinned 分支曾绕过 `available(author)`，falsifier 也曾直接从 `self.models` 选；两处都修过 |
| 机器丢弃（anchor_mismatch / provider_error / refuted…）不写 `findings.status` | 该列只表示人工裁决，混入会污染采纳率；原因聚合在 `run_rejections` 表 |
| 被证伪的单源 finding 进 `collection.refuted_findings`，不进 `findings` | 它要计入画像的 `n_refuted`，但不能变成人工待办 |
| `max_units` 省略一律走 `None`（auto=60） | MCP 曾硬编码 10，同一份 diff 换入口就少审 6 倍；0 才表示全量 |

## 环境坑

- pytest 默认临时目录 `.../pytest-of-root` 会 EEXIST，必须 `--basetemp=<项目内目录>`。
- 沙箱禁止 `rm -rf` / `shutil.rmtree` 清理构建产物；用 `mv` 到 `~/.Trash` 或 `.old_<版本>`。
- 测试 fake provider 不必实现 `aclose`：orchestrator 用 `getattr` 保护，但真实 provider 必须关闭共享 client。

## 改什么去哪个文件

| 任务 | 入口 |
|---|---|
| 单元切分 / 预算 | `ingest/git.py`、`pipeline.py::_unit_budget` |
| 模型选座 | `models/registry.py::route` / `route_quick_tier` |
| 状态词分类 | `report/outcomes.py::classify_status` |
| 画像与统计口径 | `store/sqlite.py::update_profiles` / `stats` |
| Web 状态与槽位 | `web/server.py`、`web/hub.py` |
| MCP 工具 | `mcp/server.py` |
| 报告渲染 | `report/markdown.py` |
| 解析健康度 | `providers/openai_compat.py` 的 `parse_stats`，由 orchestrator 汇总进 `comparison["parse"]` |

## 测试入口

```bash
mkdir -p .pytest_tmp/local
.venv/bin/python -m pytest -q --basetemp=.pytest_tmp/local
.venv/bin/ruff check src tests
```
