"""Two-language message catalog for everything a user actually reads.

Only Chinese (default) and English exist. The active language lives in a
:class:`contextvars.ContextVar`, not in a module global: the web UI serves
requests on threads from a pool, and one tab's language choice must not leak
into another's response. ``CODEERRATUM_LANG`` seeds the default so the CLI and
the MCP server can run in English without touching call sites.

Why a catalog instead of ``if lang == "en"`` at each site: every message then
has exactly one place to fix a typo, and the Chinese text stays byte-identical
to what the tests and the reports already assert.
"""

from __future__ import annotations

import contextvars
import os
from contextlib import contextmanager

LANGS = ("zh", "en")

_ZH = "zh"


def normalize_lang(value: str | None) -> str:
    """Anything that is not clearly English falls back to Chinese."""
    text = (value or "").strip().lower()
    if text.startswith("en"):
        return "en"
    return _ZH


# Seeded once from the environment so CLI / MCP runs can pick a language without
# passing it through every call; per-request overrides live in ``_current``.
_ENV_LANG = normalize_lang(os.environ.get("CODEERRATUM_LANG"))

_current: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "codeerratum_lang", default=None
)


def get_lang() -> str:
    return _current.get() or _ENV_LANG


def set_lang(value: str | None) -> str:
    lang = normalize_lang(value)
    _current.set(lang)
    return lang


@contextmanager
def language(value: str | None):
    """Temporarily force a language (tests, one-off scripts)."""
    token = _current.set(normalize_lang(value))
    try:
        yield get_lang()
    finally:
        _current.reset(token)


# key -> (zh, en). Placeholders use str.format syntax.
MESSAGES: dict[str, tuple[str, str]] = {
    # ---- server: local paths and the native folder picker ----
    "server.path_missing": ("路径不存在：{path}", "Path does not exist: {path}"),
    "server.not_a_folder": ("不是文件夹：{path}", "Not a folder: {path}"),
    # Not just "no": the fix has to travel with the refusal, or the reader is left
    # with a dead end. Reviews come from `git diff`, so a plain folder has no
    # changes to read at all; the two lines below are the whole remedy.
    "server.not_a_repo": (
        "目录不是 git 仓库，也没有 {spec} 可以对比：{path}\n"
        "\n"
        "· 要审整个目录的代码，把审查范围填 WORKTREE（界面里点「全部代码」），不需要 git init\n"
        "· 想按 diff 审查，就先在该目录 git init && git add -A（不必 commit），再填 HEAD",
        "Not a git repository, and {spec} does not exist in it: {path}\n"
        "\n"
        "· To review the whole folder, set the range to WORKTREE (the “Whole tree” button) — "
        "no git init needed\n"
        "· To review a diff instead, run git init && git add -A there (no commit needed), "
        "then use HEAD",
    ),
    "server.picker_title": ("选择要审查的 git 仓库", "Choose a git repository to review"),
    "server.picker_no_zenity": (
        "本系统未安装 zenity，无法弹出文件夹选择器；请粘贴路径",
        "zenity is not installed, so no folder picker is available; paste a path instead",
    ),
    "server.picker_timeout": (
        "文件夹选择超时，请重试或粘贴路径",
        "The folder picker timed out; retry or paste a path",
    ),
    "server.picker_open_failed": (
        "无法打开文件夹选择器：{error}",
        "Cannot open the folder picker: {error}",
    ),
    "server.picker_cancelled": ("已取消选择文件夹", "Folder selection cancelled"),
    # ---- server: switching / opening a repository ----
    "server.repo_path_required": ("请填写本地文件夹路径", "Enter a local folder path"),
    "server.repo_abs_required": (
        "请填写绝对路径（支持 ~ 开头的家目录）",
        "Enter an absolute path (a leading ~ is fine)",
    ),
    "server.switch_blocked": (
        "审查进行中，不能切换仓库",
        "Cannot switch repositories while a review is running",
    ),
    "server.repo_dir_missing": (
        "当前仓库目录不存在：{path}",
        "The current repository directory no longer exists: {path}",
    ),
    "server.opener_missing": (
        "未找到 xdg-open / gio，请在文件管理器中手动打开当前仓库",
        "No xdg-open / gio found; open the repository manually in your file manager",
    ),
    "server.open_timeout": (
        "打开文件管理器超时，请稍后重试",
        "Opening the file manager timed out; try again later",
    ),
    "server.open_failed": (
        "无法打开当前仓库文件夹：{error}",
        "Cannot open the repository folder: {error}",
    ),
    "server.dropped_name_invalid": ("拖入的文件夹名无效", "That dropped folder name is unusable"),
    "server.open_unknown": ("系统返回失败", "the system reported no detail"),
    # ---- server: no project selected (desktop/bare launch) ----
    "server.no_project": (
        "还没有添加项目：先在总览页添加一个 git 仓库",
        "No project yet: add a git repository on the overview page first",
    ),
    # ---- server: the unit budget ----
    "server.unknown_models": (
        "没有配置这些模型：{ids}",
        "These models are not configured: {ids}",
    ),
    "server.units_clamped_low": (
        "上限 {asked} 超出范围（至少 1 个审查单元），已按 {used} 执行",
        "Limit {asked} is out of range (at least 1 review unit); running with {used}",
    ),
    "server.units_clamped_high": (
        "上限 {asked} 超出范围（最多 {cap} 个审查单元），已按 {used} 执行",
        "Limit {asked} is out of range (at most {cap} review units); running with {used}",
    ),
    # ---- server: run lifecycle ----
    "server.review_busy": (
        "已有审查正在进行中，请等待其完成后再运行",
        "A review is already running; wait for it to finish",
    ),
    "server.no_active_review": ("当前没有正在进行的审查", "No review is running"),
    "server.stop_requested_log": (
        "已请求停止，正在中断模型调用…",
        "Stop requested; cancelling in-flight model calls…",
    ),
    "server.stop_requested": ("已请求停止审查", "Review stop requested"),
    "server.queued": (
        "排队中：等待其他项目释放执行位…",
        "Queued: waiting for another project to free an execution slot…",
    ),
    "server.queue_cancelled": ("已取消排队", "Left the queue"),
    "server.stopped_by_user": ("审查已被用户停止", "Review stopped by the user"),
    "server.stream_connected": (
        "已连接进度流，准备审查…",
        "Progress stream connected; preparing the review…",
    ),
    # ---- project hub ----
    "hub.project_missing": ("项目不存在", "Project not found"),
    "hub.project_busy": (
        "该项目正在审查，先停止再移除",
        "This project is reviewing; stop it before removing",
    ),
    "hub.no_projects": (
        "还没有项目，请先添加一个本地仓库",
        "No projects yet; add a local repository first",
    ),
    "hub.add_title": ("选择要加入审查的项目", "Choose a project to add to the review board"),
    # ---- orchestrator progress ----
    "orch.unit_start": (
        "[{model}] 审查单元 {idx}/{total} · {file}",
        "[{model}] reviewing unit {idx}/{total} · {file}",
    ),
    "orch.unit_failed": (
        "[{model}] 单元 {idx}/{total} 调用失败：{error}",
        "[{model}] unit {idx}/{total} call failed: {error}",
    ),
    "orch.unit_done": (
        "[{model}] 单元 {idx}/{total} 完成（{n} 条发现）",
        "[{model}] unit {idx}/{total} done ({n} findings)",
    ),
    "orch.err_read_timeout": (
        "读取超时（{seconds}s 内没有返回任何数据；已重试 1 次）",
        "read timeout (no data for {seconds}s; retried once)",
    ),
    "orch.err_connect": (
        "连接失败（端点无响应，已重试）",
        "connection failed (endpoint unreachable, retried)",
    ),
    "orch.err_auth": (
        "鉴权失败（HTTP {status}），检查 API key 与 base_url",
        "auth failed (HTTP {status}); check the API key and base_url",
    ),
    "orch.err_rate_limited": (
        "被限流（HTTP 429，已按 Retry-After 退避重试）",
        "rate limited (HTTP 429, retried after Retry-After)",
    ),
    "orch.err_server": (
        "服务端错误（HTTP {status}，已重试）",
        "server error (HTTP {status}, retried)",
    ),
    "orch.err_transport": ("网络异常（{kind}）", "network error ({kind})"),
    "orch.start": (
        "启动 {models} 个审查模型并行评审 {units} 个单元",
        "Starting {models} review models over {units} units in parallel",
    ),
    "orch.merged": (
        "所有模型评审完成，开始聚类合并",
        "All model reviews finished; clustering and merging",
    ),
    "orch.tier_start": (
        "分层审查：{scanner} 全量扫描，{verifier} 复核命中单元",
        "Tiered review: {scanner} scans everything, {verifier} re-reviews hit units",
    ),
    "orch.verify_round": (
        "复核轮：{verifier} 重审 {n} 个命中单元",
        "Verification round: {verifier} re-reviews {n} hit units",
    ),
    "orch.verify_failed": (
        "复核 {model} 失败：{file}",
        "Verification by {model} failed: {file}",
    ),
    "orch.falsify_round": (
        "证伪轮：{n} 条高价值发现交叉验证",
        "Falsification round: cross-checking {n} high-value findings",
    ),
    "orch.falsify_verdict": (
        "证伪 {model}：{verdict} · {claim}",
        "Falsify {model}: {verdict} · {claim}",
    ),
    "orch.falsify_failed": (
        "证伪 {model}：调用失败 · {claim}",
        "Falsify {model}: call failed · {claim}",
    ),
    # ---- ingest / git ----
    "git.pathspec_relative": (
        "路径必须是仓库内的相对路径（不接受绝对路径或 ..）",
        "Paths must be relative to the repository (no absolute paths, no ..)",
    ),
    "git.diff_unresolvable": (
        "无法解析 {spec}：当前仓库可能只有一个提交。\n"
        "审查本地未提交改动请填 HEAD；审查上一个提交请用 HEAD~1（需要至少两个提交）。\n\n"
        "git 原始错误：\n{detail}",
        "Cannot resolve {spec}: this repository may have only one commit.\n"
        "Use HEAD for local uncommitted changes; use HEAD~1 for the previous commit "
        "(which needs at least two commits).\n\n"
        "git said:\n{detail}",
    ),
    "git.no_commits": (
        "这个仓库还没有任何提交，所以 {spec} 不存在（HEAD 指向一个空分支）。\n\n"
        "两条路，选一条：\n"
        "1. 先提交一次，再审查这次的提交：\n"
        '   git add -A && git commit -m "baseline"\n'
        "   然后把审查范围填成 HEAD（或 HEAD~1 审上一个提交）。\n"
        "2. 不想提交，直接审全部已暂存代码：\n"
        "   把审查范围填成 {tree}\n"
        "   （空树。每个 git add 过的文件都算“新增”，未 add 的文件不进 diff。）\n\n"
        "git 原始错误：\n{detail}",
        "This repository has no commits yet, so {spec} does not exist "
        "(HEAD points at an unborn branch).\n\n"
        "Pick one:\n"
        "1. Commit once, then review that commit:\n"
        '   git add -A && git commit -m "baseline"\n'
        "   then set the range to HEAD (or HEAD~1 for the previous commit).\n"
        "2. Review everything staged, without committing:\n"
        "   set the range to {tree}\n"
        "   (the empty tree: every `git add`ed file counts as an addition; "
        "files you never added stay out of the diff.)\n\n"
        "git said:\n{detail}",
    ),
    "git.snapshot_timeout": (
        "扫描整个工作区超过 {seconds} 秒，已中止。多半是缺少 .gitignore，"
        ".venv / node_modules / dist 被逐个读入了；补上忽略规则再重试。",
        "Scanning the working tree took longer than {seconds}s and was aborted. The "
        "project probably has no .gitignore, so .venv / node_modules / dist were "
        "being walked; add one and retry.",
    ),
    # ---- pipeline notices ----
    "pipeline.no_commits_snapshot": (
        "仓库还没有提交，已改为审查全部工作区文件（遵循 .gitignore，跳过 .codeerratum）",
        "No commits yet, so the whole working tree is reviewed instead "
        "(honouring .gitignore, skipping .codeerratum)",
    ),
    "pipeline.worktree_requested": (
        "已按要求审查全部工作区文件（遵循 .gitignore，跳过 .codeerratum）",
        "Reviewing the whole working tree as requested "
        "(honouring .gitignore, skipping .codeerratum)",
    ),
    "pipeline.snapshot_empty": (
        "工作区里没有可审查的文件：都被 .gitignore 忽略，或者目录是空的",
        "Nothing reviewable in the working tree: everything is gitignored, or it is empty",
    ),
    "pipeline.scope_fallback": (
        "{spec} 没有未提交改动（工作区是干净的），已自动改为审查全部代码"
        "（遵循 .gitignore，跳过 .codeerratum）。只想审某次改动的话，先改几个文件再发起，"
        "或把范围写成某个提交区间。",
        "{spec} has no uncommitted changes (the tree is clean), so this run reviews all "
        "the code instead (honouring .gitignore, skipping .codeerratum). To review only a "
        "change, edit something first or use a commit range.",
    ),
    "pipeline.abstain": (
        "证伪轮 {n}/{total} 判为证据不足（>30%），上下文构建可能不足，结论请谨慎采用",
        "Falsification returned insufficient_context for {n}/{total} (over 30%), so "
        "context gathering may be too thin — treat the conclusions with care",
    ),
    "pipeline.path_scope": (
        "仅审查路径 {paths} 内的改动（其他目录未审）",
        "Reviewing only changes under {paths} (other directories were not reviewed)",
    ),
    "pipeline.rules_truncated": (
        "项目规范超过 {limit} 字，已截断",
        "Project rules exceeded {limit} characters and were truncated",
    ),
    "pipeline.snapshot_fallback": (
        "无法读取 diff 版本的文件内容（{error}），锚点改为基于工作区校验",
        "Could not read file contents at the diff revision ({error}); anchors fall back "
        "to the working tree",
    ),
    "pipeline.units_auto": (
        "自动选定 {kept}/{total} 个单元（按风险排序）。想全审请把上限设为「全部」。",
        "Auto-selected {kept} of {total} units (highest risk first). Choose "
        "\u201ceverything\u201d to review all of them.",
    ),
    "pipeline.models_invalid": (
        "模型选择不合法：{detail}",
        "Invalid model selection: {detail}",
    ),
    "pipeline.units_capped": (
        "仅审查 {kept}/{total} 个单元（--max-units 可调整）",
        "Reviewing {kept} of {total} units (raise it with --max-units)",
    ),
    "pipeline.non_code_skipped": (
        "已跳过 {n} 个非代码单元（文档/配置/i18n）；如需审查请开启 include_non_code",
        "Skipped {n} non-code units (docs/config/i18n); enable include_non_code to review them",
    ),
    "pipeline.report_writing": ("生成报告并写入存储", "Writing the report and storing findings"),
    "pipeline.stopped": ("审查已被停止", "Review stopped"),
    "pipeline.stopped_partial": (
        "审查已停止：仅完成 {done}/{total} 个单元，报告只包含这些单元的结果；"
        "证伪/验证未完成",
        "Review stopped after {done}/{total} units; the report covers only those "
        "units and falsification/verification did not finish",
    ),
    "pipeline.stopped_partial_mixed": (
        "审查已停止：{done}/{total} 个单元被全部模型完成，另有 {partial} 个单元"
        "只有部分模型完成；报告按现状保留，证伪/验证未完成",
        "Review stopped: {done}/{total} units finished for every model, {partial} "
        "more are only partially reviewed; the report keeps what exists and "
        "falsification/verification did not finish",
    ),
    "pipeline.units_failed": (
        "{n} 个单元调用模型失败，相关文件未被审查",
        "Model calls failed for {n} units; those files were not reviewed",
    ),
    "pipeline.units_failed_reason": ("失败原因：{reasons}", "Reason: {reasons}"),
    "resume.no_such_run": (
        "找不到运行 {run_id}（{dir} 下没有这个目录）",
        "No such run {run_id} (no directory under {dir})",
    ),
    "resume.no_report": (
        "运行 {run_id} 没有 findings.json，无法续跑",
        "Run {run_id} has no findings.json to resume from",
    ),
    "resume.no_seats": (
        "运行记录里没有审查席位（reviewer_models 为空）",
        "Run record has no reviewer seats (reviewer_models empty)",
    ),
    "resume.nothing_left": (
        "运行 {run_id} 的队列已全部完成，无需续跑",
        "Run {run_id} queue is fully covered — nothing to resume",
    ),
    "resume.models_missing": (
        "当前配置里找不到原运行使用的模型：{models}，无法保证席位一致，已取消续跑",
        "Models from the original run are missing from the config: {models} — resume aborted",
    ),
    "resume.start": (
        "续跑：已完成 {done}/{total} 个单元，本批再审 {batch} 个（同席位，不重跑已完成单元）",
        "Resume: {done}/{total} covered; this batch reviews {batch} more (same seats)",
    ),
    "resume.note": (
        "续跑完成：本批 {batch} 个单元，累计覆盖 {done}/{total}（已完成单元未重跑、未重复计费）",
        "Resumed: batch of {batch}; cumulative coverage {done}/{total} (no re-runs, no re-billing)",
    ),
    "cli.resume_remaining": (
        "剩余 {n} 个单元未审（{done}/{total} 已完成）。继续：codeerratum resume {run_id}",
        "{n} units remain ({done}/{total} done). Continue: codeerratum resume {run_id}",
    ),
    "pipeline.verify_failed": (
        "{n} 个单元的验证器调用失败，对应发现保留为未验证",
        "Verifier calls failed for {n} units; their findings were kept as unverified",
    ),
    "pipeline.machine_dropped": (
        "机器丢弃 {n} 条（{reasons}），明细见报告末尾与 run_rejections",
        "{n} machine-dropped ({reasons}); details in the report and run_rejections",
    ),
    "pipeline.max_units_negative": (
        "--max-units 不能为负数（{n}）：负数会被当成全量审查，请改用 0 表示全量",
        "--max-units cannot be negative ({n}); use 0 for a full sweep",
    ),
    "pipeline.parse_failures": (
        "{n} 次结构化输出解析失败（成功率 {rate}），样本见 parse_failures.json",
        "{n} structured outputs failed to parse (success {rate}); samples in parse_failures.json",
    ),
    # ---- model probe ----
    "model.timeout": ("连接超时（{seconds:g}s）", "Connection timed out ({seconds:g}s)"),
    "model.unreachable": (
        "无法连接端点（地址或网络不可达）{suffix}",
        "Cannot reach the endpoint (bad host or offline){suffix}",
    ),
    "model.reason_suffix": ("：{reason}", ": {reason}"),
    "model.network_error": ("网络错误：{error}", "Network error: {error}"),
    "model.request_failed": ("请求失败：{error}", "Request failed: {error}"),
    "model.missing_id": ("缺少 model_id", "model_id is missing"),
    "model.bad_base_url": (
        "base_url 必须以 http:// 或 https:// 开头",
        "base_url must start with http:// or https://",
    ),
    "model.redirect": (
        "端点返回 {status} 重定向{suffix}：base_url 可能填成了控制台网页地址；"
        "请使用以 /v1 结尾的 API 端点",
        "The endpoint answered {status} with a redirect{suffix}: base_url may point at a "
        "console web page instead of an API endpoint ending in /v1",
    ),
    "model.target_suffix": (" → {target}", " → {target}"),
    "model.auth": (
        "鉴权失败（{status}）：检查 api_key",
        "Authentication failed ({status}): check api_key",
    ),
    "model.token_plan_hint": (
        "；sk-sp- 是 Token Plan 专属密钥，请用 "
        "token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1",
        "; sk-sp- is a Token Plan key — use "
        "token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1",
    ),
    "model.not_found": ("404：检查 base_url 与 model_id", "404: check base_url and model_id"),
    "model.rate_limited": ("429：端点已限流，稍后重试", "429: rate limited — retry later"),
    "model.server_error": ("服务端错误（{status}）", "Server error ({status})"),
    "model.rejected": ("请求被拒（{status}）", "Request rejected ({status})"),
    "model.no_choices": (
        "响应无 choices，端点可能不兼容 OpenAI 协议",
        "The response has no choices — the endpoint may not speak the OpenAI protocol",
    ),
    "model.bad_json": (
        "响应不是合法 JSON：{error}",
        "The response is not valid JSON: {error}",
    ),
    "model.ok": ("可用 · {ms}ms", "Available · {ms}ms"),
    # ---- config ----
    "config.no_models": (
        "未配置模型：设置环境变量，或在界面里保存配置到 {path}",
        "No models configured: set the environment variables, or save a config to {path} "
        "from the UI",
    ),
    # ---- desktop window ----
    "cli.models_missing": (
        "尚未配置模型（CODEERRATUM_BASE_URL / API_KEY / MODEL），"
        "界面可以看历史记录，但不能发起新审查。",
        "No model configured (CODEERRATUM_BASE_URL / API_KEY / MODEL): history is "
        "readable, but a new review cannot start.",
    ),
    "cli.stop_hint": ("Ctrl+C 停止", "Ctrl+C to stop"),
    "cli.stopped": ("已停止", "Stopped"),
    "cli.host_exposed": (
        "警告：--host {host} 会把无鉴权的 UI 暴露给网络——同网段设备可触发付费审查、"
        "读取代码内容并改写人工裁决。建议保持 127.0.0.1。",
        "Warning: --host {host} exposes the unauthenticated UI to the network — "
        "anyone on the LAN can start paid reviews, read code and rewrite verdicts. "
        "Keep it on 127.0.0.1.",
    ),
    "cli.reconcile_missing": ("找不到工单文件：{path}", "Work order not found: {path}"),
    "cli.reconcile_no_table": (
        "工单里没有《执行结果》表，无法回填",
        "The work order has no execution-result table; nothing to ingest",
    ),
    "cli.reconcile_empty": (
        "《执行结果》表里没有可回填的行（待处理的行会被跳过）",
        "The execution-result table has no rows to ingest (pending rows are skipped)",
    ),
    "cli.reconcile_summary": (
        "共 {rows} 行：已回填 {updated}（采纳 {accepted} / 驳回 {rejected}），"
        "跳过 {skipped}，编号未匹配 {unmatched}",
        "Rows: {rows}; ingested {updated} (accepted {accepted} / rejected {rejected}), "
        "skipped {skipped}, unmatched {unmatched}",
    ),
    "cli.reconcile_dry": ("[预演] 未写入数据库", "[dry run] nothing written"),
    "cli.reconcile_done": ("已写入反馈库：{path}", "Written to feedback store: {path}"),
    "cli.reconcile_no_writes": (
        "没有任何结果写入反馈库：请检查报告编号是否对应当前审查记录",
        "No outcomes were written: check that the report's finding ids belong to "
        "the current review history",
    ),
    "desk.missing": (
        "缺少图形组件 pywebview：先执行 uv pip install -e '.[desktop]'，或改用 codeerratum ui",
        "The pywebview GUI extra is missing: run uv pip install -e '.[desktop]', "
        "or use codeerratum ui instead",
    ),
    "desk.title": ("CodeErratum 代码审查", "CodeErratum"),
    "desk.closed": ("窗口已关闭，服务停止", "Window closed; server stopped"),
    "desk.close_busy_title": ("审查进行中，确定退出？", "Review in progress — quit anyway?"),
    "desk.close_busy_message": (
        "退出会立即中断正在进行的审查，未完成的模型调用被取消；本次已产生的费用不会退回。",
        "Quitting stops the review immediately and cancels in-flight model calls. "
        "Tokens already used are not refunded.",
    ),
    "desk.close_busy_quit": ("退出并中断", "Quit and stop"),
    "desk.close_busy_cancel": ("继续审查", "Keep reviewing"),
    "desk.parent_gone": (
        "启动本服务的进程已退出，服务随之停止",
        "The process that launched this server exited; stopping",
    ),
}


def t(key: str, **kw) -> str:
    """Look up ``key`` in the active language and interpolate ``kw``.

    Unknown keys return the key itself rather than raising: a missing message
    should never turn a red cross into a crash.
    """
    entry = MESSAGES.get(key)
    if entry is None:
        return key
    zh, en = entry
    text = en if get_lang() == "en" else zh
    if not kw:
        return text
    try:
        return text.format(**kw)
    except (KeyError, IndexError, ValueError):
        return text


def catalog_en() -> dict[str, str]:
    """Flat ``{key: english}`` view — handy for tests and for exporting strings."""
    return {key: en for key, (_zh, en) in MESSAGES.items()}
