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

续跑是同一主链路的第二入口：`resume.resume_review` 读取运行产物账本（units.json / calls.json / findings.json），跳过已完成单元后再次调用 `run_review`，原席位（子集 registry）跑剩余队列并合并——run_id 与产物路径不变。口径约定见 0.6.6：dropped / failed_units / 人工裁决三者互不混淆。

## 硬约定

1. **版本号末位 +0.1**（0.6.2 → 0.6.3），同时改 `__init__.py` 与 `CHANGELOG.md` 顶部小节标题。
2. **改完必须打包并替换桌面应用**：先把旧 `dist/` 移走，再 `.venv/bin/python packaging/build.py`；构建脚本内含 `--selftest`（必须过，包括 webview 后端导入）；然后跑 `packaging/install.sh`（**不要直接 `ditto` 到 `/Applications`**，见下）。用户按桌面应用使用，只改源码不算交付。
3. **提交前跑全量测试 + ruff**：`ruff check .` 与 `ruff format --check .` 都要跑（CI 查的是**全仓库**，不只是 `src tests`——`.` 里还有 `packaging/`、`scripts/`、`demo/`）。当前基线是 **530 passed / 0 failed**，没有例外项。

## CI（0.7.1 修好之后，run #47 起重新变绿）

CI 从 run #12 到 #46 连续红了 35 次，根因有三类，逐条别再犯：

- **改了 Python 文件就顺手 `ruff format`**。只跑 `ruff check` 不跑 `ruff format`，CI 的 Format check 会红。
- **测试不许依赖运行机器的环境**。终端宽度（`COLUMNS`）和平台（`sys.platform`）都不许靠继承：typer 用 rich 渲染 help，rich 在 `Console()` 构造时就把宽度固化，而 `cli.py` 有一个模块级 `console = Console()`；平台相关的分支（zenity / xdg-open / explorer）也各有各的前置条件。要断言「某个选项存在」，就问命令对象（`get_command(app).commands[...].params`）或 click 的 `format_options`，别去读渲染后的文本。
- **CI 挂了先看 error annotation**，不用开原始日志：Test 步骤把 `FAILED`/`ERROR` 行以 `::error::` 输出，提交页上直接可见。

## 桌面应用安装（0.7.0 踩过的坑，务必按脚本走）

PyInstaller 的 bundle **就是它的归档**：冻结程序在 import 时才从自己的可执行文件里懒加载 PYZ 模块。`ditto src dst` 对已存在的目标文件是**原地覆盖**（同一个 inode，截断重写），所以在应用运行时直接 `ditto` 覆盖 `/Applications/CodeErratum.app`，运行中的实例就会读到**撕裂的归档**——目录表来自旧字节、模块载荷来自新字节，下一次懒加载报：

```
zlib.error: Error -3 while decompressing data: incorrect header check
```

迷惑之处在于磁盘上的 bundle 是好的（`--selftest` 过）、新开一个实例也正常，只有**已经在跑的那个实例**在打印中文时炸——Rich 只在遇到需要查字符宽度的字符（中文/emoji）时才懒加载 `rich._unicode_data`。它还会**不再响应 SIGTERM**：信号处理跑在独立线程里，拆解过程同样要打印中文，线程一死进程就活着不走。

正确顺序（`packaging/install.sh` 已实现并自检）：
1. 结束运行中的实例（SIGTERM → 等待 → 必要时 SIGKILL）；
2. `mv` 旧 bundle 到一边（**改名，不是覆盖**，运行中的进程继续握着旧 inode）；
3. `ditto` 新 bundle 到一个**尚不存在**的路径；
4. 跑 `--selftest` 验证；
5. 旧 bundle 再移进回收站。

排查这类问题的手法：`.venv/bin/python` 里用 `PyInstaller.archive.readers` 直接抽 PYZ 里每个模块，抽不出来的就是坏的那一个；再和 `dist/` 里的同名文件比。


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

- pytest 的临时目录必须放在**仓库外**：`--basetemp=/tmp/ce_pytest`。放在仓库内（`--basetemp=.pytest_tmp/local`）会让 `tmp_path` 落进 git 工作树，`resolve_git_root` / `is_git_repo` 向上遍历找到项目根，于是 5 个测试**假失败**（hub 两条、snapshot、desktop、web 各一）——它们曾被误记成「既有基线失败」，害得每轮都在核对并不存在的问题。系统默认的 TMPDIR 在本机会 EEXIST，所以要显式给一个仓库外的路径。
- 终端宽度会被测试读到：typer 用 rich 渲染 help，rich 在 `Console()` 构造时就把宽度固化，而 `cli.py` 有模块级 `console = Console()`。`tests/conftest.py` 在**导入期**（conftest 先于测试模块导入）把 `COLUMNS` 钉成 120，CI 的 Test 步骤也显式设同样的值。
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
# 临时目录必须在仓库外；理由见「环境坑」
.venv/bin/python -m pytest -q --basetemp=/tmp/ce_pytest
.venv/bin/ruff check .
.venv/bin/ruff format --check .
```
