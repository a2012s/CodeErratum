# CodeErratum

[English](README.md) | **简体中文**

[![CI](https://github.com/a2012s/CodeErratum/actions/workflows/ci.yml/badge.svg)](https://github.com/a2012s/CodeErratum/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue.svg)](pyproject.toml)

**给 AI 写的代码开一张勘误表。**

`CodeErratum` 读一次 git diff，输出一份 `review.md` —— 不是给人看的评论列表，而是**给另一个写代码的 AI 直接执行的修改工单**：精确定位、说明期望行为、给出改法和验收命令。

> 项目早期代号 `reviewCodeAgent` / `CrossCheck` / `Erratum`。名称已在 PyPI、npm、GitHub 完成注册表级查重，均无占用；备选名 `Snag`。

## 第一期目标（唯一）

> 把 `review.md` 原样交给 Claude Code / Cursor / Cline，它能在不追问的情况下改对代码。

判据：随机抽 10 条 finding，下游 AI 直接执行成功的比例 ≥ 7 条。

## 当前状态

- **第一至六期已实现并通过测试**（170 tests + 真实 HTTP 端到端验证 + CI）
- 可用命令：`review`（出工单）/ `pr`（贴到 PR）/ `docs`（模块文档与签名漂移）/ `mcp`（给 agent 用的 stdio 服务）/ `ui`（本地图形界面）/ `accept` / `reject` / `stats` / `compare`
- 实现语言 Python ≥3.11；Python 走符号级审查，其他语言走文件级；OpenAI 兼容 provider（云端或本地）；correctness 视角
- 三种审查模式：`quick`（单模型）/ `dual`（双模型+证伪）/ `committee`（三模型）
- 不依赖 Docker；单模型三环境变量即可跑通（含本地 Ollama），多模型用 JSON 配置

## 安装

```bash
git clone git@github.com:a2012s/CodeErratum.git
cd CodeErratum
uv sync --extra dev
uv run codeerratum --version
```

不想用 uv 时等价做法：

```bash
python3.11 -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/codeerratum --version
```

> PyPI 尚未发布，`pip install codeerratum` 现在不可用；装完 `uv sync` 后 `codeerratum` 就在 `.venv/bin/` 里。

## 配置

CLI **不会**自动读 `.env`，必须导出为环境变量：

```bash
cp .env.example .env && vi .env     # 填 BASE_URL / API_KEY / MODEL
set -a && source .env && set +a
```

单模型只需三个变量；异构多模型用 `CODEERRATUM_MODELS`（JSON）+ `CODEERRATUM_MODE`，见下文。

## 运行

在**被审查的仓库**里执行（不是在本项目目录里）：

```bash
cd /path/to/your-repo
codeerratum review --diff HEAD~1 -o review.md
```

`--diff` 收任意 git revspec：`HEAD~1`、`HEAD~3..HEAD`、`main...feature`、某个 commit sha。审本地未提交的改动用 `--diff HEAD`（暂存与未暂存一起）；全新文件要先 `git add`（不必 commit）。加 `--path src/api` 可只审该目录的改动，已跟踪文件不需要先 `git add`。默认最多审 10 个审查单元（`--max-units` 可调，这是防超支的硬闸）。产物 `review.md` 在原仓库根目录，溯源文件落在 `.codeerratum/runs/<run_id>/`。

> 审 `main...feature` 这类**未 checkout 的版本**时，源码、摘录与锚点校验都直接取 diff 版本的 git blob，不需要先切分支。只有当某个文件的 blob 取不到（例如未提交的工作区改动）时才回退到工作区，内容对不上会跳过该文件并在「本次覆盖说明」写明。

## 不花钱的自检

```bash
uv run pytest -q                     # 期望 170 passed
```

想验证全链路又不想消耗 token，用仓库自带的假模型端点（固定返回一条 finding）：

```bash
uv run python scripts/mock_server.py          # 终端 A，监听 127.0.0.1:8899

# 终端 B，在任意有 ≥2 次提交的仓库里
CODEERRATUM_BASE_URL=http://127.0.0.1:8899/v1 CODEERRATUM_API_KEY=x \
CODEERRATUM_MODEL=mock codeerratum review --diff HEAD~1 -o review.md
```

## 快速开始

配好三个环境变量后，一条命令：

```bash
export CODEERRATUM_BASE_URL=https://api.example.com/v1
export CODEERRATUM_API_KEY=sk-...          # 本地端点可留空
export CODEERRATUM_MODEL=your-model-id
codeerratum review --diff HEAD~1 -o review.md
```

双模型异构交叉（dual，第三期核心价值）：至少配两家不同厂商 + 可选第三家作证伪。

```bash
export CODEERRATUM_MODELS='[
  {"model_id":"gpt-4o-2024-08-06","base_url":"https://api.openai.com/v1","api_key":"sk-...","family":"openai"},
  {"model_id":"claude-3-5-sonnet","base_url":"https://api.anthropic.com/v1","api_key":"sk-...","family":"anthropic"},
  {"model_id":"deepseek-chat","base_url":"https://api.deepseek.com/v1","api_key":"sk-...","family":"deepseek"}
]'
export CODEERRATUM_MODE=dual
codeerratum review --diff HEAD~1 -o review.md
```

`family` 是异构判据：同厂商两个型号不算异构，会被路由拒绝。证伪轮优先用第三家；没配第三家时在两个审查模型间交叉证伪。
如果这段代码是某个模型写的，用 `--author-model <model_id>` 把它排除出审查席位（设计底线第 1 条）。

产出 `review.md` 交给 Claude Code / Cursor / Cline 执行；每次运行的可追溯产物落在 `.codeerratum/runs/<run_id>/`（`input.diff` / `units.json` / `calls.json` / `findings.json` / `static_diags.json`），反馈数据库在同一目录，不会污染工作区。

不花 token 就能看到真实格式的产物：[`examples/sample-review.md`](examples/sample-review.md)——格式是真的，内容来自仓库自带的假模型端点。
双模型下报告分三区：**独有发现**（仅一家报出，最值得看）/ **冲突区**（意见相反，待人工裁决）/ **共识区**。
审查后反馈：`codeerratum accept <finding_id>` / `codeerratum reject <finding_id> --reason "..."`，`codeerratum stats` 看采纳率，`codeerratum compare` 看各模型独有发现数与实测画像。这三个命令在仓库任意子目录执行都指向同一个数据库。

### 项目规范（可选）

告诉审查「本项目已经拍过板的事」，让它别再报：

```bash
codeerratum review --diff HEAD~1 --rules project-rules.md
# 也可以完全不传：.codeerratum/rules.md 会被自动发现
```

```markdown
<!-- .codeerratum/rules.md -->
- 本项目不用类型注解，缺注解不算问题
- 日志里 f-string 的性能开销是权衡过的，不要报
```

规范是**追加，不是替换**。它被包在 `<house_rules>` 块里，位置在硬规则之后、输出契约之前，prompt 里明确写了「与硬规则冲突时以硬规则为准」。这个顺序不是排版偏好：如果允许整段替换 system prompt，用户就可能要求更长的 `claim`、或非逐字的 `snippet`，锚点校验会把这类发现全部丢弃——最后返回一份干净的空报告，读起来和「没有 bug」一模一样。

上限 4000 字，超出会截断并在报告里声明。实际生效的规范以 12 位 sha256 同时写进 `review.md` 头部与 `findings.json`：同一个 diff 两次结果不一样时，先看这里是不是换了提示词。证伪轮**故意不接收**项目规范——一个吸收了你家规矩的证伪者，就不再是独立检验。

## 图形界面（可选）

```bash
codeerratum ui                      # 起服务并打开 http://127.0.0.1:8765/
codeerratum ui --port 9000 --no-browser
```

![CodeErratum 图形界面](https://raw.githubusercontent.com/a2012s/CodeErratum/main/docs/ui.png)

文档抽屉用的是同一个服务，点一下就到——模块地图与签名漂移，不需要任何构建步骤：

![CodeErratum 文档抽屉](https://raw.githubusercontent.com/a2012s/CodeErratum/main/docs/docs.png)

纯标准库实现——没有 FastAPI、没有打包工具、没有 node_modules。装依赖的故事仍然只有"三个环境变量"，界面只是补上终端给不了的东西：三区分流并排、每条发现的证据强度、证伪轮各方意见，以及直接写回 `feedback.db` 的采纳/驳回按钮。

左侧选 diff 范围、路径过滤、模式、单元上限和作者模型（写这段代码的模型不进入审查席位），下方是历史运行，点开任意一次即可。只绑本机回环地址，拒绝非回环 Host（防 DNS rebinding），并要求每次 POST 带同源 Origin——否则随便哪个网页都能替你烧 token。

顶栏「配置」可查看并修改当前模式与模型端点：保存后写入 `.codeerratum/config.json`（权限 0600），优先级高于环境变量，重启界面依然生效，命令行也读同一份。api_key 不会回显到浏览器，留空即沿用已保存的值。

### 贴到 PR 上

```bash
codeerratum pr                        # dry-run：只打印将要发送的内容
GITHUB_TOKEN=... codeerratum pr --post
```

行内评论上限 5 条，且每一行都必须真的在 diff 里。GitHub 会因为一条评论指到 diff 外而
把**整个 review** 拒掉，所以放不进 diff 的发现会转到摘要表，而不是拖着另外四条一起失败。
同一 commit 重跑不会重复刷评论（摘要带隐藏 marker，发之前先查）。

在 Actions 里用仓库自带的 [`action.yml`](action.yml)，示例 workflow 见
[`docs/github-action.yml`](docs/github-action.yml)。两个坑要提前知道：`fetch-depth: 0`
是必须的（浅克隆看不到 merge base）；fork 的 PR 拿不到 secrets，示例里显式跳过，
而不是让每个外部贡献都红一次。

### 模块文档与签名漂移

```bash
codeerratum docs                   # 生成 docs/api/*.md 与 .codeerratum/docs-index.json
codeerratum docs --check --strict  # CI 闸门：公开签名变了就 exit 1
```

文档由 AST 生成、不经模型，所以它自己不会漂。漂移的定义是「公开 API 的形状变了」：
行号会记录但从不比较——每次重构都报警的检查，一周之内就会被人关掉。

### MCP 服务

```bash
codeerratum mcp --repo /path/to/repo
```

stdio 上的 JSON-RPC，暴露四个工具：`review_diff` / `list_runs` / `get_finding` /
`check_docs_drift`。注册成 stdio server，agent 就能在交活之前先审一遍自己的输出，
不用切终端、不用复制粘贴：

```json
{
  "mcpServers": {
    "codeerratum": {
      "command": "codeerratum",
      "args": ["mcp", "--repo", "/absolute/path/to/repo"]
    }
  }
}
```

### 本地模型

任何 OpenAI 兼容端点都能用，且不需要 API key：

```bash
export CODEERRATUM_BASE_URL=http://localhost:11434/v1   # Ollama
export CODEERRATUM_MODEL=qwen2.5-coder:14b
codeerratum review --diff HEAD~1
```

## 设计底线

1. **审查者 ≠ 作者**：生成代码的模型不得进入审查席位
2. **每条结论带定位**：文件、行号、原文片段对不上就不输出
3. **输出是工单不是评论**：期望行为、具体改法、验收标准，三样缺一不可
4. **AI 审查通过 ≠ 可以合入**：报告固定保留人工确认位
5. **不靠多数投票定真假**：LLM 错误高度相关；用跨厂商「证伪」替代投票，独有发现单独成区

## 路线图（按重要程度）

| 期次 | 主题 | 状态 |
|---|---|---|
| 一 | 可执行工单：diff → AI 能照着改的 `review.md` | ✅ 已实现 |
| 二 | 可信度：消除幻觉定位与空泛结论 | ✅ 已实现 |
| 三 | 异构交叉：双模型 + 证伪，产出独有发现 | ✅ 已实现 |
| 四 | 自动化接入：GitHub Action、PR 评论、规则库 | ✅ 已实现 |
| 五 | 文档生成：模块地图 + 签名漂移 | ✅ 已实现 |
| 六 | 扩展：MCP 服务、本地模型、语言标签（**沙箱复现明确不做**） | ✅ 已实现（按设计裁剪） |
| 旁支 | 本地 Web UI：三区分流、界面内裁决、文档抽屉 | ✅ 已实现 |

沙箱复现是唯一明确拒绝的功能：它意味着在无鉴权的本地服务里执行来自 PR 的不可信代码。
风险收益不成比例，而且「我运行你的代码来证明 bug」这个承诺，个人项目兜不住。

完整范围与验收见 [开发计划.md](开发计划.md)。

## 文档说明

内部产品设计、竞品与成本分析、技术架构细节**不随本仓库发布**，保存在本地私有目录。本仓库只公开 README、开发计划、示例与用户文档。

## 许可

[MIT](LICENSE) © 2026 a2012s。可商用、可修改、可闭源再分发，只需保留版权声明与许可声明。许可证不覆盖模型供应商的服务条款，也不对审查结论的正确性作任何担保。
