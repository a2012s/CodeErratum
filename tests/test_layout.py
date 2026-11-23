"""Layout invariants for the two static pages.

These are string/AST-level checks, not rendering tests: they lock the *reasons*
behind two bugs that were invisible in code review and only showed up on screen.

1. The shell wraps each project in an iframe inside ``#frames``. A wrapper with
   no height gives ``height: 100%`` nothing to resolve against, so every iframe
   collapses to the browser default 150px — a sliver of UI over a black void.
2. The review page used to size its grid as ``calc(100vh - 51px)``. The header
   is 51px in Chinese but 57px in English (78px when it wraps on a narrow
   window), so the grid overflowed by the difference and grew a scrollbar.
3. Translating a node by replacing its ``innerHTML`` wipes any child that has an
   id. One such child (``#maxParallel``) was read by the shell's boot code; in
   English the whole shell threw and rendered empty.
"""

from __future__ import annotations

from html.parser import HTMLParser

import pytest

from codeerratum.web.server import STATIC_DIR


def read(name: str) -> str:
    return (STATIC_DIR / name).read_text(encoding="utf-8")


class _IdCollector(HTMLParser):
    """Collect every id in a markup fragment."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.ids: list[str] = []

    def handle_starttag(self, tag, attrs):
        node_id = dict(attrs).get("id")
        if node_id:
            self.ids.append(node_id)


def ids_in(fragment: str) -> list[str]:
    collector = _IdCollector()
    collector.feed(fragment)
    collector.close()
    return collector.ids


def test_project_iframe_wrapper_can_resolve_percentage_height():
    html = read("projects.html")
    assert "#frames { height: 100%; }" in html
    assert "iframe.project" in html
    # The iframe must not carry its own fixed height: the wrapper owns it.
    assert "height: 100%" in html.split("iframe.project", 1)[1][:120]


def test_review_grid_sizes_itself_from_the_flex_chain():
    html = read("index.html")
    assert "calc(100vh - 51px)" not in html, (
        "a hard-coded header height overflows whenever the header reflows "
        "(English labels, narrow windows)"
    )
    layout = html.split(".layout {", 1)[1].split("}", 1)[0]
    assert "flex: 1" in layout
    assert "min-height: 0" in layout
    # An implicit auto row lets a tall sidebar push the row past the viewport.
    assert "grid-template-rows: minmax(0, 1fr)" in layout


def test_header_height_is_published_for_the_fixed_drawer():
    html = read("index.html")
    assert "syncHeaderVar" in html
    assert 'setProperty("--header-h"' in html
    drawer = html.split(".drawer {", 1)[1].split("}", 1)[0]
    assert "top: var(--header-h" in drawer


def test_overview_button_never_navigates_a_tab_into_the_shell():
    """Clicking 项目总览 used to load the shell inside the tab that asked for it.

    ``/p/<slug>/`` is rendered *in* the shell's iframe, so ``location.href = "/"``
    replaced the tab with another copy of the shell — every click added a nested
    header and no click ever reached the overview. The page must ask its parent
    instead, and the shell must honour both messages and ``#overview``.
    """
    review = read("index.html")
    shell = read("projects.html")

    assert 'postMessage({ type: "ce:overview" }, location.origin)' in review
    assert 'type: "ce:overview"' in shell, "the shell must listen for the request"
    assert 'hash === "overview"' in shell, (
        "an explicit #overview must beat the single-project shortcut, or the "
        "overview button bounces back into the only project"
    )
    # Belt and braces: if "/" is loaded in a frame anyway, no second header.
    assert 'html[data-nested="1"] header.top { display: none; }' in shell


@pytest.mark.parametrize("name", ["index.html", "projects.html"])
def test_translated_markup_keeps_every_identified_child(name):
    """Replacing innerHTML must not silently delete an element the JS reads."""
    html = read(name)
    missing: list[str] = []
    for node in _split_translated_nodes(html):
        zh_ids = set(ids_in(node["body"]))
        en_ids = set(ids_in(node["en"]))
        for node_id in zh_ids - en_ids:
            missing.append(f"{name}: <{node['tag']} data-en=...> drops #{node_id}")
    assert not missing, "\n".join(missing)


class _NodeSplitter(HTMLParser):
    """Yield (tag, data-en value, zh inner markup) for each data-en element."""

    def __init__(self, source: str) -> None:
        super().__init__(convert_charrefs=False)
        self.source = source
        self.nodes: list[dict] = []

    def handle_starttag(self, tag, attrs):
        pairs = dict(attrs)
        if pairs.get("data-en") is None:
            return
        start = self.getpos()
        end = self.source.find(">", self._offset(start)) + 1
        self.nodes.append({"tag": tag, "en": pairs["data-en"], "body": ""})
        # Inner markup starts right after this start tag; the corresponding end
        # tag is found by a literal scan, which is enough for these flat pages.
        body_start = end
        close = f"</{tag}>"
        body_end = self.source.find(close, body_start)
        if body_end != -1:
            self.nodes[-1]["body"] = self.source[body_start:body_end]

    def _offset(self, pos) -> int:
        line, col = pos
        lines = self.source.splitlines(keepends=True)
        return sum(len(part) for part in lines[: line - 1]) + col


def _split_translated_nodes(source: str) -> list[dict]:
    splitter = _NodeSplitter(source)
    splitter.feed(source)
    splitter.close()
    return [node for node in splitter.nodes if node["body"]]


def test_mode_restore_refills_the_model_picker():
    """The saved mode decides how many seats exist, so restoring it must re-render.

    Without this the page opened on "dual" (from the saved config) with a single
    chip selected: `renderModels` had run once, while the select still said quick.
    """
    html = read("index.html")
    assert 'id="modelPicker"' in html
    assert "function renderModels" in html
    assert "models: [...PICKED]," in html
    after_mode = html.split('$("mode").value = m.mode;', 1)[1][:400]
    assert "renderModels()" in after_mode


def test_budget_select_sends_all_and_auto_as_the_server_expects():
    html = read("index.html")
    assert 'id="unitBudget"' in html
    # 0 means every unit; "auto" leaves the count to the server.
    assert 'if (mode === "all") return 0;' in html
    assert 'if (mode === "auto") return "auto";' in html


def test_tab_hides_the_language_switch_the_shell_already_has():
    """Two identical selects, two rows apart: the shell's is the only one.

    ``/p/<slug>/`` renders inside the hub's iframe, whose header carries the
    language select. The tab keeps its own for a directly opened URL, so the
    rule has to be conditional on being framed.
    """
    review = read("index.html")
    assert "html.embedded #langSel { display: none; }" in review
    # Before <body>, so the control never flashes in and out on first paint.
    head = review.split("</head>", 1)[0]
    assert "window.self !== window.top" in head
    assert 'classList.add("embedded")' in head


def test_findings_stream_into_the_right_pane_one_by_one():
    """One SSE frame per finding, rendered as a card on the right as it lands.

    The run is long enough that a panel only updating at "done" looks frozen; and
    the finished report must replace these raw cards rather than stacking under
    them.
    """
    html = read("index.html")
    assert 'id="liveView"' in html
    assert 'id="liveCards"' in html
    assert 'ev.type === "finding"' in html
    assert "liveAddFinding(ev.finding" in html
    # Raw cards are explicitly not the report.
    assert "尚未聚类" in html
    # The streaming path must swap to the report at "done", exactly like the
    # tracking path does — otherwise the pane stays on one-line cards and the
    # detailed report never appears without a manual click.
    assert "openRun(runId)" in html
    assert 'ev.type === "done"' in html
    # Live cards carry the reasoning behind the claim, collapsed by default.
    assert "live-detail" in html
    # Opening a finished run (or switching repo) hides the live list.
    assert '$("liveView").hidden = true' in html
    # A run starts on the live view, not on the "fill in a diff range" placeholder.
    reset = html.split("function progressReset()", 1)[1][:600]
    assert '$("liveView").hidden = false' in reset
    assert '$("empty").hidden = true' in reset


def test_empty_diff_offers_whole_tree_review_in_both_paths():
    """A clean HEAD ends at "nothing to review"; the UI must offer a way out.

    Two paths can end empty: the SSE frame this tab owns, and the status poll
    tracking a run started elsewhere. Both must ask, and saying yes relaunches
    against the whole worktree with the prompt disabled so an actually-empty
    repo cannot trap the user in a dialog loop. Pure DOM modal only — the
    pywebview native confirm() deadlocks the macOS main thread.
    """
    html = read("index.html")
    # A DOM confirmation dialog, not a native/confirm() call.
    assert 'id="askOverlay"' in html
    assert 'id="askYes"' in html
    assert 'id="askNo"' in html
    assert "create_confirmation_dialog" not in html
    assert "confirm(" not in html
    assert "function askReviewWholeTree()" in html
    # The retry uses the whole-worktree spec and does not re-prompt.
    yes = html.split('$("askYes").onclick', 1)[1][:220]
    assert 'startReview({ diff: "WORKTREE" }, false)' in yes
    # Both terminal-empty paths ask.
    sse_empty = html.split('ev.type === "empty"', 1)[1][:600]
    assert "askReviewWholeTree()" in sse_empty
    poll_empty = html.split('st.status === "empty"', 1)[1][:300]
    assert "askReviewWholeTree()" in poll_empty
    # The launch logic is a reusable function so the retry does not duplicate it.
    assert "async function startReview(" in html
    assert '$("runBtn").onclick = () => startReview();' in html
    # Bilingual strings ship with the dialog.
    assert 'data-en="No changes found"' in html
    assert 'data-en="Review whole tree"' in html


def test_drop_switch_survives_the_missing_drawer_button():
    """showDropMatches() replaces #repoBody, removing #repoSwitch; the drag path
    then calls doSwitchRepo with no button in the DOM. An unguarded
    btn.disabled threw a TypeError and the switch silently never happened."""
    html = read("index.html")
    body = html.split("async function doSwitchRepo", 1)[1].split(
        "\nasync function openRepoFolder", 1
    )[0]
    assert "if (btn) btn.disabled = true;" in body
    assert "if (btn) btn.disabled = false;" in body
    assert "\n  btn.disabled" not in body


def test_review_md_link_stays_inside_the_project_base():
    """Under /p/<slug>/ a root-relative /api/runs/... href opens the root
    project's report (or 404) instead of the tab's own run."""
    html = read("index.html")
    assert 'href="/api/runs/' not in html
    assert "${BASE}/api/runs/${encodeURIComponent(run.run_id)}/review.md" in html
    assert 'href="${esc(mdUrl)}"' in html
    assert 'rel="noopener"' in html


def test_overview_token_counts_are_escaped():
    """tokens.total goes into card.innerHTML like every sibling field; a
    non-numeric value from a polluted store must render as text, not markup."""
    html = read("projects.html")
    assert "esc(Number(p.tokens.total) || 0)" in html
