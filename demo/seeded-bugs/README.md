# 种 bug 对照实验（seeded-bug experiment）

目的：一次运行同时展示 CodeErratum 的两个卖点——**证伪轮**与**审查质量可度量**。

## 流程

1. `run_experiment.sh` 在临时目录建一个 git 仓库，提交一个干净的 `refunds.py`，
   再提交埋了两个 bug 的版本：
   - **幻觉 API**：`os.replace_atomic` —— 听起来真、读起来真，`os` 里根本没有这个函数；
   - **覆盖丢数据**：审计日志用 `"w"` 模式写临时文件再 rename，历史条目被静默清空
     （即使修好幻觉 API，这个 bug 依然在）。
2. 对 `HEAD~1..HEAD` 跑真实 dual 审查（两家用不同厂商模型互审 + 第三家证伪）。
3. 人工裁决落库，`stats` 出采纳率仪表盘。

## 2026-11-23 实跑结果（3 个单元，qwen3.8-flash + qwen4-max-preview + deepseek-v4.1）

| 环节 | 真实数字 |
|---|---|
| 审查者原始断言 | 11 条 |
| 证伪轮（deepseek） | 9 次调用：2 确认 / **6 反例推翻** / 1 格式重试 |
| 机器丢弃 | 5 条（reason=refuted，与人工驳回分离） |
| 最终工单 | 4 条，分三区：独有 / 冲突 / 共识，每条带精确锚点 + 验收命令 |
| 两个埋的 bug | **全部抓到**（幻觉 API 走共识区+独有区双报） |
| 人工裁决 | 2 采纳 / 2 驳回（重复上报、修复建议引入竞态）→ 采纳率 50% |

完整真实产物见 `expected-review.md`（审查工单）与 `expected-stats.txt`（仪表盘）。
动图见 [`docs/seeded-bug-demo.gif`](../../docs/seeded-bug-demo.gif)。

## 复现

```bash
# ~/.codeerratum/config.json 配好至少两个不同厂商的模型后：
CODEERRATUM_BIN=/path/to/codeerratum bash demo/seeded-bugs/run_experiment.sh
```
