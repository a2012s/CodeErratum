#!/usr/bin/env bash
# 种 bug 对照实验：把两个故意埋下的 bug（一个幻觉 API、一个 tz 比较崩溃）
# 放进一次性 git 仓库，对 HEAD~1..HEAD 的 diff 跑一次真实 dual 审查。
#
# 前提：~/.codeerratum/config.json 里配好至少两个不同厂商的模型。
# 产物：临时仓库的 erratumReview.md 与 .codeerratum/runs/<run_id>/
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
WORK="$(mktemp -d /tmp/codeerratum-seeded-XXXX)"

git -C "$WORK" init -q
cp "$HERE/base_refunds.py" "$WORK/refunds.py"
git -C "$WORK" add -A
git -C "$WORK" -c user.email=demo@codeerratum.dev -c user.name=demo commit -qm "baseline: refund helpers"

cp "$HERE/buggy_refunds.py" "$WORK/refunds.py"
git -C "$WORK" add -A
git -C "$WORK" -c user.email=demo@codeerratum.dev -c user.name=demo commit -qm "add atomic audit write + UTC window check"

cd "$WORK"
echo "seeded repo: $WORK"
# CODEERRATUM_BIN 可指到本地 venv，如 ../.venv/bin/codeerratum
"${CODEERRATUM_BIN:-codeerratum}" review --diff HEAD~1 --mode dual --out erratumReview.md
echo "--- artifacts ---"
echo "$WORK/erratumReview.md"
ls "$WORK"/.codeerratum/runs/
