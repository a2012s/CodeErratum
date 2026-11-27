"""CLI entry point — the primary interface; `ui` opens the optional web UI."""

from __future__ import annotations

import json
import os
import signal
import sqlite3
import sys
import threading
import webbrowser
from datetime import UTC, datetime
from pathlib import Path

import typer
from rich.console import Console

from . import __version__
from . import eval as _eval
from .i18n import t
from .pipeline import (
    HOUSE_RULES_FILE,
    ConfigError,
    DiffFailed,
    NoChanges,
    NotARepository,
    execute_review,
    find_repo_root,
    store_for,
)
from .report.outcomes import apply_outcomes, parse_outcomes
from .review import MAX_HOUSE_RULES_CHARS
from .store import ReviewStore
from .suggest import append_rules, build_suggestions


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
    out: Path = typer.Option(Path("erratumReview.md"), "--out", "-o", help="Output markdown path"),
    repo: Path = typer.Option(Path("."), "--repo", help="Repository root"),
    max_units: int | None = typer.Option(
        None,
        "--max-units",
        help="Units to review. Default: auto (all if small, else 60). 0 = every unit",
    ),
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
    path: str = typer.Option(
        "", "--path", help="Limit the diff to a git pathspec, e.g. src/api (no need to git add)"
    ),
    include_non_code: bool = typer.Option(
        False,
        "--include-non-code",
        help="Also review docs/config/i18n units (skipped by default)",
    ),
    no_tiered: bool = typer.Option(
        False,
        "--no-tiered",
        help="Disable quick-mode tiered review (fast scan + strong-model verification)",
    ),
    token_budget: int = typer.Option(
        0,
        "--token-budget",
        help="Fuse for total tokens this sitting may spend; tripping it keeps a partial, "
        "resumable report. 0 falls back to $CODEERRATUM_TOKEN_BUDGET when set",
    ),
) -> None:
    """Review a git diff and write an executable erratumReview.md work order."""
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
            paths=path,
            include_non_code=include_non_code,
            tiered=not no_tiered,
            token_budget=token_budget or None,
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
    # "Rejected" used to read like a human verdict; these are machine drops,
    # and unit call failures never dropped a finding at all.
    console.print(
        f"Findings: {summary['findings']} | Machine-dropped: {summary['dropped']}"
        + (f" | Failed units: {summary['failed_units']}" if summary["failed_units"] else "")
    )
    console.print(
        f"Static diagnostics: {summary['static_diags']} | Model calls: {summary['calls']}"
    )
    if outcome.calls:
        console.print(f"Avg latency: {summary['latency_ms_avg']:.0f} ms/call")
    if outcome.collection.comparison:
        console.print(f"Comparison: {outcome.collection.comparison}")
    console.print(f"Artifacts: {outcome.run_dir}")


@app.command()
def resume(
    run_id: str = typer.Argument(..., help="Run id to continue (see .codeerratum/runs/)"),
    repo: Path = typer.Option(Path("."), "--repo", help="Repository root"),
    more: int | None = typer.Option(
        None,
        "--more",
        help="Review at most N more units this sitting (default: everything left)",
    ),
    path: str = typer.Option("", "--path", help="Narrow the batch to a file prefix, e.g. src/api"),
    out: Path | None = typer.Option(
        None, "--out", "-o", help="Also write the merged work order here"
    ),
    token_budget: int = typer.Option(
        0,
        "--token-budget",
        help="Fuse for tokens this resume batch may spend (0 = $CODEERRATUM_TOKEN_BUDGET)",
    ),
) -> None:
    """Continue an interrupted or budget-truncated run without re-paying.

    Finished units are skipped (their calls stay on record), the remaining
    queue runs with the original seats, and the merged report overwrites the
    run's review.md. Made for token plans with a rolling usage window: review
    60 units now, resume the rest after the window resets.
    """
    from .resume import plan_resume, resume_review

    repo_root = find_repo_root(repo)
    try:
        plan = plan_resume(repo_root, run_id, paths=path)
        if plan["batch_size"] == 0:
            console.print(f"[yellow]{t('resume.nothing_left', run_id=run_id)}[/yellow]")
            raise typer.Exit(0)
        console.print(
            t(
                "resume.start",
                done=plan["units_done"],
                total=plan["units_total"],
                batch=plan["batch_size"],
            )
        )
        outcome = resume_review(
            repo_root,
            run_id,
            more=more,
            paths=path,
            out=out,
            progress=lambda msg: console.print(msg),
            token_budget=token_budget or None,
        )
    except LookupError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc
    except ConfigError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc
    except NoChanges as exc:  # a resumed batch can hit an empty queue too
        console.print(f"[yellow]{exc}[/yellow]")
        raise typer.Exit(0) from exc

    summary = outcome.summary()
    console.print(f"\n[green]Wrote {outcome.out_path or outcome.run_dir / 'review.md'}[/green]")
    console.print(
        f"Findings: {summary['findings']} | Machine-dropped: {summary['dropped']}"
        + (f" | Failed units: {summary['failed_units']}" if summary["failed_units"] else "")
    )
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
    max_units: int | None = typer.Option(
        None,
        "--max-units",
        help="Units to review. Default: auto (all if small, else 60). 0 = every unit",
    ),
    mode: str = typer.Option("", "--mode", help="quick|dual|committee"),
    rules: Path | None = typer.Option(None, "--rules", help="Project rules file"),
    path: str = typer.Option("", "--path", help="Limit the diff to a git pathspec, e.g. src/api"),
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
    from .report import finding_labels

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
            paths=path,
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
    # The same numbers the work order printed. Embedding them in the summary is what
    # lets `pr-collect` turn "F-001 accept" into a verdict on the right finding.
    labels = finding_labels(collection)
    body = summary_body(collection, inline, rest, marker, labels)

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
                "labels": labels,
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
        result = post_review(pull, gh_token, collection, inline, rest, labels=labels)
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


@app.command(name="pr-collect")
def pr_collect(
    repo: Path = typer.Option(Path("."), "--repo", help="Repository root"),
    pr_ref: str = typer.Option(
        "", "--pr", help="owner/repo#12 (default: the PR this run was posted for)"
    ),
    event: Path | None = typer.Option(
        None, "--event", help="GitHub event JSON, when running inside Actions"
    ),
    run_id: str = typer.Option(
        "", "--run-id", help="Run whose work order supplies the F-xxx labels"
    ),
    token: str = typer.Option("", "--token", help="GitHub token (default: $GITHUB_TOKEN)"),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Show what would be written; change nothing"
    ),
    as_json: bool = typer.Option(False, "--json", help="Machine-readable result"),
) -> None:
    """Read maintainer accept/reject replies off a PR into the feedback store.

    The closed loop, on the other side: `accept`/`reject` need the Web UI open on the
    machine that ran the review, but a pull request is where a finding actually gets
    judged. Collecting from the PR is what makes the adoption rate real for a team.
    """
    from .ci import CIError, PullRequest, collect_verdicts, load_event, parse_pull_request
    from .report import labels_from_markdown

    gh_token = token or os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or ""
    if not gh_token:
        console.print("[red]需要 token：设置 GITHUB_TOKEN 或传 --token[/red]")
        raise typer.Exit(1)

    repo_root = _resolve_repo_or_exit(repo)
    try:
        if pr_ref:
            slug, _, number = pr_ref.partition("#")
            owner, _, name = slug.partition("/")
            if not owner or not name or not number.isdigit():
                raise CIError(f"--pr 需要 owner/repo#12 形式，收到 {pr_ref!r}")
            pull = PullRequest(
                owner=owner.strip(), repo=name.strip(), number=int(number), base_sha="", head_sha=""
            )
        else:
            pull = parse_pull_request(load_event(event))
    except CIError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc

    # Prefer the ids from the PR itself; the local ledger only fills in for reviews
    # posted before the summary carried them.
    labels: dict[str, str] = {}
    from .pipeline import STATE_DIR

    runs = repo_root / STATE_DIR / "runs"
    target = run_id or _latest_pr_run(runs, pull)
    if target:
        work_order = runs / target / "review.md"
        if work_order.is_file():
            labels = labels_from_markdown(work_order.read_text(encoding="utf-8"))
        else:
            console.print(f"[yellow]找不到该 run 的工单：{work_order}[/yellow]")

    store = _store(repo_root)
    try:
        result = collect_verdicts(pull, gh_token, store, labels, dry_run=dry_run)
    except CIError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc

    if as_json:
        typer.echo(json.dumps(result, ensure_ascii=False, indent=2))
        return

    applied = result["applied"]
    accepted = sum(1 for a in applied if a["status"] == "accepted")
    rejected = sum(1 for a in applied if a["status"] == "rejected")
    verb = "（dry run，未写入）" if dry_run else ""
    console.print(
        f"[green]PR {pull.slug}#{pull.number}[/green] 回收裁决 {len(applied)} 条{verb}："
        f"采纳 {accepted}，驳回 {rejected}，已是该状态 {result['unchanged']}。"
    )
    for item in applied:
        colour = "green" if item["status"] == "accepted" else "yellow"
        console.print(
            f"  [{colour}]{item['label']}[/{colour}] {item['status']} "
            f"@{item['author']} -> {item['finding_id'][:12]}"
        )
    if result["unknown"]:
        console.print(
            "[yellow]这些编号不在本次工单里（可能是旧 run 或拼错）："
            + "、".join(result["unknown"])
            + "[/yellow]"
        )
    if result["not_found"]:
        console.print(
            "[yellow]反馈库里没有对应 finding：" + "、".join(result["not_found"]) + "[/yellow]"
        )
    if not applied:
        console.print(
            "[dim]没有可回收的回复。工单里请回复一行 `F-001 accept` 或 "
            "`F-001 reject: 原因`；标签来源：" + result["labels_source"] + "。[/dim]"
        )


def _latest_pr_run(runs_dir: Path, pull) -> str:
    """The newest run that was posted to this PR, via its pr-comments.json."""
    if not runs_dir.is_dir():
        return ""
    best = ""
    best_stamp = ""
    for candidate in runs_dir.iterdir():
        meta = candidate / "pr-comments.json"
        if not meta.is_file():
            continue
        try:
            data = json.loads(meta.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        linked = (
            (data.get("pull_request") or {}).get("slug"),
            (data.get("pull_request") or {}).get("number"),
        )
        if linked != (pull.slug, pull.number):
            continue
        stamp = candidate.name
        if stamp > best_stamp:
            best, best_stamp = stamp, stamp
    return best


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


def _resolve_repo_or_exit(repo: Path) -> Path:
    # A plain folder is reviewable now — the snapshot path reads its files — so
    # only a missing path is fatal here. Which range makes sense for a folder
    # without history is decided by the pipeline, which can explain itself.
    if not repo.exists():
        console.print(f"[red]{t('server.path_missing', path=repo)}[/red]")
        raise typer.Exit(1)
    return find_repo_root(repo)


def _bind_or_exit(repo_root, host: str, port: int, max_parallel: int):
    from .web.bootstrap import build

    try:
        return build(repo_root, host=host, port=port, max_parallel=max_parallel)
    except OSError as exc:
        console.print(f"[red]Cannot bind {host}:{port} — {exc}[/red]")
        raise typer.Exit(1) from exc


def _warn_non_loopback(host: str) -> None:
    """The UI has no auth; a non-loopback bind hands it to the whole network.

    The Host/Origin checks stop DNS rebinding through a browser, not a direct
    client that forges both headers. An explicit --host is still the user's
    choice, so warn loudly instead of refusing.
    """
    if host not in {"127.0.0.1", "localhost", "::1"}:
        console.print(f"[yellow]{t('cli.host_exposed', host=host)}[/yellow]")


def _url_for(httpd, host: str, print_port: bool) -> str:
    """Actual bound URL, plus the machine-readable line a shell waits for.

    ``--port 0`` is the desktop case: the launcher cannot know which port was
    granted, so it reads this line off stdout instead of polling for a listener.
    """
    port = httpd.server_address[1]
    if print_port:
        print(f"PORT={port}", flush=True)
    return f"http://{host}:{port}/"


def review_busy(hub, app_ui) -> bool:
    """True while any project is running — or queued for — a review.

    The CLI sees either one repo (``app_ui``) or a hub of several; the close
    guard needs the same answer in both modes. Queued counts too: killing the
    server while a run waits for a slot still discards work the user started.
    """
    if hub is not None:
        return hub.snapshot()["running"] > 0
    if app_ui is not None:
        return bool(app_ui.review_status()["running"])
    return False


def _watch_owner(parent_pid: int, httpd, extra=None) -> None:
    """Stop when the launcher exits — otherwise a crash leaves a paid review
    running with nobody to read the result."""
    if parent_pid <= 0:
        return
    from .web.lifecycle import stop_server, watch_parent

    def _orphaned() -> None:
        print(t("desk.parent_gone"), file=sys.stderr)
        if extra is not None:
            extra()  # close the GUI, which owns the main thread
        if not stop_server(httpd):
            os._exit(1)  # accept a lost request over an unreachable orphan

    watch_parent(parent_pid, _orphaned)


def _on_terminate(httpd, extra=None) -> None:
    """Make SIGTERM graceful so a shell can close us with a plain kill."""
    from .web.lifecycle import stop_server

    def _handler(signum, frame) -> None:
        if extra is not None:
            extra()
        stop_server(httpd)

    try:
        signal.signal(signal.SIGTERM, _handler)
    except (AttributeError, OSError, ValueError):
        pass  # not the main thread, or a platform that will not take the handler


@app.command()
def ui(
    repo: Path = typer.Option(Path("."), "--repo", help="Repository root to review"),
    port: int = typer.Option(
        8765, "--port", help="Loopback port for the local UI; 0 lets the OS pick"
    ),
    host: str = typer.Option("127.0.0.1", "--host", help="Bind address (keep it on loopback)"),
    no_browser: bool = typer.Option(False, "--no-browser", help="Do not open a browser tab"),
    no_repo: bool = typer.Option(
        False, "--no-repo", help="Start with an empty project list; no repository needed"
    ),
    print_port: bool = typer.Option(
        False, "--print-port", help="Print PORT=<n> on stdout once bound, for a launcher"
    ),
    parent_pid: int = typer.Option(
        0, "--parent-pid", help="Exit when this PID disappears (desktop shells)"
    ),
    max_parallel: int = typer.Option(
        2, "--max-parallel", help="How many projects may run a review at the same time"
    ),
) -> None:
    """Open the local web UI (multi-project tabs, run history, report, accept/reject)."""
    _warn_non_loopback(host)
    repo_root = None if no_repo else _resolve_repo_or_exit(repo)
    _hub, httpd, app_ui = _bind_or_exit(repo_root, host, port, max_parallel)

    url = _url_for(httpd, host, print_port)
    console.print(f"[green]CodeErratum UI[/green] on {url}")
    if repo_root is not None:
        console.print(f"Repo: {repo_root}")
    elif app_ui is not None:
        # --no-repo still restores the saved list; name the project the root API
        # points at instead of leaving the line out.
        console.print(f"Repo: {app_ui.repo_root}")
    else:
        console.print(f"[dim]{t('server.no_project')}[/dim]")
    if app_ui is not None and not app_ui.meta()["configured"]:
        console.print(f"[yellow]{t('cli.models_missing')}[/yellow]")
    console.print(f"[dim]{t('cli.stop_hint')}[/dim]")
    _watch_owner(parent_pid, httpd)
    _on_terminate(httpd)
    if not no_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        console.print(f"\n{t('cli.stopped')}")
    finally:
        httpd.server_close()


@app.command()
def desktop(
    repo: Path = typer.Option(Path("."), "--repo", help="Repository to open first"),
    no_repo: bool = typer.Option(
        False, "--no-repo", help="Open the project overview with nothing selected yet"
    ),
    port: int = typer.Option(0, "--port", help="Loopback port; 0 lets the OS pick"),
    host: str = typer.Option("127.0.0.1", "--host", help="Bind address (keep it on loopback)"),
    parent_pid: int = typer.Option(
        0, "--parent-pid", help="Exit when this PID disappears (launcher watchdog)"
    ),
    max_parallel: int = typer.Option(
        2, "--max-parallel", help="How many projects may run a review at the same time"
    ),
    width: int = typer.Option(1280, "--width", help="Window width"),
    height: int = typer.Option(860, "--height", help="Window height"),
) -> None:
    """Open the UI in a native window — same server, system WebView, no browser tab."""
    from .web.desktop import close_window, launch
    from .web.lifecycle import defer_signals, stop_server

    _warn_non_loopback(host)
    repo_root = None if no_repo else _resolve_repo_or_exit(repo)
    _hub, httpd, app_ui = _bind_or_exit(repo_root, host, port, max_parallel)
    url = _url_for(httpd, host, False)

    # Closing the window must stop the server, and a stop signal must close the
    # window — either alone leaves a live half behind. Bound after the server
    # exists, but before any thread starts, so every thread inherits the mask.
    def _teardown(_signum: int = 0) -> None:
        close_window()
        stop_server(httpd)

    def _review_busy() -> bool:
        """Any project running (or queued for) a review — hub or single-repo mode."""
        return review_busy(_hub, app_ui)

    deferred = defer_signals(_teardown)

    console.print(f"[green]CodeErratum[/green] on {url}")
    if repo_root is not None:
        console.print(f"Repo: {repo_root}")
    elif app_ui is not None:
        console.print(f"Repo: {app_ui.repo_root}")
    else:
        console.print(f"[dim]{t('server.no_project')}[/dim]")
    if app_ui is not None and not app_ui.meta()["configured"]:
        console.print(f"[yellow]{t('cli.models_missing')}[/yellow]")

    _watch_owner(parent_pid, httpd, extra=close_window)
    if not deferred:
        _on_terminate(httpd, extra=close_window)
    try:
        launch(
            httpd,
            url,
            t("desk.title"),
            width=width,
            height=height,
            is_busy=_review_busy,
        )
    except RuntimeError as exc:
        # Only the optional GUI extra is allowed to fail with RuntimeError here;
        # the server and its socket are already released by launch().
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc
    console.print(f"[dim]{t('desk.closed')}[/dim]")


@app.command()
def accept(finding_prefix: str = typer.Argument(..., help="Finding id prefix to accept")) -> None:
    """Mark a finding as accepted (true positive)."""
    try:
        store = _store(find_repo_root(Path(".")))
        found = store.find_finding(finding_prefix)
    except (sqlite3.Error, OSError) as exc:
        console.print(f"[red]feedback store unavailable: {exc}[/red]")
        raise typer.Exit(1) from exc
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
    try:
        store = _store(find_repo_root(Path(".")))
        found = store.find_finding(finding_prefix)
    except (sqlite3.Error, OSError) as exc:
        console.print(f"[red]feedback store unavailable: {exc}[/red]")
        raise typer.Exit(1) from exc
    if not found:
        console.print(f"[red]No finding starts with {finding_prefix}[/red]")
        raise typer.Exit(1)
    f = found[0]
    store.set_status(f["id"], "rejected", reason)
    console.print(f"[yellow]Rejected {f['id'][:12]}: {f['claim']}[/yellow]")


@app.command()
def reconcile(
    report: Path = typer.Argument(
        Path("erratumReview.md"), help="Work order whose 执行结果 table to read"
    ),
    repo: Path = typer.Option(Path("."), "--repo", help="Repository root"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Parse and report without writing"),
) -> None:
    """Read the work order's execution-result table back into feedback.db."""
    repo_root = _resolve_repo_or_exit(repo)
    # The default work order is generated at the repo root. From a subdirectory,
    # a bare relative "erratumReview.md" would otherwise be looked up next to
    # the CWD while the store still points at the repo root — inconsistent with
    # the promise that commands behave the same from any subdirectory. An
    # explicit path that exists as given always wins.
    if not report.is_absolute() and not report.is_file():
        report = repo_root / report
    if not report.is_file():
        console.print(f"[red]{t('cli.reconcile_missing', path=report)}[/red]")
        raise typer.Exit(1)
    parsed = parse_outcomes(report.read_text(encoding="utf-8", errors="replace"))
    if not parsed.found_table:
        console.print(f"[red]{t('cli.reconcile_no_table')}[/red]")
        raise typer.Exit(1)

    store = _store(repo_root)
    counts = apply_outcomes(store, parsed.rows, dry_run=dry_run)
    if counts["rows"] == 0:
        console.print(f"[yellow]{t('cli.reconcile_empty')}[/yellow]")
        raise typer.Exit(1)

    console.print(
        t(
            "cli.reconcile_summary",
            rows=counts["rows"],
            updated=counts["updated"],
            accepted=counts["accepted"],
            rejected=counts["rejected"],
            skipped=counts["skipped"],
            unmatched=counts["unmatched"],
        )
    )
    for problem in parsed.problems[:10]:
        console.print(f"[yellow]- {problem}[/yellow]")
    if dry_run:
        # Deliberate: a dry run is a preview, and "this report is stale" is a
        # legal thing to preview, not a failure of the preview itself.
        console.print(f"[dim]{t('cli.reconcile_dry')}[/dim]")
    elif counts["updated"]:
        console.print(f"[green]{t('cli.reconcile_done', path=store.db_path)}[/green]")
    elif counts["unmatched"]:
        # Parsing succeeded, but none of the rows belongs to a finding in this
        # feedback store. Exiting 0 here makes a stale or mistyped work order
        # look like a successful feedback-loop closure.
        console.print(f"[red]{t('cli.reconcile_no_writes')}[/red]")
        raise typer.Exit(1)


@app.command()
def compare() -> None:
    """Show cross-model comparison and per-model profile (phase-3 resume evidence)."""
    try:
        store = _store(find_repo_root(Path(".")))
        history = store.comparison_history()
        profiles = store.model_profiles()
    except (sqlite3.Error, OSError) as exc:
        console.print(f"[red]feedback store unavailable: {exc}[/red]")
        raise typer.Exit(1) from exc

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
    try:
        s = _store(find_repo_root(Path("."))).stats()
    except (sqlite3.Error, OSError) as exc:
        console.print(f"[red]feedback store unavailable: {exc}[/red]")
        raise typer.Exit(1) from exc
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
        console.print("\n[bold]按模型（断言数）[/bold]")
        for row in s["by_model"]:
            console.print(f"  {row['model_id']:<32} {row['n']:>3} asserts")
    if s.get("by_model_roles"):
        console.print("\n[bold]按模型/角色[/bold]")
        for row in s["by_model_roles"]:
            console.print(f"  {row['model_id']:<28} {row['role']:<9} {row['n']:>3}")
    if s.get("dropped_by_reason"):
        console.print("\n[bold]机器丢弃原因（非人工驳回）[/bold]")
        for row in s["dropped_by_reason"][:10]:
            console.print(f"  {row['reason']:<32} {row['n']:>3}")


@app.command(name="eval")
def eval_command(
    repo: Path = typer.Option(Path("."), "--repo", help="仓库路径"),
    seeded: Path | None = typer.Option(
        None, "--seeded", help="种 bug 清单（JSON），测召回率与证伪误杀"
    ),
    export: Path | None = typer.Option(
        None, "--export", help="把待标注数据写成 JSONL 后退出（refuted 行的 human 留空待回填）"
    ),
    labels: Path | None = typer.Option(
        None, "--labels", help="用人工标注好的 JSONL 代替库内裁决来算模型精度"
    ),
    run_id: list[str] = typer.Option(None, "--run-id", help="只评指定 run（可重复）"),
    limit: int = typer.Option(20, "--limit", help="未指定 run 时最多评最近的几个"),
    as_json: bool = typer.Option(False, "--json", help="输出机器可读 JSON"),
) -> None:
    """离线评测审查质量。stats 回答人工「用了没有」，eval 回答审查「对不对」。"""
    root = find_repo_root(repo)
    payload: dict = {}
    warnings: list[str] = []
    ids = [str(r) for r in (run_id or [])]
    one_run = ids[0] if len(ids) == 1 else None

    if export:
        try:
            rows = _eval.export_labels(_store(root), repo_root=root, run_id=one_run)
        except (sqlite3.Error, OSError) as exc:
            console.print(f"[red]feedback store unavailable: {exc}[/red]")
            raise typer.Exit(1) from exc
        try:
            with export.open("w", encoding="utf-8") as fh:
                for row in rows:
                    fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        except OSError as exc:
            console.print(f"[red]cannot write {export}: {exc}[/red]")
            raise typer.Exit(1) from exc
        console.print(f"[green]已写出 {len(rows)} 行标注数据到 {export}[/green]")
        console.print(
            "[dim]kind=refuted 行的 human 字段请人工回填：true=证伪轮误杀，false=杀得对[/dim]"
        )
        return

    if seeded:
        try:
            bugs = _eval.load_seeded_bugs(seeded)
        except (OSError, ValueError) as exc:
            console.print(f"[red]种 bug 清单不可用：{exc}[/red]")
            raise typer.Exit(1) from exc
        wanted = ids or None
        collections, warns = _eval.run_collections(root, wanted, limit)
        warnings.extend(warns)
        if not collections:
            console.print(
                f"[yellow]没有可评分的 run 账本（{root}/.codeerratum/runs 为空或均已清理）[/yellow]"
            )
        else:
            payload["seeded"] = _eval.score_seeded(collections, bugs)

    try:
        if labels:
            rows = [
                json.loads(line)
                for line in labels.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
        else:
            rows = _eval.export_labels(_store(root), repo_root=root, run_id=one_run)
    except (sqlite3.Error, OSError, ValueError) as exc:
        console.print(f"[red]标注数据不可用：{exc}[/red]")
        raise typer.Exit(1) from exc
    payload["labels"] = _eval.score_labels(rows)

    if warnings:
        payload["warnings"] = warnings
    if as_json:
        console.print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        console.print(_eval.format_eval(payload.get("seeded"), payload.get("labels")))
        for warning in warnings:
            console.print(f"[yellow]{warning}[/yellow]")


@app.command(name="suggest-rules")
def suggest_rules(
    repo: Path = typer.Option(Path("."), "--repo", help="仓库路径"),
    min_count: int = typer.Option(2, "--min", help="同一目录+分类被驳回几次才算模式"),
    append: bool = typer.Option(False, "--append", help="追加写入 .codeerratum/rules.md"),
) -> None:
    """把反复被人工驳回的发现模式，提炼成候选项目规范（只追加，不覆盖）。"""
    try:
        root = find_repo_root(repo)
        rows = _store(root).rejected_by_file_category()
    except (sqlite3.Error, OSError) as exc:
        console.print(f"[red]feedback store unavailable: {exc}[/red]")
        raise typer.Exit(1) from exc

    suggestions = build_suggestions(rows, min_count=min_count)
    if not suggestions:
        console.print(
            f"[yellow]暂无达到阈值（≥{min_count} 次）的驳回模式。[/yellow]\n"
            "[dim]先多用 accept/reject 裁决工单，闭环数据攒起来后这里才有话可说。[/dim]"
        )
        return

    console.print("[bold]候选项目规范（来自人工驳回的聚合）[/bold]\n")
    for s in suggestions:
        console.print(f"  {s['rule']}  [dim](驳回 {s['count']} 次)[/dim]")

    path = root / HOUSE_RULES_FILE
    if not append:
        console.print(
            f"\n[dim]预览模式。确认后加 --append 追加写入 {path}"
            f"（上限 {MAX_HOUSE_RULES_CHARS} 字符，超限会拒绝写入而不是截断）[/dim]"
        )
        return

    try:
        added, overflow = append_rules(path, [s["rule"] for s in suggestions])
    except OSError as exc:
        console.print(f"[red]无法写入 {path}: {exc}[/red]")
        raise typer.Exit(1) from exc
    if overflow:
        console.print(
            f"[red]{path} 追加后会超出 {MAX_HOUSE_RULES_CHARS} 字符上限约 {overflow} 字。"
            "请先精简旧规范再重试（不会替你截断）。[/red]"
        )
        raise typer.Exit(1)
    if added == 0:
        console.print(f"[dim]{path} 已包含这些规则，未重复追加。[/dim]")
    else:
        console.print(f"[green]已追加 {added} 条到 {path}（只追加，旧内容原样保留）[/green]")
