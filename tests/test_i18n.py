"""Bilingual message catalog tests.

Two languages exist — Chinese (default) and English — so the cheap failure mode
is a key that was added to one side only, or an English entry that is still the
Chinese text. Both are caught here without touching the network.
"""

from __future__ import annotations

import pytest

from codeerratum.i18n import (
    LANGS,
    MESSAGES,
    catalog_en,
    get_lang,
    language,
    normalize_lang,
    set_lang,
    t,
)


def test_every_key_has_both_languages_and_they_differ():
    missing = [k for k, pair in MESSAGES.items() if len(pair) != 2 or not all(pair)]
    assert missing == []
    # Punctuation-only suffixes are identical by design (" → {target}").
    untranslated = [
        k for k, (zh, en) in MESSAGES.items() if zh == en and k not in {"model.target_suffix"}
    ]
    assert untranslated == [], f"English text equals Chinese for {untranslated}"


def test_placeholders_match_between_languages():
    """A {placeholder} present in one language but not the other loses data."""
    import re

    problems = []
    for key, (zh, en) in MESSAGES.items():
        zh_fields = set(re.findall(r"\{(\w+)\}", zh))
        en_fields = set(re.findall(r"\{(\w+)\}", en))
        if zh_fields != en_fields:
            problems.append((key, sorted(zh_fields), sorted(en_fields)))
    assert problems == []


def test_default_is_chinese():
    set_lang(None)
    assert get_lang() == "zh"
    assert t("server.review_busy") == "已有审查正在进行中，请等待其完成后再运行"


def test_english_switch_and_context_isolation():
    set_lang("en")
    assert t("server.review_busy").startswith("A review is already running")
    with language("zh"):
        assert get_lang() == "zh"
        assert t("server.review_busy").startswith("已有审查")
    assert get_lang() == "en"  # restored, not reset to the default
    set_lang(None)


def test_unknown_values_fall_back_to_chinese():
    for value in ("", "fr", "zh-CN", None):
        assert normalize_lang(value) == "zh"
    for value in ("en", "EN", "en-US", " en "):
        assert normalize_lang(value) == "en"


def test_unknown_key_returns_the_key_instead_of_raising():
    assert t("nope.missing") == "nope.missing"


def test_missing_placeholder_does_not_explode():
    # Callers build strings from user data; a broken format must not 500 a run.
    assert t("server.open_failed") == "无法打开当前仓库文件夹：{error}"


def test_language_is_per_context_not_global():
    """Two threads must be able to answer in different languages at once."""
    import threading

    seen: dict[str, str] = {}

    def worker(name: str, lang: str) -> None:
        set_lang(lang)
        seen[name] = t("server.stop_requested")

    threads = [
        threading.Thread(target=worker, args=("zh", "zh")),
        threading.Thread(target=worker, args=("en", "en")),
    ]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    assert seen["zh"] == "已请求停止审查"
    assert seen["en"] == "Review stop requested"


def test_catalog_en_exports_only_english():
    en = catalog_en()
    assert set(en) == set(MESSAGES)
    assert all(value not in LANGS for value in en.values())


def test_pipeline_notice_english():
    from codeerratum.pipeline import abstain_warning

    class _Verdict:
        def __init__(self, role: str) -> None:
            self.role = role

    class _Finding:
        def __init__(self, roles: list[str]) -> None:
            self.verdicts = [_Verdict(r) for r in roles]

    findings = [_Finding(["abstain", "abstain", "assert"])]
    with language("en"):
        text = abstain_warning(findings)
        assert "insufficient_context" in text
        assert "证据不足" not in text
    with language("zh"):
        assert "证据不足" in abstain_warning(findings)


@pytest.mark.parametrize(
    ("key", "kwargs", "expected"),
    [
        ("orch.unit_start", {"model": "m", "idx": 1, "total": 2, "file": "a.py"}, "[m] review"),
        ("server.units_clamped_high", {"asked": 5000, "used": 500, "cap": 500}, "Limit 5000"),
        ("model.timeout", {"seconds": 10}, "timed out"),
    ],
)
def test_english_interpolation(key, kwargs, expected):
    with language("en"):
        assert expected in t(key, **kwargs)
