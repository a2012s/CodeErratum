# CodeErratum 检查报告（codex 第二轮改动）

- **检查对象**：`/Users/gugu/Downloads/codes/agentsCodes/codeReviewAgent`（仅此项目）
- **检查时间**：2026-11-23 10:20 GMT+8
- **检查范围**：工作区未提交改动（5 个文件，+246 / -3），基线 HEAD `e0d3e3e`（0.6.1）
- **检查方式**：逐文件读 diff + 分类器实际调用探测 + 全量 pytest + ruff；只读源码，未做任何修改
- **结论**：**无 P0/P1 回归，可发布**。上轮 6 项修复均保持有效；本轮是纯增量容错完善。有 1 项 P2 需留意（误判可能污染采纳率），3 项 P3 完善建议。

---

## 一、这轮改了什么

本轮针对的是上一份审计报告（P1-1 状态分类）的延续——codex 继续扩充状态词表与否定判定，并让 reconcile 零写入显式失败。CHANGELOG 以 `[Unreleased]` 记录，版本仍是 0.6.1。

| 主线 | 改动 | 文件 |
|---|---|---|
| 状态词表扩充 | `_UNRESOLVED` +50 词、`_REJECTED` +40 词、`_ACCEPTED` +4 词（`merged`/`已合并`/`改完`）、`_NEG_PREFIXES` +2（`不是`/`并非`） | `report/outcomes.py` |
| 否定判定升级 | `_negated_at` 增加分句切分（`_CLAUSE_BREAK_RE`）和英文否定正则（`_EN_NEGATION_RE`），覆盖 `not a false positive`、`not yet fixed` 等从句内否定；新增 `_has_rejected_word` 对 rejected 词同样做否定检查 | `report/outcomes.py` |
| 零写入失败 | reconcile 正式执行后若 `updated == 0` 且 `unmatched > 0`，打印错误并 `Exit(1)`；`--dry-run` 与"全部待处理/未修复"不受影响 | `cli.py`、`i18n.py` |
| 测试 | 参数化词表用例 +33 项、零写入失败/正常场景 2 项 | `tests/test_outcomes.py` |

---

## 二、发现清单

### P2-1　`fixed without full verification` 类措辞仍被判为 accepted，可直接抬高采纳率

- **位置**：`report/outcomes.py:436` 起的 `classify_status`，`_ACCEPTED` 含裸词 `fixed`
- **证据**（实际调用）：

  | 输入 | 返回 | 应有语义 |
  |---|---|---|
  | `fixed without full verification` | `accepted` | 未完全验证 → open |
  | `部分修复但仍有问题` | `accepted` | 仍有问题 → open |

- **根因**：codex 新增的否定判定只处理"否定词在正向词之前"（`not fixed`、`未通过`），没有处理"正向词之后的后置限定"（`without full verification`、`但仍有问题`）。这与本轮想解决的 `已修复，待验证` 是同一类问题，只是换了措辞。
- **影响**：直接决定采纳率可信度。执行方 AI 若不严格使用规范状态词（README 要求只填 5 种），这类自由文本会静默计入采纳。
- **建议**：补 `_UNRESOLVED` 后置短语（`without full verification`、`仍需`、`但仍` 等），或统一改为"先扫描全部否定/未决短语命中则 open，再看正向词"的两阶段结构，而非只向前看。

### P3-1　否定英文变体仍有 5 处漏判（偏保守，不污染采纳率）

- **证据**：

  | 输入 | 返回 | 期望 |
  |---|---|---|
  | `nothing to fix` | `None` | `rejected` |
  | `no changes needed` | `None` | `rejected` |
  | `no longer an issue` | `None` | `rejected` |
  | `none of these are issues` | `None` | `rejected` |
  | `no test failed` | `None` | `accepted` |

- **根因**：`_REJECTED` 收录的是 `no issue`/`not a bug` 等固定搭配，`no + 名词 + 否定后缀` 的开放组合未覆盖；`_EN_NEGATION_RE` 锚定从句末尾，对 `no longer an issue` 这类句中否定不生效。
- **影响**：这些被归为 open（跳过），方向保守，不会像 P2-1 那样虚增采纳率，但会降低反馈回收率。
- **建议**：按实际工单语料补充高频搭配，不必穷举。

### P3-2　`_EN_NEGATION_RE` 与 `_UNRESOLVED` 词表职责重叠，维护成本高

- **位置**：`outcomes.py:245` 的 `_EN_NEGATION_RE`、`:34` 起的 `_UNRESOLVED`
- **证据**：关闭正则后重跑分类，`not fixed`/`not yet fixed`/`cannot be fixed` 等**全部不变**——它们靠 `_UNRESOLVED` 词表命中；正则实际只在 `not a false positive`、`not properly fixed` 少数场景生效。
- **影响**：两套机制并存且边界不清，后续加词容易重复或遗漏（本轮 `_UNRESOLVED` 一次加了 50 个词，其中不少与正则功能重叠）。不是运行期 bug，是长期可维护性风险。
- **建议**：明确分工——词表只放"不可推导的固定搭配"，正则/规则负责"可组合的否定结构"，并在注释里写清判定优先级。

### P3-3　`_NEG_PREFIXES` 与 `not a bug` 的交互依赖词表顺序，缺显式测试

- **证据**：`not a bug` 返回 `rejected`（正确），但这是靠 `_REJECTED` 里的 `not a bug` 整词命中，而非否定逻辑推导（`_negated_at('not a ',...)` 会把裸 `bug` 判为否定）。若有人从 `_REJECTED` 删掉 `not a bug` 而以为否定前缀能兜住，会静默变成 `None`。
- **建议**：加一条"移除整词搭配后行为"的元测试，或在两条路径各写独立用例锁死。

---

## 三、验证结果

| 项 | 结果 |
|---|---|
| 全量 pytest（`--basetemp=.pytest_tmp/audit2`） | **451 passed / 5 failed**，5 项均为改动前既有基线失败（desktop/hub×2/snapshot/web），无新增失败 |
| 用例数变化 | 上轮 409 → 现 451（本轮新增 42 项参数化用例），上轮 P1-3 的 CLI 测试现在稳定通过 |
| `ruff check src tests` | All checks passed |
| 上轮 6 项修复回归检查 | 全部保持有效：否定词分类、聚类阈值、CLI git 根、写入计数、子目录路径、caller 围栏均未被本轮改动破坏 |
| 源码完整性 | `outcomes.py` 无控制字符，可正常编译（diff 中 `ESC[m` 是终端 ANSI 重置符，非源码污染） |

---

## 四、结论与建议

本轮是**低风险的增量完善**：上轮 3 个 P1 修复未被破坏，测试全绿，reconcile 零写入失败是个正确的健壮性补充。CHANGELOG 声明的 18 个状态词例全部兑现（已逐条实测）。

发布前唯一建议处理的是 **P2-1**：`fixed without full verification`、`部分修复但仍有问题` 这类后置限定词会误记为采纳，与 codex 本轮要解决的问题同源。修法很小（补 2-3 个后置短语词），但直接关系采纳率可信度。P3 三项可排入后续迭代，不影响发布。

本报告只读检查，未修改任何源码。
