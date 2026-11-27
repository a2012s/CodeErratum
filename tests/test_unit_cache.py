"""Cross-run unit cache — an unchanged unit must not be paid for twice.

Every test here counts provider calls. The claim being protected is not "the store
has a row" but "the second review of identical code spent zero tokens".
"""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime, timedelta

import pytest

from codeerratum.ingest import get_diff, split_diff
from codeerratum.pipeline import StoreUnitCache, unit_cache_enabled
from codeerratum.providers.base import Completion, Usage, cache_key
from codeerratum.review import review_unit
from codeerratum.review.schema import LLMAnchor, LLMFinding, LLMReview, LLMSuggestion
from codeerratum.store import ReviewStore
from codeerratum.store.sqlite import UNIT_CACHE_TTL_DAYS


def _now() -> str:
    """A stamp inside the TTL, in the format the cache is really written with.

    Hard-coded dates rot. "2026-01-01" was fresh when these tests were drafted and is
    already past the 120-day window, so every row disappeared on write and three
    tests ended up asserting eviction instead of hits.
    """
    return datetime.now(UTC).isoformat(timespec="seconds")


class CountingProvider:
    family = "stub"

    def __init__(self, line: int, snippet: str):
        self.line = line
        self.snippet = snippet
        self.calls = 0

    async def complete(self, model_id, messages, schema, temperature=0.0, max_tokens=2048):
        self.calls += 1
        review = LLMReview(
            summary="stub",
            findings=[
                LLMFinding(
                    lens="correctness",
                    category="boundary",
                    severity="P1",
                    claim=f"问题 {self.calls}",
                    detail="空列表取首元素会抛 IndexError。",
                    anchor=LLMAnchor(
                        file="m.py",
                        start_line=self.line,
                        end_line=self.line,
                        snippet=self.snippet,
                    ),
                    suggestion=LLMSuggestion(explanation="判空", acceptance="pytest -q"),
                )
            ],
        )
        return Completion(
            text=json.dumps(review.model_dump(mode="json")),
            parsed=review,
            usage=Usage(input_tokens=1000, output_tokens=500),
            raw_request_hash="stub",
            latency_ms=7,
        )


@pytest.fixture
def repo(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.io"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path, check=True)
    (tmp_path / "m.py").write_text("def foo():\n    return 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=tmp_path, check=True)
    (tmp_path / "m.py").write_text("def foo():\n    x = []\n    return x[0]\n", encoding="utf-8")
    return tmp_path


@pytest.fixture
def store(tmp_path):
    return ReviewStore(tmp_path / "cache_test.db")


async def _review(repo, provider, cache=None, **kwargs):
    units = split_diff(get_diff("HEAD", repo), repo)
    assert units
    out = []
    for unit in units:
        findings, _rej, completion, _diags = await review_unit(
            provider, "stub-model", unit, repo, "run-1", cache=cache, **kwargs
        )
        out.append((findings, completion))
    return out


@pytest.mark.asyncio
async def test_an_identical_unit_is_never_paid_for_twice(repo, store):
    """The headline: two reviews of the same diff, one review's tokens."""
    cache = StoreUnitCache(store)
    first = CountingProvider(line=3, snippet="    return x[0]\n")
    await _review(repo, first, cache)
    assert first.calls >= 1

    second = CountingProvider(line=3, snippet="    return x[0]\n")
    await _review(repo, second, cache)
    assert second.calls == 0, "a byte-identical request must not reach the provider"
    assert cache.hits >= 1

    # The replay still yields findings, and it survives a fresh process.
    again = CountingProvider(line=3, snippet="    return x[0]\n")
    results = await _review(repo, again, StoreUnitCache(store))
    assert again.calls == 0
    assert all(f for f, _c in results)


@pytest.mark.asyncio
async def test_a_cached_reply_is_replayed_not_reconverted(repo, store):
    """Only the model's reply is cached, so local post-processing stays in force."""
    cache = StoreUnitCache(store)
    before = await _review(repo, CountingProvider(line=3, snippet="    return x[0]\n"), cache)
    provider = CountingProvider(line=3, snippet="    return x[0]\n")
    after = await _review(repo, provider, cache)

    assert provider.calls == 0
    for (old_f, old_c), (new_f, new_c) in zip(before, after, strict=True):
        assert new_c.cached is True
        assert old_c.cached is False
        assert [f.claim for f in new_f] == [f.claim for f in old_f]


@pytest.mark.asyncio
async def test_changed_source_is_a_miss(repo, store):
    """Edit the file and the prompt changes, so the cache must not answer."""
    cache = StoreUnitCache(store)
    await _review(repo, CountingProvider(line=3, snippet="    return x[0]\n"), cache)

    (repo / "m.py").write_text(
        "def foo():\n    x = []\n    return x[0] if x else None\n", encoding="utf-8"
    )
    provider = CountingProvider(line=3, snippet="    return x[0]\n")
    await _review(repo, provider, cache)
    assert provider.calls >= 1, "different source text is a different question"


@pytest.mark.asyncio
async def test_house_rules_and_language_are_part_of_the_key(repo, store):
    """Rules and output language both change what the model was asked."""
    cache = StoreUnitCache(store)
    await _review(repo, CountingProvider(line=3, snippet="    return x[0]\n"), cache)

    with_rules = CountingProvider(line=3, snippet="    return x[0]\n")
    await _review(repo, with_rules, cache, house_rules="always check None")
    assert with_rules.calls >= 1

    # English injects no directive at all -- it *is* the prompt's language -- so the
    # request is byte-identical, and refusing to pay twice here is the whole point.
    in_english = CountingProvider(line=3, snippet="    return x[0]\n")
    await _review(repo, in_english, cache, output_lang="en")
    assert in_english.calls == 0, "en and the default prompt are the same bytes"

    in_chinese = CountingProvider(line=3, snippet="    return x[0]\n")
    await _review(repo, in_chinese, cache, output_lang="zh")
    assert in_chinese.calls >= 1, "a different output language is a different question"


@pytest.mark.asyncio
async def test_a_corrupt_cache_row_is_a_miss(repo, store):
    """A bad blob reads as "no cache", never as a broken or empty review."""
    cache = StoreUnitCache(store)
    await _review(repo, CountingProvider(line=3, snippet="    return x[0]\n"), cache)
    with store._conn() as conn:
        conn.execute("UPDATE unit_cache SET review_json='{\"nonsense\": 1}'")

    provider = CountingProvider(line=3, snippet="    return x[0]\n")
    await _review(repo, provider, StoreUnitCache(store))
    assert provider.calls >= 1, "an unparseable cache row must fall through to a real call"


@pytest.mark.asyncio
async def test_no_cache_means_no_rows(repo, store):
    """With the cache off the review is byte-for-byte the old behaviour."""
    provider = CountingProvider(line=3, snippet="    return x[0]\n")
    await _review(repo, provider, None)
    await _review(repo, provider, None)
    assert provider.calls >= 2
    assert store.unit_cache_stats()["entries"] == 0


def test_the_env_kill_switch_works(monkeypatch):
    monkeypatch.delenv("CODEERRATUM_UNIT_CACHE", raising=False)
    assert unit_cache_enabled() is True
    for off in ("0", "false", "no", "off", "FALSE"):
        monkeypatch.setenv("CODEERRATUM_UNIT_CACHE", off)
        assert unit_cache_enabled() is False


def test_cache_key_isolates_everything_that_changes_the_answer():
    messages = [{"role": "user", "content": "x"}]
    same = cache_key("m", messages, None, 0.0, 2048)
    assert same == cache_key("m", messages, None, 0.0, 2048)
    assert same != cache_key("other", messages, None, 0.0, 2048)
    assert same != cache_key("m", messages, None, 0.7, 2048)
    assert same != cache_key("m", messages, None, 0.0, 4096)
    assert same != cache_key("m", messages, LLMReview, 0.0, 2048)
    assert same != cache_key("m", [{"role": "user", "content": "y"}], None, 0.0, 2048)


def test_cache_is_bounded(store, monkeypatch):
    """A server that never restarts cannot be allowed to grow this forever."""
    from codeerratum.store import sqlite as store_sqlite

    # Stamped inside the TTL: with an expired created_at every row is dropped by the
    # age branch and the row-count branch is never reached, so this test would pass
    # while proving nothing.
    cap = 50
    monkeypatch.setattr(store_sqlite, "UNIT_CACHE_MAX_ROWS", cap)
    for i in range(cap + 25):
        store.put_unit_cache(f"k{i}", "m", {"summary": "s"}, _now())
    assert store.unit_cache_stats()["entries"] == cap


def test_stale_rows_are_dropped_on_write(store):
    """An expired row never answers, and writing it is what removes it."""
    store.put_unit_cache("stale", "m", {"summary": "s"}, "2000-01-01T00:00:00+00:00")
    assert store.get_unit_cache("stale") is None, "past the TTL it must not answer"
    assert store.unit_cache_stats()["entries"] == 0, "the write itself evicts it"

    store.put_unit_cache("fresh", "m", {"summary": "s"}, _now())
    assert store.get_unit_cache("fresh") == {"summary": "s"}
    assert store.unit_cache_stats()["entries"] == 1


def test_the_ttl_bound_is_format_consistent(store):
    """The TTL compares text, so the bound and the stamp must share one format.

    SQLite orders TEXT byte-wise. Comparing a ``T``-separated stamp against
    ``datetime('now')`` (space-separated) puts every same-day row on the wrong side of
    the bound; this pins the two formats together so a change to either breaks here.
    """
    just_inside = (datetime.now(UTC) - timedelta(days=UNIT_CACHE_TTL_DAYS - 1)).isoformat(
        timespec="seconds"
    )
    just_outside = (datetime.now(UTC) - timedelta(days=UNIT_CACHE_TTL_DAYS + 1)).isoformat(
        timespec="seconds"
    )
    store.put_unit_cache("inside", "m", {"summary": "in"}, just_inside)
    assert store.get_unit_cache("inside") == {"summary": "in"}, "a 119-day row is still valid"

    store.put_unit_cache("outside", "m", {"summary": "out"}, just_outside)
    assert store.get_unit_cache("outside") is None


def test_hits_are_counted_per_entry(store):
    store.put_unit_cache("k", "m", {"summary": "s"}, _now())
    store.note_unit_cache_hit("k")
    store.note_unit_cache_hit("k")
    assert store.unit_cache_stats()["hits"] == 2


@pytest.mark.asyncio
async def test_the_tiered_verify_stage_is_cached_too(repo, store, monkeypatch):
    """A reused unit must be free in the second opinion as well as the scan.

    The cache was once wired into the scan call but passed to the tiered verifier as
    a name that does not exist. ``review_unit`` raised NameError, the orchestrator
    recorded it as ``verify_error``, and every tiered run silently lost all of its
    verification while still looking like a normal report. Running the flow twice
    pins both halves: the verdict merges, and the second pass pays for nothing.
    """
    import codeerratum.review.orchestrator as orch
    from codeerratum.models import ModelRegistry, ModelSpec
    from codeerratum.review import run_review

    units = split_diff(get_diff("HEAD", repo), repo)
    lines = (repo / "m.py").read_text().splitlines(keepends=True)
    line_no = next(i for i, line in enumerate(lines, 1) if "return x[0]" in line)
    snippet = lines[line_no - 1]

    for round_no in (1, 2):
        stubs = {
            "qwen3-flash": CountingProvider(line=line_no, snippet=snippet),
            "qwen-max-preview": CountingProvider(line=line_no, snippet=snippet),
        }
        monkeypatch.setattr(orch, "_make_provider", lambda spec, _t=stubs: _t[spec.model_id])
        registry = ModelRegistry(
            models=[
                ModelSpec(model_id="qwen-max-preview", base_url="http://a"),
                ModelSpec(model_id="qwen3-flash", base_url="http://b"),
            ]
        )
        collection, calls, _ = await run_review(
            registry,
            "quick",
            units,
            repo,
            f"run-tier-{round_no}",
            pinned=["qwen-max-preview"],
            tiered=True,
            unit_cache=StoreUnitCache(store),
        )
        assert collection.rejected == [], f"tiered round {round_no} dropped something"
        assert set(collection.findings[0].source_models) == {
            "qwen3-flash",
            "qwen-max-preview",
        }, f"round {round_no}: the verifier never agreed"

        if round_no == 1:
            assert any(c.get("role") == "verify" for c in calls)
            assert all(not c.get("cached") for c in calls)
        else:
            assert all(c.get("cached") for c in calls), "a cached tiered run is all replay"
            assert all(c["usage"]["input_tokens"] == 0 for c in calls)
            for stub in stubs.values():
                assert stub.calls == 0
