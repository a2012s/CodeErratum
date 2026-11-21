"""CLI entry point — the primary interface; `ui` opens the optional web UI."""

from __future__ import annotations

import json
import os
import threading
import webbrowser
from datetime import UTC, datetime
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
def pr(
    post: bool = typer.Option(
        False, "--post", help="Actually post to GitHub; without it this is a dry run"
    ),
    event: Path | None = typer.Option(
        None, "--event", help="GitHub event JSON (default: $GITHUB_EVENT_PATH)"
    ),
    diff: str = typer.Option("", "--diff", help="Override the range (default: base...head)"),
    repo: Path = typer.Option(Path("."), "--repo", help="Repository root"),
    token: str = typer.Option("", "--token", help="GitHub token (default: $GITHUB_TOKEN)"),
    max_inline: int = typer.Option(
        5, "--max-inline", help="Inline comments cap (GitHub noise limit)"
    ),
    max_units: int = typer.Option(10, "--max-units", help="Max review units (cost guard)"),
    mode: str = typer.Option("", "--mode", help="quick|dual|committee"),
    rules: Path | None = typer.Option(None, "--rules", help="Project rules file"),
) -> None:
    """Review a pull request and post the result as PR comments."""
    from .ci import (
        CIError,
        inline_body,
        load_event,
        parse_pull_request,
        post_review,
        split_by_anchor,
        summary_body,
    )
    from .ci.github import marker_for

    try:
        pull = parse_pull_request(load_event(event))
    except CIError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc
    console.print(f"PR {pull.slug}#{pull.number} · {pull.base_sha[:8]}...{pull.head_sha[:8]}")

    try:
        outcome = execute_review(
            repo.resolve(),
            diff_spec=diff or pull.diff_spec,
            mode=mode,
            max_units=max_units,
            out=None,
            progress=lambda msg: console.print(msg),
            house_rules=_read_rules_file(rules),
        )
    except (NotARepository, ConfigError, DiffFailed) as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc
    except NoChanges as exc:
        console.print(f"[yellow]{exc}[/yellow]")
        raise typer.Exit(0) from exc

    collection = outcome.collection
    inline, rest = split_by_anchor(collection.ranked_findings(), outcome.units, max(0, max_inline))
    marker = marker_for(pull.head_sha)
    body = summary_body(collection, inline, rest, marker)

    # Keep the exact payload with the run: a CI failure is easier to debug from the
    # artifact than from the Actions log.
    (outcome.run_dir / "pr-comments.json").write_text(
        json.dumps(
            {
                "pull_request": {
                    "slug": pull.slug,
                    "number": pull.number,
                    "base_sha": pull.base_sha,
                    "head_sha": pull.head_sha,
                },
                "inline": [
                    {
                        "path": f.anchors[0].file,
                        "line": f.anchors[0].end_line or f.anchors[0].start_line,
                        "body": inline_body(f),
                    }
                    for f in inline
                ],
                "summary": body,
                "posted": False,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    if not post:
        console.print(
            f"\n[cyan]Dry run[/cyan] — {len(inline)} 条行内评论，summary {len(body)} 字符。"
            "\n加上 [bold]--post[/bold] 才会真正发到 GitHub。"
            f"\n待发内容已写入 {outcome.run_dir / 'pr-comments.json'}\n"
        )
        for f in inline:
            anchor = f.anchors[0]
            typer.echo(f"\n--- {anchor.file}:{anchor.start_line} ({f.severity}) ---")
            typer.echo(inline_body(f))
        console.print("\n[bold]=== Summary ===[/bold]")
        typer.echo(body)
        return

    gh_token = token or os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or ""
    if not gh_token:
        console.print("[red]--post needs a token: set GITHUB_TOKEN or pass --token[/red]")
        raise typer.Exit(1)
    try:
        result = post_review(pull, gh_token, collection, inline, rest)
    except CIError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc

    if result.get("skipped"):
        console.print(f"[yellow]已发过（{result['skipped']}），未重复评论。[/yellow]")
    else:
        console.print(
            f"[green]Posted[/green] {result['inline_posted']} inline + 1 summary"
            + (" [yellow](降级为逐条发送)[/yellow]" if result.get("degraded") else "")
        )
    if result.get("summary_url"):
        console.print(result["summary_url"])


@app.command()
def docs(
    repo: Path = typer.Option(Path("."), "--repo", help="Repository root"),
    src: Path | None = typer.Option(
        None, "--src", help="Subdirectory to scan (default: whole repo)"
    ),
    out: Path = typer.Option(Path("docs/api"), "--out", help="Markdown output directory"),
    check: bool = typer.Option(False, "--check", help="Only report signature drift, write nothing"),
    strict: bool = typer.Option(False, "--strict", help="With --check: exit 1 on breaking drift"),
) -> None:
    """Generate module docs from the AST, or check them for signature drift."""
    from .docs import build_index, collect, compare, load_index, save_index, write_all
    from .ingest import get_commit

    root = find_repo_root(repo)
    # Default to src/ when it exists: documenting the test suite and mock servers
    # buries the public surface the docs are for.
    scan = src or ((root / "src") if (root / "src").is_dir() else None)
    try:
        modules = collect(root, scan)
    except NotADirectoryError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc
    if not modules:
        console.print("[yellow]No Python modules found.[/yellow]")
        raise typer.Exit(0)

    commit = get_commit(root)
    index_path = root / ".codeerratum" / "docs-index.json"
    baseline = load_index(index_path)
    current = build_index(
        modules, commit=commit, generated_at=datetime.now(UTC).isoformat(timespec="seconds")
    )
    drifts = compare(baseline, current) if baseline else []

    if check:
        if baseline is None:
            console.print(
                "[yellow]尚无基线：先跑一次 `codeerratum docs` 生成，"
                "之后 --check 才有意义。[/yellow]"
            )
            raise typer.Exit(1)
        _print_drift(drifts)
        if strict and any(d.is_breaking for d in drifts):
            console.print("[red]--strict：存在破坏性签名变更[/red]")
            raise typer.Exit(1)
        return

    out_dir = out if out.is_absolute() else root / out
    written = write_all(modules, out_dir, commit=commit)
    save_index(index_path, current)
    console.print(f"[green]Wrote {len(written)} file(s) to {out_dir}[/green]")
    if drifts:
        console.print(f"\n[yellow]相对上次基线有 {len(drifts)} 处变化[/yellow]")
        _print_drift(drifts)


def _compact(detail: str, limit: int = 52) -> str:
    """Keep the new signature in the table; the old one is still in the existing docs."""
    if " → " in detail:
        new = detail.split(" → ", 1)[1]
        return f"→ {new[:limit]}…" if len(new) > limit else f"→ {new}"
    return f"{detail[:limit]}…" if len(detail) > limit else detail


def _print_drift(drifts) -> None:
    from rich.table import Table

    if not drifts:
        console.print("[green]无签名漂移[/green]")
        return
    labels = {
        "module_added": "模块新增",
        "module_removed": "模块删除",
        "symbol_added": "符号新增",
        "symbol_removed": "符号删除",
        "signature_changed": "签名变更",
    }
    table = Table(show_header=True, header_style="bold", box=None, pad_edge=False)
    table.add_column("类型", no_wrap=True)
    table.add_column("文件", no_wrap=True, max_width=36, overflow="ellipsis")
    table.add_column("符号", no_wrap=True, max_width=26, overflow="ellipsis")
    table.add_column("说明", no_wrap=True, max_width=54, overflow="ellipsis")
    for d in drifts[:40]:
        table.add_row(labels.get(d.kind, d.kind), d.path, d.symbol or "—", _compact(d.detail))
    console.print(table)
    if len(drifts) > 40:
        console.print(f"[yellow]另有 {len(drifts) - 40} 条未显示[/yellow]")


@app.command()
def mcp(
    repo: Path = typer.Option(Path("."), "--repo", help="Default repository for tool calls"),
) -> None:
    """Run the MCP server on stdio (for Claude Code, Cursor, Cline...)."""
    from .mcp import serve

    # Point the default repo at the caller's choice for one process; tools still
    # accept an explicit `repo` argument and override this.
    os.environ.setdefault("CODEERRATUM_MCP_REPO", str(find_repo_root(repo)))
    raise typer.Exit(serve())


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
