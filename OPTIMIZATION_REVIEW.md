# CodeErratum 完善清单（0.6.2 复审）

- **审查对象**：本仓库（仓库根）
- **审查时间**：2026-11-23
- **代码基线**：`main` @ `e0d3e3e`，叠加工作区未提交的 0.6.2 改动（**注**：该基线字段有误，0.6.2 实际已提交为 `cf690a8`，见 `AGENTS.md`「事实源」）
- **审查范围**：审查流水线、聚类与证伪、模型路由、SQLite 存储、CLI、MCP、Web UI、报告与执行结果回填
- **验证状态**：`pytest` 469 passed；`ruff check src tests` 通过
- **说明**：本文档只列问题与建议，本轮没有修改任何业务代码

---

## 一、结论摘要

当前项目已经具备完整的主链路：切分审查单元 → 多模型并行审查 → 锚点校验 → 聚类 → 异厂商证伪 → 三区报告 → 执行结果回填 → 模型画像。真正影响“闭环与可信度”的，不再是主流程能不能跑，而是**回流数据是否完整、对外数字是否自洽、下游 agent 是否能通过 MCP 完成闭环**。

本轮没有发现 P0 级问题。建议优先处理 5 个 P1：

| 级别 | 数量 | 主题 |
|---|---:|---|
| P1 | 5 | 可信度数据回流、MCP 默认预算、作者模型约束、连接复用、解析质量可审计 |
| P2 | 11 | 统计口径、SQLite 并发、abstain 分母、MCP 闭环、预算校验、协议版本、槽位显示、产物保留、成本预估、丢弃原因 |
| P3 | 8 | 证伪分支语义、LIKE 转义、页面注入方式、family 字段、重试抖动、数据库迁移、语言标记、安全响应头 |

如果只做三件事，建议按这个顺序：

1. **修复被证伪 finding 的画像回流**，否则 precision 和路由分数会系统性偏向“报得多”的模型。
2. **把解析失败和 parse_stats 写入运行产物**，否则“parse success rate 是硬门槛”这句话无法被外部审计。
3. **补齐 MCP 的 accept/reject/reconcile/stats**，并修正默认预算，否则下游 agent 只能“审”，不能“闭环”。

---

## 二、P1：影响可信度或核心闭环

### P1-1　被完全证伪的 finding 没有回流到模型画像

**现象**

单模型提出、随后被证伪的 finding 会被移出 `surviving`，只写入 `collection.rejected`：

- `src/codeerratum/review/orchestrator.py:586-610`

但 `save_run()` 只把 `collection.findings` 写入 `findings` 表，`update_profiles()` 也只遍历 `collection.findings`：

- `src/codeerratum/store/sqlite.py:83-96`
- `src/codeerratum/store/sqlite.py:167-207`

结果是被完全证伪的 finding 既没有 `n_assert`，也没有 `n_refuted`。

**影响**

`measured_precision = n_confirmed / (n_confirmed + n_refuted)`：

- `src/codeerratum/pipeline.py:286-287`

漏掉 `n_refuted` 会让 precision 系统性偏高，而且漏掉的恰好是“独有发现被推翻”这种最能区分模型质量的样本。模型画像又会回灌到路由分数，最终形成“越敢报、越容易显得准”的逆向激励。

**建议**

- 不要把被证伪的 finding 写成 `findings.status='rejected'`。这个状态目前表示“人工驳回”，混入机器证伪会进一步污染采纳率。
- 在 `collection.rejected` 的 `refuted` 记录里补 `finding_id`、`source_models`、`verdicts`；或者新增 `collection.refuted_findings: list[Finding]`。
- `update_profiles()` 同时遍历存活 finding 和完全证伪 finding，按同样的 `verdict_events` 幂等规则累计 `n_assert` / `n_unique` / `n_refuted`。
- 加一条回归测试：单模型 P1 finding 被证伪后，该模型的 `n_assert +1`、`n_refuted +1`，且不会出现在人工 `findings.status='rejected'` 统计里。
- 历史数据库中已经丢失的证伪样本无法回补，修复后从下一次运行开始准确。

---

### P1-2　MCP 默认审查预算与文档不一致

**现象**

MCP 工具 schema 写的是“省略即 auto（最多 60 个单元）”：

- `src/codeerratum/mcp/server.py:55-62`

实际代码在省略参数时固定为 10：

- `src/codeerratum/mcp/server.py:147`

CLI 的 auto 预算是 60：

- `src/codeerratum/pipeline.py:187`
- `src/codeerratum/cli.py:71-75`

**影响**

通过 MCP 调用的 coding agent 默认只审 10 个单元，而通过 CLI 审 60 个。同一份 diff，换一个入口就少审 6 倍，且调用方从 schema 上完全看不出来。

**建议**

- 直接复用 `AUTO_UNIT_BUDGET`，把 `else 10` 改成 `else None`，让 pipeline 走统一的 auto 逻辑。
- 如果 10 是为了避免 MCP 长调用超时，应把原因写进 schema，并同时补进度通知；不要用“偷偷降预算”解决超时。
- 加一条 MCP 测试：省略 `max_units` 时选中的单元数与 CLI auto 一致。

---

### P1-3　显式 pin 模型会绕过“作者模型不坐审”

**现象**

`ModelRegistry.available()` 明确承诺排除作者模型：

- `src/codeerratum/models/registry.py:95-98`

但 `route()` 的 `pinned` 分支直接从 `by_id` 取模型，没有经过 `available(author_model)`：

- `src/codeerratum/models/registry.py:119-127`

此外，`--author-model` 传入一个不存在的模型 id 时也不会报错，`available()` 只是过滤不到任何东西。

**影响**

- 用户同时传 `--author-model` 和 `--model` 时，作者模型会被静默接受为 reviewer，违背“作者永不坐审”的硬承诺。
- `--author-model` 拼写错误时，作者模型仍可能被选中，而且没有任何提示。

**建议**

- pinned 分支同样先过滤 `author_model`；过滤后为空则抛出明确错误。
- 在加载 registry 时校验 `author_model` 是否存在于已配置模型；不存在时直接失败或至少给出显著警告。
- 加回归测试：`pinned=[author]` 必须报错，`pinned=[author, other]` 只能返回 `other`。

---

### P1-4　Provider 每个请求新建 `httpx.AsyncClient`

**现象**

`_post()` 每次调用都 `async with httpx.AsyncClient(...)`：

- `src/codeerratum/providers/openai_compat.py:100-107`

重试时 `_post()` 仍会被重新调用，因此每次重试也会重新建连接。

**影响**

- 每个请求重新做 TLS 握手，连接池完全失效。
- dual/committee 模式下，60 个单元 × 2~3 个模型会产生上百次握手，增加尾延迟和瞬时连接数。
- 429/超时集中发生时，重试风暴更容易压垮同一个端点。

**建议**

- Provider 持有 lazy 创建的共享 `self._client`，增加 `async def aclose()`。
- 在 orchestrator 的 `finally` 中关闭所有 provider，确保异常路径也能释放连接。
- 保留 `timeout` 作为 client 级配置，而不是每次请求级配置。
- 用 `httpx.MockTransport` 加测试，断言同一 provider 的多次请求复用同一个 client。

---

### P1-5　解析失败与 parse_stats 没有落盘，无法审计

**现象**

Provider 维护了 `parse_stats` 和 `failure_log`：

- `src/codeerratum/providers/openai_compat.py:79-91`

也有 `dump_failures()`：

- `src/codeerratum/providers/openai_compat.py:308-313`

但 pipeline 只写 `calls.json`、`findings.json`、`static_diags.json`：

- `src/codeerratum/pipeline.py:606-613`

`ReviewOutcome.summary()` 也没有 parse 统计：

- `src/codeerratum/pipeline.py:127-152`

全仓搜索 `dump_failures`，除了 provider 自身和测试，没有任何生产调用点。

**影响**

“parse success rate 是 v0.1 hard gate”目前只存在于进程内存。一次运行结束后，用户和下游 agent 都无法知道：

- 第一次解析成功了多少次；
- 修复重试救回了多少；
- 有多少结构化输出彻底失败；
- 失败样本长什么样。

当报告“看起来正常但少了很多 finding”时，无法区分“模型没发现问题”和“解析器把问题丢了”。

**建议**

- orchestrator 汇总每个 provider 的 `parse_stats`，随 `ReviewOutcome` 返回。
- pipeline 写 `parse_stats.json` 和 `parse_failures.json`（`failure_log` 已经截断到 4000 字符，风险可控）。
- `ReviewOutcome.summary()` 增加 `parse` 块，至少包含 `calls / first_try / repaired / failed / success_rate`。
- 当失败率大于 0 时，在报告 notes 和 MCP summary 中显式提示，而不是只留在日志里。

---

## 三、P2：影响统计可信度、集成体验或长期稳定性

### P2-1　`stats.by_model` 在 dual 模式下口径错误

**现象**

`by_model` 直接对 `runs.model_id` 做 `GROUP BY`：

- `src/codeerratum/store/sqlite.py:318-320`

而 dual/committee 运行时 `model_id` 是逗号拼接串：

- `src/codeerratum/review/orchestrator.py:623`

**影响**

quick 模式下看起来是按模型统计，dual 模式下会得到 `"model-a,model-b": 12 runs` 这样的组合串。用户会误以为“每个模型跑了多少次”，实际统计的是“模型组合出现了多少次”。

**建议**

- 改为从 `model_profile` 或 `verdict_events` 聚合：

```sql
SELECT model_id, role, COUNT(*)
FROM verdict_events
GROUP BY model_id, role
```

- 输出每个模型的 `assert / unique / confirm / refute` 和 precision，而不是“run 数”。
- 如果仍要保留 run 维度，新增 `run_models` 关联表，一格一个模型。

---

### P2-2　SQLite 连接管理不适合多线程 Web

**现象**

`ReviewStore._conn()` 每次新建连接，没有 `timeout`、没有 `busy_timeout`、没有 WAL：

- `src/codeerratum/store/sqlite.py:22-25`

并且 `with self._conn() as conn` 只负责提交/回滚，**不会关闭连接**。每次调用都会留下一个待 GC 的连接对象。

**影响**

Web UI 同时存在 SSE、状态轮询、accept/reject 写入时，容易出现 `database is locked`；长跑场景下还可能积累文件描述符。

**建议**

- 连接显式设置 `timeout=30`，并执行 `PRAGMA busy_timeout=30000`。
- 在 `_init()` 中执行一次 `PRAGMA journal_mode=WAL`；写连接使用 `PRAGMA synchronous=NORMAL`。
- 把 `_conn()` 改成 `contextmanager`，在 `finally` 中 `close()`。
- 增加并发写测试：多线程同时 `set_status()` 和 `save_run()` 不应抛 `database is locked`。
- WAL 会产生 `-wal`/`-shm` 文件，现有 `.git/info/exclude` 已经覆盖整个 `.codeerratum/` 目录。

---

### P2-3　abstain_warning 的分母包含 assert，信号被稀释

**现象**

`abstain_warning()` 把一条 finding 上所有 verdict 都放进分母，包括 reviewer 的 `assert`：

- `src/codeerratum/pipeline.py:254-267`

**影响**

一条 finding 通常有 1~3 个 `assert`，但只有 falsifier 会产生 `abstain`。即使证伪环节大面积“证据不足”，只要 assert 足够多，整体比例仍可能低于 30%，警告不触发。

**建议**

- 分母只统计参与证伪的 verdict：`role in ("confirm", "refute", "abstain")`。
- 或者直接以“被送去证伪的 finding 数”为分母，计算“至少一个 abstain 的 finding 占比”。
- 补测试：`[assert, assert, abstain]` 应触发警告，而不是被稀释到 1/3 以下。

---

### P2-4　MCP 缺少闭环工具，且关键参数没有暴露

**现象**

MCP 目前只有 4 个工具：

- `review_diff`
- `list_runs`
- `get_finding`
- `check_docs_drift`

见 `src/codeerratum/mcp/server.py:208-210`。

同时 `review_diff` 没有暴露 `author_model`、`paths`、`tiered`、`include_non_code` 等 CLI 已有参数。

**影响**

下游 coding agent 可以“发起审查”，但无法通过 MCP：

- 标记 fixed / false_positive；
- 回填执行结果；
- 读取采纳率和模型画像；
- 指定作者模型，遵守“作者不坐审”；
- 只审某个目录或关闭分层审查。

于是 MCP 只能完成“审”，不能完成“闭环”，用户仍要回到 CLI/UI 手工操作。

**建议**

- 新增 `accept_finding`、`reject_finding`、`reconcile_report`、`get_stats`，内部复用现有 CLI 的 store 方法。
- 可选新增 `get_run`，让 agent 直接拿到某次运行的 `review.md` 和结构化 findings。
- 把 `author_model`、`paths`、`tiered`、`include_non_code` 加入 `review_diff` 的 schema 和 handler。
- 工具描述里写清幂等语义：重复 accept/reject 不应报错。

---

### P2-5　MCP 长任务没有进度、取消和超时策略

**现象**

`review_diff` 是同步 tool call，内部直接跑完整 `execute_review`：

- `src/codeerratum/mcp/server.py:130-157`

服务端没有发送 `notifications/progress`，也没有把 `notifications/cancelled` 转成实际的停止信号。

**影响**

60 个单元、dual 模式、端点较慢时，一次调用可能持续数分钟。MCP 客户端通常会先超时，用户看到“工具失败”，但服务端仍在继续烧 token。

**建议**

- 支持 MCP progress token，至少按“单元完成数 / 总单元数”发送进度。
- 把取消通知接到 `execute_review` 的 `should_stop`，停止后续单元。
- 如果短期不做异步任务，至少在 schema 中说明这是长任务，并把默认预算与超时策略写清楚。

---

### P2-6　CLI 的 `--max-units` 没有校验，负数会变成“审全部”

**现象**

CLI 直接透传整数：

- `src/codeerratum/cli.py:71-75`

`_unit_budget()` 把所有 `<= 0` 都当成“全部”：

- `src/codeerratum/pipeline.py:236-245`

README 还写着 CLI 上限 500：

- `README.zh-CN.md:69`

但 CLI 代码并没有 500 的上限。

**影响**

- `--max-units -1` 这种手滑输入会直接审全部代码，可能产生几百次模型调用。
- 用户按 README 以为有 500 上限，实际可以被一个很大的正整数突破。

**建议**

- Typer 参数加 `min=0`，负数直接报错；`0` 保留“全部”语义。
- 明确策略：CLI 要么和 Web/MCP 一样封顶 500，要么在 README 中删掉“上限 500”的表述并说明 CLI 是显式全量。
- 加测试：负数报错、0 等于全部、超大正数的行为符合文档。

---

### P2-7　MCP 协议版本写死为 2024-11-05，且忽略客户端协商

**现象**

`PROTOCOL_VERSION = "2024-11-05"`：

- `src/codeerratum/mcp/server.py:32`

`initialize` 始终返回这个版本，没有读取请求里的 `params.protocolVersion`：

- `src/codeerratum/mcp/server.py:231-241`

**影响**

较新的 MCP 客户端如果只支持后续修订，可能直接断开；即使不断开，也会误以为服务端只支持旧能力。

**建议**

- 读取客户端请求的版本，在服务端支持集合内回显；不支持时返回明确的错误或回退到最新支持版本。
- 至少核对并覆盖 `2025-03-26`、`2025-06-18` 等后续修订，再决定升级目标。
- 加协议协商测试：客户端请求新版本时返回预期版本，请求未知版本时行为可预测。

---

### P2-8　Hub 的 `slots` 把“排队中”算成“运行中”

**现象**

项目获取 review lock 后立刻把 `running` 置为 `True`：

- `src/codeerratum/web/server.py:728-746`

排队阶段只改 `status="queued"`，不改 `running`：

- `src/codeerratum/web/server.py:786-802`

Hub 用 `running` 计算剩余槽位：

- `src/codeerratum/web/hub.py:103-131`

**影响**

排队中的项目没有占用全局执行槽，但 UI 会显示“槽位已满”。用户看到 `max_parallel=2, running=2, slots=0`，实际可能只有一个项目在跑。

**建议**

- 增加 `queued` 字段，`slots` 只按 `status in ("running", "stopping")` 计算。
- 前端分别显示“运行中 / 排队中 / 空闲槽位”。
- 加测试：两个项目，一个 running、一个 queued，`slots` 应为 1。

---

### P2-9　运行产物没有保留策略，且目录权限偏宽

**现象**

每次运行写入 `.codeerratum/runs/<run_id>/`：

- `input.diff`
- `units.json`
- `calls.json`
- `findings.json`
- `static_diags.json`
- `review.md`

见 `src/codeerratum/pipeline.py:462-464` 和 `src/codeerratum/pipeline.py:606-619`。

没有 prune/retention 命令。目录用默认 `mkdir` 创建，文件用默认 `write_text` 写入，通常会是 `0755` 目录和 `0644` 文件。

**影响**

- `input.diff` 可能是整份工作区代码，长期累积会占用大量磁盘。
- 源码 diff 对同机其他用户可读，和 `config.json` 的 `0600` 保护不一致。
- 用户很难知道哪些运行可以安全删除。

**建议**

- 增加 `codeerratum prune --keep N --days D [--dry-run]`，按时间或数量清理运行目录。
- 清理时保留数据库里的 finding 与人工裁决，只删大体积产物；或在删除前确认这些 finding 不再需要溯源。
- 创建状态目录时收紧到 `0700`，产物文件 `0600`。
- 在 `list_runs` 中标记产物是否仍存在，避免 UI 打开已删除的 run 时才报错。

---

### P2-10　运行前没有成本预估

**现象**

只有运行结束后的 `token_totals()`：

- `src/codeerratum/pipeline.py:93-125`

运行前只决定单元数，没有告诉用户“这次大概会发多少次模型调用”。

**影响**

`--max-units 0` 或大仓库 auto=60 时，用户很难预估成本。dual/committee 还要乘以模型数，证伪又会增加额外调用。误操作的成本主要发生在运行前，而当前提示发生在运行后。

**建议**

- 在确认页/CLI 输出一个保守上界：`selected_units × reviewers`，再加“最多 N 次证伪调用”。
- 如果配置了单价，显示粗略费用区间；没有单价时至少显示调用次数上界。
- 对 `max_units=0` 的全量运行加二次确认，或要求显式 `--yes`。

---

### P2-11　机器丢弃原因没有聚合，且 `runs.n_rejected` 与人工 rejected 语义混用

**现象**

`collection.rejected` 里混有：

- `provider_error`
- `anchor_file_mismatch`
- `anchor_out_of_range`
- `anchor_mismatch`
- `duplicate`
- `refuted`

但数据库只保存一个总数：

- `src/codeerratum/store/sqlite.py:83-96`

`stats()` 的 `rejected` 又来自人工裁决的 `findings.status='rejected'`：

- `src/codeerratum/store/sqlite.py:303-331`

同一个词 `rejected` 在两处含义不同。

**影响**

- 用户看到“rejected 12”时，无法区分“AI 定位错了”“模型调用失败”“重复项”“被证伪”。
- 排查质量退化时，缺少最关键的分布维度。
- UI 和文档容易把机器丢弃误解为人工驳回。

**建议**

- 新增 `run_rejections(run_id, model_id, reason, count)` 或 `runs.rejection_breakdown` JSON。
- 把 `runs.n_rejected` 改名为 `n_dropped`，人工裁决继续用 `findings.status='rejected'`。
- `stats()` 增加 `dropped_by_reason`，并在报告 notes 中显示前 3 个原因。

---

## 四、P3：维护性、体验和小型正确性问题

### P3-1　`should_falsify` 的 `reproduced` 分支是死条件，verification 语义也偏混

**现象**

`should_falsify()` 对 `verification == "reproduced"` 直接返回 `False`：

- `src/codeerratum/review/falsification.py:40-46`

但全仓没有任何路径把 finding 设成 `reproduced`；目前只设置 `suspected`：

- `src/codeerratum/review/executor.py:489-493`

而且有 `strength="hard"` 的静态诊断证据时，`verification` 仍然是 `suspected`。

**影响**

- 该分支目前永远不会命中，容易让维护者误以为“复现过的 finding 会被跳过证伪”。
- “证据强度”和“结论验证状态”被塞进同一个字段，后续很难表达“有硬证据但结论仍需确认”。

**建议**

- 短期：删除死分支，或在 executor 中真正产生 `reproduced` 状态。
- 中期：把 `verification` 拆成“证据强度（hard/soft）”和“结论状态（unverified/suspected/reproduced/accepted）”两个维度。
- 有静态诊断硬证据时，至少不要让 UI 看起来像“纯猜测”。

---

### P3-2　`find_finding` 的 LIKE 前缀没有转义

**现象**

```sql
SELECT * FROM findings WHERE id LIKE ? ORDER BY created_at DESC LIMIT 10
```

参数是 `prefix + "%"`：

- `src/codeerratum/store/sqlite.py:262-268`

**影响**

如果 prefix 里包含 `%` 或 `_`，会被当成通配符。CLI 正常输入十六进制 id 时影响很小，但 MCP 的 `get_finding` 接受任意字符串，可能返回意外结果。空前缀还会直接匹配最近 10 条。

**建议**

- 对 `\`、`%`、`_` 做转义，并使用 `LIKE ? ESCAPE '\'`。
- 拒绝空前缀；前缀长度少于 3 个字符时要求用户确认或返回候选列表。
- 加测试：`%`、`_` 不应扩大匹配范围。

---

### P3-3　`_project_page` 依赖硬编码的 `<html lang="zh-CN">`

**现象**

```python
html.replace('<html lang="zh-CN">', f'<html lang="zh-CN" data-base="/p/{slug}">', 1)
```

- `src/codeerratum/web/server.py:1124-1132`

**影响**

只要 `index.html` 的 lang 变化、属性顺序变化、或根标签换成单引号，替换就会静默失效，多项目页面的 API base 全部指向错误路径。

**建议**

- 在 `index.html` 中显式放一个占位符，例如 `data-base="__BASE__"`，服务端只替换占位符。
- 替换后断言占位符已消失；未命中时记录错误并回退到安全默认。
- 加一个 HTML 结构测试，防止模板改动悄悄破坏多项目路由。

---

### P3-4　`model_profile.family` 永远为空

**现象**

`_bump_profile()` 插入时固定写 `family=''`：

- `src/codeerratum/store/sqlite.py:210-219`

读取时返回空字符串：

- `src/codeerratum/store/sqlite.py:222-237`

**影响**

模型画像表里 family 字段没有实际信息；如果后续按厂商做统计或 UI 展示，会得到空白。

**建议**

- 读取时用 `derive_family(model_id)` 兜底，避免改表。
- 或在 `update_profiles()` 传入 `family_by_model`，写入真实 family。

---

### P3-5　重试退避没有抖动

**现象**

429/5xx 的退避是 `delay *= 3`，没有随机抖动：

- `src/codeerratum/providers/openai_compat.py:126-153`

**影响**

多个单元同时被限流时，会在几乎同一时刻再次打到同一个端点，容易形成同步重试风暴。

**建议**

- 指数退避上叠加 `random.uniform(0, delay * 0.3)` 的抖动。
- 尊重 `Retry-After` 的分支保持现有上限逻辑。
- 测试里用注入的随机函数保证可重复。

---

### P3-6　SQLite 没有 schema 版本与迁移机制

**现象**

`_init()` 只有 `CREATE TABLE IF NOT EXISTS`：

- `src/codeerratum/store/sqlite.py:27-75`

**影响**

新增表没问题，但一旦需要给 `findings`、`runs`、`model_profile` 加列，旧数据库不会自动升级，升级用户会遇到 `no such column`。

**建议**

- 引入 `PRAGMA user_version`，每个版本一个幂等迁移函数。
- 迁移在事务中执行，失败时保留旧库并给出可操作提示。
- 在 CI 中加一个“旧版本数据库升级后仍能读写”的测试。

---

### P3-7　`_FENCE_LANG` 覆盖仍有少量缺口

**现象**

当前已覆盖 Python、TS/JS、Java、Go、Rust、Ruby、PHP、C#、Kotlin、Swift、C/C++、SQL、Shell：

- `src/codeerratum/review/executor.py:45-69`

仍缺少 `.mjs` / `.cjs` / `.mts` / `.cts`、`.vue`、`.svelte`、`.dart`、`.scala`、`.lua` 等。

**影响**

只影响提示词里的代码块语言标记，不影响锚点校验，属于小体验问题。

**建议**

- 补常见后缀即可，不需要穷举。
- 更好的做法是从文件后缀和仓库语言检测结果共同推导，而不是维护一张手工表。

---

### P3-8　Web 响应头缺少 CSP / 防嵌套

**现象**

当前只设置了 `Cache-Control`、`X-Content-Type-Options`：

- `src/codeerratum/web/server.py:918-925`

没有 `Content-Security-Policy` 和 `X-Frame-Options`。

**影响**

现有 HTML 转义做得比较完整，暂未发现可利用的 XSS；但本地 UI 能读写配置和触发审查，增加响应头是低成本的纵深防御。

**建议**

- 增加 `X-Frame-Options: DENY`。
- 增加 CSP：至少 `default-src 'self'; frame-ancestors 'none'`；内联样式较多时可先允许 `'unsafe-inline'`，再逐步收敛。
- 不需要为本地 UI 引入复杂的 nonce 机制，先把“不能被嵌套、不能加载外域脚本”定住。

---

## 五、已验证、不建议本轮改的点

这些问题在复审时重点看过，结论是**当前实现是安全的或已知权衡**，不需要为了“看起来更严格”而改：

| 项目 | 结论 |
|---|---|
| `X-Lang` 请求头注入 system prompt | `normalize_lang()` 只返回 `zh`/`en`，任意字符串都会回落到中文，不存在把请求头内容原样拼进 prompt 的路径（`src/codeerratum/i18n.py:28-33`）。 |
| Web UI 渲染模型输出导致 XSS | `esc()` 覆盖 `& < > " '`，`cardHTML`、实时卡片、diff 染色都先转义再拼 HTML（`src/codeerratum/web/static/index.html:938-939`、`:1204-1235`、`:1251-1257`）。 |
| `.codeerratum/` 被误提交 | 已自动写入 `.git/info/exclude`，不会修改用户 `.gitignore`，也不会把含 API key 的配置推进 git（`src/codeerratum/repo_ignore.py`）。 |
| `config.json` 明文密钥 | 目录 `0700`、文件 `0600`，并有掩码读取路径（`src/codeerratum/config_store.py:257-271`）。 |
| `_FENCE_LANG` 缺少 Kotlin/C#/Rust | 实际已经覆盖，早先的判断不成立（`src/codeerratum/review/executor.py:45-69`）。 |
| 同行跨类别聚类阈值 `0.8` | 这是上一轮明确保留的权衡，当前没有足够标注集证明应调整，不建议现在改。 |
| `verdict_events` 幂等设计 | 以 `(run_id, finding_id, model_id, role)` 去重，重复保存不会放大画像，设计是对的；问题只在“被证伪 finding 根本没进来”。 |

---

## 六、建议实施顺序

### 第 1 批：可信度闭环（优先）

1. P1-1 被证伪 finding 回流画像。
2. P1-5 parse_stats / parse_failures 落盘。
3. P1-3 作者模型校验与 pinned 过滤。
4. P2-1 `stats.by_model` 改为按模型聚合。
5. P2-3 abstain 分母修正。
6. P2-11 丢弃原因聚合，区分机器丢弃与人工驳回。

这一批完成后，报告里的“precision / 采纳率 / abstain / parse 成功率”才真正能互相对上。

### 第 2 批：MCP 执行闭环

1. P1-2 默认预算改为统一 auto。
2. P2-4 增加 accept/reject/reconcile/stats 工具和关键参数。
3. P2-5 进度与取消。
4. P2-7 协议版本协商。

这一批完成后，下游 coding agent 才能在不回到 CLI 的情况下完成“审查 → 修复 → 回填 → 查看采纳率”。

### 第 3 批：稳定性与成本

1. P1-4 共享 HTTP client。
2. P2-2 SQLite WAL / busy_timeout / 连接关闭。
3. P2-6 预算参数校验与文档对齐。
4. P2-9 产物保留与权限。
5. P2-10 运行前成本预估。

### 第 4 批：小修与防御性改进

P2-8 槽位显示、P3 全部条目可以合并成一轮小改动，逐项补测试即可。

---

## 七、复审方式

- 逐模块阅读：`pipeline`、`review/orchestrator`、`review/executor`、`review/falsification`、`review/aggregator`、`context/builder`、`store/sqlite`、`providers/openai_compat`、`models/registry`、`cli`、`mcp/server`、`web/server`、`web/hub`、`report/outcomes`。
- 用 `rg` 核对调用点：确认 `dump_failures()` 没有生产调用、`parse_stats` 没有写入产物、`stats.by_model` 的真实数据源。
- 实测：`uv run pytest -q` → 469 passed；`uv run ruff check src tests` → All checks passed。
- 本轮未修改业务代码，未提交任何 commit。
