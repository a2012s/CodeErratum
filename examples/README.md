# 示例

`sample-review.md` 是一份真实的 `review.md` 产物。它由仓库自带的假模型端点
（`scripts/mock_server.py`）生成，所以——**内容是假的，格式是真的**：字段结构、
行号引用、验收命令、《执行结果》表都是实际渲染结果，改代码改出来的。

接真实模型后差别只在内容质量：`detail` 更长、`suggestion.diff` 更具体、验收命令更贴合。

## 自己复现

```bash
# 终端 A
uv run python scripts/mock_server.py

# 终端 B：任意有 ≥2 次提交的仓库里
CODEERRATUM_BASE_URL=http://127.0.0.1:8899/v1 \
CODEERRATUM_API_KEY=x \
CODEERRATUM_MODEL=mock \
codeerratum review --diff HEAD~1 -o review.md
```

不花一次 token，能验证从 diff → 单元切分 → 模型调用 → 锚点校验 → 工单渲染的整条链路。
