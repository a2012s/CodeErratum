# CodeErratum

**English** | [简体中文](README.zh-CN.md)

[![CI](https://github.com/a2012s/CodeErratum/actions/workflows/ci.yml/badge.svg)](https://github.com/a2012s/CodeErratum/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue.svg)](pyproject.toml)

**An errata sheet for code written by AI.**

CodeErratum reads a git diff and writes a `review.md` — not a list of comments for humans to skim, but a **work order another coding agent can execute directly**: exact location, expected behavior, a concrete fix, and an acceptance command.

> Formerly known as `reviewCodeAgent` / `CrossCheck` / `Erratum`. The name was checked against the PyPI, npm and GitHub registries — none taken. Fallback name: `Snag`.

## The one goal (phase 1)

> Hand `review.md` to Claude Code / Cursor / Cline as-is, and it fixes the code without asking a single follow-up question.

Success criterion: sample 10 findings at random; at least 7 must be executed correctly by a downstream agent with no clarification.

## Status

- **Phases 1–3 implemented and tested** (50 tests + real-HTTP end-to-end + CI)
- Commands: `review` (write the work order) / `accept` / `reject` / `stats` (feedback loop) / `compare` (cross-model comparison and model profiles)
- Written in Python ≥3.11; reviews Python; any OpenAI-compatible provider; `correctness` lens
- Three review modes: `quick` (single model) / `dual` (two models + falsification) / `committee` (three models)
- No Docker. Three environment variables get a single model running; multi-model uses one JSON config

## Install

```bash
git clone git@github.com:a2012s/CodeErratum.git
cd CodeErratum
uv sync --extra dev
uv run codeerratum --version
```

Without uv, the equivalent:

```bash
python3.11 -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/codeerratum --version
```

> Not on PyPI yet — `pip install codeerratum` will not work today. After `uv sync` the command lives in `.venv/bin/`.

## Configuration

The CLI does **not** load `.env` on its own; export the variables into your shell:

```bash
cp .env.example .env && vi .env     # fill in BASE_URL / API_KEY / MODEL
set -a && source .env && set +a
```

A single model needs three variables. Heterogeneous multi-model uses `CODEERRATUM_MODELS` (JSON) plus `CODEERRATUM_MODE` — see below.

## Usage

Run it **inside the repository under review**, not inside this project's directory:

```bash
cd /path/to/your-repo
codeerratum review --diff HEAD~1 -o review.md
```

`--diff` takes any git revspec: `HEAD~1`, `HEAD~3..HEAD`, `main...feature`, a raw commit sha. At most 10 review units per run by default (`--max-units` to change it) — a hard cost gate, not a suggestion. The `review.md` lands in the root of the reviewed repo; trace artifacts go to `.codeerratum/runs/<run_id>/`.

## Zero-cost self-check

```bash
uv run pytest -q                     # expect 50 passed
```

To exercise the whole pipeline without spending tokens, use the bundled fake model endpoint (always returns one fixed finding):

```bash
uv run python scripts/mock_server.py          # terminal A, listens on 127.0.0.1:8899

# terminal B, inside any repo with at least 2 commits
CODEERRATUM_BASE_URL=http://127.0.0.1:8899/v1 CODEERRATUM_API_KEY=x \
CODEERRATUM_MODEL=mock codeerratum review --diff HEAD~1 -o review.md
```

## Quick start

With three environment variables set, it is one command:

```bash
export CODEERRATUM_BASE_URL=https://api.example.com/v1
export CODEERRATUM_API_KEY=sk-...          # may be empty for local endpoints
export CODEERRATUM_MODEL=your-model-id
codeerratum review --diff HEAD~1 -o review.md
```

Two heterogeneous models (dual — the core of phase 3). At least two different vendors, plus an optional third to act as falsifier:

```bash
export CODEERRATUM_MODELS='[
  {"model_id":"gpt-4o-2024-08-06","base_url":"https://api.openai.com/v1","api_key":"sk-...","family":"openai"},
  {"model_id":"claude-3-5-sonnet","base_url":"https://api.anthropic.com/v1","api_key":"sk-...","family":"anthropic"},
  {"model_id":"deepseek-chat","base_url":"https://api.deepseek.com/v1","api_key":"sk-...","family":"deepseek"}
]'
export CODEERRATUM_MODE=dual
codeerratum review --diff HEAD~1 -o review.md
```

`family` is the heterogeneity test: two model IDs from the same vendor do not count as heterogeneous and the router will reject them. The falsification round prefers the third vendor; without one, the two reviewers falsify each other's findings. If a model wrote the code, pass `--author-model <model_id>` to keep it out of the reviewer seats (design invariant #1).

Hand the resulting `review.md` to Claude Code / Cursor / Cline. Every run leaves traceable artifacts in `.codeerratum/runs/<run_id>/` (`input.diff`, `units.json`, `calls.json`, `findings.json`, `static_diags.json`); the feedback database sits in the same directory and never pollutes your working tree.

In dual mode the report splits into three sections: **unique findings** (reported by only one model — the most valuable part) / **conflicts** (models disagree, awaiting a human call) / **consensus**.

Feedback after a review: `codeerratum accept <finding_id>`, `codeerratum reject <finding_id> --reason "..."`, `codeerratum stats` for the acceptance rate, `codeerratum compare` for per-model unique-finding counts and measured profiles. All three resolve to the same database from any subdirectory of the repo.

## Design invariants

1. **The reviewer is never the author** — a model that generated the code may not sit on the review panel
2. **Every finding carries a location** — if file, line and quoted snippet don't line up, the finding is dropped
3. **Output is a work order, not a comment** — expected behavior, concrete fix, acceptance criterion; all three or it doesn't ship
4. **AI review passing ≠ mergeable** — the report always keeps a human-confirmation slot
5. **Truth is not decided by majority vote** — LLM errors are highly correlated; a cross-vendor *falsification* round replaces voting, and unique findings get their own section

## Roadmap (in priority order)

| Phase | Theme | Status |
|---|---|---|
| 1 | Executable work order: diff → a `review.md` an AI can follow | ✅ Done |
| 2 | Trust: eliminate hallucinated locations and vacuous claims | ✅ Done |
| 3 | Heterogeneous cross-check: two models + falsification, surfacing unique findings | ✅ Done |
| 4 | Automation: GitHub Action, PR comments, rule library | Planned |
| 5 | Doc generation: API docs / user manuals (separate milestone, cuttable) | Planned |
| 6 | Extensions: sandbox reproduction, more languages, MCP (cuttable) | Planned |

Full scope and acceptance criteria: [开发计划.md](开发计划.md) (Chinese).

## About the docs

Internal product design, competitive and cost analysis, and architecture details are **not published with this repo**; they stay in a private local directory. This repo ships only the README, the development plan, examples and user docs.

## License

[MIT](LICENSE) © 2026 a2012s. Commercial use, modification and closed-source redistribution are all permitted; keep the copyright and license notice. The license does not cover your model provider's terms of service, and it warrants nothing about the correctness of the review output.
