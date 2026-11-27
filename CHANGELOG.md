# Changelog

## [0.7.7] — 2026-11-28

自查 0.7.6 未推送代码，修掉两个会让门禁/曲线给出错误结论的问题：

- **阻断项被渲染上限截断**：`gate_sections` / `--fail-on-blocking` 都从
  `render_sections` 取数，而它只取 `ranked_findings()[:10]`。构造出的复现：10 条
  带 `test_failure` 硬证据的 P1 排在前，1 条带 `static_diag` 硬证据的 P1 排第 11，
  门禁返回「本次没有阻断项」、退出码 0 —— 一条模型已报出、tsc 也确认的 P1 直接放行。
  新增 `render_selection()`：`MAX_RENDER` 只约束建议项，阻断项一律渲染（排在其后，
  按排名顺序）。工单正文、F 编号、门禁摘要、PR 标签四处仍读同一个列表。
- **`eval --json` 可能不是合法 JSON**：输出走 `console.print`，Rich 按终端宽度软换行，
  会在 JSON 字符串中间插入换行。用 40 列终端 + 一条中文 claim 复现
  `JSONDecodeError: Invalid control character`。夜间曲线脚本正是靠
  `json.loads(stdout)` 解析，宽 diff 就会让 job 挂掉而不是暴露 bug。改用
  `typer.echo`（与 `pr --json` 一致）。
- `eval --missed-export --json` 现在也输出 JSON（此前忽略该 flag，只打绿色提示）。
- `pr --fail-on-blocking` 先打 summary URL 再判门禁：退出码 2 会中断命令，
  原来夹在中间的评论链接永远看不到。
- 措辞修正：`MAX_RENDER` 之外的条目全部是建议项，未展开提示改为「N 条建议项」。
- 新增 4 条回归测试（截断、建议项仍受上限、排名靠后的阻断项、JSON 宽值可解析），
  均先在旧实现上确认失败。测试基线 713 passed。

## [0.7.6] — 2026-11-28

按《AI-Coding-Review-策略.md》v2 的三件事落地：

- **seeded 定时回归曲线**：新增 `.github/workflows/seeded.yml`（nightly +
  手动触发），用 `scripts/seeded_regression.py` 在一次性 fixture 仓库上跑固定
  dual 审查，再把 recall / precision / false_kill_rate 追加到
  `metrics/seeded.jsonl`。模型集由 `SEEDED_MODEL_CONFIG` secret 钉死，曲线只记
  model id、不记密钥；同一 (date, commit, roster) 重跑不重复画点。**不放进
  per-PR CI**：seeded 分数来自真实付费审查，PR 没有密钥也不该烧钱。
- **证据分层进工单与退出码**：`Finding.is_blocking()` 要求 P0/P1 + `static_diag`
  硬证据 + 可执行验收命令三者同时成立（severity ×10 的排序权重压不过这条，
  所以门禁按证据而非严重级别判定）。工单新增《门禁摘要》阻断段/建议段，每条
  finding 标『处置』；`review` / `pr` 增加 `--fail-on-blocking`，命中退出码 2
  （区别于失败的 1）。门禁执行者是下游 PR check / 编码 agent，审查器只发信号。
- **裁决回流漏报样本**：`eval --missed-export` 把 accepted finding 中未报出的
  席位导出为漏报样本（按 lens/category/目录聚类），`eval --missed` 对新 run
  计算覆盖率与 per-model recall。样本不是 seeded 真值，只用于扩大 recall 分母。
- 新增 3 组回归测试，覆盖门禁判定与编号一致性、漏报样本归因、曲线去重与密钥不外泄。

## [0.7.5] — 2026-11-28

复核 0.7.4 后修掉的六个问题，全部有可复现证据（先证明测试在旧代码上失败）：

- **P1 PR 裁决否定反向**：`parse_verdict_line` 先匹配 accept 词再找否定，导致
  `不采纳`/`不认可`/`不同意`/`not accepted`/`do not approve` 全被记成 accepted，
  直接反转采纳率并污染模型路由。改为拒绝词优先、接受词检测中英文否定前缀；
  疑问句（`这个同意吗`）不再被当成裁决。
- **P1 `eval --run-id` 直接崩溃**：`adjudication_rows(run_id=...)` 把 findings
  查询的绑定参数传给一条没有占位符的 events 查询，抛 `ProgrammingError`。
  events 改用自身的 `run_id` 谓词；补跨 run 隔离测试。
- **P1 tsc 缓存绕过项目级失效**：`_collect_ts` 把 tsc 和 eslint 一起按
  (path, mtime, size) 缓存，改一个被 import 的 types.ts 时，字节未变的 a.ts
  直接命中旧结果，把上一轮的类型错误喂给模型。tsc 改为每次走项目级指纹缓存，
  只缓存真正 per-file 的 eslint；项目指纹从 (count, newest mtime) 升级为
  含相对路径与 size 的摘要，捕捉等量替换/改名。
- **P2 显式 token 预算 0 被环境变量覆盖**：pipeline 与 resume 用
  `token_budget or env`，把 MCP/Web 明示的「0 = 不设上限」折叠成回退值，
  悄悄重新启用用户关掉的预算。改为仅 `None` 回退环境。
- **P2 熔断横幅跳出引用块**：预算熔断说明行缺少 `> ` 前缀，Markdown 关闭了
  停止横幅的引用块，执行方 AI 会把熔断原因读成正文而非停止协议的一部分。

全量测试与静态检查见本轮提交说明。

## [0.7.4] — 2026-11-28

把 0.7.2 挂在文末的五个待办全部做掉，再把闭环从「本机 Web UI」推到两个此前够不着
的地方：PR 评论区（裁决真正发生的位置）与离线（有 ground truth 可比的位置）。

### Added — 闭环与可信度

- **PR 裁决回收**（`91139d3`…`50771e7`）：摘要评论内嵌
  `<!-- codeerratum:labels:F-001=<finding_id>,... -->`，维护者回一行
  `F-001 accept` / `F-001 reject: 原因`，`codeerratum pr-collect` 写回同一个反馈库
  并回灌模型路由。裁决此前只能在跑审查的机器上产生，而一条 finding 真正被判定是在
  PR 里——团队用时采纳率几乎恒为空。工单编号只有一处真源（`report.render_sections`
  同时驱动正文与标签映射），因为把裁决记到另一条 finding 上比没有裁决更糟。
  只认「标签 + 裁决词」，散文不算裁决；自家评论整条跳过，否则会把我们教的语法读成
  采纳；同 PR 两次 run 取最新映射而非并集。重跑幂等。
- **离线评测 harness**：`codeerratum eval`。种 bug 清单（`demo/seeded-bugs/bugs.json`）
  对 run 账本算 recall / precision / false_kill_rate，聚类吞并率与证伪杀率按阶段分报；
  `--export` 导出标注文件（被证伪的 claim 也在内，等人回填）。没有标注集时
  false_kill_rate 是 `null` 而不是 0——这是「会自我度量」从采纳率一个数字升级成
  可比较的指标，也是调聚类阈值前本该先有而一直没有的东西。
- **驳回模式 → 候选项目规范**：`codeerratum suggest-rules`。同一目录同一分类被反复
  驳回时聚成候选规则，默认 dry-run，`--append` 才写 `.codeerratum/rules.md`，且旧内容
  逐字保留、重复行跳过、超 4000 字符上限时拒绝写入并报超出量（不是写了再让加载端截断）。
  闭环数据第一次反过来改提示词，而不只是改路由分数。
- **token 成本护栏**：`review` / `resume` 新增 `--token-budget`（或
  `CODEERRATUM_TOKEN_BUDGET`）。第一次付费调用前先写 `estimate.json`（单元数/席位数/
  输入字符数/预估 token），并在报告与进度流里提前说出「预估已超预算」；运行中越线即
  熔断，走与人工中断相同的 partial 路径，报告注明是预算熔断并可 `resume` 续跑。
  此前只有 `max_units` 限制审多少单元，不限制烧多少 token。
  三个入口平权：Web 表单新增「token 预算」可选输入（续跑流同样接受），MCP 的
  `review_diff` schema 暴露 `token_budget`；「留空 = 读环境变量」与「0 = 明确不限」
  的区分在 CLI、Web、MCP 完全一致——同一控制此前只在 CLI 有，正是 AGENTS.md
  记过的入口不平权 bug 类。
- **TypeScript/JavaScript 静态证据**：`.ts/.tsx/.js/.jsx/.mjs/.cjs` 走 `tsc` +
  `eslint --format json`，此前 TS 项目只有裸 LLM 审查，可信度机制全在 Python 一侧。
  repo-local（`node_modules/.bin`）优先，其次 PATH，都没有就静默降级：绝不
  `npm install`、绝不联网、绝不让审查失败。没有 `package.json` 的仓库不启动该路径。
  `tsc` 是整项目编译，故按 repo 记忆 + repo 级锁（并行单元排队而非同开 N 个编译），
  失效指纹带源码文件数与最新 mtime，常驻的桌面应用不会把上一轮的类型错误喂给模型。

### Added — 成本与长任务

- **单元级检查点**：账本原来只在批次结束时落盘，跑到 95% 被杀掉就作废 95% 的 token。
  现在每完成一个单元即可刷 `findings.json` / `calls.json` / `review.md` 与 SQLite，
  节流默认「新增 3 个单元或距上次 45 秒」任一满足（`CODEERRATUM_CHECKPOINT_UNITS` /
  `_SECONDS`），review 阶段收尾强制刷一次，让账本在证伪轮花钱之前先落定。落盘顺序
  抽成 `write_run_ledger()`：findings 先、`calls.json` 最后，守住「账本落后于结论」
  这个不变量——崩在中间只会多审几个单元，不会把没写过的结论当成已覆盖。
- **跨 run 单元缓存**：`cache_key` 覆盖实际发出的完整请求
  （model+messages+schema+temperature+max_tokens），逐字节相同的 prompt 直接重放模型
  回复；`convert_findings` 与锚点校验每次都重跑，所以复用不绕过可信度检查。
  `Completion.cached` 记进 `calls.json` 且 usage 全 0，token 汇总不虚高；表按
  TTL(120d)/行数(20k) 双上限淘汰；`CODEERRATUM_UNIT_CACHE=0` 关闭。

### Fixed

- **P1 `already_posted` 只看第 1 页评论**：活跃 PR 超过 100 条时幂等 marker 看不见，
  同一个 commit 会把整份审查重发一遍。统一走全分页的 `list_issue_comments`。
- **P1 tiered 复核路径整轮静默失效**：把不存在的 `cache` 变量传进 `review_unit`，
  `NameError` 被吞成 `verify_error`，分层快审的验证结论一条都没落地。
- **P1 预算熔断会被写成人工决定**：被取消的审查任务在 gather 处已包装成
  `ReviewCancelled("stopped by user")`，先判异常类型就把机器的熔断记成用户停止；
  现在先判熔断标志。
- **P1 `_partial_collection` 死分支**：`mode == "tiered"` 永假（mode 实际取值是
  quick/dual/committee），tiered 已付费的 verify 结论在部分报告里被丢掉。
- **P1 `hub.remove` 的 TOCTOU**：「读状态再移除」之间启动的审查会变成孤儿——仍在跑，
  却再也拿不到。改走 `reserve_close()`，锁 + closed 标志让移除与启动互斥。
- **P2 config 就地改分数污染缓存**：`apply_measured_profiles` 直接改 config 里的路由
  分数，缓存必须返回深拷贝；`save` 加进程内串行锁，两个标签页同时保存不再互吞改动。
- **P2 provider_error 单元不进 partial sink**：停止时的 drop 计数漏掉失败单元，
  `units_completed` 与真实覆盖不一致。
- **P2 裁决原因取错位置**：`F-005：已修` 的冒号在裁决词之前，旧实现把「已修」当成
  原因写进库；现在只认裁决词之后的冒号，`F-007 这条我同意，改一下` 也不会把尾部散文
  编成理由。
- **P2 注释 note 起点**：`F-001 reject: 误报` 里作为原因起点的裁决别名不再被吞掉。
- **P2 已知事实头部谎报工具**：TS 仓库的诊断块写着「已由 ruff/mypy 检出」，把
  tsc/eslint 的结论归给从没跑过的工具——提示词里的错误事实模型无法自行纠正。
  头部现在按块内实际出现的工具命名。

### Performance

- `/api/review/status` 增量协议：加 `revision` / `since`，空闲标签页只拿两个字段
  的 stub，不再每次重传整份消息日志与 live findings（项目多起来以后 Web UI 的
  轮询开销与闪烁都出在这里）。跟踪运行中的标签页仍取全量快照——
  `running → 终态` 的翻转正是它不能漏的东西。
- `_body()` 加 60s socket 读超时：半开连接不再永久占用 handler 线程；超时只作用于
  请求体，读完即恢复原值，分钟级静默的 SSE 长帧不受影响。
- `store_for` 按仓库缓存 `ReviewStore`，config 按 `mtime_ns+size` 缓存读取：状态轮询
  不再每请求重建 schema、重读磁盘。

### Tests

新增 121 个测试函数（参数化展开后 149 项），全量 **682 passed / 0 failed**
（0.7.3 基线 533 项），`ruff check .` 与 `ruff format --check .` 均通过。
新覆盖：`test_pr_verdicts.py`(36) / `test_evidence_ts.py`(23) / `test_eval.py`(20) /
`test_budget.py`(17) / `test_unit_cache.py`(13) / `test_checkpoint.py`(7) /
`test_suggest.py`(9)，以及 `test_web.py` / `test_hub.py` / `test_mcp.py` /
`test_layout.py` 的增量协议、token 预算三入口平权与竞态用例。
TS 证据测试用假可执行脚本，不需要本机装 Node。

## [0.7.3] — 2026-11-24

### Fixed — 续跑并发

- **P1 `resume.lock` 只串行了写，没有串行读**：0.7.2 在读取 `findings.json`、
  `plan_resume()` 选出本批之后才拿锁。两个并发 sitting 会选中同一批单元：
  前一个写完合并账本后，后一个拿着等待前读到的旧 `old_collection` 再跑一遍，
  把旧 findings 写回去，而 `calls.json` 却已经记着这些单元已完成——正是声称
  要消除的 lost update，且账本内部自相矛盾。现在**先拿锁再读账本和规划**，
  排队者醒来后按已合并的账本重新计算剩余队列，无事可做就干净退出。
- 新增回归测试 `test_resume_replans_after_waiting_for_the_run_lock`：两个线程
  抢同一 run，断言第二个 sitting 不重复审同一单元；在 0.7.2 代码上该测试
  失败（`reviewed == ["u1", "u1"]`），修复后通过。
- `resume.lock` 不再仅凭 1 小时 mtime 抢锁：先读锁里的 pid 判活，活着的持有者
  永不被按时间偷锁（长批次会被重复执行），只有已退出的进程才在 2 秒宽限后回收。
- 补 `test_resume_replays_original_pin_from_run_meta`：tiered quick 下续跑必须
  回放 `run_meta.models` 的用户原始 pin，而不是把记录里的 scanner 晋升为 verifier。

### Fixed — 复核 0.7.2 的残留窗口

- **P0 原子写的跨文件不一致**：`calls.json` 是续跑判完成所依据的账本，但首跑和
  续跑都把它写在 `findings.json` 之前。崩溃恰在两次 `os.replace` 之间发生，
  下一轮就会按新 calls 判已完成、按旧 findings 丢失结果。现在 `calls.json`
  一律**最后写**：最坏情况退化为重复审查（多花 token），不再静默丢 finding。
- `atomic_write_text` 改用 `mkstemp` 的唯一临时名；固定 `.tmp` 名会被同一路径的
  两个并发 writer 互相覆盖。
- `resume.lock` 已修（见上）：0.7.2 的“pid 与过期回收”实际没读 pid，活进程
  跑过 1 小时也会被另一个进程偷锁。
- verify 阶段的 Brother-task 回收与 falsify 对齐：停止时仍在 gate 排队的付费
  校验调用一并取消，不再后台继续烧 token。
- verify 已完成判定镜像进 partial sink：tiered 停止时，那些已经付费拿到的
  verifier 判定会参与部分报告的合并，不再被打回 scanner-only 状态。
- 英文状态词边界扩到 `[A-Za-z0-9_]`：`not_fixed` / `fixed2` 不再被当成 `fixed`。
- `new_side_sources` 改用 `git diff --raw --abbrev=40`（`--full-index` 只管
  patch 的 index 行，对 raw 无效）：缩略 sha 互为前缀时不再把别的 blob 当成
  源码上下文。

## [0.7.2] — 2026-11-24

全量代码审查（review.md，基线 9909739）的修复批次：1 项 P0、11 项 P1 中的 9 项、
一批低成本 P2。架构级条目（review_status 全量轮询增量协议、运行中崩溃的可续跑、
请求体读超时、config 并发保存）保留待办，见文末。

### Fixed — 数据完整性

- **P0 产物账本原子写**：首跑与续跑写 `calls.json` / `findings.json` /
  `static_diags.json` / `parse_stats.json` / `parse_failures.json` /
  `review.md` 全部改为临时文件 + `os.replace`。此前崩溃半写会让下一次
  resume 按新 calls.json 把丢了 findings 的单元判成"已完成"，发现永久丢失。
- **P1 续跑跨进程文件锁**：`resume.lock`（O_CREAT|O_EXCL，带 pid 与过期
  回收）串行化同一 run 的并发 sitting，消除"后写者覆盖先写者"的 lost update。
- **P1 账本读取容错**：损坏的 JSON 账本以指名文件的清晰错误拒绝续跑，
  不再裸抛 traceback。
- **P2 resume 覆盖计数**：`units_done` 只累计本批真正被全部 reviewer 完成的
  单元；provider_error 单元不再虚增进度。
- **P2 续跑替换旧 finding 不再残留**：被本批替换且未被新报告复现的 finding
  行按 run_id 定向删除，`stats()` / `get_finding()` 不再统计已废弃记录。

### Fixed — 审查正确性

- **P1 falsify 阶段阻塞事件循环**：`_context_for`（jedi 全项目引用 +
  ruff/mypy 子进程，各 120s 超时）改为 `asyncio.to_thread`，与 review 阶段
  一致。此前单次 mypy 就能把整个并发运行冻结数十秒。
- **P2 verify/falsify 接入 partial sink**：停止落在 verify/falsify 阶段时，
  已完成的调用（含 token 账单）与 refuted 丢弃镜像进部分报告，不再丢失。
- **P2 verify 取消不再丢弃已完成结果**：`CancelledError` 处理改为先收完
  其余已完成判定再进入部分报告路径。
- **P2 falsify gather 补齐兄弟任务回收**：一个证伪协程取消后，其余仍在
  gate 排队的付费调用一并取消，不再在后台继续烧 token。
- **P1 resume 席位翻转**：改用 run_meta.json 里用户的原始 pin（旧运行回落
  到记录的 reviewer），tiered quick 不再把原 scanner 错误晋升为 verifier；
  `tiered` 对无 run_meta 的旧运行按 comparison 实测推断，不再一律默认 True；
  resume 现在透传 author_model。
- **P1 resume 规则与语言等价性**：新 sitting 以仓库 `rules.md` 与当前
  `get_lang()` 运行（此前 house_rules="" / output_lang="" 却沿袭旧 sha，
  报告虚报规则生效、语言混杂）。
- **P2 pinned 路径 falsifier 按评分排序**，与未 pin 路径一致；registry 声明
  顺序不再决定席位。
- **P2 route_quick_tier 显式拒绝全被排除的 pinned**，与 `route()` 行为对齐。
- **P2 token 计量**：schema 修复重试的第二次请求 usage 累加进总账（此前被
  覆盖，恰好漏记最贵的双倍上下文路径）；`raw_request_hash` 按最终发出的
  payload 计算，降级/修复路径可审计。
- **P2 "已复现" 计入采纳**：`已复现` / `reproduced` 补进 _ACCEPTED 词表，
  执行方按报告最高证据等级回填的行不再静默丢失。
- **P2 英文否定前缀词边界**：`un` / `non` / `pending` 改用词边界正则，
  "tests run" / "begun" 不再被误判为否定；英文正/负词匹配加词边界，
  "bypassed" 不再命中 "pass"。

### Fixed — 安全与健壮性

- **P1 check_model 密钥转发限制**：`__KEEP__` 解析出的已存 API key 只允许
  发往它保存时的 base_url，同 host 校验失败即拒绝探测，堵住密钥外泄原语。
- **P1 resume SSE 信号量过度释放**：`_stream_resume` 只在真正获取执行槽位
  后释放，排队中被取消不再虚增 BoundedSemaphore 计数、破坏其他项目的运行。
- **P1 MCP 畸形请求不再杀会话**：`tools/call` 的 params/arguments 解析移入
  try 块，非对象 params 回协议错误而非 AttributeError 终止 stdio 循环。
- **P1 git 子进程全面加超时**（120s）：get_diff / rev-parse / cat-file 等
  此前可无限挂起并卡死 MCP 单线程循环。
- **P1 new_side_sources 改 `git cat-file --batch`**：大 diff 从每文件一次
  进程创建改为单进程流式读取。
- **P2 CLI accept/reject/compare/stats 异常兜底**：损坏/不可用的 feedback.db
  报可读错误并 exit 1，不再向用户抛 traceback。
- **P2 set_status 的 reason 写库前截断 1000 字符**，与 outcomes 路径对齐。
- **P2 `_safe_error` 路径脱敏大小写/分隔符无关**，Windows 变体路径不再
  泄入前端错误文本。
- **P2 GitHub 幂等检测翻页**：活跃 PR 超 100 条评论后重跑不再整份重发。
- **P2 `set_lang(None)` 回退 `CODEERRATUM_LANG`**，无 `X-Lang` 头的请求不再
  被硬置为中文。
- 审查项复核后**维持现状**：`reconcile --dry-run` 在有未匹配行时保持 exit 0
  ——既有测试明确了"预演过期报告属合法"的设计意图，dry-run 是预演而非
  失败信号，不按 review.md 的建议改。

### Changed — 性能

- **P2 提示词进程内缓存**：系统提示词与证伪提示词每进程读一次，不再
  每 unit × 模型 / 每 finding 重读文件。
- **P2 聚类分词记忆化**（lru_cache），文件桶内 O(n²) 配对不再重复分词。
- **P2 `_symbol_for_line` 改二分**，大文件逐变更行线性扫描消除。
- **P2 `_hunk_matches_source` 的 `splitlines()` 提出 hunk 循环**。
- **P2 静态诊断缓存改裁剪最旧一半**，不再全清导致命中率周期归零。
- **P2 `docs collect()` 的 SKIP_DIRS 过滤前置**，不再先 ast.parse 再丢弃。
- **P2 `annotation_coverage` 目录过滤补齐** node_modules / venv /
  site-packages / .tox。
- **P2 I18N 路径匹配收紧**：`/locale` 改 `/locale/`，`locale_utils` 等代码
  目录不再被误判为非代码跳过评审。
- **P2 known_facts_block 提示词头部走 i18n**，英文运行不再混入中文指令。
- **P2 "No reviewable units" 与无完成单元的取消清理 run 目录**，不再留垃圾
  空壳在运行列表。
- **P2 MCP 参数运行时校验**：布尔字符串矫正、max_units/limit 钳制到 schema
  范围（负 limit 曾返回全表）。
- **P2 `read_markdown` 快照 repo_root**，切仓竞态不再拼错项目路径。
- **P2 store 的 profiles 写入并入 save_run 同一事务**，中途崩溃不再丢失
  该 run 的画像样本。
- **P3 `git` 布尔探测（has_commits / is_git_repo / get_commit）超时降级**。

### 待办（本轮明确不做，均已在 review.md 记录）

以下已在 **0.7.4** 完成，保留原文以说明当时的判断依据：`review_status` 增量协议、
运行中崩溃的可续跑（单元级检查点）、`_body()` 读超时、config 并发保存丢更新、
hub `remove` TOCTOU、store/config 的每请求缓存。

两条刻意不做：hub `restore()` 批量写盘（P3，启动时 N 次小 JSON 写，收益低于改动风险）；
docs AST 的每请求缓存（`docs_overview` 的注释已写明理由——几十文件的解析比让缓存与
编辑保持一致更便宜，加了缓存反而要背失效正确性的债）。

### Tests

全量 `pytest --basetemp=/tmp/ce_pytest`、`ruff check .`、`ruff format --check .`
已跑。Windows 开发机上 521 passed / 9 failed——这 9 项与未修改的 0.7.1 基线
逐一对齐（GBK 编码 ×2、CRLF ×3、POSIX 权限位 ×2、Windows 绝对路径语义 ×2），
均为平台差异或既有问题；Linux CI 基线 530 passed 不受影响。本批实际净修复
了基线上 Windows 的 `test_resume` GBK 失败（补 `encoding="utf-8"`）。

`hub 隔离测试（test_project_api_is_isolated_per_slug）`在未修改基线上同样
失败，属既有问题，不在本批范围。

## [0.7.1] — 2026-11-23

### Fixed

- **CI 从第 12 次提交起就没有绿过**（run #12 ~ #43 连续 30 次红灯）。红灯分三段，
  成因各不相同：
  - **Lint 失败**（run #38 起）：`demo/seeded-bugs/buggy_refunds.py` 用
    `timezone.utc`，ruff UP017 要求 `datetime.UTC` 别名（Python ≥3.11）。
  - **Format check 失败**（此前被 Lint 提前失败掩盖）：17 个 Python 文件的
    排版与 `ruff format` 不一致——这些文件是逐轮手工编辑的，只跑了
    `ruff check` 没跑 `ruff format`。现已全部格式化。
  - **Test 失败**（run #12 ~ #43）：两个互相独立的原因。
    - **测试依赖运行机器的平台**（run #12 ~ #37）：`test_pick_repo_*` 与
      `test_open_repo_folder_*` 没有指定平台，macOS 上直接走 `osascript`/`open`
      分支就通过，Linux 上则先卡在「找不到 zenity / xdg-open」。实现本身早已
      支持三平台（zenity、xdg-open / gio、explorer），是测试把平台假设写死了。
      现已把平台显式参数化（darwin / win32 / linux），并补一条「Linux 缺 zenity
      时要指名道姓地报错」的用例；顺带钉住 Windows 特例——explorer 打开窗口后
      返回非零状态不算失败。
    - **`--help` 断言受终端宽度影响**（run #43 ~ #46）：typer 用 rich 渲染
      help，rich 在窄终端下会折断放不下的词——20 列时 `--no-repo` 变成 `--no-`
      加 `repo`，中文短语会被逐字拆开。而 rich 在 `Console()` 构造时就把宽度
      **固化**了，`cli.py` 的模块级 `console = Console()` 于是在 import 时刻
      就把宽度定死，测试里再改环境变量已经太晚。现由 `tests/conftest.py` 在
      **导入期**（conftest 先于测试模块导入）把 `COLUMNS` 钉成 120，CI 的 Test
      步骤也显式设同样的值；断言本身也改为**先问命令对象**
      （`get_command(app).commands["ui"].params`）再问 **click 自己的选项列表**
      （`cmd.format_options`）——typer 只覆盖了 `format_help`，`format_options`
      仍是 click 原生实现，输出纯文本，不经过 rich，也就不受 rich 版本、终端
      宽度、`TERM=dumb`、`force_terminal` 这些环境影响。

- 用 `ghcr.io/astral-sh/uv` 容器（Ubuntu + Python 3.11 + git）本地复现 CI，
  确认红灯来源；容器内 530 passed / 0 failed。

- **本地「5 项既有基线失败」是假的**：那 5 项（hub 两条、snapshot、desktop、
  web 各一）不是产品缺陷，而是 pytest 临时目录被固定在仓库内
  （`.pytest_tmp/local`），`tmp_path` 因此落进 git 工作树，
  `resolve_git_root` / `is_git_repo` 向上遍历找到项目根，普通文件夹被误判成
  仓库子目录。改用仓库外的 basetemp 后三项恢复通过；`pyproject.toml` 早年的
  注释已警告过不要把 basetemp 钉在仓库里。以后本地跑测试一律用仓库外的
  `--basetemp`。

### Changed

- `[tool.ruff] extend-exclude = ["*.md"]`：ruff 0.17 起会格式化 Markdown 里的
  Python 代码块。本仓库的 `.md` 是审查工单与变更记录，代码片段是从 diff 里
  摘出来的单行，ruff 无法在不改变语义的前提下重排（`x = f(a)` 会被改成
  `x = (f(a),)`）。格式化器不应改写历史审查记录，故排除 Markdown。

- CI 的 Test 步骤把失败摘要以 `::error::` annotation 输出：一个打不开原始
  日志的红任务什么都说明不了，而 annotation 会直接出现在提交页上。

### Tests

Python 3.11 / 3.12 / 3.13 三版本各 **530 passed / 0 failed**，
Ubuntu 容器内 530 passed / 0 failed，`ruff check .` 与 `ruff format --check .`
全绿。CI 自 run #47 起重新变绿（#1 ~ #11 是上一次全绿，之后连续 29 次
红灯）。

### Known

- `CliRunner` 路径下 rich 渲染的 `--help` 与真实 CLI 的输出在 CI 上不一致
  （真实 CLI 正常，base64 实测确认 `--no-repo` 在选项列表里）。渲染断言已
  改为不依赖 rich，但如果将来要断言「rich 渲染出的 help 里有什么」，得先
  弄清这处差异。

## [0.7.0] — 2026-11-23

### Fixed

- 按 `erratumReview.md`（0.6.9 自审工单）逐条核实并修复 9 条成立发现：
  - **拖拽定位仓库静默失效**（F-001/F-002，P0）：`showDropMatches` 用 innerHTML
    覆盖 `#repoBody` 后 `#repoSwitch` 已不存在，`doSwitchRepo` 对 null 设
    `disabled` 抛 TypeError，单匹配拖拽这一最常见路径既不切换也不报错。
    按钮忙碌态改为存在才操作。
  - **非法 mode 的异常契约**（F-003，P0）：`ModelRegistry.route` 由字典下标
    （KeyError）改为显式成员检查抛 `ValueError`，与调用方的 `except ValueError`
    对齐；pipeline/resume 入口本有 `resolve_mode` 先行拦截，此修复保护直接
    调用 registry 的路径。
  - **get_run 并发切库拼数据**（F-005）：入口快照 `repo_root` 并只取一次
    `store_for`，产物文件与状态库保证来自同一项目；`plan_resume` 也用快照。
  - **review.md 链接漏 BASE**（F-006）：多项目 `/p/<slug>/` 下不再指向根项目；
    run_id 经 `encodeURIComponent` + `esc`，并补 `rel="noopener"`。
  - **CI 摘要失败丢 marker**（F-007）：行内评论已落地而摘要 POST 失败时，
    补发仅含 marker 的占位评论再抛原错，重跑不再重复行内评论；无行内落地
    则不留 marker，避免误跳过后续成功运行。占位评论附一行可读说明——marker
    本身是 HTML 注释，单发会在 PR 上渲染成一个空白气泡。
  - **非 loopback --host 无告警**（F-008）：`ui`/`desktop` 对非
    {127.0.0.1, localhost, ::1} 的绑定打印醒目告警（UI 无鉴权；Host/Origin
    校验只防浏览器 DNS rebinding，不防伪造头部的同网段直连客户端）。
  - **总览页 tokens 未转义**（F-009）：`tokens.total` 规范成数字并经 `esc()`
    写入 innerHTML，与相邻字段一致。
- F-004（主 excerpt 固定围栏可被源码闭合）判为**误报**：`render_excerpt`
  对每行无条件加行号前缀，行首不可能是反引号，围栏闭不掉；证伪轮的反驳成立。
  但仍按同文件 `render_callers` 的既有做法给主 excerpt 换上自适应围栏
  （`_fence(excerpt)`）——结论不依赖「行号前缀永远存在」这个格式细节。

### Added

- 恢复 `erratumReview.md`（0.6.9 自审工单）到仓库根目录，CHANGELOG 与工单
  互为引用；并用 `codeerratum reconcile erratumReview.md` 把工单《执行结果》
  表回填进 `feedback.db`（10 行，采纳 9 / 驳回 1），闭环在自审工单上跑通。
- `packaging/install.sh`：安全的桌面应用安装脚本。PyInstaller bundle 就是它
  自己的归档，`ditto` 原地覆盖会让运行中的实例读到撕裂的归档（见 AGENTS.md
  「桌面应用安装」一节）。脚本按「结束旧实例 → 改名移走旧 bundle → 拷进新
  bundle → `--selftest` 验证 → 旧件入回收站」执行，杜绝 `zlib.error: Error -3
  while decompressing data: incorrect header check`。

### Fixed

- 桌面应用启动失败的根因定位（非代码缺陷，属安装流程）：在应用运行时用
  `ditto` 覆盖 `/Applications/CodeErratum.app`，运行中实例的 PYZ 归档被撕裂，
  打印中文时 Rich 懒加载 `rich._unicode_data` 报 zlib 错误，弹「启动失败」，
  且实例不再响应 SIGTERM。已用 `packaging/install.sh` 重新安装 0.7.0 并清理
  `/Applications` 下 4 个历史遗留的 `.old_*` bundle。

### Tests

- tiered quick 的轻模型调用断言由 `>= 1` 收紧为 `== len(units)`（F-010），
  钉住「每单元恰扫一次」，杜绝第二次全量扫描的成本回归。
- 新增 9 条回归：未知 mode、CI marker 补发（有/无行内两例）、拖拽按钮判空、
  BASE 链接、tokens 转义、get_run 并发切库快照、非 loopback 告警、
  主 excerpt 自适应围栏。

测试 516 passed / 5 项既有基线失败不变，ruff 全绿。桌面应用已重新打包并
替换 `/Applications/CodeErratum.app`（0.7.0）。

## [0.6.9] — 2026-11-23

### Added

- **断点续跑（分段 × 续跑结合）**：一次运行可分多次坐席完成，专配 token plan
  的连续用量窗口。产物本身就是账本——`units.json`（完整队列）、`calls.json`
  （成功评审记录，失败单元天然不在其中）、`findings.json`（席位与全部结论），
  无需额外 checkpoint 文件。
- 新命令 `codeerratum resume <run_id>`：跳过已完成单元（不重跑、不重复计费），
  以原席位（子集 registry 固定 reviewer 与证伪者，防止画像更新后重路由换座）
  跑剩余队列，合并两批结论后原地重写 `review.md` / `findings.json` / `calls.json`
  （run_id 不变）。`--more N` 限制本批单元数，`--path` 按文件前缀收窄。
- Web 运行详情新增「继续审查（N 个单元）」按钮与 `POST /api/review/resume/stream`
  （SSE，帧协议与首轮审查一致）；`GET /api/runs/<id>` 返回 `resumable` 覆盖信息。
- 首轮运行新增 `run_meta.json`（diff_spec / models / tiered / paths），续跑据此
  重建 reviewer 上下文；旧运行缺该文件时降级为空 sources（锚点不受影响）。
- 合并语义：重跑单元的机器侧记录（provider_error、旧结论、旧证伪）整体被新批
  替换；未重跑单元的原有结论与人工裁决不受影响（`save_run` upsert + findings
  INSERT OR IGNORE）。

### Tests

- 新增 `tests/test_resume.py` 五例：按模型交集计覆盖（非并集）、合并与陈旧失败
  替换、store upsert 保留人工裁决、缺模型硬报错、空队列干净拒绝。

测试 507 passed / 5 项既有基线失败不变，ruff 全绿。未重打包：0.6.7 桌面应用
仍在执行审查，替换安装推迟到其空闲时。

## [0.6.8] — 2026-11-23

### Added

- **停止审查后保留部分报告**：点击「停止审查」不再等于丢弃已经付费的结果。已完成
  单元会立即镜像到独立收集器，停止后继续聚类并写出 `review.md` /
  `findings.json` / `calls.json`，状态为 `stopped`；一个单元都没完成时才以
  `cancelled` 收尾、不写空报告。
- 报告顶部新增醒目的停止横幅，写明停在哪一阶段、完整覆盖了 N/M 个单元、是否有
  单元只被部分模型完成，以及证伪/验证未执行；下游执行方不会把“停止”误读成
  “没有发现问题”。
- Web 端新增 `stopped` 事件与状态：进度标题显示「审查已停止，已保存部分报告」，
  并自动打开刚写入的部分报告；`/api/review/status` 保留 `run_id` 与 token 统计。

### Fixed

- **停止时同模型下的其它单元继续跑**：`asyncio.gather` 只传播第一个异常，不会
  取消同级任务；一个单元观察到停止后，等待信号量的下一个单元仍可能开始新的付费
  调用。现在失败/停止会设置中止标志并在退出时取消所有同级任务。
- 部分运行不写 `model_profile`：未完成的样本（尤其是证伪根本没跑）会把
  “没有反驳”错误地记成模型质量，污染后续路由评分。

### Tests

- 完成一个单元后停止返回 `stopped` 部分集合；零单元停止仍抛 `ReviewCancelled`。
- `_review_with` 在停止时保存已完成单元，并取消等待中的同级单元。
- 停止的 pipeline 写出带停止横幅的 `review.md` / `findings.json`，且不更新画像。
- Web 流发出 `stopped` 事件，状态保留 `run_id` 与 token。

测试 502 passed / 5 项既有基线失败不变，ruff 全绿。本轮未重打包：0.6.7 桌面
应用正在执行审查，替换安装推迟到其空闲时。

## [0.6.7] — 2026-11-23

### Fixed

- **WORKTREE 快照在 .gitignore 列了 `.codeerratum/` 的仓库直接失败**
  （用户实测报告）：快照曾把 `.codeerratum` 作为显式 `:!` pathspec 传给
  `git add`，git 把它当成"明确要求添加的忽略路径"，打印
  "The following paths are ignored..." 提示并以退出码 1 失败——凡是自己的
  .gitignore 写了 `.codeerratum/` 的仓库（包括本仓库）都无法做 WORKTREE 审查。
  改为 `core.excludesFile` 方案：临时 GIT_DIR 指向合并了
  `.codeerratum/` 与用户 `.git/info/exclude` 的排除文件，路径里不再点名忽略项，
  `git add` 恢复退出码 0。
- 排除规则顺带补齐一个安全缺口：临时 GIT_DIR 原本读不到用户的
  `.git/info/exclude`，仅用本地排除文件的仓库会让 `.codeerratum/config.json`
  （明文 API key）进入 diff 并发给模型；现在该文件的内容被并入
  excludesFile，与 `.gitignore` 叠加生效。

### Tests

- .gitignore 列 `.codeerratum/` 的仓库快照成功且不泄露 config.json。
- 仅 `.git/info/exclude` 有规则的仓库，本地排除在快照中仍然生效。
- 无 git 的目录快照继续隐藏 `.codeerratum/`。

测试 497 passed / 5 项既有基线失败不变，ruff 全绿；app 重打包替换
/Applications/CodeErratum.app（selftest OK，旧备份 .old_066）。

## [0.6.6] — 2026-11-23

按 `REVIEW_0.6.5.md` 复核修复：机器丢弃与单元失败的口径彻底分离（0.6.5 让
`verify_error` 可见的同时，暴露了它与"丢弃发现"混算的矛盾）。

### Fixed

- **单元失败与发现丢弃分开统计**（P2-1）：`provider_error` / `verify_error`
  是单元级调用失败，从未删除任何 finding（verify_error 甚至保留发现，只是
  未验证），不再计入 `dropped_total` / `n_dropped` / notes「机器丢弃 N 条」。
  kind 由 reason 推导（`models.finding.is_unit_failure`），旧数据与
  `run_rejections` 表无需迁移。
- **`review.md` 丢弃区重写**（P2-2）：新增「失败的单元」小节，
  provider_error / verify_error 条目以 `unit_id` + `error_hint` 渲染，
  不再输出空 bullet；发现丢弃与单元失败分属两个小节。
- **notes 三分**（P2-3）：`provider_error` →「相关文件未被审查」；
  `verify_error` →「验证器调用失败，发现保留为未验证」；机器丢弃汇总只计
  finding 级丢弃。
- **CLI summary 标签**（P3-1）：`Rejected: M` → `Machine-dropped: M`，
  有单元失败时追加 `Failed units: N`；`summary()` 新增 `dropped` /
  `failed_units` 字段，`rejected` 保留为 dropped 的兼容别名。
- **旧库回退**（P3-2）：`list_runs` 在运行没有 `run_rejections` 明细行时
  回退到旧 `n_rejected` 列，避免 0.6.3 前的历史运行显示"0 丢弃"。
- **报告截断提示**（P3-3）：丢弃/失败条目超过 10 条时明确标注
  「共 N 条，仅显示前 10 条」。
- **`verified_units` 口径**（P3-4）：新增 `verify_failed_units`，
  交叉审查统计不再把失败的验证调用算成成功。

### Tests

- 单元失败不进丢弃统计（stats / list_runs / summary / notes 四处口径）。
- 旧库无 `run_rejections` 明细时 `n_dropped` 回退。
- markdown 渲染：单元失败带 unit_id 与 error_hint、非空 bullet、两小节分离、
  截断提示。
- tiered verifier 失败时 `verify_failed_units` 计数。

测试 494 passed / 5 项既有基线失败不变，ruff 全绿；app 重打包替换
/Applications/CodeErratum.app（selftest OK，旧备份 .old_065）。

## [0.6.5] — 2026-11-23

### Fixed

- **机器丢弃与人工驳回的口径贯通到展示层**（P2-11 收尾）：
  - tiered 的 verifier 调用失败此前只进 `calls.json`（verdict=error），
    `run_rejections` 与报告 notes 都统计不到；现在与 `provider_error` 同等
    记为 `verify_error` 机器丢弃；
  - 报告 notes 新增机器丢弃汇总：总数 + 前三个原因（如
    `机器丢弃 5 条（refuted×3、verify_error×2）`）；
  - Web 运行详情的「{n} 条被拒」改为「{n} 条机器丢弃」，rejectedBox 标题
    不再把证伪/模型失败误标成"定位校验丢弃"；
  - `list_runs` 新增 `n_dropped` 字段（取自 `run_rejections`，逐原因的权威
    来源）；`n_rejected` 列保留兼容，新调用方应使用 `n_dropped`。
- **`--max-units` 负数不再被当成全量**（P2-6）：`_unit_budget` 对负值抛
  `ConfigError`（CLI / MCP / Web 三个入口共用），0 才表示全量。

### Tests

- tiered verifier 抛异常 → rejected 含 `verify_error` 记录且 `calls` 有
  verdict=error（两处都可见）。
- `_unit_budget(-1, n)` 抛错、`0/None/正数` 语义不变。
- `list_runs` 返回 `n_dropped` 且与 `run_rejections` 聚合一致。
- 报告 notes 在有机器丢弃时输出汇总行。

## [0.6.4] — 2026-11-23

### Fixed

- **quick/tiered 解析健康度不再丢失**（P1）：parse 汇总提成 `_parse_snapshot()`，
  在 quick 的早返回和多模型返回前都调用。此前只有 dual/committee 写入
  `parse_stats.json`，默认 quick 模式即使丢 finding 也显示解析全绿。
- **quick 不再污染跨模型对照**：只有 parse 数据的 quick comparison 不再写入
  `comparisons` 表，`codeerratum compare` 不会再把每次 quick 运行显示成
  `raw={} / 聚类后=0`；tiered 仍保留 scanner/verifier 对照。

### Added

- **MCP 闭环工具**（P2-4）：新增 `accept_finding`、`reject_finding`、
  `reconcile_report`、`get_stats`。accept/reject 幂等，reject 必须带原因；
  前缀匹配到多条 finding 时直接报歧义，不猜第一条。
- **`review_diff` 暴露关键参数**：新增 `author_model`、`paths`、`tiered`、
  `include_non_code`，与 CLI/pipeline 保持一致。
- **reconcile 零写入防护**：执行结果表为空、或所有编号都匹配不到本仓库
  finding 时返回工具错误，避免陈旧工单被当成闭环成功。
- README 的 MCP 工具清单同步为八个工具及闭环语义。

### Tests

- 新增 quick、tiered 两条 parse 汇总测试，以及 quick 不写对照表的回归测试。
- 新增 MCP 参数转发、accept/reject 幂等、前缀歧义、reconcile dry-run/落库/
  零匹配、stats 读取共 8 项测试。

## [0.6.3] — 2026-11-23

按 `OPTIMIZATION_REVIEW.md` 的实证结论修复 5 个 P1 + 5 个 P2（P2-1/2/3/8/11）。

### Fixed

- **被证伪 finding 回流画像**（P1）：单源断言被 falsifier 推翻后原来只留一条 `rejected`，画像里既无 `n_assert` 也无 `n_refuted`，precision 系统性偏高。现在完整对象存入 `collection.refuted_findings`，画像按幂等规则累计，但不写入人工裁决表。
- **MCP 默认预算对齐**（P1）：省略 `max_units` 由硬编码 10 改为 `None`，与 CLI/schema 承诺的 auto=60 一致；0 仍表示全量。
- **作者模型不坐任何席位**（P1）：pinned 分支此前绕过 `available(author)`，且 falsifier 从 `self.models` 选——作者模型可当自己的审查者或证伪者。现在 reviewer/verifier/falsifier 全部排除作者，未知作者 id 直接报错。
- **HTTP 连接复用**（P1）：provider 惰性持有共享 `AsyncClient`，orchestrator 在 `finally` 关闭所有 provider；不再每个单元和每次重试重建 TLS 连接。
- **解析健康度落盘**（P1）：`parse_stats` 与失败样本汇总进 `comparison["parse"]`，写 `parse_stats.json` / `parse_failures.json`，summary 暴露 `parse` 块，失败率>0 时进报告 notes。
- **按模型统计口径**（P2）：`stats.by_model` 改从 `verdict_events` 聚合断言数，不再把 dual 模式的逗号拼接串当成模型；新增 `by_model_roles` 明细。
- **SQLite 并发**（P2）：连接显式关闭、加 `busy_timeout=30000`、启用 WAL；SSE 轮询与写入不再互相锁死或泄漏描述符。
- **abstain 分母**（P2）：只统计证伪角色（confirm/refute/abstain），reviewer 的 assert 不再稀释“证据不足”警告。
- **槽位显示**（P2）：Hub `snapshot()` 区分 `executing` 与 `queued`，`slots` 只扣真正占用执行槽的项目；`running` 保持包含排队项，关窗保护不变。
- **机器丢弃原因聚合**（P2）：新增 `run_rejections` 表按 reason/model 聚合，`stats()` 暴露 `dropped_by_reason` / `dropped_total`，与人工驳回的 `rejected` 明确区分。

### Docs

- 新增 `AGENTS.md`：事实源、版本与打包硬约定、禁区与刻意权衡、沙箱环境坑、文件索引。供接手的 coding agent 阅读。

### Tests

- 新增 11 项：证伪画像回流（含幂等）、作者模型排除、MCP 默认预算、client 复用与关闭、parse 汇总、按模型统计、并发写、abstain 分母、槽位拆分、丢弃原因分组。
- 全量 475 passed / 5 项既有基线失败，ruff 全绿。

## [0.6.2] — 2026-11-23

### Fixed

- **后置限定词不再被记为采纳**（P2）：`fixed without full verification`、
  `部分修复但仍有问题` 含正向词但明确未完全验证，过去直接命中 `fixed` / `修复`
  计为 accepted，抬高采纳率。现在 `_UNRESOLVED_TRAILING` 与未决词表同阶段优先判定。
- **英文否定组合改为整格规则**（P3）：`nothing to fix`、`no changes needed`、
  `no longer an issue`、`none of these are issues` 归入驳回，`no test failed` /
  `all tests passed` 归入采纳；整格匹配保证 `no test failed but not fixed` 仍为 open。
- **判定职责厘清**（P3）：固定词表只放不可推导搭配，前置/后置否定各由规则负责，
  模块文档写明三阶段优先级；`not a bug` 的驳回判定由整格规则与固定词表双路径承担，
  各加一条元测试，避免删词后静默退化为 open。
- **执行结果分类补齐后置失败与跨词否定**：`已修复但测试不通过`、`修复中`、
  `not yet fixed`、`none of the tests passed` 等状态不再因包含 `修复` / `fixed` /
  `passed` 被误记为采纳；`不是误报` / `not a false positive` 不再误记为驳回，
  而 `not a bug` / `no issue` / `无法重现` 仍明确归入驳回。
- **待验证与“无需修复”变体不再污染采纳率**：`已修复，待验证`、`待测试`、
  `fixed, pending verification` 等未验证结论保持 open；`无须修复`、`毋须修复`、
  `无需处理` 与 `false alarm` / `duplicate` 归入驳回；`已合并` / `PR merged`
  与 `改完了` 归入采纳。
- **reconcile 零写入不再静默成功**：正式执行时若所有编号都未匹配、实际回填为 0，
  命令返回失败并提示检查报告编号；`--dry-run` 与“全部待处理/未修复”仍正常返回成功。

### Tests

- `test_outcomes.py` 补 11 个后置限定/否定组合词例，以及 `not a bug` 双路径元测试。

## [0.6.1] — 2026-11-23

### Fixed

- **状态分类不再把否定/待办误判为采纳**（P1）：`classify_status` 用子串匹配正向词，
  `未通过` / `not done` / `待修复` / `无法确认` 都命中了 `通过` / `done` / `修复`，
  直接抬高采纳率。现在匹配位置若紧跟否定前缀（`未/不/无/待/尚/not /un/pending ` 等）
  则不计为正向；`无需修复` / `不需要修复` 归入 rejected。README 声明的规范状态不变。
- **同行不同缺陷不再被聚类吞并**（P1）：上一版虽删除了 `gap == 0 -> True`，但同行跨分类
  仍按 0.35 的 Jaccard 合并，而“返回值未判空导致崩溃”与“返回值未校验导致注入”这类
  共享前缀的描述相似度就有 0.43，一条会静默消失。同行跨分类现在要求 ≥0.8 的措辞一致；
  同一个缺陷被标成不同分类（措辞完全相同）仍能合并。
- **reconcile 测试不再依赖 basetemp 位置**（P1）：`tmp_path` 未含 `.git` 时，CLI 会向上
  找到项目的反馈库，导致零写入却 `exit_code == 0`。测试内自建独立 git 根，并断言汇总
  数字与真实落库状态。
- **`apply_outcomes` 按实际写入计数**（P2）：`set_status` 返回 `False`（行被并发删除）时
  不再计为已回填，改计 unmatched。
- **子目录执行 reconcile 也能找到工单**（P2）：相对路径在 CWD 找不到时改为基于仓库根
  拼接，兑现 README“任意子目录同一行为”的承诺；显式存在的路径仍优先。
- **caller 片段围栏自适应 + Windows 路径归一化**（P2）：复用报告渲染器的围栏长度算法，
  片段内含 ``` 的 docstring 不再提前闭合 prompt 代码块；`callers` 的反斜杠路径与
  `details` 的正斜杠路径统一比较，避免同一调用点重复列出、绕过最多 5 处的限制。

### Tests

- `test_outcomes.py` 补 13 个否定/待办状态词、子目录 reconcile、失败写入计数用例；
  `test_regressions.py` 补同行相似但不同缺陷、围栏自适应、Windows 路径用例。

## [0.6.0] — 2026-11-23

### Added

- **执行结果回收：`codeerratum reconcile erratumReview.md`**。工单末尾的《执行结果》表过去
  只写不读，采纳率实际衡量的只是“人记不记得敲 accept”。现在可以把执行方 AI 填好的表
  直接回填到 `feedback.db`：`已修复` / `部分修复` → accepted，`误报` / `不适用` / `跳过`
  → rejected，`未修复` / `待处理` / 无法判断的状态保持 open 并计入“跳过”，不会污染采纳率。
  表头列顺序、中英文状态、转义竖线都可容错；同一 finding 出现多行时以最后一行为准。
  编号通过每条发现里的隐藏标记 `<!-- finding:F-001=<id> -->` 解析，旧工单回退到
  `问题编号` 行，因此升级前生成的报告也能回收。`--dry-run` 只解析不写库。
- 工单的执行协议和表头现在明确状态词表；下游 AI 不再需要猜“完成”该写成什么。

### Changed

- **caller 上下文从坐标升级为调用代码片段**。过去 prompt 只给 `app.py:5`，模型知道
  有人在用，却看不到怎么用；签名/返回值变更后调用方没改，这类最高频的回归只能靠猜。
  现在会读取每个调用点 ±2 行并按文件附带代码，最多 5 处，其余仍列坐标。片段明确标注
  仅供上下文、不可作为 finding 位置引用，避免锚点校验因跨文件引用而丢结果。
- 证伪轮也使用同一份 caller 片段，不再只拿到坐标串。

### Fixed

- **聚类跨分类漏合并**：`cluster_key` 不再包含 category。同一个缺陷被两个模型分别标成
  `boundary` 和 `off_by_one` 时，过去永远进不了同一个簇，两条都掉进“独有发现”，虚增了
  差异化信号。现在按文件分组，再用行距 + 措辞重叠判定，category 只作同行时的辅助证据。
- **同一行不同问题被吞并**：删除了 `gap == 0 -> True` 的无条件合并。同一行、不同分类的
  两条发现现在必须措辞重叠达到阈值才合并；同一行且同分类仍直接视为同一问题。

### Tests

- 新增 `tests/test_outcomes.py`（7 项）：回填 accepted/rejected/open、旧报告 prose 回退、
  状态词表、列顺序与转义竖线、未知编号、重复行最后一条生效，以及 CLI 默认路径 /
  `--dry-run` 不落库 / 正式执行落库。
- 回归测试新增 caller 片段与两条聚类边界用例。

## [0.5.3] — 2026-11-23

### Fixed

- **空 diff 不再是一条死路：弹窗询问是否审查全部代码**。当所选范围没有改动（例如全部
  已提交、工作区干净），SSE 与状态轮询两条路径都会弹出确认框：“是否改为审查整个工作区的
  全部代码？”；选“是”即以 `diff=WORKTREE` 重新发起审查（遵循 .gitignore），选“取消”
  维持原状。重试再次为空时不再追问，避免死循环。全程用页面内 DOM 弹窗，不调用
  pywebview 原生对话框——后者正是 0.5.0 在 macOS 卡死的原因。新增 layout 测试锁定行为。

## [0.5.2] — 2026-11-23

### Changed

- **quick 模式三项提速优化**，针对实测一轮 31 单元 / 10.5 分钟 / 67 条发现的回顾：
  1. **默认跳过非代码单元**：文档（`.md`/`.rst`/…）、配置（`.json`/`.toml`/`.yaml`/…）和
     `i18n`/`locales`/`translations` 目录不再占用审查预算；这类内容没有可执行语义，
     交给 lint/schema/doc-drift 更准。跳过数量写入报告 notes，CLI `--include-non-code`
     / Web `include_non_code: true` 可恢复旧行为。
  2. **超大单元按符号切分**：改动超过 220 行的单元不再一次送给模型——Python 用既有 AST
     索引，其他语言用轻量顶层声明索引，找不到符号边界则按 220 行窗口回退。每个子单元
     独立成一次调用，消除 900 行单元造成的 4–5 分钟尾延迟；小文件行为完全不变。
  3. **分层复核（quick）**：最快的可选模型先全量扫描，用户指定/最强模型只复核“命中”单元，
     而不是对所有单元跑第二遍；整轮干净时零复核调用。复核命中的发现获得第二个
     `assert` 裁决并升级为 `unanimous`，复用聚类判重（`same_issue`），复核独有的发现
     标为 `unique`。只有 1 个模型时自动退化为原单模型 quick，绝不把同一模型的第二次
     调用伪装成独立确认。CLI `--no-tiered` / Web `tiered: false` 可关闭。
- 新增 `tests/test_optimizations.py`（6 项）：过滤开关、切分上限与符号名、轻量/信任模型
  路由、单模型退化、命中单元只复核一次、干净扫描不触发复核。

## [0.5.1] — 2026-11-23

### Fixed

- **0.5.0 的关窗确认框会让应用卡死 48 秒（macOS 主线程死锁）**：Cmd+Q 触发的栈停在
  `applicationShouldTerminate_ → should_close → events.closing.set()`，紧接着是
  `lock_PyThread_acquire_lock → _PySemaphore_Wait → __psynch_cvwait`——主线程在等一个
  永远不会执行的回调。根因是 pywebview 的 `create_confirmation_dialog()` 在 macOS 上把
  NSAlert 用 `AppHelper.callAfter()` 排到主线程 runloop，然后 `semaphore.acquire()` 阻塞；
  而 closing handler 本身就同步跑在那个 runloop 的退出处理里——弹窗等 runloop，runloop
  等弹窗，自锁。现在 handler 只翻转 pywebview 官方的 `window.confirm_close` 标志，由
  pywebview 自己的 `should_close()` 在主线程内联 `NSAlert.runModal()`，无信号量、不自等；
  确认框文案通过 `localization` 覆盖成中英双语的审查专用提示。看门狗/信号触发的强制关闭
  设置 `_FORCED` 标志，即使审查在跑也不弹框——那种情况下没有人在屏幕前回答。
  测试改为断言 handler 不再调用任何对话框方法，并覆盖强制关闭路径。全量 365 passed，
  遗留 5 项为改动前既有的基线失败。

## [0.5.0] — 2026-11-23

### Added

- **关窗前若审查仍在跑，先弹确认框**：过去桌面端点关闭按钮或按 Cmd+Q，会直接杀掉进程、
  中断进行中的付费审查，而且没有任何提示。现在 pywebview 的 `closing` 事件注册了守卫：
  检测到任一项目 `running`（含排队中）就弹「审查进行中，确定退出？」——取消则窗口保持，
  确认才退出；空闲时行为不变。看门狗与信号触发的 `window.destroy()` 不经过此守卫，
  崩溃、父进程退出等强制关闭不会被对话框卡住。判定在 hub 与单仓库两种模式下都生效，
  文案跟随界面语言。新增 4 项测试覆盖确认框逻辑、事件注册、回调透传与两种模式的忙碌判定。

## [0.4.9] — 2026-11-23

### Fixed

- **发现的 detail 论据不再固定英文，跟随界面语言**：0.4.7 做双语时，输出语言指令
  （`language_directive`）只做到了 executor 一层——`execute_review → run_review →
  _review_with → review_unit` 整条链都没传 `output_lang`，证伪轮的 prompt 同样没传，
  所以模型始终按默认 prompt 语言（英文）写 `claim` / `detail` / `suggestion`，界面切中文
  也没用。现在 pipeline 在运行起点（请求线程里语言已就位时）捕获语言并显式传值：
  审查与证伪两条路径都收到指令，证伪裁决的标签（「反例 / 错误假设」）也随语言切换；
  语言随 run 固化，审查进行中切换界面语言不影响本次输出。enum 值与代码标识符不译，
  解析与锚点校验不受影响。新增 `tests/test_output_language.py`（8 项）锁住
  指令内容、审查 prompt、证伪 prompt、pipeline 捕获四个环节。

## [0.4.8] — 2026-11-23

### Fixed

- **运行结束后右侧停在实时卡片，详细报告不再自动出现**：流式路径收到 `done` 帧后只更新了
  左侧日志，右侧还停在"实时发现"列表——每条只有一句 claim（模型约定 claim ≤ 60 字符），
  而旧版运行结束时右侧直接是完整报告（代码片段、detail 论据、修改建议、证伪轮裁决）。
  这就是"右边没原来详细"的真相：不是报告少了内容，是**报告根本没被打开**。跟踪路径
  （轮询）在 0.4.6 已有 `openRun`，流式路径漏了。现在 `done` 后自动刷新历史并打开报告，
  与跟踪路径一致。新增布局测试锁住两条路径都必须 `openRun`。
- **实时卡片只有标题没有论据**：SSE 的 finding 帧补上 `detail`（触发条件 + 影响 + 原因），
  卡片里以「详情」折叠展示——默认收起不刷屏，展开即有完整判断依据。

## [0.4.7] — 2026-11-23

### Added

- **模型配置只填一次，新项目自动沿用**：过去模型清单（端点、key、席位、mode）只存在被审仓库的
  `.codeerratum/config.json` 里，所以每加一个项目，设置抽屉都是空的，同一批 key 要重打一遍。
  现在配置分三层解析：**项目文件 → 用户级 `~/.codeerratum/config.json` → 环境变量**，项目文件
  仍然优先，单个仓库可以钉住不同的模型组合。保存时默认勾选「同时设为新项目的默认配置」，
  把当前清单写到用户级（0600，目录 0700）；取消勾选则只写本项目。界面会明确显示当前来源是
  「本地配置文件 / 全局默认配置 / 环境变量」，`/api/meta` 的 `source` 相应多一个 `user`。
  配套改动：`__KEEP__`（沿用已存 key）现在也会去默认层找，否则一个正在用默认配置的项目第一次
  保存时会把自己的 key 清空；`pipeline.available_models()` 与 CLI 运行同样走这条链，模型选择器
  不会列出与真实运行不一致的清单；`CODEERRATUM_HOME` 可覆盖用户级目录（测试与便携安装）。
  测试新增 `tests/test_config_layers.py`（10 项）并在 `tests/conftest.py` 里为每个用例隔离
  用户目录，避免开发机上的真实 key 混进断言。全部 **357 passed**。

## [0.4.6] — 2026-11-23

### Added

- **关于（About）里能看到 GitHub 地址**：打包应用的 About 是系统面板——macOS 只显示一行静态版权、
  Windows 根本没有，两者都放不了可点的链接。现在三层都有：`Info.plist` 的
  `NSHumanReadableCopyright` 写成 `MIT License · https://github.com/a2012s/CodeErratum`（系统
  About 面板底部那行）；审查页顶栏的版本徽标改成按钮（`v0.4.6`），点开是带版本、简介、地址的
  对话框，可复制、可「在 GitHub 打开」；外壳顶栏加 `GitHub` 入口（桌面版打开的就是外壳）。
  地址只在 `codeerratum/__init__.py` 的 `HOMEPAGE` 定义一次，`/api/meta` 下发，spec 从源码正则读。
  打开动作走 `POST /api/open/homepage`——URL 是常量、不接受请求参数，避免变成"打开任意链接"的
  通用原语；webview 里的页面本来也没法自己调起系统浏览器。新增 4 项测试。
- **实时发现现在两条路都能看到**：SSE 只属于打开它的那个请求，刷新页面、开第二个标签页、或在别处
  启动的运行都走 `/api/review/status` 轮询——那条路过去只有进度文字，右侧一无所有。现在每条发现
  同时写进状态快照（`WebApp._note_finding`，按 `id + model` 去重、上限 500 条），轮询端用同一套
  卡片渲染并去重；运行结束时跟踪端会自动 `openRun` 打开报告。顺带修了一个会让"所有发现都看不见"的
  接线错误：回调里 `self` 是 HTTP handler 而非 `WebApp`，`_note_finding` 抛 `AttributeError` 又被
  上层的"展示失败不能中断付费运行"吞掉。现在该分支会把异常打到 stderr，同类接线错误不再静默。
  新增 3 项测试（状态里有发现、换轮次清空、按模型去重）。
- **发现逐条实时出现在右侧**：过去要等 `done` 帧才有结果，中途只有「单元 12/53」在动，看不出
  到底有没有发现问题。现在每个单元解析完就发一条 `finding` SSE 帧
  （`server._stream_findings`，只带 severity/file/line/claim/model，完整记录仍随运行归档），
  右侧换成实时列表、一条一张卡片；运行结束后正式报告替换它。界面明说这些发现**尚未聚类、
  未证伪**。回调在 `orchestrator._review_with` 里按单元触发，异常一律吞掉——展示层不该让跑了
  一半的付费审查失败。新增 2 项测试（`finding` 帧必须早于 `done`；回调按单元触发，报告仍来自
  完整集合）。
- **Windows 打包**：`packaging/` 早有 win32 分支，缺的是两件只会在 Windows 上暴露的东西：
  `webview` 包内自带 `WebView2Loader.dll` / `Microsoft.Web.WebView2.Core.dll`，由
  `collect_data_files("webview")` 收集（漏了的表现是"图标闪一下、没有窗口"）；`--selftest`
  增加 `webview.platforms.edgechromium` 导入检查——`import webview` 成功不代表后端可用，
  真缺 pythonnet 时是开窗那一刻才炸。窗口版没有 stdout，selftest 结果写入
  `~/.codeerratum/desktop.log`，构建脚本回显末尾几行。新增 `.github/workflows/build.yml`
  （windows-latest + macos-latest，手动触发或推 `v*` 标签出 zip 并挂到 Release 草稿）。

### Fixed

- **`调用失败：ReadTimeout` 既没说在哪一步超时，也说不清为什么**：唯一的 `timeout=120` 同时管
  连接和读取，而 8192 的生成上限在实测 ~50 token/s 下需要 ~165s——长答案天然跑不完，表现就是一批
  单元同时「读取超时」。重试虽然覆盖了 `TransportError`，但读取超时也吃满 3 次 ×120s（近 6 分钟），
  每次都重发输入、重新计费。现在：连接 10s / 读取 240s（`CODEERRATUM_READ_TIMEOUT` 可调，推理型
  模型建议 420）/ 写 30s / 连接池 10s；读取超时只重试 1 次（对端已安静一整窗），429/5xx/连接错误
  仍是 3 次且尊重 `Retry-After`（上限 30s）。失败文案改成会解释原因的中文/英文：
  `读取超时（240s 内没有返回任何数据；已重试 1 次）`、`鉴权失败（HTTP 401），检查 API key 与 base_url`、
  `被限流（HTTP 429，已按 Retry-After 退避重试）`；运行结束的 notice 追加去重后的失败原因列表。
  新增 5 项测试（窗口取值、读取超时只重试一次、Retry-After 上限、失败文案）。
- **两个语言切换按钮**：壳（`projects.html`）顶部有语言选择，标签页里的 `index.html` 头部
  又有一个，相隔 40px。现在内页在 iframe 里时用 `html.embedded #langSel { display: none }`
  隐藏自己的那个，判断放在 `<head>` 的内联脚本里（`window.self !== window.top`），首帧就不闪；
  语言切换仍由壳 `postMessage({type:"ce:lang"})` 驱动，直接打开 `/p/<slug>/` 时内页那个照常可用。
- **「不是 git 仓库」不再是死路**：这句报错只说了不行，没说怎么办——把一份拷来的项目
  文件夹选进界面，用户只会看到「不是 git 仓库：<路径>」。现在拒绝的理由和补救一起给出：
  目录里没有 `.git` 就没有 diff 可读，在该目录 `git init && git add -A`（暂存即可，
  不必 commit）；要审已有的全部代码，把审查范围填成空树
  `4b825dc642cb6eb9a060e54bf8d69288fbee4904`。空树哈希抽成 `ingest.EMPTY_TREE_SHA1`，
  Web、CLI、`pipeline` 三处校验共用；弹窗 `#modalMsg` 加 `white-space: pre-line`
  让多行提示不再挤成一句，审查范围下方也补了这条说明。新增 2 项测试
  （空树能审出单元、报错必须带上两条补救）。
- **英文界面侧栏横向溢出 23px**：`.row2` 首列 `1fr` 会被下拉框的 min-content 撑住，
  英文选项 `committee · three models` 把整行推出 292px 侧栏（中文不触发）。改为
  `minmax(0, 1fr)`；实测中英两语 `asideOverflowX` 均为 0。
- **多项目视图里每个项目只渲染出 150px 高**：外壳把项目放在 `<div id="frames">` 里，而该容器
  没有高度，`iframe.project { height: 100% }` 无从解析，浏览器退回 iframe 默认的 150px ——
  标签栏下方只有一条界面、其余全黑。现在 `#frames` 有 `height: 100%`，并在总览页 `hidden`
  （两个 100% 子元素并存会把外壳撑出一屏滚动）。打包产物同样受影响，已重新构建。
- **审查页高度硬减 51px**：`.layout { height: calc(100vh - 51px) }` 假设 header 恒为 51px，
  实际中文/英文 57px、窄窗口换行后 78px，多出的部分让页面出现滚动条。改为 body 纵向
  flex + `.layout { flex: 1; min-height: 0 }` + `grid-template-rows: minmax(0, 1fr)`；
  header 实测高度写入 `--header-h`，右侧抽屉的 `top` 跟随它（原先固定 51px，英文下会压住
  header 底部）。
- **英文界面下外壳白屏**：`<p data-en="…">` 的英文值丢了 `<b id="maxParallel">`，`applyLang()`
  用 `innerHTML` 覆盖后元素消失，`refresh()` 读到 null 抛 `TypeError`，标签、卡片、iframe
  全部不渲染。文案拆成三段让数字元素不被覆盖，该行也改成可选访问。新增
  `tests/test_layout.py`（5 项）把这三条成因锁进测试。
- **关闭/刷新页面不再把审查整轮判失败**（本次「审查失败」的真实原因）：SSE 写帧遇到
  `BrokenPipeError: [Errno 32] Broken pipe` 时会从 `progress` 回调一路抛回 `run_review`，
  被当成 `internal error`，已跑完的评审与证伪结果全部丢在 `status=error`。
  实测 traceback 确认：标签页关闭后下一行 `say(...)` 写帧失败即中止。
  现在 `_write_sse()` 捕获 `OSError` 并返回 False，处理函数置 `_client_gone` 后停止写帧，
  **审查继续在服务端跑完**，其他标签页或刷新后的页面通过 `/api/review/status` 接着看；
  `_send()` 同样吞掉断连写失败，不再为每个已关闭的请求打印 traceback。新增 2 项测试。
- **失败原因不再丢失**：`GET /api/review/status` 过去只保留 running/status/进度，异常一句话发进 SSE 就被丢掉；
  其他标签页看到「审查失败」四个字却无从判断。现在失败原因写入状态并回显（含其他标签页的轮询路径），
  服务端打印完整 traceback；下发前把仓库根/家目录替换成 `<repo>` / `~`，不泄露本地路径。
  同时修正 `finished_at` 把 `time.monotonic()` 当墙上时间下发的问题。新增 3 项测试。
- **证伪进度截断**：SSE 里的 `证伪 …` 行过去硬切 `claim[:40]`，英文文案断在半个单词上。
  现在输出完整 claim（Finding 本身限制 ≤60 字符），只把换行折成空格以保持单帧。新增 1 项测试。

### Changed

- **单元上限不再静默截断**：界面里把「上限」填成 200，过去服务端直接按 50 跑，界面一个字都不说。
  现在两侧都明说：输入框下方即时显示琥珀色提示（「超出 1–50，服务端会按 50 执行，并在进度里提示」），
  服务端夹取后把原因写进进度日志首行并着色为警告——`上限 200 超出范围（最多 50 个审查单元），已按 50 执行`，
  同时随 `notices` 字段进 `/api/review` 的 JSON 汇总和 `/api/review/status` 的 `params`，其他标签页也看得到。
  夹取边界抽成 `MAX_UNITS_CAP`；顺带修掉前端 `Number(v) || 10` 会把 0 悄悄变成 10 的问题。新增 5 项测试。
- **进度日志按重要程度着色**：阶段行（启动、Split diff、证伪轮）蓝色；警告/降级/证据不足/被证伪（refuted）琥珀色；
  通过（confirmed）、整体完成绿色；失败、调用失败、超时、异常红色并加粗；逐单元例行日志保持灰色。
  明暗主题分别定义 `--warn` / `--ok` / `--bad`，避免暗色下红绿字看不清。显式级别优先于自动推断。新增 1 项测试。

- **审查中重复点击的反馈**：过去在审查进行中再点「运行审查」，报英文 `a review is already running in this process`，
  按钮也没进入审查态。现在 WebApp 维护全进程唯一的审查状态（running / status / 进度消息 / 范围 / 计时），
  新增 `GET /api/review/status`；各标签页每 1.5s 轮询——审查在进行中时「运行审查」显示「审查中…」并禁用，
  进度日志跟着刷新（含其他标签页或断线连接启动的审查）。抢跑 409 不再弹错误框，转为跟踪已有审查；
  进度回调统一写状态，锁冲突文案改中文。新增 2 项测试。

### Changed

- **可拖动左右分栏 + 进度面板常驻**：`.layout` 从固定 `292px | 1fr` 改为 `sidebar | 拖拽柄 | content` 三列，
  拖动竖直分割线实时改变两侧占比（220px ~ 视口-420px），双击复位 292px，宽度记在 localStorage。
  进度面板在侧栏内改为 `position: sticky; top: 0`，滚动历史运行不再被顶出视野；日志默认 300px 高，
  底部新增拖拽手柄调高度（双击复位），同样持久化。拖动用 Pointer Events + window 级 move/up，
  不依赖 setPointerCapture 成功与否；`flex-shrink:0` 防止 sticky 面板被 flex 容器压塌。
  移动端（<860px）自动回退单列、隐藏分割线。新增 1 项静态断言测试，CDP 实测拖动/sticky/持久化通过。
- **异构判据从厂商改为模型版本**：过去 dual/committee 按 `family`（厂商）去重，同厂两个版本会被拒
  （`mode 'committee' needs 3 heterogeneous models but only 2 distinct families are available`）。
  现在按锁定的 `model_id` 判定：`qwen3.8-flash` 与 `qwen4-max-preview` 算两个独立评审者，版本侧重点不同
  正好互补；相同 `model_id` 重复配置仍被拒。`family` 退为展示/报告标签（留空自动推断），不影响同席。
  证伪轮优先用未参与审查的空闲模型，没有则从提出发现之外的评审者中挑一个交叉证伪。
  更新 registry / orchestrator 注释、配置抽屉说明、中英文 README。新增 4 项测试。

### Added

- **可双击安装的桌面应用打包**：新增 `packaging/`（`desktop_entry.py` 入口、`CodeErratum.spec`、`build.py`），
  `uv run python packaging/build.py` 产出 `dist/CodeErratum.app`（macOS）/ `dist/CodeErratum/`（Win、Linux）。
  用 onedir 而非 onefile——onefile 每次启动都要解包到临时目录，冷启动慢且易被当释放器。实测 67 MB，
  主体是 Python 运行时。spec 把 `codeerratum/web/static` 与 `codeerratum/prompts` 都放进包，并按平台
  补 pywebview 的后端 hiddenimport（`webview.platforms.cocoa` 等，它用 importlib 导入，静态分析跟不到）。
  打包脚本额外做了 ad-hoc 签名（`codesign -s -`），够在本机运行；分发到别的机器仍需 Developer ID + 公证。
- **`--selftest`：防「启动正常、一花钱就炸」**：`prompts/*.md` 只在真发起审查时读取，漏打包不影响启动。
  构建脚本因此执行冻结后的 `CodeErratum --selftest`，逐个检查 `static/`、`prompts/`、`webview` 是否在包内，
  缺一个就让构建失败。实测冻结包内路径落在 `Contents/Frameworks/codeerratum/{web/static,prompts}`，
  正是 `Path(__file__)` 与 `_locate_static()` 的解析位置，源码逻辑无需改。
- **窗口版没有终端，所以有日志和弹窗**：入口脚本在冻结模式下把 stdout/stderr 重定向到
  `~/.codeerratum/desktop.log`（>1 MB 轮转一份，`COLUMNS=120` 避免 rich 在日志里断词），
  未捕获异常除了写 traceback 还会弹一个原生提示框（osascript / MessageBoxW / zenity）。
- **双击启动强制 `--no-repo`**：Finder 拉起时 cwd 是 `/`，按 cwd 播种会以 `not a git repository` 在窗口出现前退出。
  入口脚本因此默认加 `--no-repo`，只有显式传 `--repo` 才不加。隔离 HOME 实测：空总览、
  `/api/projects` 为 `[]`、`/api/meta` 返回「还没有添加项目」、SIGTERM 干净退出并释放端口。
- **桌面窗口（pywebview，可选依赖）**：新增 `codeerratum desktop`，起同一个服务、同一份界面，把回环地址交给
  系统 WebView（WKWebView / WebView2 / WebKitGTK）显示，不打包 Chromium。窗口必须直接加载 `http://127.0.0.1:<port>/`，
  不能用 `file://` 或自定义协议——服务端的同源校验要求 `Origin` 等于回环 `Host`，换来源会让所有 POST 403、
  审查按钮静默失效。`text_select` 显式打开（pywebview 默认禁止选文字，报告页要能复制行号），
  `private_mode=False` + 固定 `storage_path` 让 localStorage 里的语言与分栏宽度跨重启保留。
  关窗即停服务、释放端口。新增 `pyproject.toml` 的 `desktop` extra、`web/desktop.py`、`web/lifecycle.py`、
  `web/bootstrap.py`，19 项测试；真实窗口实测：页面渲染、`evaluate_js` 读到标题与按钮、SIGTERM 关窗后端口释放。
- **服务端给启动器留的四个口子**：`ui` / `desktop` 都支持 `--no-repo`（空项目列表启动，不要求 cwd 是 git 仓库；
  桌面应用从启动器拉起时 cwd 通常是 `/`，原来会直接报 `not a git repository` 退出）、`--port 0`（由系统分配端口，
  `server_address[1]` 读回真实值）、`--print-port`（绑定后往 stdout 打一行 `PORT=<n>`，给外壳解析）、
  `--parent-pid`（看门狗，见下）。空项目时根路径 `/api/*` 返回 404 并说明「还没有添加项目」，不再是 500。
  `make_server()` 的 `app` 改为可空（必须有 hub），`make_handler()` 同步。
- **进程看门狗与优雅退出**：`--parent-pid` 每 2 秒探一次父进程，消失即停服务——崩溃或强杀留下的孤儿会抱着
  一次付费审查空转。POSIX 用「被 reparent 则直接父进程已死」+ `kill(pid, 0)` 兜底（纯 `kill` 会把僵尸当活的，
  正好是崩溃场景）；Windows 的 `getppid()` 在解释器启动后就固定、`os.kill(pid, 0)` 更会直接终止进程，
  改用 `OpenProcess` + `WaitForSingleObject(0)`，并显式声明 `HANDLE` 返回类型避免 64 位截断。
  SIGTERM / SIGINT 从 `signal.signal` 改为阻塞信号 + 专用线程 `sigwait`：桌面窗口的主线程卡在 Cocoa 事件循环里时，
  Python 的处理器根本没机会执行（实测 SIGTERM 十秒关不掉窗口），换线程取信号后才生效。
  顺带把 `ui` 的中文硬编码提示（未配置模型、Ctrl+C）并入消息目录。
- **冻结打包的静态资源查找**：`STATIC_DIR` 不再写死 `Path(__file__).parent / "static"`，
  改为探测 `sys._MEIPASS` 下三种常见 `--add-data` 布局再回落到源码目录——否则 PyInstaller/py2app 冻出来的
  程序首页直接白屏或 500。找不到就回落到源码路径，让错误表现成「模板 404」而不是导入期崩溃。
- **中英双语界面**：顶栏新增语言下拉（中文 / English），默认中文，选择存 localStorage。切换会同时改界面文案与
  服务端消息——页面按请求带 `X-Lang`，SSE 进度行、错误提示、状态文案都由 `src/codeerratum/i18n.py` 的消息目录
  按当前语言渲染，语言存在 `ContextVar` 里，多标签页 / 多项目各自独立。项目外壳页切换语言时通过 `postMessage`
  通知 iframe 立即重绘，不重载页面（重载会丢进度日志）。静态标记用 `data-en` / `data-en-ph` / `data-en-title`，
  动态文案用 `T()`；英文补了单复数（`1 finding` / `2 findings`）。CLI 与 MCP 用 `CODEERRATUM_LANG=en` 获得
  英文消息。报告骨架与历史归档 notice 不随界面回改。新增 13 项目录测试 + 4 项 HTTP 测试。

- **多项目同时审查（便签页）**：`/` 改为项目外壳，一个项目一个便签，便签内嵌原来的完整界面，
  另有「总览」把状态/进度/token 摊成卡片。后端新增 `ProjectHub`（`web/hub.py`）：每个项目一个
  `WebApp`，审查锁、进度状态、配置与 `feedback.db` 全部按仓库隔离，路由挂在 `/p/<slug>/`；
  根 `/api/*` 仍指向第一个项目，单仓库模式行为不变（未传 hub 时与旧版逐字节一致）。
  新增 `GET/POST /api/projects`、`/api/projects/pick|find`、`/api/projects/<slug>/remove|open`。
  项目列表持久化到 `~/.codeerratum/projects.json`，重启恢复并跳过失效路径。
  并发用 `threading.BoundedSemaphore` 限制（默认 2 个项目，`--max-parallel` 可调），
  超出进入 `queued` 状态、可取消排队；`BoundedSemaphore` 在 `execute_prepared` 里
  兜底 release，异常路径不会漏槽位。前端页面通过 `data-base` 前缀所有 API 请求，
  项目页里的「切换仓库」改为「项目总览」；拖拽落在项目标签页时用 `postMessage` 转给外壳。
  新增 `tests/test_hub.py` 10 项测试（隔离、404、增删、持久化、排队与取消排队）。
- **打开当前仓库文件夹**：仓库抽屉的当前仓库信息下方新增「打开当前仓库文件夹」按钮，
  调用 `POST /api/repo/open` 用系统文件管理器打开服务端已定位的 git 根（macOS `open` /
  Windows `explorer` / Linux `xdg-open` 或 `gio`）。目标不接受页面传入，避免被当成任意目录打开器；
  失败弹可复制对话框，与其他写接口一样要求同源 Origin / Host。新增 4 项测试。
- **审查中停止**：进度面板标题栏新增「停止审查」按钮，点一次即向服务端发 `POST /api/review/stop`。
  WebApp 用线程安全 Event 传递停止信号，编排层在 asyncio 循环内轮询并 cancel 所有在途模型调用
  （httpx 请求被真正中断，不会等几十秒才响应），SSE 推 `cancelled` 帧收尾，状态记为 `cancelled`
  而非失败——不弹错误框、不写半截报告。重复点击幂等，无审查可停时返回 409，跨标签页可停同一场审查。
  新增 4 项测试（含在途调用确实被取消的单元验证）。
- **token 消耗统计**：每次模型调用的 usage 本来就逐条采集，现在汇总进运行 summary 与 SSE `done` 帧：
  输入 / 输出 / 缓存 token 总量 + 按模型分解（调用次数与各自小计），进度面板完成后逐行显示。
  `GET /api/review/status` 也带上最后一次运行的 token 汇总，跨标签页跟踪时同样可见。新增 2 项测试。
- **审查进度实时推送（SSE）**：点「运行审查」后侧栏展开进度日志，逐条显示 diff 切分、模型/单元
  评审、证伪、报告落盘，不再只靠一个转圈干等。新增 `POST /api/review/stream` 走 `text/event-stream`，
  进度回调贯穿 pipeline → orchestrator（每模型每单元完成即上报）；`log` / `done` / `empty` / `error`
  四类帧，结束帧关闭连接让浏览器 reader 正常收尾。参数校验与审查锁在发流之前完成，非法输入仍是真
  400/409；错误帧走可复制对话框、不回显本地路径。原 `/api/review` 保留兼容 CLI/测试。新增 5 项测试。
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
- **默认写工作单 + 文件名改名**：界面「同时写 erratumReview.md 到仓库根」默认勾选；
  产品报告文件名由 `review.md` 改为 `erratumReview.md`（CLI `-o` 默认值与 README 同步）。
  每次运行的内部归档仍为 `.codeerratum/runs/<run_id>/review.md`，历史运行不受影响。
- **顶栏面板互斥**：切换仓库 / 配置 / 文档 / 统计同一时刻只显示一个，当前按钮高亮；
  再点当前按钮收起，点另一个直接切换，点击面板外空白处也关闭（拖拽定位也会切到仓库面板）。

### Fixed

- **模型检测的端点错配诊断**：控制台网页地址收到 POST 会返回 302，原先报含糊的 JSONDecodeError；
  现在直接指出 base_url 可能填成了网页地址并附上重定向目标。`sk-sp-` 密钥配通用 dashscope 端点
  返回 401 时，提示改用 Token Plan 专属端点，不再让用户以为是密钥失效。
- **错误提示改为模态对话框**：原先所有错误走 2~5 秒自动消失的 toast，看不清也复制不了。
  现在错误统一进入带遮罩的对话框，需点「关闭」（或 Esc / 点遮罩）才消失；错误文本框可选中，
  另有「复制错误」按钮。审查失败附带 diff 范围、路径与模式，模型检测失败列出每个端点原因。
- **单提交仓库默认 diff 报错**：UI 输入框默认 `HEAD~1`，仓库只有一个提交时 git 直接报
  `fatal: ambiguous argument 'HEAD~1'`。默认值改为 `HEAD`（本地未提交改动），
  后端遇到该情形给出中文提示并保留原始 git 错误供复制。新增 2 项测试。
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
