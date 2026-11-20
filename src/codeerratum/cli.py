"""CLI entry point — the only public interface in v0.1."""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import UTC, datetime
from pathlib import Path

import typer
from rich.console import Console

from . import __version__
from .config import Config
from .ingest import DiffError, get_commit, get_diff, split_diff
from .models import ReviewUnit
from .report import render_markdown
from .review import run_review
from .store import ReviewStore

STATE_DIR = ".codeerratum"


def _find_repo_root(path: Path) -> Path:
    """Nearest ancestor containing .git, or the given path when there is none."""
    current = path.resolve()
    for candidate in (current, *current.parents):
        if (candidate / ".git").exists():
            return candidate
    return current


def _store(repo_root: Path) -> ReviewStore:
    return ReviewStore(repo_root / STATE_DIR / "feedback.db")


def _prioritize(units: list[ReviewUnit]) -> list[ReviewUnit]:
    """Spend the unit budget on risky, public, larger changes first."""
    return sorted(
        units,
        key=lambda u: (
            0 if u.risk_tags else 1,
            0 if u.is_public else 1,
            -u.changed_lines,
        ),
    )


app = typer.Typer(name="codeerratum", help="给 AI 写的代码开一张勘误表", no_args_is_help=True)
console = Console()


def _version_callback(value: bool) -> None:
    if value:
        console.print(f"codeerratum {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: bool = typer.Option(
        False, "--version", callback=_version_callback, is_eager=True, help="Show version"
    ),
) -> None:
    pass


@app.command()
def review(
    diff: str = typer.Option("HEAD~1", "--diff", help="Git diff spec, e.g. HEAD~1 or HEAD~3..HEAD"),
    out: Path = typer.Option(Path("review.md"), "--out", "-o", help="Output markdown path"),
    repo: Path = typer.Option(Path("."), "--repo", help="Repository root"),
    max_units: int = typer.Option(10, "--max-units", help="Max review units per run (cost guard)"),
    mode: str = typer.Option(
        "", "--mode", help="quick|dual|committee (overrides CODEERRATUM_MODE)"
    ),
    author_model: str = typer.Option(
        "",
        "--author-model",
        help="Model that wrote this code; it will never be seated as a reviewer",
    ),
) -> None:
    """Review a git diff and write an executable review.md work order."""
    repo_root = repo.resolve()
    if not (repo_root / ".git").exists():
        console.print(f"[red]Error: {repo_root} is not a git repository[/red]")
        raise typer.Exit(1)

    try:
        config = Config.from_env()
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc

    try:
        diff_text = get_diff(diff, repo_root)
    except DiffError as exc:
        console.print(f"[red]git diff failed: {exc}[/red]")
        console.print(
            "[yellow]提示：首个 commit 没有父提交时请用 `--diff HEAD`（暂存区+工作区）"
            "或 `--diff $(git hash-object -t tree /dev/null)`。[/yellow]"
        )
        raise typer.Exit(1) from exc

    if not diff_text.strip():
        console.print("[yellow]No changes in diff. Nothing to review.[/yellow]")
        raise typer.Exit(0)

    run_id = uuid.uuid4().hex[:12]
    run_dir = repo_root / STATE_DIR / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "input.diff").write_text(diff_text, encoding="utf-8")

    notices: list[str] = []
    units = split_diff(diff_text, repo_root, notices=notices)
    (run_dir / "units.json").write_text(
        json.dumps([u.model_dump() for u in units], ensure_ascii=False, indent=2), encoding="utf-8"
    )
    console.print(f"Split diff into {len(units)} review unit(s)")
    for note in notices:
        console.print(f"  [yellow]{note}[/yellow]")
    if not units:
        console.print("[yellow]No reviewable units found.[/yellow]")
        raise typer.Exit(0)

    selected = _prioritize(units)[:max_units]
    if len(units) > max_units:
        notices.append(f"仅审查 {max_units}/{len(units)} 个单元（--max-units 可调整）")
        console.print(f"[yellow]Capped at {max_units} units (use --max-units to raise)[/yellow]")

    run_mode = mode or config.mode
    if run_mode not in ("quick", "dual", "committee"):
        console.print(f"[red]Invalid mode {run_mode!r}; choose quick/dual/committee[/red]")
        raise typer.Exit(1)
    console.print(f"Mode: {run_mode} | models: {len(config.registry.models)}")

    try:
        collection, calls, all_diags = asyncio.run(
            run_review(
                config.registry,
                run_mode,  # type: ignore[arg-type]
                selected,
                repo_root,
                run_id,
                author_model=author_model or None,
            )
        )
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc

    failed = [r for r in collection.rejected if r.get("reason") == "provider_error"]
    if failed:
        notices.append(f"{len(failed)} 个单元调用模型失败，相关文件未被审查")
    collection.notes = notices
    collection.commit = get_commit(repo_root)

    (run_dir / "calls.json").write_text(
        json.dumps(calls, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (run_dir / "findings.json").write_text(
        json.dumps(collection.model_dump(mode="json"), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (run_dir / "static_diags.json").write_text(
        json.dumps(all_diags, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    ts = datetime.now(UTC).isoformat(timespec="seconds")
    markdown = render_markdown(collection, ts)
    out_path = out if out.is_absolute() else repo_root / out
    out_path.write_text(markdown, encoding="utf-8")

    # Persist run + findings + phase-3 comparison/profile into the feedback store.
    store = _store(repo_root)
    store.save_run(collection, ts)
    store.save_comparison(collection, ts)

    console.print(f"\n[green]Wrote {out_path}[/green]")
    console.print(f"Findings: {len(collection.findings)} | Rejected: {len(collection.rejected)}")
    console.print(f"Static diagnostics: {len(all_diags)} | Model calls: {len(calls)}")
    if calls:
        units_called = len(calls)
        latency = sum(c.get("latency_ms", 0) for c in calls) / units_called
        console.print(f"Avg latency: {latency:.0f} ms/call")
    if collection.comparison:
        console.print(f"Comparison: {collection.comparison}")
    console.print(f"Artifacts: {run_dir}")


@app.command()
def accept(finding_prefix: str = typer.Argument(..., help="Finding id prefix to accept")) -> None:
    """Mark a finding as accepted (true positive)."""
    store = _store(_find_repo_root(Path(".")))
    found = store.find_finding(finding_prefix)
    if not found:
        console.print(f"[red]No finding starts with {finding_prefix}[/red]")
        raise typer.Exit(1)
    f = found[0]
    store.set_status(f["id"], "accepted")
    console.print(f"[green]Accepted {f['id'][:12]}: {f['claim']}[/green]")


@app.command()
def reject(
    finding_prefix: str = typer.Argument(..., help="Finding id prefix to reject"),
    reason: str = typer.Option("", "--reason", "-r", help="Why it is a false positive"),
) -> None:
    """Mark a finding as rejected (false positive) with a reason."""
    store = _store(_find_repo_root(Path(".")))
    found = store.find_finding(finding_prefix)
    if not found:
        console.print(f"[red]No finding starts with {finding_prefix}[/red]")
        raise typer.Exit(1)
    f = found[0]
    store.set_status(f["id"], "rejected", reason)
    console.print(f"[yellow]Rejected {f['id'][:12]}: {f['claim']}[/yellow]")


@app.command()
def compare() -> None:
    """Show cross-model comparison and per-model profile (phase-3 resume evidence)."""
    store = _store(_find_repo_root(Path(".")))
    history = store.comparison_history()
    profiles = store.model_profiles()

    console.print("[bold]交叉审查对照记录[/bold]\n")
    if not history:
        console.print("[yellow]暂无对照数据。用 --mode dual 跑一次 review。[/yellow]")
    else:
        for row in history[:10]:
            raw = json.loads(row["raw_counts"] or "{}")
            zones = json.loads(row["zones"] or "{}")
            console.print(
                f"- {row['created_at'][:19]} [{row['mode']}] 原始={raw} "
                f"聚类后={row['after_clustering']} 证伪后={row['after_falsification']} "
                f"分区={zones}"
            )

    console.print("\n[bold]模型实测画像[/bold]")
    if not profiles:
        console.print("[yellow]暂无画像数据。[/yellow]")
    else:
        console.print(f"{'model_id':<32}{'assert':>7}{'unique':>7}{'refuted':>8}{'confirmed':>10}")
        for p in profiles:
            console.print(
                f"{p['model_id']:<32}{p['n_assert']:>7}{p['n_unique']:>7}"
                f"{p['n_refuted']:>8}{p['n_confirmed']:>10}"
            )
        console.print(
            "\n[dim]unique = 仅该模型报出且通过证伪的发现数（差异化价值的直接度量）[/dim]"
        )


@app.command()
def stats() -> None:
    """Show acceptance-rate stats — the resume-ready evidence produced by daily use."""
    s = _store(_find_repo_root(Path("."))).stats()
    console.print("[bold]CodeErratum 使用统计[/bold]\n")
    console.print(f"审查运行数：{s['runs']}")
    console.print(f"发现总数：{s['findings_total']}")
    console.print(f"已采纳：{s['accepted']} | 已驳回：{s['rejected']}")
    rate = s["acceptance_rate"]
    console.print(f"采纳率：{f'{rate:.1%}' if rate is not None else '尚无人工裁决'}")
    if s["by_category"]:
        console.print("\n[bold]按分类（total / rejected）[/bold]")
        for row in s["by_category"][:10]:
            console.print(f"  {row['category']:<24} {row['total']:>3} / {row['rejected']:>3}")
    if s["by_model"]:
        console.print("\n[bold]按模型[/bold]")
        for row in s["by_model"]:
            console.print(f"  {row['model_id']:<32} {row['n']:>3} runs")
