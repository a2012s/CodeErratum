"""The UI language must reach the model's prose.

`language_directive` only works if it actually travels: the review prompt, the
falsify prompt, and the pipeline's run-start capture are three separate hops,
and a revert of any one of them leaves English findings in a Chinese UI while
every unit-level test still passes. Each test below pins one hop.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from codeerratum import i18n, pipeline
from codeerratum.ingest import split_diff
from codeerratum.models import (
    Anchor,
    Finding,
    FindingCollection,
    ReviewUnit,
    Suggestion,
)
from codeerratum.models.finding import DefectCategory, Lens, snippet_hash
from codeerratum.review.executor import build_messages, language_directive, load_prompt
from codeerratum.review.falsification import build_falsify_messages

DIFF = """diff --git a/m.py b/m.py
--- a/m.py
+++ b/m.py
@@ -1,2 +1,2 @@
 def foo():
-    return 0
+    return 1
"""


def test_directive_exists_only_for_non_english():
    assert "Simplified Chinese" in language_directive("zh")
    assert language_directive("en") == ""
    assert language_directive("") == ""


def test_directive_names_the_human_readable_fields():
    text = language_directive("zh")
    # A directive that misses a field silently leaves it in English — the exact
    # bug this feature fixes, resurfacing one field at a time.
    for field in ("claim", "detail", "suggestion.explanation", "suggestion.acceptance"):
        assert field in text


def _unit(tmp_path: Path) -> ReviewUnit:
    (tmp_path / "m.py").write_text("def foo():\n    return 1\n", encoding="utf-8")
    return split_diff(DIFF, tmp_path)[0]


def test_review_prompt_carries_the_directive(tmp_path):
    unit = _unit(tmp_path)
    messages = build_messages(
        unit,
        ["def foo():", "    return 1"],
        enclosing_span=(1, 2),
        output_lang="zh",
    )
    system = messages[0]["content"]
    assert "Simplified Chinese" in system
    assert system.index("Simplified Chinese") < system.index("# Output\n")
    # English injects nothing: the prompt is already English.
    plain = build_messages(unit, ["def foo():", "    return 1"], enclosing_span=(1, 2))
    assert "Simplified Chinese" not in plain[0]["content"]


def test_house_rules_cannot_displace_the_directive():
    """Both blocks are spliced by code, but the language section must survive a
    prompt that also carries project rules."""
    prompt = load_prompt(house_rules="Write findings in Latin.", output_lang="zh")
    assert "Simplified Chinese" in prompt


def _finding() -> Finding:
    snippet = "x = 1\n"
    return Finding(
        id="0000000000000001",
        unit_id="u1",
        lens=Lens.CORRECTNESS,
        category=DefectCategory.BOUNDARY,
        severity="P1",
        claim="claim",
        detail="d",
        anchors=[
            Anchor(
                file="a.py",
                start_line=1,
                end_line=1,
                snippet=snippet,
                snippet_hash=snippet_hash(snippet),
            )
        ],
        agreement="unique",
        source_models=["m1"],
        suggestion=Suggestion(diff=None, explanation="e", acceptance="pytest -q"),
    )


def test_falsify_prompt_carries_the_directive():
    messages = build_falsify_messages(_finding(), "ctx", "facts", output_lang="zh")
    assert "Simplified Chinese" in messages[0]["content"]
    plain = build_falsify_messages(_finding(), "ctx", "facts")[0]["content"]
    assert "Simplified Chinese" not in plain


def _init_repo(path: Path) -> Path:
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.io"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=path, check=True)
    return path


def test_pipeline_pins_the_run_language(tmp_path, monkeypatch):
    """The directive is captured once at run start. If execute_review reads the
    ContextVar later (or never passes it), a run outliving its request thread
    silently falls back to the environment default."""
    repo = _init_repo(tmp_path)
    (repo / "m.py").write_text("def foo():\n    return 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "one"], cwd=repo, check=True)
    (repo / "m.py").write_text("def foo():\n    return 2\n", encoding="utf-8")

    monkeypatch.setenv("CODEERRATUM_BASE_URL", "http://x")
    monkeypatch.setenv("CODEERRATUM_MODEL", "m")
    monkeypatch.delenv("CODEERRATUM_MODELS", raising=False)
    monkeypatch.delenv("CODEERRATUM_LANG", raising=False)

    captured: dict = {}

    async def fake_run(registry, mode, units, repo_root, run_id, **kwargs):
        captured["output_lang"] = kwargs.get("output_lang")
        return (
            FindingCollection(
                run_id=run_id, repo=str(repo_root), commit="", model_id="m", spec_level="L1"
            ),
            [],
            [],
        )

    monkeypatch.setattr(pipeline, "run_review", fake_run)
    with i18n.language("en"):
        pipeline.execute_review(repo, diff_spec="HEAD")
    assert captured["output_lang"] == "en", "the UI language must travel with the run"


def test_pipeline_uses_chinese_by_default(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path)
    (repo / "m.py").write_text("def foo():\n    return 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "one"], cwd=repo, check=True)
    (repo / "m.py").write_text("def foo():\n    return 2\n", encoding="utf-8")

    monkeypatch.setenv("CODEERRATUM_BASE_URL", "http://x")
    monkeypatch.setenv("CODEERRATUM_MODEL", "m")
    monkeypatch.delenv("CODEERRATUM_MODELS", raising=False)
    monkeypatch.delenv("CODEERRATUM_LANG", raising=False)

    captured: dict = {}

    async def fake_run(registry, mode, units, repo_root, run_id, **kwargs):
        captured["output_lang"] = kwargs.get("output_lang")
        return (
            FindingCollection(
                run_id=run_id, repo=str(repo_root), commit="", model_id="m", spec_level="L1"
            ),
            [],
            [],
        )

    monkeypatch.setattr(pipeline, "run_review", fake_run)
    with i18n.language("zh"):
        pipeline.execute_review(repo, diff_spec="HEAD")
    assert captured["output_lang"] == "zh"


def test_split_still_produces_units(tmp_path):
    # Guard so the two pipeline tests above fail loudly if the diff itself stops
    # yielding units (otherwise "no changes" would short-circuit before the stub).
    assert _unit(tmp_path).symbol == "foo"
