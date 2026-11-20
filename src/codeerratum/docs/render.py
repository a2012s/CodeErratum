"""Render module docs from the AST map.

The output is deliberately plain: signature, location, docstring. It is a map of what
exists, not prose about why it exists — prose is the part that drifts.
"""

from __future__ import annotations

from pathlib import Path

from .extract import ModuleDoc, SymbolDoc

KIND_TITLES = {"class": "类", "function": "函数", "method": "方法"}
KIND_ORDER = ("class", "function", "method")


def _render_symbol(symbol: SymbolDoc) -> list[str]:
    out = [
        f"### `{symbol.qualname}`",
        "",
        "```python",
        symbol.signature,
        "```",
        "",
        f"`L{symbol.line}` · 签名 `{symbol.sig_hash}`",
        "",
        symbol.docstring or "_（无 docstring）_",
        "",
    ]
    return out


def render_module(module: ModuleDoc, *, commit: str = "") -> str:
    lines = [f"# `{module.module}`", ""]
    meta = f"> 源文件 `{module.path}`"
    if commit:
        meta += f" ｜ 基线 commit `{commit[:8]}`"
    lines += [meta, ""]
    if module.docstring:
        lines += [module.docstring, ""]

    public = [s for s in module.flatten() if s.is_public]
    if not public:
        lines += ["_本模块没有公开符号。_", ""]
        return "\n".join(lines)

    for kind in KIND_ORDER:
        group = [s for s in public if s.kind == kind]
        if not group:
            continue
        lines += [f"## {KIND_TITLES[kind]}", ""]
        for symbol in group:
            lines += _render_symbol(symbol)
    return "\n".join(lines)


def module_output_path(out_dir: Path, module: str) -> Path:
    return (out_dir / Path(*module.split("."))).with_suffix(".md")


def render_overview(modules: list[ModuleDoc], *, commit: str = "") -> str:
    lines = [
        "# 模块索引",
        "",
        f"> 共 {len(modules)} 个模块" + (f" ｜ 基线 commit `{commit[:8]}`" if commit else ""),
        "",
        "由 `codeerratum docs` 从 AST 生成。改动代码后请重跑，`--check` 可检测签名漂移。",
        "",
        "| 模块 | 源文件 | 公开符号 |",
        "|---|---|---|",
    ]
    for m in modules:
        public = sum(1 for s in m.flatten() if s.is_public)
        link = module_output_path(Path("."), m.module).as_posix()
        lines.append(f"| [`{m.module}`]({link}) | `{m.path}` | {public} |")
    lines.append("")
    return "\n".join(lines)


def write_all(modules: list[ModuleDoc], out_dir: Path, *, commit: str = "") -> list[Path]:
    written: list[Path] = []
    for module in modules:
        path = module_output_path(out_dir, module.module)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(render_module(module, commit=commit), encoding="utf-8")
        written.append(path)
    overview = out_dir / "README.md"
    overview.parent.mkdir(parents=True, exist_ok=True)
    overview.write_text(render_overview(modules, commit=commit), encoding="utf-8")
    written.append(overview)
    return written
