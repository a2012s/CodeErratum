# Changelog

## [0.4.0] — 四、五、六期

第四期把结果送到 PR 上，第五期补文档与漂移，第六期接 MCP 与多语言。同时清掉三处
「看起来是数字、其实是常量」的设计债。

### Added

- `codeerratum pr [--post]`：读 GitHub event，跑审查，行内评论上限 5 条 + 1 条摘要。
  默认 dry-run 并把待发内容写入 `pr-comments.json`。
- `action.yml` 与 `docs/github-action.yml` 示例；fork PR 拿不到 secrets 的坑已注明。
- `codeerratum docs [--check] [--strict]`：从 AST 生成模块文档与签名基线，
  漂移检测只看 API 形状变化，不看行号移动。
- `codeerratum mcp`：stdio MCP server，四个工具 `review_diff` / `list_runs` /
  `get_finding` / `check_docs_drift`。
- Web UI 新增「文档」抽屉：模块树、公开符号数、签名漂移（`GET /api/docs`）。
- `detect_language()`：按扩展名给单元打语言标签，非 Python 文件按文件级审查。
- `spec.py`：规格等级真实探测（L4 契约 / L3 外部规格 / L2 自述 / L1 仅代码），
  报告头同时给出判定依据。
- `abstain_warning()`：证伪轮 `insufficient_context` 超过 30% 时进入 notices。
- 模型画像回流：`save_run` 累计 `model_profile`（断言/独有/确认/反驳），
  下次运行注入 `ModelSpec.measured_*`；`apply_measured_profiles()` 负责这一步。

### Changed

- `split_diff` 按文件扩展名推断语言，`language=` 参数变成强制覆盖。
- prompt 的代码块标记改用单元的真实语言，而不是写死 `python`。
- 报告头「规格等级」由 `L1` 变成 `L1（无规格来源，仅凭代码判断）` 这类可辩驳的表述。

### Fixed

- `annotation_coverage` 曾用绝对路径判断跳过目录，任何位于 dot 目录下的仓库
  （临时目录、CI workspace）都会被整体跳过，覆盖率恒为 0。
- `Store.model_profile_map()` 与既有 `model_profiles()` 重名，改为独立名称。

### Security

- PR 评论按 hunk 过滤锚点：GitHub 会因一行不在 diff 而拒掉整批，降级为摘要而非全丢。
- 同一 commit 重跑不重复评论（隐藏 marker + 先查后发）。
- MCP server 只读本地仓库，工具参数中的路径不接受 URL；stdout 只输出协议消息。

## [0.3.0] — 项目规范

让审查知道「本项目的规矩」，降低噪音。形态是**追加**而非替换：把 system prompt 交给用户整段覆盖，会把静默失败引进来（锚点校验全挂 → 空报告 → 看起来像「没有 bug」）。

### Added

- `codeerratum review --rules <file>`；不传则自动发现 `<repo>/.codeerratum/rules.md`。
- 规范包在 `<house_rules>` 块内，位置在硬规则之后、`# Output` 之前；块头显式声明「与硬规则冲突时硬规则优先」。
- UI 侧栏折叠区可直接编辑规范；`/api/meta` 回传自动发现状态（路径 / 字数 / sha），空白 textarea 的语义因此不再含糊。
- `FindingCollection` 新增 `house_rules_sha` / `house_rules_chars` / `house_rules_source`，`review.md` 头部同步显示。同一 diff 两次结果不同时，先看这里。
- `tests/test_house_rules.py` 14 项，含「规则确实进了发给模型的 messages」这条端到端断言。

### Changed

- prompt 模板新增 `{{HOUSE_RULES}}` 占位符；加载器找不到占位符时**抛错**，而不是静默跳过——否则以后改 prompt 时规则会无声消失。
- 证伪轮不接收项目规范（有意为之）：吸收了项目规矩的证伪者会退化成第二个审查者。

### Security

- 用户文本里的 `<house_rules>` / `</house_rules>` 先剥离，防止提前闭合标签越界成指令。
- 规范上限 4000 字，超出截断并声明；防止用户文本把硬规则挤出上下文窗口。
- UI 只接受规范**文本**，不接受路径，本地服务不具备读任意文件的能力。

## [0.2.0] — 图形界面

`codeerratum ui`：本地 Web UI，标准库实现，零新增依赖。目的不是好看，是把终端里读不出来的卖点（三区分流、证据等级、证伪记录）变成一屏能看懂的东西。

### Added

- `codeerratum ui [--port] [--host] [--no-browser] [--repo]`：起一个本地服务并打开浏览器。
- 界面内发起审查：diff 范围 / 模式 / 单元上限 / **作者模型排除** / 可选同时写 `review.md`。
- 三区分流页签（独有 / 冲突 / 共识），quick 模式退化为「全部发现」。
- 逐条卡片：严重度、`文件:行` + 带行号代码片段、证据与强度、修改建议（diff 上色）、验收命令一键复制、证伪轮各方意见。
- 采纳 / 驳回（强制填理由）/ 重置，直接回写 `feedback.db`；状态按 finding 内容 id 关联，重跑不丢裁决。
- 被定位校验丢弃的发现可展开查看——防幻觉这条设计底线在界面上可见。
- 历史运行列表 + 统计抽屉（运行数、发现数、采纳率、按分类）。

### Changed

- 抽出 `pipeline.execute_review()` 作为 CLI 与 UI 的唯一执行路径。此前逻辑内嵌在 `review` 命令里，UI 若另写一份必然漂移（不同的上限、不同的落盘、不同的错误处理）。
- 每次运行固定归档 `review.md` 到 `.codeerratum/runs/<run_id>/`；`-o/--out` 只是额外副本，不再要求写进仓库根。
- CLI 提示文案统一为中文（数值与结构不变）；单元超限提示由英文 `Capped at N units` 改为中文说明。

### Security

- 服务默认只绑 `127.0.0.1`；POST 校验 `Origin`/`Host`，跨站请求返回 403（否则任意网页都能替你发起付费调用）。
- run_id / finding_id 走正则校验，阻断路径穿越；请求体上限 2MB。
- 进程内互斥锁：一次只跑一个审查，第二个请求返回 409。

## [Unreleased]

一次针对已完成代码的审计修复，重点是"让输出可信"与"让数据可积累"。

### Fixed — 会导致功能失效或数据错误

- **严格 JSON Schema 不合规**：`strict: true` 的 schema 缺 `additionalProperties: false`、`required` 未覆盖全部字段、`str | None` 仍是 `anyOf`，真实 OpenAI 端点会直接 400。现在发送前统一改写为 strict 子集；端点仍拒绝时自动降级为 `json_object` 并记住该能力。
- **单条非法 finding 摧毁整块结果**：`claim` 超 60 字符会抛异常，导致同一次调用里其他有效结论一起丢失，并被误报成 provider 错误。改为逐条校验，非法项进入 `rejected`（`invalid_finding`）。
- **跨文件锚点被接受**：模型写 `file: other.py`、片段却来自被审查文件，校验仍通过，报告会指向一个不存在的定位。现在文件名不匹配一律拒绝（`anchor_file_mismatch`）。
- **删除文件被当作可审查单元**：源文件不存在，读取时抛 `FileNotFoundError`。删除文件与工作区不存在的文件现在跳过，并在报告《覆盖说明》中列出。
- **parse 成功率统计口径错误**：重试成功的调用被记为 `success=0 / failed=1`。改为按调用计数（`calls / first_try / repaired / failed`），新增 `success_rate`。
- **重复发现重置人工裁决**：`INSERT OR REPLACE` 把 `accepted` 改回 `open`，采纳率被自己抹掉。改为 upsert 只更新内容字段，保留状态与首次时间。

### Fixed — 质量、成本、一致性

- **调用方检索恒为空**：jedi 在 hunk 首行第 0 列查询，永远拿不到引用。改为定位符号名 token，并按仓库作用域创建 `jedi.Project`。
- **每次发送整个文件**：一行改动会把整文件（外加重复的函数体）塞进 prompt。改为按 hunk ±6 行 + 包围符号生成带绝对行号的摘录，超长时截断并标注省略行。
- **多文件 diff 丢失末个 hunk**：跨文件边界前没有回填 body，除最后一个文件外每个文件的最后一个 hunk 内容为空，导致 risk tag 与符号归属失效。
- **报告自相矛盾**：正文渲染 13 条、摘要写 10 条、执行结果表 10 行。现在三处口径统一，并在头部与摘要显式披露"共 N 条，渲染前 M 条"。
- **排序被丢弃**：聚合器算出的证据/一致性权重被 `(severity, id)` 重排覆盖。新增 `ranked_findings()`：严重度 → 硬证据 → 一致性。
- **lens 被硬编码**：模型给的安全类视角被丢弃，报告统一显示 `correctness`。
- **静态工具重复执行**：同一文件每个模型各跑一次 ruff/mypy。按 (path, mtime, size) 缓存；超时/缺失降级为空结果而不中断审查。
- **无效 diff 抛原始堆栈**：新增 `DiffError`，CLI 给出中文提示与替代命令。

### Changed

- 运行产物与反馈库统一落在仓库根的 `.codeerratum/`（`runs/<run_id>/` + `feedback.db`），不再随 cwd 漂移，也不污染工作区。
- `review` 新增 `--author-model`：作者模型不得进入审查席位（此前只有路由层约束，CLI 从未传入）。
- 单元预算按"风险标签 → 是否公开符号 → 改动行数"排序后再截断。
- 报告新增《本次覆盖说明》，把跳过的文件、失败的调用、被截断的单元数显式写出。

### Added

- `LICENSE`（MIT）、GitHub Actions CI（3.11/3.12/3.13：lint + format + test）。
- 版本号单一来源（`pyproject.toml` 从 `src/codeerratum/__init__.py` 读取）。
- `tests/test_regressions.py`：16 条回归测试，每条对应上面一个已修缺陷。
- 中英双语 README：英文 `README.md` 作默认首页（PyPI 描述同源），中文 `README.zh-CN.md`，两版顶部互链。
