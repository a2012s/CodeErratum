# Changelog

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
