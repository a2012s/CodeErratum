# Code Review 工单

> 生成信息：commit=6fffa54b | 模式=dual | 规格等级=L2（99% 的公开符号带注解或 docstring）
> 审查者=qwen4-max-preview, deepseek-v4.1-flash-1017 | 证伪=qwen3.8-flash | 时间=2026-11-23T07:50:51+00:00
> 项目规范=未应用
> 本次共 327 条发现，本工单渲染前 10 条（按严重度 / 证据 / 一致性排序）。
>
> **执行协议**（交给执行方 AI）：
> 1. 按 F-001 起顺序逐条处理，不要跳条。
> 2. 每条先核对【现状】与代码是否一致；不一致则跳过，不要猜测。
> 3. 按【修改建议】改动，然后运行【验收】中的命令。
> 4. 全部完成后，在文末《执行结果》表填写：finding_id / 状态 / 验证结果。
>    状态只填：已修复 / 部分修复 / 未修复 / 误报 / 不适用；未处理的保持“待处理”。
> 5. AI 审查通过 ≠ 可以合入；必须人工确认。

## 独有发现（仅单模型报出，需重点确认）

## F-001 [P0] 单匹配时 doSwitchRepo 访问被移除的 #repoSwitch 直接抛错
<!-- finding:F-001=4a585268c376c689 -->

- 位置：`src/codeerratum/web/static/index.html:2180`
- 视角：correctness
- 分类：error_code_inconsistent
- 证据等级：**仅推断**
- 一致性：**单模型独有**
- 来源模型：deepseek-v4.1-flash-1017
- 问题编号：`4a585268c376c689`

**现状**：
```
    showDropMatches(found.name, found.matches || []);
```

**问题**：当 /api/find-repo 只返回 1 个匹配（或用户在候选列表里点选）时，showDropMatches 已用 innerHTML 覆盖了 #repoBody，其中的 #repoSwitch 也随之从 DOM 移除；doSwitchRepo 里 `const btn = $("repoSwitch")` 得到 null，紧接着 `btn.disabled = true` 抛 TypeError。这里 showDropMatches 既没有被 await，也没有被 try/catch 包住，Promise 变成未处理的 rejection：仓库既不切换，也不弹任何错误提示，拖拽定位在最常见的单匹配场景下静默失效。

**修改建议**：

```diff
--- a/src/codeerratum/web/static/index.html
+++ b/src/codeerratum/web/static/index.html
@@ -2074,9 +2074,11 @@
 async function doSwitchRepo(path) {
   if (!path) { showError(T("请填写本地仓库文件夹的绝对路径，或用「浏览本地…」选择"), T("没有选择仓库")); return; }
   const btn = $("repoSwitch");
-  btn.disabled = true;
+  // #repoSwitch 只存在于仓库抽屉里；showDropMatches() 会替换 #repoBody，
+  // 拖拽路径上这个按钮并不存在。
+  if (btn) btn.disabled = true;
   try {
     const meta = await api("/api/repo", { method: "POST", body: JSON.stringify({ path }) });
     rememberRepo(meta.repo);
@@ -2090,7 +2092,7 @@
     showError(T("目标路径：{path}", { path }) + "\n\n" + e.message, T("切换仓库失败"));
   } finally {
-    btn.disabled = false;
+    if (btn) btn.disabled = false;
   }
 }
```

doSwitchRepo 同时被「正常抽屉」和「拖拽候选列表」两条路径调用，后者里 #repoSwitch 不存在。改成存在才操作按钮，既修掉崩溃，也保留原抽屉的忙碌态。若还想让调用方捕获错误，可把第 2180 行改为 `await showDropMatches(...)` 并把 showDropMatches 声明为 async。

**验收**：`运行 `grep -c 'if (btn) btn.disabled = true;' src/codeerratum/web/static/index.html` 输出 1，且 `grep -c '^  btn\.disabled = true;$' src/codeerratum/web/static/index.html` 输出 0（退出码 1）即为通过；随后在浏览器里拖入一个只匹配单个仓库的文件夹，应弹出「已切换到 …」toast 而不是没有任何反应。`

---
## F-002 [P0] 拖拽命中唯一仓库时 doSwitchRepo 空引用崩溃
<!-- finding:F-002=cf7252eaca1e025b -->

- 位置：`src/codeerratum/web/static/index.html:2076-2077`
- 视角：correctness
- 分类：null_deref
- 证据等级：**仅推断**
- 一致性：**单模型独有**
- 来源模型：qwen4-max-preview
- 问题编号：`cf7252eaca1e025b`

**现状**：
```
  const btn = $("repoSwitch");
  btn.disabled = true;
```

**问题**：handleFolderDrop 调用 showDropMatches，后者用 dropMatches 的 HTML 整体覆盖了 repoBody（2144-2147 行），此时 #repoSwitch 已不存在；当 matches.length === 1 时 2151 行立刻调用 doSwitchRepo(matches[0])，2076-2077 行 $("repoSwitch") 返回 null 并执行 btn.disabled = true，抛出 TypeError。触发条件：拖入的文件夹名在家目录下只匹配到 1 个 git 仓库（最常见路径）。影响：仓库根本没切换成功，异常发生在 handleFolderDrop 的 try 之外之前？不——它在 try 内被 catch 吞掉后只弹出「定位仓库失败」，用户看到的是一个假的失败提示，且 finally 里 btn.disabled = false 会再次抛错形成未处理的 Promise rejection。

**修改建议**：

```diff
--- a/src/codeerratum/web/static/index.html
+++ b/src/codeerratum/web/static/index.html
@@ -2074,5 +2074,5 @@
 async function doSwitchRepo(path) {
   if (!path) { showError(T("请填写本地仓库文件夹的绝对路径，或用「浏览本地…」选择"), T("没有选择仓库")); return; }
   const btn = $("repoSwitch");
-  btn.disabled = true;
+  if (btn) btn.disabled = true;
   try {
@@ -2090,5 +2090,5 @@
     if (err) { err.textContent = e.message; err.hidden = false; }
     showError(T("目标路径：{path}", { path }) + "\n\n" + e.message, T("切换仓库失败"));
   } finally {
-    btn.disabled = false;
+    if (btn) btn.disabled = false;
   }
```

doSwitchRepo 有三个调用点：repoDrawer 表单、最近列表、以及 showDropMatches 覆盖后的候选列表。只有第一种存在 #repoSwitch，所以按钮的禁用/恢复必须做存在性判断（与已有的 $("repoErr") 判空保持一致）。

**验收**：`执行 `grep -n "if (btn) btn.disabled" src/codeerratum/web/static/index.html`，应输出 2 行（doSwitchRepo 开头与 finally）；随后启动 UI，拖入一个在家目录下唯一同名的 git 文件夹，仓库应切换成功且浏览器控制台无 TypeError。`

---
## F-003 [P0] 非法 mode 抛 KeyError 而不是 ValueError
<!-- finding:F-003=f17c9706d0a4c305 -->

- 位置：`src/codeerratum/models/registry.py:129`
- 视角：correctness
- 分类：error_code_inconsistent
- 证据等级：**仅推断**
- 一致性：**单模型独有**
- 来源模型：deepseek-v4.1-flash-1017
- 问题编号：`f17c9706d0a4c305`

**现状**：
```python
        n = {"quick": 1, "dual": 2, "committee": 3}[mode]
```

**问题**：当 mode 不是 quick/dual/committee 之一时（pipeline.py:453 与 orchestrator.py:376 都以 # type: ignore[arg-type] 传入运行期字符串，模式可能来自配置或 CLI），字典下标取值直接抛 KeyError；而这两个调用点都只捕获 ValueError，异常会逃逸成 500，而不是预期的 models_invalid 配置错误，用户拿不到可读的提交期提示。

**修改建议**：

```diff
--- a/src/codeerratum/models/registry.py
+++ b/src/codeerratum/models/registry.py
@@ -127,7 +127,10 @@
         """
         lenses = lenses or []
-        n = {"quick": 1, "dual": 2, "committee": 3}[mode]
+        seats = {"quick": 1, "dual": 2, "committee": 3}
+        if mode not in seats:
+            raise ValueError(f"unknown review mode: {mode}")
+        n = seats[mode]
         self._check_author(author_model)
```

把下标取值改为显式成员检查，非法模式统一抛 ValueError，与调用点的 except ValueError 对齐；不要依赖 Literal 类型标注，运行期传入的是裸 str。

**验收**：`运行 `python -c "from codeerratum.models.registry import ModelRegistry, ModelSpec; r = ModelRegistry(models=[ModelSpec(model_id='gpt-4o', base_url='http://x')]); r.route('turbo')"`，成功标准：抛出 `ValueError: unknown review mode: turbo`，而不是 `KeyError: 'turbo'`。`

---
## 冲突区（模型意见相反，需人工裁决）

## F-004 [P1] 主 excerpt 用固定三反引号围栏，可被源码提前闭合
<!-- finding:F-004=bc39fcedbfab488e -->

- 位置：`src/codeerratum/review/executor.py:259`
- 视角：security
- 分类：injection
- 证据等级：**仅推断**
- 一致性：**模型冲突**
- 来源模型：qwen4-max-preview, deepseek-v4.1-flash-1017
- 问题编号：`bc39fcedbfab488e`

**模型分歧**（需人工裁决）：
- [qwen4-max-preview·主张] 主 excerpt 用固定三反引号围栏，可被源码提前闭合
- [deepseek-v4.1-flash-1017·主张] excerpt 用固定三反引号围栏，源码含 ``` 时被提前闭合
- [qwen3.8-flash·反驳]  | 反例: 当被审查源码某行本身为 ``` 时，render_excerpt 仍会按提示要求输出绝对行号前缀，例如 “ 453 | ```”。该行不以 ``` 开头，因此不会关闭外层 Markdown 围栏。 | 错误假设: 该结论假设 excerpt 是原始源码行，并且任意包含 ``` 的行都能关闭围栏；但所示代码使用带绝对行号前缀的 excerpt，且 Markdown 围栏关闭有行首约束。

**现状**：
````python
        f"```{unit.language or 'text'}\n{excerpt}\n```"
````

**问题**：build_messages 把被审查源码放进固定 ``` 围栏。当被审查文件的某行含 ``` （Markdown 文档字符串、.md/.rst 文件，或本仓库这类构造 prompt 的代码）时，该行提前闭合围栏，其余源码以自由文本进入 user prompt，可被当作指令执行，且行号说明与代码边界错乱导致 anchor 失效。render_callers 已用 _fence 自适应围栏防同一问题，主 excerpt 遗漏，防护不一致。

**修改建议**：

````diff
--- a/src/codeerratum/review/executor.py
+++ b/src/codeerratum/review/executor.py
@@ -254,7 +254,8 @@
     ranges = excerpt_ranges(unit, len(source_lines), enclosing_span)
     excerpt = render_excerpt(source_lines, ranges)
+    fence = _fence(excerpt)
     parts = [
         f"# File: {unit.primary_file}\n# Symbol: {unit.symbol}\n"
         "# Line numbers below are absolute line numbers of this file.\n\n"
-        f"```{unit.language or 'text'}\n{excerpt}\n```"
+        f"{fence}{unit.language or 'text'}\n{excerpt}\n{fence}"
     ]
````

复用报告渲染器的自适应围栏 _fence(excerpt)，使外层围栏长度严格大于 excerpt 内最长反引号串，源码中的 ``` 无法提前闭合代码块。

**验收**：`新增用例：source_lines 含一行 "```" 时调用 build_messages，断言包裹 excerpt 的围栏反引号数 >= 4；运行 `python -m pytest tests/review/test_executor.py -k fence -q`，期望 1 passed。`

---
## 共识区（多模型一致）

## F-005 [P1] repo_root 未快照，切库导致跨项目拼数据
<!-- finding:F-005=10628340d9e05708 -->

- 位置：`src/codeerratum/web/server.py:527-528`
- 视角：concurrency
- 分类：race_condition
- 证据等级：**仅推断**
- 一致性：**多模型一致**
- 来源模型：qwen4-max-preview, deepseek-v4.1-flash-1017
- 问题编号：`10628340d9e05708`

**现状**：
```python
        run_dir = self.repo_root / STATE_DIR / "runs" / run_id
        findings_path = run_dir / "findings.json"
```

**问题**：当本请求执行期间另一标签页 POST /api/repo（switch_repo 只持 _review_lock，读路径不加锁）改写 self.repo_root 时，get_run 用旧 repo 的 run_dir 读到 findings，却用新 repo 的 store（self.store 每次访问都重新 store_for(self.repo_root)）取 statuses 与 list_runs。结果 findings 被套上另一个项目数据库的 status/rejection_reason，created_at 变空，repo 字段也可能指向新仓库——静默返回错误数据。

**修改建议**：

```diff
--- a/src/codeerratum/web/server.py
+++ b/src/codeerratum/web/server.py
@@ -527,2 +527,4 @@
-        run_dir = self.repo_root / STATE_DIR / "runs" / run_id
+        repo_root = self.repo_root  # snapshot: switch_repo may run concurrently
+        store = store_for(repo_root)
+        run_dir = repo_root / STATE_DIR / "runs" / run_id
         findings_path = run_dir / "findings.json"
@@ -534,1 +536,1 @@
-        statuses = self.store.statuses_for(ids)
+        statuses = store.statuses_for(ids)
@@ -542,1 +544,1 @@
-        runs = {r["run_id"]: r for r in self.store.list_runs(limit=200)}
+        runs = {r["run_id"]: r for r in store.list_runs(limit=200)}
@@ -559,1 +561,1 @@
-            "repo": collection.get("repo", str(self.repo_root)),
+            "repo": collection.get("repo", str(repo_root)),
```

入口处把 self.repo_root 快照为局部变量，并只取一次 store_for(repo_root)，整个 get_run 使用同一对 (repo_root, store)，保证产物文件与状态库来自同一项目。

**验收**：`新增并发用例：线程 A 执行 get_run（读完 findings 后阻塞），线程 B 执行 switch_repo 切到另一仓库；断言返回的 status/created_at/repo 全部来自原仓库。运行 `python -m pytest tests -q -k "get_run or switch_repo"`，期望通过。`

---
## F-006 [P1] review.md 链接漏掉 BASE，多项目下指向错误项目
<!-- finding:F-006=1abe802ac5dbf4b4 -->

- 位置：`src/codeerratum/web/static/index.html:1168-1171`
- 视角：api_contract
- 分类：semantic_drift
- 证据等级：**仅推断**
- 一致性：**多模型一致**
- 来源模型：qwen4-max-preview, deepseek-v4.1-flash-1017
- 问题编号：`1abe802ac5dbf4b4`

**现状**：
```
  run.models.forEach((m) => chips.push(`<span class="chip model">${esc(m)}</span>`));
  if (run.has_markdown) {
    chips.push(`<a class="chip" style="text-decoration:none" href="/api/runs/${run.run_id}/review.md" target="_blank">review.md ↗</a>`);
  }
```

**问题**：当页面被多项目壳以 /p/<slug>/ 提供（BASE 非空）时，renderRun 生成的 review.md 链接仍写死根路径 /api/runs/...，点击后请求落到根项目而非当前 slug 项目：轻则 404 打不开报告，重则读到另一个项目的审查内容，破坏了 BASE 建立的项目隔离约定（本文件其余 API 调用均为 BASE + path）。同时 run_id 未经 encodeURIComponent / esc 直接插入 href 属性，含引号或特殊字符的 id 会截断属性。

**修改建议**：

```diff
--- a/src/codeerratum/web/static/index.html
+++ b/src/codeerratum/web/static/index.html
@@ -1168,4 +1168,5 @@
   run.models.forEach((m) => chips.push(`<span class="chip model">${esc(m)}</span>`));
   if (run.has_markdown) {
-    chips.push(`<a class="chip" style="text-decoration:none" href="/api/runs/${run.run_id}/review.md" target="_blank">review.md ↗</a>`);
+    const mdUrl = `${BASE}/api/runs/${encodeURIComponent(run.run_id)}/review.md`;
+    chips.push(`<a class="chip" style="text-decoration:none" href="${esc(mdUrl)}" target="_blank" rel="noopener">review.md ↗</a>`);
   }
```

用 BASE 前缀 + encodeURIComponent(run.run_id) 构造 URL，再 esc 后写入 href，并补 rel="noopener"，与 api() 的路径规则保持一致。

**验收**：`执行 `grep -n 'review.md' src/codeerratum/web/static/index.html`，应看到 href 由 `${BASE}/api/runs/${encodeURIComponent(run.run_id)}/review.md` 生成；再在 /p/<slug>/ 页面打开一条 has_markdown 的运行并点击 review.md，Network 中请求路径必须是 /p/<slug>/api/runs/<id>/review.md 且返回 200（修复前为 /api/runs/... 的 404 或他项目内容）。`

---
## F-007 [P1] 摘要发送失败会丢 marker，重跑重复行内评论
<!-- finding:F-007=6c78e010abc1d73c -->

- 位置：`src/codeerratum/ci/github.py:287-294`
- 视角：resilience
- 分类：non_idempotent
- 证据等级：**仅推断**
- 一致性：**多模型一致**
- 来源模型：qwen4-max-preview, deepseek-v4.1-flash-1017
- 问题编号：`6c78e010abc1d73c`

**现状**：
```python
    body = summary_body(collection, inline, rest, marker)
    comment = _request(
        "POST",
        f"{api}/repos/{pr.slug}/issues/{pr.number}/comments",
        token,
        {"body": body},
    )
    result["summary_url"] = comment.get("html_url", "")
```

**问题**：marker 只写在最后发送的摘要评论里。当行内 review 已成功提交、随后摘要 POST 失败（限流 403、网络抖动、body 超长 422）时，CIError 直接抛给 CLI，PR 上留下行内评论但没有任何 marker；下一次重跑 already_posted 返回 False，于是同一 commit 的行内评论被重复发布，且失败次数越多重复越多（部分更新导致状态不一致）。

**修改建议**：

```diff
--- a/src/codeerratum/ci/github.py
+++ b/src/codeerratum/ci/github.py
@@ -287,9 +287,19 @@
     body = summary_body(collection, inline, rest, marker)
-    comment = _request(
-        "POST",
-        f"{api}/repos/{pr.slug}/issues/{pr.number}/comments",
-        token,
-        {"body": body},
-    )
+    comments_url = f"{api}/repos/{pr.slug}/issues/{pr.number}/comments"
+    try:
+        comment = _request("POST", comments_url, token, {"body": body})
+    except CIError:
+        if result["inline_posted"]:
+            # 行内评论已落地：留下 marker，避免重跑重复发布。
+            try:
+                _request("POST", comments_url, token, {"body": marker})
+            except CIError:
+                pass
+        raise
     result["summary_url"] = comment.get("html_url", "")
     return result
```

摘要发送失败且已有行内评论落地时，补发一条仅含 marker 的占位评论再抛出原错误，保证幂等键先于/伴随副作用写入；也可改为把 marker 同时写进 review body 并让 already_posted 一并扫描 reviews。

**验收**：`运行 `python -m pytest tests/ci -q -k summary_failure`（新增用例：批量 review 成功、摘要 POST 抛 CIError，断言随后发出 body == marker 的补发请求且异常仍向外抛出），全部通过即修复成功。`

---
## F-008 [P1] 任意 --host 都被接受，无鉴权的本地 UI 会暴露到网络
<!-- finding:F-008=6e25b743e25c0e40 -->

- 位置：`src/codeerratum/cli.py:471`
- 视角：security
- 分类：authz_bypass
- 证据等级：**仅推断**
- 一致性：**多模型一致**
- 来源模型：deepseek-v4.1-flash-1017, qwen4-max-preview
- 问题编号：`6e25b743e25c0e40`

**现状**：
```python
    host: str = typer.Option("127.0.0.1", "--host", help="Bind address (keep it on loopback)"),
```

**问题**：当用户传入 `--host 0.0.0.0` 或某个网卡地址时（help 只写“keep it on loopback”，代码没有任何强制或告警），Web UI 会监听所有接口，而它本身不带认证：同网段的任何人都可以触发审查（消耗付费模型调用）、读取仓库 diff 内容，并通过界面改写 feedback.db 里的 accepted/rejected 状态。

**修改建议**：

```diff
@@ -485,6 +485,8 @@
 ) -> None:
     """Open the local web UI (multi-project tabs, run history, report, accept/reject)."""
+    if host not in {"127.0.0.1", "localhost", "::1"}:
+        console.print(f"[yellow]警告：--host {host} 会把无鉴权的 UI 暴露给网络[/yellow]")
     repo_root = None if no_repo else _resolve_repo_or_exit(repo)
```

在 `ui` 开头对 host 做 loopback 白名单校验，非 loopback 时至少打印醒目告警（严格模式下可直接 typer.Exit(1)）；`desktop` 同样处理。

**验收**：`运行 `timeout 3 python -m codeerratum.cli ui --host 0.0.0.0 --no-repo || true`，输出中应出现“会把无鉴权的 UI 暴露给网络”告警行；再运行 `timeout 3 python -m codeerratum.cli ui --no-repo || true` 时不应出现该告警。`

---
## F-009 [P1] tokens 未转义直接拼进 innerHTML，存在 XSS
<!-- finding:F-009=7a7343e25c03b235 -->

- 位置：`src/codeerratum/web/static/projects.html:299-300`
- 视角：correctness
- 分类：injection
- 证据等级：**仅推断**
- 一致性：**多模型一致**
- 来源模型：qwen4-max-preview, deepseek-v4.1-flash-1017
- 问题编号：`7a7343e25c03b235`

**现状**：
```
    const tokens = p.tokens
      ? ` · ${p.tokens.total ?? 0} tokens` : "";
```

**问题**：当 /api/projects 返回的 p.tokens.total 不是数字而是字符串（例如被污染的历史统计、代理返回的 JSON 或字段类型变更为字符串）时，第 300 行把它原样插入模板，第 307 行的 ${tokens} 又未经 esc 处理写入 card.innerHTML，于是 `<img src=x onerror=...>` 之类的内容会在总览页以同源身份执行，可读取 localStorage 并调用 /api/projects/*/remove 等接口。相邻字段 p.name、p.path、p.progress 都走了 esc()，唯独 tokens 漏掉，属于明确的转义缺口。

**修改建议**：

```diff
--- a/src/codeerratum/web/static/projects.html
+++ b/src/codeerratum/web/static/projects.html
@@ -296,8 +296,8 @@
   for (const p of PROJECTS) {
     const card = document.createElement("div");
     card.className = "card " + statusClass(p);
-    const tokens = p.tokens
-      ? ` · ${p.tokens.total ?? 0} tokens` : "";
+    const tokens = p.tokens
+      ? ` · ${esc(Number(p.tokens.total) || 0)} tokens` : "";
     card.innerHTML = `
       <div class="card-head">
         <div class="card-name">${esc(p.name)}</div>
```

把 tokens.total 先规范成数字（非数字/NaN 归 0），再经 esc() 输出，保证任何服务端返回值都不会被当作 HTML 解析。若需要保留千分位等格式，也应先格式化再 esc()，不要直接插入原始值。

**验收**：`运行 `node -e "const s=require('fs').readFileSync('src/codeerratum/web/static/projects.html','utf8');if(!/esc\\(Number\\(p\\.tokens\\.total\\)/.test(s))process.exit(1);console.log('ok')"`，应输出 ok 且退出码 0；再在 DevTools 里把 /api/projects 响应改写为 {"projects":[{"slug":"a","name":"a","path":"/a","tokens":{"total":"<img src=x onerror=alert(1)>"}}]}，打开总览页应显示文本 “0 tokens” 且不弹窗、无 img 请求。`

---
## F-010 [P1] 扫描调用数用 >=1 断言，放过重复全量扫描
<!-- finding:F-010=804319abe0edf945 -->

- 位置：`tests/test_optimizations.py:230`
- 视角：testability
- 分类：test_cheat
- 证据等级：**仅推断**
- 一致性：**多模型一致**
- 来源模型：qwen4-max-preview, deepseek-v4.1-flash-1017
- 问题编号：`804319abe0edf945`

**现状**：
```python
    assert stubs["qwen3-flash"].calls >= 1
```

**问题**：模块 docstring 第 3 条要求 quick 模式“without charging a second full sweep”。当实现回归为对每个单元用轻模型扫描两次（或复核阶段又触发一次全量扫描）时，stubs["qwen3-flash"].calls 变为 2*len(units)，而 `>= 1` 依然成立，测试通过，成本翻倍的回归无法被发现。

**修改建议**：

```diff
--- a/tests/test_optimizations.py
+++ b/tests/test_optimizations.py
@@ -230 +230 @@
-    assert stubs["qwen3-flash"].calls >= 1
+    assert stubs["qwen3-flash"].calls == len(units)
```

把轻模型调用次数收紧为精确等于单元数，确保每个单元只被扫描一次，杜绝第二次全量扫描。

**验收**：`运行 `python -m pytest -q tests/test_optimizations.py::test_quick_tier_reviews_hit_units_only_once_each` 应 1 passed；临时在 orchestrator 中对同一单元重复调用扫描后重跑必须失败。`

---
> 还有 317 条发现未在本工单展开（已写入 `findings.json`）。如需全部展开，提高渲染上限后重新生成。


## 摘要

| 严重度 | 数量 |
|---|---|
| P0 | 3 |
| P1 | 7 |
| P2 | 0 |
| P3 | 0 |
| 合计（本工单） | 10 |
| 未展开 | 317 |

## 本次覆盖说明（降级项必须可见）

- HEAD 没有未提交改动（工作区是干净的），已自动改为审查全部代码（遵循 .gitignore，跳过 .codeerratum）。只想审某次改动的话，先改几个文件再发起，或把范围写成某个提交区间。
- 已跳过 26 个非代码单元（文档/配置/i18n）；如需审查请开启 include_non_code
- 自动选定 60/1128 个单元（按风险排序）。想全审请把上限设为「全部」。
- 10 个单元调用模型失败，相关文件未被审查
- 失败原因：读取超时（240s 内没有返回任何数据；已重试 1 次） · ValueError
- 机器丢弃 60 条（refuted×53、duplicate×4、anchor_mismatch×2），明细见报告末尾与 run_rejections
- 证伪轮 242/316 判为证据不足（>30%），上下文构建可能不足，结论请谨慎采用
- 7 次结构化输出解析失败（成功率 0.974），样本见 parse_failures.json

## 交叉审查统计

- 模式：dual
- 各模型原始发现数：{'qwen4-max-preview': 246, 'deepseek-v4.1-flash-1017': 177}
- 聚类后：380 条；证伪后：327 条
- 分区：{'consensus': 18, 'unique': 308, 'conflict': 1}

## 已丢弃的发现（定位校验未通过 / 被证伪推翻）

这些结论未通过校验，请勿据此修改：

> 共 60 条，仅显示前 10 条，完整明细见 runs 目录与 feedback.db 的 run_rejections。

- `duplicate` [deepseek-v4.1-flash-1017] write_markdown 用真值判断，字符串 "false" 也会写文件 — src/codeerratum/web/server.py:719-719
- `anchor_mismatch` [deepseek-v4.1-flash-1017] 日志仅在条数变化时重建，内容轮换后不刷新 — src/codeerratum/web/static/index.html:1466-1469
- `duplicate` [deepseek-v4.1-flash-1017] refresh 静默吞掉 /api/projects 错误 — src/codeerratum/web/static/projects.html:366-366
- `anchor_mismatch` [deepseek-v4.1-flash-1017] 连续拖入两次时后到的响应会覆盖先到的结果 — src/codeerratum/web/static/index.html:2174-2177
- `duplicate` [deepseek-v4.1-flash-1017] 只断言 findings 非空，未校验数量与内容 — tests/test_orchestration.py:439-440
- `anchor_out_of_range` [deepseek-v4.1-flash-1017] st["status"] 直接下标，缺少 status 键时 snapshot 抛 KeyError — src/codeerratum/web/hub.py:0-0
- `duplicate` [deepseek-v4.1-flash-1017] save_comparison 从不重试，其重复写 profile 的缺陷未被覆盖 — tests/test_regressions.py:612-612
- `refuted` [qwen4-max-preview] pr-comments.json 的 posted 恒为 False，发布结果未落盘 — 
- `refuted` [qwen4-max-preview] 默认工单路径会被 CWD 下的同名旧文件劫持 — 
- `refuted` [deepseek-v4.1-flash-1017] --strict 单独使用时被静默忽略，不报错也不生效 — 

## 失败的单元（模型调用未完成）

这些单元的调用失败；provider_error 的单元未被审查，verify_error 的发现保留为未验证：

- `provider_error` [qwen4-max-preview] ce307b4346d4a114 — 读取超时（240s 内没有返回任何数据；已重试 1 次）
- `provider_error` [qwen4-max-preview] c42647812498cd13 — 读取超时（240s 内没有返回任何数据；已重试 1 次）
- `provider_error` [qwen4-max-preview] 9614907b25385cbb — 读取超时（240s 内没有返回任何数据；已重试 1 次）
- `provider_error` [deepseek-v4.1-flash-1017] ce307b4346d4a114 — ValueError
- `provider_error` [deepseek-v4.1-flash-1017] 8f11df84865663fa — ValueError
- `provider_error` [deepseek-v4.1-flash-1017] 9da2f57e0e1b6a15 — ValueError
- `provider_error` [deepseek-v4.1-flash-1017] c42647812498cd13 — ValueError
- `provider_error` [deepseek-v4.1-flash-1017] a68660e34fc1d8ae — ValueError
- `provider_error` [deepseek-v4.1-flash-1017] 8b6a124693c5c063 — ValueError
- `provider_error` [deepseek-v4.1-flash-1017] e5c51a618fe460b6 — ValueError

## 执行结果（由执行方 AI 填写）

> 状态填 `已修复` / `部分修复` / `未修复` / `误报` / `不适用`；`codeerratum reconcile erratumReview.md` 会把这张表回填到 feedback.db。

| finding_id | 状态 | 验证结果 |
|---|---|---|
| F-001 | 已修复 | doSwitchRepo 两处 btn.disabled 加存在性判断；test_layout.py::test_drop_switch_survives_the_missing_drawer_button 通过。与 F-002 为同一 bug |
| F-002 | 已修复 | 同 F-001。注：原描述「被 handleFolderDrop 的 catch 吞掉」不准确——showDropMatches 未被 await，TypeError 成为未处理 rejection（F-001 的描述正确），修法相同 |
| F-003 | 已修复 | route 对未知 mode 显式抛 ValueError("unknown review mode: ...")；test_models.py::test_route_rejects_an_unknown_mode_as_value_error 通过。注：pipeline/resume 入口已有 resolve_mode 先行拦截，此修复统一 registry 自身的异常契约 |
| F-004 | 误报 | render_excerpt 对每行无条件加「4 位行号 + 竖线」前缀（含省略标记行），任何行都不以反引号开头，固定 ``` 围栏无法被提前闭合；证伪者的反驳成立，未改动 |
| F-005 | 已修复 | get_run 入口快照 repo_root 并只取一次 store_for，statuses/list_runs/repo 回退/plan_resume 全部用同一对；test_web.py::test_get_run_snapshots_repo_and_store_before_a_concurrent_switch 通过 |
| F-006 | 已修复 | 链接改为 ${BASE}/api/runs/${encodeURIComponent(run.run_id)}/review.md，esc 后写入 href 并补 rel="noopener"；test_layout.py::test_review_md_link_stays_inside_the_project_base 通过 |
| F-007 | 已修复 | 摘要 POST 失败且 inline_posted>0 时补发仅含 marker 的评论再抛原错；test_ci.py 两例（有行内补 marker 且重跑 skip、无行内不留 marker）通过 |
| F-008 | 已修复 | ui/desktop 对非 {127.0.0.1, localhost, ::1} 的 --host 打印醒目告警（保留用户显式选择，不硬拒绝）；test_desktop.py::test_non_loopback_host_is_warned_not_silent 通过 |
| F-009 | 已修复 | tokens.total 改为 esc(Number(p.tokens.total) || 0)；test_layout.py::test_overview_token_counts_are_escaped 通过 |
| F-010 | 已修复 | 断言收紧为 == len(units)（已确认 tiered 实现每单元恰扫一次：review_unit 单次 complete，verifier 只复核命中单元）；test_quick_tier_reviews_hit_units_only_once_each 通过 |
