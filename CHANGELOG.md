# Changelog

## [未发布]

### Added

- **模型连通性检测**：配置表格每行新增「检测」按钮 +「检测全部」。发一个最小 chat 请求
  （`max_tokens=1`、单次不重试、10s 超时）验证端点可达 / 鉴权通过 / model_id 有效；绿勾「✓ 可用」带延迟，
  红叉「✗ 不可用」的悬停提示给出 HTTP 状态或原因（401/403 鉴权、404 端点、429 限流、超时等）。
  改动任意字段后结果自动失效，避免拿旧结果判断新配置。`__KEEP__` 会解析已保存的 api_key 再测，
  不产生假红叉。检测不写配置、不回显密钥，与其他写接口一样要求同源 Origin / Host。新增 14 项测试。
- **原生文件夹选择器 + 拖拽定位仓库**：仓库抽屉新增「浏览本地…」，直接弹出系统文件夹对话框
  （macOS `osascript` / Windows PowerShell / Linux `zenity`），选中即定位 git 根并切换；取消不报错。
  文件夹可直接拖到窗口，按名字在最近仓库、家目录（限 5 层、跳过依赖与缓存目录）里定位，唯一命中自动切换、
  多命中列候选。浏览器沙箱不暴露目录绝对路径，故原生对话框跑在本地服务进程里。新增 6 项测试。
- **界面直接切换本地仓库**：顶栏「切换仓库」抽屉，粘贴任意本地绝对路径（支持 `~`）即可把审查目标
  指向该 git 仓库；填仓库内子目录会自动定位到 git 根。最近用过的 6 个仓库保存在浏览器 localStorage，
  一点即切。模型配置按仓库隔离——每个仓库读各自的 `.codeerratum/config.json`，切过去若有配置立即生效。
  切换走同源 Origin / Host 校验，且审查进行中拒绝切换（artifacts 按仓库路径落盘，中途切换会写错地方）。
  浏览器不允许网页读取文件夹选择器，因此用粘贴路径而非原生目录对话框。新增 8 项测试。
- **界面查看与修改配置**：顶栏「配置」抽屉展示当前模式、模型清单、端点与 api_key 脱敏提示，
  可直接增删改并保存。配置写入 `.codeerratum/config.json`（权限 0600），文件优先于环境变量，
  重启 UI 后仍生效，CLI 也读同一份。api_key 从不回显，输入框留空即沿用原值。
  配置写入和审查一样要求同源 loopback Origin / Host；配置文件损坏时直接报错，不静默退回环境变量。
- 界面 diff 输入框补提示：本地未提交改动填 `HEAD`（含已暂存），新文件需先 `git add`；README 同步。

### Fixed

- **模型画像重复计数**：`save_run` 与 `save_comparison` 各自写一遍 `model_profile`，
  dual/committee 下每次运行的 `n_assert` 被算两遍；且证伪轮的 `confirm` 仍记在证伪者头上，
  一个从不提交发现的模型会拿到 100% precision。现在 `update_profiles` 是唯一写入者，
  每个增量由 `verdict_events` 的 (run, finding, model, role) 去重，重复保存同一 collection 不再膨胀。
- **`--diff` 可被当作 git 选项**：`get_diff` 把 revspec 直接放在 `git diff` 选项之后，
  `--output=/path` 会被 git 接受，把 diff 写到任意文件并返回空 stdout —— 与“没有变更”无法区分。
  现在拒绝以 `-` 开头的 revspec，并加 `--end-of-options` 让 git 自身兜底。
- **风险标签按子串匹配**：`author_name` 被判成 auth、`blocking_call` 被判成锁，单元预算被导向
  无关代码。改为标识符词匹配并拆分 camelCase，`author` 不再命中 `auth`。
- **误报历史从未回流**：`rank_findings` 的 `rejected_category_counts` 参数一直没人传。
  新增 `ReviewStore.rejected_category_counts()`，执行时读 `feedback.db` 并传给排序。
- **工单渲染能被模型输出破坏**：`claim` 里的换行会注入伪章节，源码/docstring 里的 ``` 会提前
  闭合围栏。claim/detail/说明折叠为单行，围栏长度按内容中最长反引号动态加长。
- **跨版本 diff 会审错区域**：审 `main...feature` 等未 checkout 的版本时，hunk 行号被用来切
  工作区源码，模型看到错位代码，而锚点哈希校验的正是同一份错位源码，所以仍会通过。
  现在 `git diff --raw` 取出新侧 blob，切分、摘录、上下文与锚点校验都用 diff 版本的内容
  （`new_side_sources`）；blob 取不到时回退工作区并逐行核对，不一致就跳过并写明。
- **测试临时目录固定在仓库内**：pytest 的 `current` 符号链接使下一次会话的 `rm_rf` 以
  ENOTEMPTY 失败，收集阶段就中断。移除固定的 `--basetemp=.pytest_tmp`，改用 pytest 默认临时目录。

### Tests

新增 9 条回归测试（画像幂等与归属、选项注入、pathspec 兼容、风险标签、误报回流、工单渲染
防注入、跨版本 blob 切分、跨版本锚点校验、pipeline 传递 blob）；配置界面新增 11 条（脱敏、
往返与权限、保留密钥、坏输入、同源校验、配置驱动真实运行、损坏文件不静默回退）。合计 180 项通过。

## [0.4.2] — 路径过滤

### Added

- `--path` / UI「路径过滤」：把 diff 限定到指定目录（如 `src/api`），不必先 `git add`
  再借 `--cached` 绕路。支持空格分隔的多个路径；报告「覆盖说明」会写明审查了哪些路径。
- 路径校验：拒绝绝对路径与 `..`（含 Windows 式 `..\`），在Web 层即以 400 返回。

### Tests

新增 `tests/test_pathspec.py` 5 项。合计 161 项通过。

## [0.4.1] — 审查回归与安全加固

### Fixed

- **`独有区永久为空`**：`agreement()` 把证伪者的 `confirm` 当成第二个独立审查者，
  也把同一模型跨审查单元的重复断言算成共识。现在按「独立断言的模型数」判定，
  证伪裁决只决定发现是否保留，不再参与一致性计算。实测：同一份 mock 输入从
  `unique=0 / consensus=2` 变为 `unique=2 / consensus=0`。
- **模型画像计错对象**：证伪裁决过去累加到裁决者（专职证伪者）头上，导致一个从不
  提交发现的模型拿到 100% precision 并被优先选为审查者。现在裁决归给被裁决发现的
  作者，且一个发现对每个作者只算一个精度样本。
- **Web UI 跨站 POST / DNS rebinding**：过去「没有 Origin 就放行」使任意网页能用
  text/plain 表单提交 JSON body 来烧 token；Host 也未校验，evil.com 指向 127.0.0.1
  同样能读到本地状态。现在 POST 必须带与 loopback Host 完全一致的 Origin，
  所有请求一律拒绝非 loopback Host。
- **Web UI 输出路径越界**：`write_markdown` 的 `out` 曾接受绝对路径与 `..`，
  本地页面可借审查请求写仓库外文件；现在只接受简单文件名。
- **Web UI 错误回显**：未捕获异常的文本（可能含用户磁盘绝对路径）不再返回浏览器，
  只写入服务端日志。
- `codeerratum docs --check` 的漂移表格被打印两遍。

### Tests

新增 4 条回归测试，覆盖 5 种情形（证伪者 confirm 不算共识、同模型重复不算共识、
无 Origin 的 POST 被拒、非 loopback Host 被拒、越界 out 被拒）。合计 156 项通过。

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
