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

- **第一、二、三期已实现并通过测试**（50 tests + 真实 HTTP 端到端验证 + CI）
- 可用命令：`review`（出工单）/ `accept` / `reject` / `stats`（反馈闭环）/ `compare`（交叉对照与模型画像）
- 实现语言 Python ≥3.11；目标语言 Python；OpenAI 兼容 provider；correctness 视角
- 三种审查模式：`quick`（单模型）/ `dual`（双模型+证伪）/ `committee`（三模型）
- 不依赖 Docker；单模型三环境变量即可跑通，多模型用 JSON 配置

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

`--diff` 收任意 git revspec：`HEAD~1`、`HEAD~3..HEAD`、`main...feature`、某个 commit sha。默认最多审 10 个审查单元（`--max-units` 可调，这是防超支的硬闸）。产物 `review.md` 在原仓库根目录，溯源文件落在 `.codeerratum/runs/<run_id>/`。

## 不花钱的自检

```bash
uv run pytest -q                     # 期望 50 passed
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
双模型下报告分三区：**独有发现**（仅一家报出，最值得看）/ **冲突区**（意见相反，待人工裁决）/ **共识区**。
审查后反馈：`codeerratum accept <finding_id>` / `codeerratum reject <finding_id> --reason "..."`，`codeerratum stats` 看采纳率，`codeerratum compare` 看各模型独有发现数与实测画像。这三个命令在仓库任意子目录执行都指向同一个数据库。

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
| 四 | 自动化接入：GitHub Action、PR 评论、规则库 | 待开发 |
| 五 | 文档生成：API 文档 / 用户手册（独立里程碑，可砍） | 待开发 |
| 六 | 扩展：沙箱复现、多语言、MCP（可砍） | 待开发 |

完整范围与验收见 [开发计划.md](开发计划.md)。

## 文档说明

内部产品设计、竞品与成本分析、技术架构细节**不随本仓库发布**，保存在本地私有目录。本仓库只公开 README、开发计划、示例与用户文档。

## 许可

[MIT](LICENSE) © 2026 a2012s。可商用、可修改、可闭源再分发，只需保留版权声明与许可声明。许可证不覆盖模型供应商的服务条款，也不对审查结论的正确性作任何担保。
