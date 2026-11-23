# Code Review 工单

> 生成信息：commit=f0e11633 | 模式=dual | 规格等级=L2（100% 的公开符号带注解或 docstring）
> 审查者=qwen3.8-flash, qwen4-max-preview | 证伪=deepseek-v4.1-flash-1017 | 时间=2026-11-23T04:56:11+00:00
> 项目规范=未应用
> 本次共 4 条发现，本工单渲染前 4 条（按严重度 / 证据 / 一致性排序）。
>
> **执行协议**（交给执行方 AI）：
> 1. 按 F-001 起顺序逐条处理，不要跳条。
> 2. 每条先核对【现状】与代码是否一致；不一致则跳过，不要猜测。
> 3. 按【修改建议】改动，然后运行【验收】中的命令。
> 4. 全部完成后，在文末《执行结果》表填写：finding_id / 状态 / 验证结果。
>    状态只填：已修复 / 部分修复 / 未修复 / 误报 / 不适用；未处理的保持“待处理”。
> 5. AI 审查通过 ≠ 可以合入；必须人工确认。

## 独有发现（仅单模型报出，需重点确认）

## F-001 [P0] os.replace_atomic 不存在导致 AttributeError
<!-- finding:F-001=7c0d4bfaa0f7e497 -->

- 位置：`refunds.py:22`
- 视角：correctness
- 分类：error_code_inconsistent
- 证据等级：**仅推断**
- 一致性：**单模型独有**
- 来源模型：qwen3.8-flash
- 问题编号：`7c0d4bfaa0f7e497`

**现状**：
```python
    os.replace_atomic(tmp, path)
```

**问题**：当调用 write_audit_log 时，os 模块没有 replace_atomic 属性，触发 AttributeError；重命名不执行，审计日志不会落盘。

**修改建议**：

```diff
--- a/refunds.py
+++ b/refunds.py
@@ -22,1 +22,1 @@
-    os.replace_atomic(tmp, path)
+    os.replace(tmp, path)
```

使用标准库 os.replace 实现原子重命名。

**验收**：`python -c "import os; assert not hasattr(os, 'replace_atomic'); import refunds, tempfile; d=tempfile.mkdtemp(); p=refunds.write_audit_log('x',d); assert open(p).read() == 'x' + chr(10)" 成功时无输出且退出码为 0。`

---
## F-002 [P0] “追加”审计日志实际被整体覆盖，历史条目丢失
<!-- finding:F-002=f82649f7db6b10cc -->

- 位置：`refunds.py:20-21`
- 视角：correctness
- 分类：semantic_drift
- 证据等级：**仅推断**
- 一致性：**单模型独有**
- 来源模型：qwen4-max-preview
- 问题编号：`f82649f7db6b10cc`

**现状**：
```python
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(entry + "\n")
```

**问题**：当 write_audit_log 被第二次调用时（审计日志天然会被多次调用），第 20 行以 "w" 模式打开临时文件，只写入本次 entry，随后 rename 覆盖目标文件，于是 audit.log 中此前所有审计条目被静默清空，最终文件只剩最后一行。函数文档承诺 “Append an audit line”，实现却是 truncate+replace，属于退款审计场景下的不可逆数据丢失；即使修好 os.replace_atomic，这个丢数据行为依然存在。另外文档承诺 crash-proof，但写入后没有 flush/fsync，断电时 rename 后可能得到空文件。

**修改建议**：

```diff
--- a/refunds.py
+++ b/refunds.py
@@ -16,8 +16,8 @@
 def write_audit_log(entry: str, log_dir: str) -> str:
-    """Append an audit line to today's log file, crash-proof via atomic rename."""
+    """Append an audit line durably (O_APPEND + fsync); never truncates history."""
     path = os.path.join(log_dir, "audit.log")
-    tmp = path + ".tmp"
-    with open(tmp, "w", encoding="utf-8") as fh:
-        fh.write(entry + "\n")
-    os.replace_atomic(tmp, path)
+    with open(path, "a", encoding="utf-8") as fh:
+        fh.write(entry + "\n")
+        fh.flush()
+        os.fsync(fh.fileno())
     return path
```

追加语义不需要临时文件+rename：改用 "a"（O_APPEND）直接追加，单次小行写入在 POSIX 上是原子的，既保住历史条目，也顺带消除幻觉 API 与固定 .tmp 竞争。写入后 flush+os.fsync 才真正满足文档的持久化承诺，并同步更新 docstring。若必须保留 rename 方案，则须先把 path 的既有内容完整拷入临时文件再追加，并对目标文件加进程间锁。

**验收**：`cd /private/tmp/codeerratum-seeded-btbO && python -c "import tempfile, pathlib, refunds; d=tempfile.mkdtemp(); refunds.write_audit_log('e1', d); refunds.write_audit_log('e2', d); print(repr(pathlib.Path(d,'audit.log').read_text()))" —— 成功标准：输出 'e1\ne2\n'（两行都在），而非仅 'e2\n'。`

---
## 冲突区（模型意见相反，需人工裁决）

## F-003 [P0] open(tmp, "w") 覆盖旧审计日志导致数据丢失
<!-- finding:F-003=5b4d0d2c97bc3008 -->

- 位置：`refunds.py:20-21`
- 视角：correctness
- 分类：resource_leak
- 证据等级：**仅推断**
- 一致性：**模型冲突**
- 来源模型：qwen3.8-flash, qwen4-max-preview
- 问题编号：`5b4d0d2c97bc3008`

**模型分歧**（需人工裁决）：
- [qwen3.8-flash·主张] open(tmp, "w") 覆盖旧审计日志导致数据丢失
- [qwen4-max-preview·主张] 异常路径不清理临时文件，.tmp 残留累积
- [deepseek-v4.1-flash-1017·反驳] 该断言成立的前提是 os.replace_atomic(tmp, path) 能成功执行并把 tmp 重命名覆盖 path。但代码上下文（模块 docstring 与已知静态事实）明确指出 os.replace_atomic 是幻觉 API，os 模块中并不存在该属性。Python 在执行到该行时会在属性查找阶段立即抛出 AttributeError，重命名/覆盖根本没有发生，因此 audit.log 的旧内容不会被丢弃。断言的因果链（写入 tmp → replace 覆盖 path → 旧日志丢失）在所示代码中不可达，数据丢失的结论不成立。 | 反例: 调用 write_audit_log("new-entry", log_dir)，且 log_dir/audit.log 已包含历史行（例如 'old-line-1\nold-line-2\n'）：
1) open(tmp, "w") 把 'new-entry\n' 写入 audit.log.tmp（此时 audit.log 未被触碰）；
2) 执行 os.replace_atomic(tmp, path) 时抛出 AttributeError: module 'os' has no attribute 'replace_atomic'；
3) 异常向上传播，函数未返回 path，os.replace 从未执行。
结果：audit.log 仍为 'old-line-1\nold-line-2\n'，旧审计行并未丢失。附带可观察后果是残留一个 audit.log.tmp 文件，但那是另一类问题（临时文件泄漏/写失败），不是本断言所说的旧日志被覆盖。 | 错误假设: 断言隐含假设 os.replace_atomic 是一个真实存在且成功返回的 os 函数，从而后续 replace 会以原子方式用仅含新条目的 tmp 覆盖 path。该假设在本仓库中不成立：代码自身文档说明它是编造的 API，实际调用必然在替换发生前失败，覆盖步骤永远执行不到。

**现状**：
```python
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(entry + "\n")
```

**问题**：当 audit.log 已存在且再次调用 write_audit_log 时，tmp 以 w 模式只写入新 entry，随后 replace 覆盖 path，旧审计行全部丢失。

**修改建议**：

```diff
--- a/refunds.py
+++ b/refunds.py
@@ -20,2 +20,6 @@
-    with open(tmp, "w", encoding="utf-8") as fh:
-        fh.write(entry + "\n")
+    existing = ""
+    if os.path.exists(path):
+        with open(path, "r", encoding="utf-8") as old:
+            existing = old.read()
+    with open(tmp, "w", encoding="utf-8") as fh:
+        fh.write(existing + entry + "\n")
```

写入 tmp 前先读取并保留 path 已有内容，再原子替换。

**验收**：`python -c "import os, refunds, tempfile; os.replace_atomic = os.replace; d=tempfile.mkdtemp(); p=refunds.write_audit_log('a',d); p=refunds.write_audit_log('b',d); assert open(p).read() == 'a' + chr(10) + 'b' + chr(10)" 成功时无输出且退出码为 0。`

---
## 共识区（多模型一致）

## F-004 [P0] 覆盖旧日志且调用不存在的 os.replace_atomic
<!-- finding:F-004=8fe26a17d9af614c -->

- 位置：`refunds.py:20-22`
- 视角：correctness
- 分类：error_code_inconsistent
- 证据等级：**仅推断**
- 一致性：**多模型一致**
- 来源模型：qwen3.8-flash, qwen4-max-preview
- 问题编号：`8fe26a17d9af614c`

**现状**：
```python
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(entry + "\n")
    os.replace_atomic(tmp, path)
```

**问题**：当 write_audit_log 被多次调用时，tmp 以 w 模式只写入新 entry 并替换 audit.log，旧审计记录被永久覆盖；同时 os.replace_atomic 在 os 模块中不存在，正常调用会抛出 AttributeError。

**修改建议**：

```diff
--- a/refunds.py
+++ b/refunds.py
@@ -19,5 +19,13 @@
     tmp = path + ".tmp"
-    with open(tmp, "w", encoding="utf-8") as fh:
-        fh.write(entry + "\n")
-    os.replace_atomic(tmp, path)
+    try:
+        with open(path, "r", encoding="utf-8") as src:
+            existing = src.read()
+    except FileNotFoundError:
+        existing = ""
+    if existing and not existing.endswith("\n"):
+        existing += "\n"
+    with open(tmp, "w", encoding="utf-8") as fh:
+        fh.write(existing)
+        fh.write(entry + "\n")
+    os.replace(tmp, path)
     return path
```

复制已有 audit.log 内容到临时文件并追加新行，使用 os.replace 原子替换；若原文件末尾无换行则先补齐。

**验收**：`python -c "import os, sys; sys.path.insert(0, '/private/tmp/codeerratum-seeded-btbO'); import refunds; d='/tmp/audit_check'; os.makedirs(d, exist_ok=True); p=os.path.join(d, 'audit.log'); os.path.exists(p) and os.remove(p); refunds.write_audit_log('a', d); refunds.write_audit_log('b', d); assert open(p, encoding='utf-8').read() == 'a\nb\n'; print('OK')"`

---

## 摘要

| 严重度 | 数量 |
|---|---|
| P0 | 4 |
| P1 | 0 |
| P2 | 0 |
| P3 | 0 |
| 合计（本工单） | 4 |

## 本次覆盖说明（降级项必须可见）

- 1 个单元调用模型失败，相关文件未被审查
- 失败原因：读取超时（240s 内没有返回任何数据；已重试 1 次）
- 机器丢弃 5 条（refuted×5），明细见报告末尾与 run_rejections
- 1 次结构化输出解析失败（成功率 0.867），样本见 parse_failures.json

## 交叉审查统计

- 模式：dual
- 各模型原始发现数：{'qwen3.8-flash': 4, 'qwen4-max-preview': 12}
- 聚类后：9 条
- 证伪后：4 条
- 分区：{'consensus': 1, 'unique': 2, 'conflict': 1}

## 已丢弃的发现（定位校验未通过 / 被证伪推翻）

这些结论未通过校验，请勿据此修改：

- `refuted` [qwen3.8-flash] deadline 为 naive 时比较抛 TypeError — 
- `refuted` [qwen4-max-preview] 以 "w" 覆盖写，审计日志历史被整段清空 — 
- `refuted` [qwen4-max-preview] 固定 .tmp 名 + 读改写，并发调用会丢审计条目 — 
- `refuted` [qwen4-max-preview] 模块 docstring 描述的 bug 与实现不符 — 
- `refuted` [qwen4-max-preview] naive deadline 传入时比较抛 TypeError — 

## 失败的单元（模型调用未完成）

这些单元的调用失败；provider_error 的单元未被审查，verify_error 的发现保留为未验证：

- `provider_error` [qwen3.8-flash] d80197e3419e4339 — 读取超时（240s 内没有返回任何数据；已重试 1 次）

## 执行结果（由执行方 AI 填写）

> 状态填 `已修复` / `部分修复` / `未修复` / `误报` / `不适用`；`codeerratum reconcile erratumReview.md` 会把这张表回填到 feedback.db。

| finding_id | 状态 | 验证结果 |
|---|---|---|
| F-001 | 待处理 | — |
| F-002 | 待处理 | — |
| F-003 | 待处理 | — |
| F-004 | 待处理 | — |
