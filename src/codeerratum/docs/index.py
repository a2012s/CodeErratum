"""Signature baseline and drift detection.

Drift means "the API shape changed", not "the file moved". Line numbers are recorded
for navigation but never compared: treating a moved function as drift would make the
check cry wolf after every refactor, and a check nobody trusts is worse than none.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .extract import ModuleDoc

INDEX_VERSION = 1


@dataclass
class Drift:
    kind: str
    path: str
    symbol: str
    detail: str
    module: str = ""

    @property
    def is_breaking(self) -> bool:
        return self.kind in ("module_removed", "symbol_removed", "signature_changed")


def build_index(modules: list[ModuleDoc], *, commit: str = "", generated_at: str = "") -> dict:
    return {
        "version": INDEX_VERSION,
        "commit": commit,
        "generated_at": generated_at,
        "modules": {
            m.path: {
                "module": m.module,
                "symbols": {
                    s.qualname: {
                        "kind": s.kind,
                        "signature": s.signature,
                        "sig_hash": s.sig_hash,
                        "line": s.line,
                        "public": s.is_public,
                    }
                    for s in m.flatten()
                },
            }
            for m in modules
        },
    }


def load_index(path: Path) -> dict | None:
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    if data.get("version") != INDEX_VERSION:
        return None
    return data


def save_index(path: Path, index: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")


def _symbol_entry(entry: dict, name: str) -> dict:
    return entry.get("symbols", {}).get(name, {})


def compare(old: dict, new: dict) -> list[Drift]:
    """Public-surface differences between the saved baseline and the current tree."""
    drifts: list[Drift] = []
    old_mods = old.get("modules", {})
    new_mods = new.get("modules", {})

    for path in sorted(set(old_mods) | set(new_mods)):
        old_entry = old_mods.get(path)
        new_entry = new_mods.get(path)
        if old_entry is None:
            count = len(new_mods[path].get("symbols", {}))
            drifts.append(
                Drift(
                    "module_added",
                    path,
                    "",
                    f"{count} 个符号",
                    new_mods[path].get("module", ""),
                )
            )
            continue
        if new_entry is None:
            drifts.append(
                Drift("module_removed", path, "", "源文件已不存在", old_entry.get("module", ""))
            )
            continue

        module_name = new_entry.get("module") or old_entry.get("module", "")
        old_symbols = old_entry.get("symbols", {})
        new_symbols = new_entry.get("symbols", {})
        for name in sorted(set(old_symbols) | set(new_symbols)):
            before = _symbol_entry(old_entry, name)
            after = _symbol_entry(new_entry, name)
            public = before.get("public") or after.get("public")
            if not public:
                # Private helpers churn constantly; reporting them would bury the signal.
                continue
            if not before:
                drifts.append(
                    Drift("symbol_added", path, name, after.get("signature", ""), module_name)
                )
            elif not after:
                drifts.append(
                    Drift("symbol_removed", path, name, "文档里的符号已删除", module_name)
                )
            elif before.get("sig_hash") != after.get("sig_hash"):
                drifts.append(
                    Drift(
                        "signature_changed",
                        path,
                        name,
                        f"{before.get('signature', '')} → {after.get('signature', '')}",
                        module_name,
                    )
                )
    return drifts


def summarize(drifts: list[Drift]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for d in drifts:
        counts[d.kind] = counts.get(d.kind, 0) + 1
    return counts
