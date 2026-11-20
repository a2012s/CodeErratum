"""Language tagging tests.

Only Python gets symbol-level splitting; everything else is reviewed per file. That
is a limitation stated in the output, not a silent "no findings" — the tag reaches
the prompt so the model knows what it is reading.
"""

from __future__ import annotations

from codeerratum.ingest import detect_language, split_diff

TS_DIFF = """diff --git a/app.ts b/app.ts
--- a/app.ts
+++ b/app.ts
@@ -1,1 +1,2 @@
 const a = 1;
+const b = a / 0;
"""

PY_DIFF = """diff --git a/m.py b/m.py
--- a/m.py
+++ b/m.py
@@ -1,2 +1,2 @@
 def foo():
-    return 0
+    return 1
"""


def test_known_suffixes_map_to_languages():
    assert detect_language("a/b.ts") == "typescript"
    assert detect_language("a/b.tsx") == "tsx"
    assert detect_language("a/b.PY") == "python"
    assert detect_language("main.go") == "go"
    assert detect_language("lib.rs") == "rust"


def test_unknown_suffix_is_text():
    assert detect_language("LICENSE") == "text"
    assert detect_language("weird.zzz") == "text"


def test_non_python_file_becomes_a_file_level_unit(tmp_path):
    (tmp_path / "app.ts").write_text("const a = 1;\nconst b = a / 0;\n", encoding="utf-8")
    units = split_diff(TS_DIFF, tmp_path)
    assert len(units) == 1
    assert units[0].language == "typescript"
    assert units[0].symbol == "<module>"


def test_python_keeps_symbol_level_units(tmp_path):
    (tmp_path / "m.py").write_text(
        "def foo():\n    return 1\n\ndef bar():\n    return 2\n", encoding="utf-8"
    )
    units = split_diff(PY_DIFF, tmp_path)
    assert units[0].language == "python"
    assert units[0].symbol == "foo"


def test_forced_language_overrides_detection(tmp_path):
    """Existing callers pass language='python' explicitly; that must still win."""
    (tmp_path / "app.ts").write_text("const a = 1;\nconst b = a / 0;\n", encoding="utf-8")
    units = split_diff(TS_DIFF, tmp_path, language="python")
    assert units[0].language == "python"
