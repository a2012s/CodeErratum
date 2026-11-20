"""CLI entry point — the primary interface; `ui` opens the optional web UI."""

from __future__ import annotations

import json
import threading
import webbrowser
from pathlib import Path

import typer
from rich.console import Console

from . import __version__
from .pipeline import (
    ConfigError,
    DiffFailed,
    NoChanges,
    NotARepository,
    execute_review,
    find_repo_root,
    store_for,
)
from .review import MAX_HOUSE_RULES_CHARS
from .store import ReviewStore


def _store(repo_root: Path) -> ReviewStore:
    return store_for(repo_root)


app = typer.Typer(name="codeerratum", help="给 AI 写的代码开一张勘误表", no_args_is_help=True)
console = Console()


def _read_rules_file(path: Path | None) -> str:
    """Read --rules. A typo in the path must fail loudly rather than review without it."""
    if path is None:
        return ""
    if not path.is_file():
        console.print(f"[red]Error: rules file not found: {path}[/red]")
        raise typer.Exit(1)
    return path.read_text(encoding="utf-8", errors="replace")


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
    rules: Path | None = typer.Option(
        None,
        "--rules",
        help="Markdown file with project-specific rules, appended to the system prompt. "
        f"Defaults to .codeerratum/rules.md when it exists (max {MAX_HOUSE_RULES_CHARS} chars)",
    ),
) -> None:
    """Review a git diff and write an executable review.md work order."""
    repo_root = repo.resolve()
    house_rules = _read_rules_file(rules)
    try:
        outcome = execute_review(
            repo_root,
            diff_spec=diff,
            mode=mode,
            max_units=max_units,
            author_model=author_model or None,
            out=out,
            progress=lambda msg: console.print(msg),
            house_rules=house_rules,
        )
    except NotARepository as exc:
        console.print(f"[red]Error: {exc}[/red]")
        raise typer.Exit(1) from exc
    except ConfigError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc
    except DiffFailed as exc:
        console.print(f"[red]git diff failed: {exc}[/red]")
        console.print(
            "[yellow]提示：首个 commit 没有父提交时请用 `--diff HEAD`（暂存区+工作区）"
            "或 `--diff $(git hash-object -t tree /dev/null)`。[/yellow]"
        )
        raise typer.Exit(1) from exc
    except NoChanges as exc:
        console.print(f"[yellow]{exc}[/yellow]")
        raise typer.Exit(0) from exc

    summary = outcome.summary()
    console.print(f"\n[green]Wrote {outcome.out_path or outcome.run_dir / 'review.md'}[/green]")
    console.print(f"Findings: {summary['findings']} | Rejected: {summary['rejected']}")
    console.print(
        f"Static diagnostics: {summary['static_diags']} | Model calls: {summary['calls']}"
    )
    if outcome.calls:
        console.print(f"Avg latency: {summary['latency_ms_avg']:.0f} ms/call")
    if outcome.collection.comparison:
        console.print(f"Comparison: {outcome.collection.comparison}")
    console.print(f"Artifacts: {outcome.run_dir}")


@app.command()
def ui(
    repo: Path = typer.Option(Path("."), "--repo", help="Repository root to review"),
    port: int = typer.Option(8765, "--port", help="Loopback port for the local UI"),
    host: str = typer.Option("127.0.0.1", "--host", help="Bind address (keep it on loopback)"),
    no_browser: bool = typer.Option(False, "--no-browser", help="Do not open a browser tab"),
) -> None:
    """Open the local web UI (run history, three-zone report, accept/reject)."""
    from .web import WebApp, make_server

    repo_root = find_repo_root(repo)
    if not (repo_root / ".git").exists():
        console.print(f"[red]Error: {repo_root} is not a git repository[/red]")
        raise typer.Exit(1)

    app_ui = WebApp(repo_root)
    try:
        httpd = make_server(app_ui, host=host, port=port)
    except OSError as exc:
        console.print(f"[red]Cannot bind {host}:{port} — {exc}[/red]")
        raise typer.Exit(1) from exc

    url = f"http://{host}:{httpd.server_address[1]}/"
    console.print(f"[green]CodeErratum UI[/green] on {url}")
    console.print(f"Repo: {repo_root}")
    if not app_ui.meta()["configured"]:
        console.print(
            "[yellow]尚未配置模型（CODEERRATUM_BASE_URL / API_KEY / MODEL），"
            "界面可以看历史记录，但不能发起新审查。[/yellow]"
        )
    console.print("[dim]Ctrl+C 停止[/dim]")
    if not no_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        console.print("\nStopped.")
    finally:
        httpd.server_close()


@app.command()
def accept(finding_prefix: str = typer.Argument(..., help="Finding id prefix to accept")) -> None:
    """Mark a finding as accepted (true positive)."""
    store = _store(find_repo_root(Path(".")))
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
    store = _store(find_repo_root(Path(".")))
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
    store = _store(find_repo_root(Path(".")))
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
    s = _store(find_repo_root(Path("."))).stats()
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
