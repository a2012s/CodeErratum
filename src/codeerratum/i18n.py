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
    "server.not_a_repo": ("不是 git 仓库：{path}", "Not a git repository: {path}"),
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
    # ---- server: the unit budget ----
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
    "orch.start": (
        "启动 {models} 个审查模型并行评审 {units} 个单元",
        "Starting {models} review models over {units} units in parallel",
    ),
    "orch.merged": (
        "所有模型评审完成，开始聚类合并",
        "All model reviews finished; clustering and merging",
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
    # ---- pipeline notices ----
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
    "pipeline.units_capped": (
        "仅审查 {kept}/{total} 个单元（--max-units 可调整）",
        "Reviewing {kept} of {total} units (raise it with --max-units)",
    ),
    "pipeline.report_writing": ("生成报告并写入存储", "Writing the report and storing findings"),
    "pipeline.stopped": ("审查已被停止", "Review stopped"),
    "pipeline.units_failed": (
        "{n} 个单元调用模型失败，相关文件未被审查",
        "Model calls failed for {n} units; those files were not reviewed",
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
