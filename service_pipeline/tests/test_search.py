"""Tests for tern.search module."""
from __future__ import annotations

import pytest

from tern.models import SearchHit, SearchQuery
from tern.search import sanitize_fts_query, SearchEngine, _pick_best_snippet


def test_sanitize_fts_query_basic():
    # Search-as-you-type: the last token gets a prefix-wildcard wrapped in
    # `(token* OR token)` so a Porter-stemmed index still matches the bare
    # form. Empty input returns the empty-match sentinel.
    sanitized = sanitize_fts_query("pricing strategy")
    assert "pricing" in sanitized
    assert "strategy" in sanitized
    assert "strategy*" in sanitized
    assert sanitize_fts_query("") == '""'


def test_sanitize_fts_query_strips_special():
    """FTS5 has reserved characters; we should strip them safely."""
    result = sanitize_fts_query("pricing & strategy! 100%")
    # Special chars stripped, quotes preserved
    assert "&" not in result
    assert "!" not in result
    assert "%" not in result
    assert "pricing" in result.lower()
    assert "strategy" in result.lower()


def test_sanitize_fts_query_preserves_quotes():
    result = sanitize_fts_query('"exact match" optional')
    assert '"' in result


def test_sanitize_fts_query_unbalanced_quote_doesnt_crash():
    """Edge: user-typed query with unbalanced quote (open quote, no
    close) must not crash and must NOT produce a FTS5 syntax error
    downstream. We balance the quote count so search-as-you-type still
    returns hits while the user is mid-typing a phrase — without the
    fix, FTS5 raises `unterminated string`, search.py swallows it, and
    the transcript/OCR buckets return zero hits with no error surfaced
    to the user."""
    # Open quote at start
    out = sanitize_fts_query('"pricing strategy')
    assert isinstance(out, str)
    assert out  # non-empty
    # Quotes get balanced (1 → 2) so the query is a valid FTS5 phrase
    assert out.count('"') % 2 == 0, f"odd quote count survives: {out!r}"
    # The branch `if '"' in raw: return sanitized` means no wildcard
    # gets appended — exact passthrough (plus the balancing quote).
    assert "*" not in out

    # Quote in the middle
    out2 = sanitize_fts_query('first "quoted partial')
    assert isinstance(out2, str)
    assert out2
    assert out2.count('"') % 2 == 0, f"odd quote count survives: {out2!r}"
    assert "*" not in out2

    # The fix matters because the un-balanced form actually crashes
    # SQLite's FTS5 MATCH parser. Pin that behaviour end-to-end so a
    # future "simplification" that drops the balancer regresses loudly.
    import sqlite3
    c = sqlite3.connect(":memory:")
    c.execute("CREATE VIRTUAL TABLE t USING fts5(content)")
    c.execute("INSERT INTO t VALUES ('pricing strategy session')")
    # Sanity: the raw unbalanced query DOES raise — that's why we need the fix.
    with pytest.raises(sqlite3.OperationalError):
        list(c.execute("SELECT rowid FROM t WHERE t MATCH ?", ('"pricing strategy',)))
    # The sanitized form must round-trip through FTS5 without raising.
    rows = list(c.execute("SELECT rowid FROM t WHERE t MATCH ?", (out,)))
    assert rows == [(1,)], f"sanitized query {out!r} should find the seeded row"


def test_sanitize_fts_query_only_special_chars_returns_empty_sentinel():
    """Edge: a query that's nothing but FTS5-special chars (`*()` etc.)
    sanitizes to empty → must return the '""' sentinel (won't error on
    MATCH). Complements test_sanitize_fts_query_empty which covers
    literal empty / whitespace / `!!!`."""
    assert sanitize_fts_query("*()") == '""'
    assert sanitize_fts_query(":^&%") == '""'


def test_sanitize_fts_query_wildcard_appended_only_for_2plus_chars():
    """The prefix-wildcard branch requires len(last_token) >= 2 —
    single-char wildcards explode FTS5 result counts. Verify the
    boundary: 1-char trailing word doesn't get `*`, 2-char does."""
    # 1-char trailing token → no wildcard branch
    out1 = sanitize_fts_query("a")
    assert "*" not in out1, f"expected no wildcard on single char, got: {out1!r}"

    # 2-char trailing token → wildcard gets appended
    out2 = sanitize_fts_query("ab")
    assert "*" in out2, f"expected wildcard on 2-char token, got: {out2!r}"


def test_sanitize_fts_query_dash_does_not_crash_fts5():
    """Regression: any user query containing `-` USED to crash FTS5 silently.

    FTS5 interprets `-token` as a NOT operator and `word-word` as `word - word`,
    so realistic inputs like "covid-19", "back-end", "tax-free", or even an
    intentional "-free" produced `OperationalError: no such column: 19`.
    search.py's per-channel try/except swallows the error, so the transcript
    and OCR buckets returned ZERO hits with nothing visible to the user (the
    visual channel still ran since it doesn't go through FTS5 — so the result
    list wasn't empty enough to tip them off).

    Fix: FTS5_STRIP now treats `-` like any other special char (turns it into
    a space). Pin that behavior end-to-end so a future "let's let users type
    NOT operators directly" change resurfaces this regression loudly.
    """
    import sqlite3
    c = sqlite3.connect(":memory:")
    c.execute("CREATE VIRTUAL TABLE t USING fts5(content)")
    c.execute("INSERT INTO t VALUES ('the covid 19 pandemic')")
    c.execute("INSERT INTO t VALUES ('back end engineer')")
    c.execute("INSERT INTO t VALUES ('tax free shopping')")
    c.execute("INSERT INTO t VALUES ('pricing strategy is free now')")

    # Each query MUST sanitize to a valid FTS5 expression — no exception.
    queries = [
        ("covid-19", "the covid 19 pandemic"),
        ("back-end", "back end engineer"),
        ("tax-free", "tax free shopping"),
        # Leading dash (someone typing `-foo` to mean "exclude") MUST also
        # not crash; sanitized form just AND-s the words now.
        ("pricing -free", "pricing strategy is free now"),
        # Pure-dash garbage must collapse to the empty-match sentinel
        # rather than fall through to `WHERE t MATCH '-'`.
        ("-", None),
        ("-- --", None),
    ]
    for raw, expected_match_text in queries:
        sanitized = sanitize_fts_query(raw)
        # No raw dash should survive the strip.
        assert "-" not in sanitized, (
            f"dash leaked through sanitization for {raw!r}: {sanitized!r}"
        )
        try:
            rows = list(c.execute(
                "SELECT content FROM t WHERE t MATCH ?", (sanitized,)
            ))
        except sqlite3.OperationalError as e:
            raise AssertionError(
                f"sanitized query {sanitized!r} (from {raw!r}) crashed FTS5: {e}"
            )
        if expected_match_text is not None:
            assert (expected_match_text,) in rows, (
                f"sanitized {sanitized!r} (from {raw!r}) didn't find "
                f"expected row {expected_match_text!r}; got {rows!r}"
            )


def test_sanitize_fts_query_trailing_space_disables_wildcard():
    """Docstring on sanitize_fts_query says the wildcard is only added
    when the user "looks like they're still typing" — i.e., the raw
    query doesn't end with whitespace. Verify: trailing space means
    the user finished the word, so no wildcard should be appended."""
    no_space = sanitize_fts_query("pricing strategy")
    with_space = sanitize_fts_query("pricing strategy ")
    assert "*" in no_space
    assert "*" not in with_space, f"trailing-space query unexpectedly got wildcard: {with_space!r}"


def test_reweight_and_dedupe():
    hits = [
        SearchHit(file_id=1, file_path="/a.mp3", ts_ms=1000, duration_ms=2000,
                  snippet="match a", source="transcript", score=0.5),
        SearchHit(file_id=1, file_path="/a.mp3", ts_ms=1500, duration_ms=2000,
                  snippet="match b", source="ocr", score=0.4),  # close ts, should dedupe
        SearchHit(file_id=2, file_path="/b.mp3", ts_ms=5000, duration_ms=2000,
                  snippet="match c", source="visual", score=0.6),
    ]
    weights = {"transcript": 1.0, "ocr": 0.5, "visual": 1.0}
    merged = SearchEngine._reweight_and_dedupe(hits, weights, dedupe_window_ms=5000)

    # File 1 should be one merged hit, file 2 a separate hit
    assert len(merged) == 2
    file_ids = {h.file_id for h in merged}
    assert file_ids == {1, 2}


def test_reweight_applies_weights():
    """A hit with low weight should be scored lower."""
    hits = [
        SearchHit(file_id=1, file_path="/a.mp3", ts_ms=1000, duration_ms=2000,
                  snippet="transcript hit", source="transcript", score=0.5),
        SearchHit(file_id=2, file_path="/b.mp3", ts_ms=2000, duration_ms=2000,
                  snippet="ocr hit", source="ocr", score=0.5),
    ]
    weights = {"transcript": 1.0, "ocr": 0.1}
    merged = SearchEngine._reweight_and_dedupe(hits, weights, dedupe_window_ms=5000)

    # Transcript hit should have higher score after weighting
    sorted_hits = sorted(merged, key=lambda h: h.score, reverse=True)
    assert sorted_hits[0].source == "transcript"


def test_search_query_model_defaults():
    query = SearchQuery(query="test")
    assert query.limit == 50
    assert "transcript" in query.sources
    assert "visual" in query.sources
    assert "ocr" in query.sources


def test_reweight_picks_snippet_by_source_priority():
    """When multiple sources hit the same bucket, the snippet picker
    should choose transcript > OCR > visual, NOT join them with ' | '."""
    hits = [
        SearchHit(file_id=1, file_path="/a.mp4", ts_ms=1000, duration_ms=2000,
                  snippet="Nine years ago I was a Stanford student", source="transcript", score=0.6),
        SearchHit(file_id=1, file_path="/a.mp4", ts_ms=1200, duration_ms=2000,
                  snippet="Stanford · University", source="ocr", score=0.4),
        SearchHit(file_id=1, file_path="/a.mp4", ts_ms=1800, duration_ms=2000,
                  snippet=None, source="visual", score=0.2),
    ]
    merged = SearchEngine._reweight_and_dedupe(hits, {"transcript":1.0,"ocr":0.5,"visual":1.0})
    assert len(merged) == 1
    assert merged[0].source == "multi"
    assert merged[0].snippet.startswith("Nine years ago")  # transcript wins
    # sources list tracks the actual contributors
    assert set(merged[0].sources) == {"transcript", "ocr", "visual"}


# ─── _reweight_and_dedupe multi-source bonus math ─────────────────────
# The "speech + on-screen + visual all agreeing" boost was added in the
# multi-source UX work but never had a focused test. The math is:
#   bonus = min(other_top_score * 0.5, base * 0.5)
# Two failure modes a future refactor could silently introduce:
#   1. Bonus larger than base — would over-rank multi-source coincidences
#      against a single very-strong hit. The min() with `base * 0.5`
#      prevents that.
#   2. No bonus at all when sources are distinct — would erase the entire
#      "agreement is signal" premise.

def test_reweight_multi_source_bonus_is_added():
    """Two distinct sources in the same bucket → score > best.score alone."""
    hits = [
        SearchHit(file_id=1, file_path="/a.mp4", ts_ms=1000, duration_ms=2000,
                  snippet="transcript hit", source="transcript", score=0.8),
        SearchHit(file_id=1, file_path="/a.mp4", ts_ms=1200, duration_ms=2000,
                  snippet="ocr hit", source="ocr", score=0.4),
    ]
    merged = SearchEngine._reweight_and_dedupe(hits, {"transcript":1.0,"ocr":1.0})
    assert len(merged) == 1
    # base=0.8, other=0.4 → bonus = min(0.4*0.5, 0.8*0.5) = 0.2
    # final = 0.8 + 0.2 = 1.0
    assert abs(merged[0].score - 1.0) < 1e-6, f"expected 1.0, got {merged[0].score}"

def test_reweight_multi_source_bonus_capped_at_half_base():
    """If the second-best source's score > base * 0.5, the bonus is
    clamped to base * 0.5 so a swarm of mid-quality OCR hits can't
    out-rank a hit with strong single-source evidence."""
    hits = [
        SearchHit(file_id=1, file_path="/a.mp4", ts_ms=1000, duration_ms=2000,
                  snippet="transcript hit", source="transcript", score=0.6),
        SearchHit(file_id=1, file_path="/a.mp4", ts_ms=1200, duration_ms=2000,
                  snippet="ocr hit", source="ocr", score=0.55),  # 0.55*0.5=0.275, capped at 0.6*0.5=0.3
    ]
    merged = SearchEngine._reweight_and_dedupe(hits, {"transcript":1.0,"ocr":1.0})
    # base=0.6, other=0.55 → min(0.55*0.5, 0.6*0.5) = min(0.275, 0.3) = 0.275
    assert abs(merged[0].score - 0.875) < 1e-6, f"expected 0.875, got {merged[0].score}"

def test_reweight_sets_source_multi_and_sorted_sources_list():
    """When a bucket has 2+ distinct sources, the output hit's `source`
    field MUST be the literal string "multi" and `sources` MUST be the
    alphabetically-sorted list of contributing sources. The frontend
    (results.js + row.js) reads these to render the "speech + on-screen"
    badge — if `source` ever drifts away from "multi" the bucketed badge
    silently disappears, and if `sources` ordering is non-deterministic
    the badge text reorders between identical searches.

    Previously no test pinned either field. The math tests above
    only checked `score` — if a future refactor returned `best.source`
    unchanged (the highest-scorer's literal source) the math would still
    pass but the UI badge would lose its multi-source signal."""
    hits = [
        SearchHit(file_id=1, file_path="/a.mp4", ts_ms=1000, duration_ms=2000,
                  snippet="t", source="transcript", score=0.6),
        SearchHit(file_id=1, file_path="/a.mp4", ts_ms=1200, duration_ms=2000,
                  snippet="o", source="ocr", score=0.4),
        SearchHit(file_id=1, file_path="/a.mp4", ts_ms=1500, duration_ms=2000,
                  snippet="v", source="visual", score=0.3),
    ]
    merged = SearchEngine._reweight_and_dedupe(hits, {"transcript":1.0,"ocr":1.0,"visual":1.0})
    assert len(merged) == 1
    h = merged[0]
    assert h.source == "multi", (
        f'merged-bucket source should be the literal "multi", got {h.source!r}'
    )
    # sorted() on the {"transcript", "ocr", "visual"} set: alphabetical
    # → ["ocr", "transcript", "visual"]
    assert h.sources == ["ocr", "transcript", "visual"], (
        f"sources should be alphabetically sorted; got {h.sources!r}"
    )


def test_reweight_does_not_merge_across_files():
    """Two hits with the SAME ts_ms but DIFFERENT file_id MUST NOT be
    merged — the bucket key is (file_id, ts_bucket), so a transcript
    hit at 10s in podcast-A and an OCR hit at 10s in lecture-B stay
    separate. Easy to break if a future refactor accidentally buckets
    on ts_ms alone (e.g. switching to a per-window cross-corpus
    multi-source fusion experiment). Two hits at the SAME ts in
    different files would also share a window, which is the exact
    bug this guards against."""
    hits = [
        SearchHit(file_id=1, file_path="/a.mp4", ts_ms=10_000, duration_ms=2000,
                  snippet="A", source="transcript", score=0.7),
        SearchHit(file_id=2, file_path="/b.mp4", ts_ms=10_000, duration_ms=2000,
                  snippet="B", source="ocr", score=0.6),
    ]
    merged = SearchEngine._reweight_and_dedupe(hits, {"transcript":1.0,"ocr":1.0})
    # MUST stay as 2 separate hits (one per file), not collapsed to one
    # cross-file "multi" hit.
    assert len(merged) == 2, (
        f"cross-file hits must NOT merge; got {len(merged)} hits: {[(h.file_id, h.source) for h in merged]}"
    )
    # Neither should be relabeled "multi" — they're still single-source per file.
    for h in merged:
        assert h.source != "multi", (
            f"cross-file hit was relabeled 'multi'; got source={h.source!r} for file_id={h.file_id}"
        )


def test_reweight_custom_dedupe_window_widens_bucket():
    """dedupe_window_ms parameter widens or narrows the time-bucket
    granularity. Pin behavior at two opposite settings: 1000 ms (tight,
    won't dedupe two hits 2 s apart) vs 60_000 ms (loose, dedupes
    hits up to a minute apart). Catches a regression where a future
    refactor hardcodes 5000 ms or flips the // direction."""
    hits = [
        SearchHit(file_id=1, file_path="/a.mp4", ts_ms=1000, duration_ms=2000,
                  snippet="a", source="transcript", score=0.5),
        SearchHit(file_id=1, file_path="/a.mp4", ts_ms=3000, duration_ms=2000,
                  snippet="b", source="ocr", score=0.5),
    ]
    # Tight window (1000 ms): 1000//1000=1 and 3000//1000=3 → different
    # buckets → no merge.
    tight = SearchEngine._reweight_and_dedupe(hits, {"transcript":1.0,"ocr":1.0}, dedupe_window_ms=1000)
    assert len(tight) == 2, (
        f"tight window should keep hits separate; got {len(tight)} hits"
    )
    # Loose window (60_000 ms): both 1000//60000=0 and 3000//60000=0 →
    # same bucket → merge to 1 hit with multi-source label.
    loose = SearchEngine._reweight_and_dedupe(hits, {"transcript":1.0,"ocr":1.0}, dedupe_window_ms=60_000)
    assert len(loose) == 1, (
        f"loose window should merge; got {len(loose)} hits"
    )
    assert loose[0].source == "multi"


def test_reweight_no_bonus_when_single_source():
    """Two hits, same source — no multi-source agreement, no bonus."""
    hits = [
        SearchHit(file_id=1, file_path="/a.mp4", ts_ms=1000, duration_ms=2000,
                  snippet="hit a", source="transcript", score=0.8),
        SearchHit(file_id=1, file_path="/a.mp4", ts_ms=1200, duration_ms=2000,
                  snippet="hit b", source="transcript", score=0.5),
    ]
    merged = SearchEngine._reweight_and_dedupe(hits, {"transcript":1.0})
    assert len(merged) == 1
    assert abs(merged[0].score - 0.8) < 1e-6, f"single-source must not get bonus; got {merged[0].score}"
    assert merged[0].source == "transcript"
    assert merged[0].sources == ["transcript"]


# ─── _apply_per_file_cap — result-page diversity guard ────────────────
# Without the cap, a densely-matching file (e.g. a one-hour podcast where
# the query word recurs every 30 s) can fill the entire result page with
# just its own moments. Function was load-bearing UX but untested.

def test_per_file_cap_zero_returns_all():
    """cap <= 0 disables the limit entirely (pass-through)."""
    hits = [
        SearchHit(file_id=1, file_path="/a", ts_ms=i*1000, duration_ms=2000,
                  snippet=None, source="transcript", score=0.5)
        for i in range(10)
    ]
    assert len(SearchEngine._apply_per_file_cap(hits, 0))  == 10
    assert len(SearchEngine._apply_per_file_cap(hits, -1)) == 10

def test_per_file_cap_limits_per_file_not_total():
    """5 hits from file 1 + 3 from file 2, cap=2 → 2 + 2 = 4 hits."""
    hits = (
        [SearchHit(file_id=1, file_path="/a", ts_ms=i*1000, duration_ms=2000,
                   snippet=None, source="transcript", score=1.0 - i*0.01)
         for i in range(5)]
        +
        [SearchHit(file_id=2, file_path="/b", ts_ms=i*1000, duration_ms=2000,
                   snippet=None, source="transcript", score=0.9 - i*0.01)
         for i in range(3)]
    )
    capped = SearchEngine._apply_per_file_cap(hits, 2)
    assert len(capped) == 4
    by_file = {fid: sum(1 for h in capped if h.file_id == fid) for fid in (1, 2)}
    assert by_file == {1: 2, 2: 2}

def test_per_file_cap_preserves_input_order():
    """Order must be the descending-score order the caller already
    established — the cap is "keep first N per file", not "re-sort"."""
    # Interleave file 1 / file 2 / file 1 / file 1 / file 2
    hits = [
        SearchHit(file_id=1, file_path="/a", ts_ms=0, duration_ms=2000,
                  snippet=None, source="transcript", score=0.9),
        SearchHit(file_id=2, file_path="/b", ts_ms=0, duration_ms=2000,
                  snippet=None, source="transcript", score=0.85),
        SearchHit(file_id=1, file_path="/a", ts_ms=1000, duration_ms=2000,
                  snippet=None, source="transcript", score=0.8),
        SearchHit(file_id=1, file_path="/a", ts_ms=2000, duration_ms=2000,
                  snippet=None, source="transcript", score=0.7),
        SearchHit(file_id=2, file_path="/b", ts_ms=1000, duration_ms=2000,
                  snippet=None, source="transcript", score=0.6),
    ]
    capped = SearchEngine._apply_per_file_cap(hits, 2)
    # File 1 caps at the first two (scores 0.9, 0.8); file 2 keeps both.
    assert [h.ts_ms for h in capped] == [0, 0, 1000, 1000]


def test_reweight_drops_visual_placeholder_when_text_available():
    """Pure visual snippet at "[visual match at Xs]" gets dropped
    when there's any text source in the bucket."""
    hits = [
        SearchHit(file_id=1, file_path="/a.mp4", ts_ms=1000, duration_ms=2000,
                  snippet="some text", source="ocr", score=0.5),
        SearchHit(file_id=1, file_path="/a.mp4", ts_ms=1200, duration_ms=2000,
                  snippet="[visual match at 1s]", source="visual", score=0.4),
    ]
    merged = SearchEngine._reweight_and_dedupe(hits, {"ocr":1.0,"visual":1.0})
    assert len(merged) == 1
    assert merged[0].snippet == "some text"
    assert "visual match" not in (merged[0].snippet or "")


def test_filename_boost_lifts_basename_match():
    """A hit whose file basename contains a query token gets a +20% nudge
    so namesake-photo queries float to the top."""
    hits = [
        SearchHit(file_id=1, file_path="/x/picsum_mountain.jpg", ts_ms=0, duration_ms=2000,
                  snippet=None, source="visual", score=0.10),
        SearchHit(file_id=2, file_path="/x/picsum_office.jpg", ts_ms=0, duration_ms=2000,
                  snippet=None, source="visual", score=0.10),
    ]
    SearchEngine._apply_filename_boost(hits, "mountain")
    # mountain file gets boosted to 0.12, office stays at 0.10
    by_path = {h.file_path: h.score for h in hits}
    assert by_path["/x/picsum_mountain.jpg"] > by_path["/x/picsum_office.jpg"]
    assert abs(by_path["/x/picsum_mountain.jpg"] - 0.12) < 1e-6


def test_filename_boost_token_separator_aware():
    """Boost matches whole-word OR substring within tokenized basename.
    `IMG_iphone_15_holiday.heic` matches `iphone` after underscore split."""
    hits = [
        SearchHit(file_id=1, file_path="/x/IMG_iphone_15_holiday.heic", ts_ms=0,
                  duration_ms=2000, snippet=None, source="visual", score=0.10),
        SearchHit(file_id=2, file_path="/x/dog_park_walk.jpg", ts_ms=0,
                  duration_ms=2000, snippet=None, source="visual", score=0.10),
    ]
    SearchEngine._apply_filename_boost(hits, "iphone")
    by_path = {h.file_path: h.score for h in hits}
    assert by_path["/x/IMG_iphone_15_holiday.heic"] > by_path["/x/dog_park_walk.jpg"]


def test_filename_boost_short_tokens_ignored():
    """Tokens shorter than 3 chars don't trigger boost (would be false
    positives on every short letter combination)."""
    hits = [
        SearchHit(file_id=1, file_path="/x/aaa_album.jpg", ts_ms=0,
                  duration_ms=2000, snippet=None, source="visual", score=0.10),
    ]
    SearchEngine._apply_filename_boost(hits, "a")
    assert hits[0].score == 0.10  # unchanged


def test_filename_boost_unicode_basename_and_query():
    """Real-world: Russian users index `~/Документы/Подкасты/` with files
    like `интервью_бюджет_2024.mp3` and search for "бюджет". The basename
    boost MUST trigger on the Cyrillic substring match — i.e. the
    `\\w+` token regex must use Python's default Unicode-aware mode
    (NOT `re.ASCII`), and `Path.stem.lower()` must round-trip Cyrillic.

    A future refactor that "tightens" the tokenizer with `re.ASCII`
    (a common drive-by mistake — perf optimization or "stricter
    validation" intent) would silently kill filename-boost for every
    non-Latin-alphabet user and they'd never report it as a bug
    (just "search seems random"). Pin the behaviour for the dominant
    non-Latin user base we know about (Russian / Ukrainian / Greek /
    Hebrew etc. — anything where lower() and \\w work natively at
    3+ char token length).

    NOTE: this test covers the Latin >=3 filter; 2-char CJK words like
    `会議` are allowed through by the CJK carve-out (see `_token_has_cjk`
    in search.py). The dedicated CJK test below covers that behaviour."""
    hits = [
        # Cyrillic basename with Cyrillic query token
        SearchHit(file_id=1, file_path="/x/интервью_бюджет_2024.mp3", ts_ms=0,
                  duration_ms=2000, snippet=None, source="transcript", score=0.10),
        # Latin control — confirms boost machinery itself works in this test
        SearchHit(file_id=2, file_path="/x/meeting_budget.m4a", ts_ms=0,
                  duration_ms=2000, snippet=None, source="transcript", score=0.10),
        # Cyrillic basename but Latin query — must NOT boost (cross-script
        # transliteration is NOT a feature of the tokenizer)
        SearchHit(file_id=3, file_path="/x/интервью_бюджет_2024.mp3", ts_ms=4000,
                  duration_ms=2000, snippet=None, source="transcript", score=0.10),
    ]

    # Cyrillic query — must boost hit 1. This is the core regression
    # guard: a future `re.ASCII` in FILENAME_BOOST_TOKEN would make
    # `re.findall(r"\\w+", "бюджет")` return [] (ASCII-only matcher
    # rejects non-Latin), no tokens, no boost. The assertion catches
    # that drift.
    SearchEngine._apply_filename_boost(hits[:1], "бюджет")
    assert abs(hits[0].score - 0.12) < 1e-6, (
        f"Cyrillic basename+query boost failed: {hits[0].score} "
        "(re.ASCII may have been added to FILENAME_BOOST tokenizer)"
    )

    # Latin control — confirms the test setup itself isn't broken
    SearchEngine._apply_filename_boost(hits[1:2], "meeting")
    assert abs(hits[1].score - 0.12) < 1e-6

    # Cross-script: Cyrillic basename, Latin query — must NOT boost.
    # Latin "byudzhet" should NOT substring-match Cyrillic "бюджет" even
    # though they're transliterations of the same word. Character
    # identity, not phonetic equivalence.
    SearchEngine._apply_filename_boost(hits[2:3], "byudzhet")
    assert hits[2].score == 0.10, (
        f"cross-script false-positive: Latin 'byudzhet' boosted Cyrillic basename: "
        f"{hits[2].score} (expected unchanged 0.10)"
    )


def test_filename_boost_cjk_two_char_words_eligible():
    """CJK / Japanese / Korean carve-out: 2-character tokens DO boost
    when they contain a CJK character. Japanese 会議 ("meeting"),
    Chinese 经济 ("economy"), Korean 회사 ("company") are full
    standalone words at exactly 2 chars — the Latin >=3 minimum
    (which exists to reject `if`/`to`/`or` noise) would silently
    block filename-boost for the most common search terms in these
    languages.

    Pin the carve-out behaviour: 2-char CJK tokens DO match, but
    2-char Latin tokens still DON'T. Catches a future "simplify the
    length filter" refactor that would re-introduce the gap.
    """
    hits_jp = [
        SearchHit(file_id=1, file_path="/x/会議_2024_予算.m4a", ts_ms=0,
                  duration_ms=2000, snippet=None, source="transcript", score=0.10),
    ]
    SearchEngine._apply_filename_boost(hits_jp, "会議")
    assert abs(hits_jp[0].score - 0.12) < 1e-6, (
        f"Japanese 会議 (2 chars, CJK) must boost basename containing it: "
        f"{hits_jp[0].score} (expected 0.12 = 0.10 * 1.20)"
    )

    hits_ko = [
        SearchHit(file_id=2, file_path="/x/회사_미팅_2024.mp3", ts_ms=0,
                  duration_ms=2000, snippet=None, source="transcript", score=0.10),
    ]
    SearchEngine._apply_filename_boost(hits_ko, "회사")
    assert abs(hits_ko[0].score - 0.12) < 1e-6, (
        f"Korean 회사 (2 chars, Hangul) must boost: {hits_ko[0].score}"
    )

    # Negative control: 2-char Latin token MUST NOT slip through the
    # carve-out. Without the `(len(t) >= 2 and _token_has_cjk(t))`
    # branch's `_token_has_cjk` guard, lowering the global minimum to
    # 2 would falsely boost on `if`/`to`/`or`.
    hits_en = [
        SearchHit(file_id=3, file_path="/x/if_we_meet_tomorrow.mp3", ts_ms=0,
                  duration_ms=2000, snippet=None, source="transcript", score=0.10),
    ]
    SearchEngine._apply_filename_boost(hits_en, "if")
    assert hits_en[0].score == 0.10, (
        f"2-char Latin token 'if' must NOT boost (no CJK carve-out): "
        f"{hits_en[0].score} (expected unchanged 0.10)"
    )


def test_token_has_cjk_script_block_coverage():
    """_token_has_cjk recognizes the 4 script blocks we care about:
    CJK Unified Ideographs (Chinese / Japanese kanji), Hiragana,
    Katakana, Hangul Syllables. Pin each block by codepoint range
    boundary so a future refactor narrowing the ranges fails loudly."""
    assert SearchEngine._token_has_cjk("会議") is True   # CJK Ideographs
    assert SearchEngine._token_has_cjk("ひらがな") is True  # Hiragana
    assert SearchEngine._token_has_cjk("カタカナ") is True  # Katakana
    assert SearchEngine._token_has_cjk("회사") is True   # Hangul
    # Mixed-script: even one CJK char in a longer token qualifies
    assert SearchEngine._token_has_cjk("hello会議") is True
    # Pure Latin / Cyrillic / Greek / Arabic — false (no CJK chars)
    assert SearchEngine._token_has_cjk("meeting") is False
    assert SearchEngine._token_has_cjk("совещание") is False
    assert SearchEngine._token_has_cjk("συνάντηση") is False
    assert SearchEngine._token_has_cjk("اجتماع") is False
    # Edge: empty string → false (no chars to match)
    assert SearchEngine._token_has_cjk("") is False


def test_sanitize_fts_query_empty():
    """Edge: empty / whitespace-only / only-punctuation queries safely
    return the empty-match sentinel (won't crash FTS5)."""
    assert sanitize_fts_query("") == '""'
    assert sanitize_fts_query("   ") == '""'
    assert sanitize_fts_query("!!!") == '""'


# ─── _pick_best_snippet — bucket-snippet selection logic ──────────────
# This helper runs once per dedupe bucket (≤ query.limit times per search)
# and decides which evidence the user actually sees. Untested before now;
# easy to silently regress in a future "simplify the priority dict" pass.


def _hit(source: str, snippet: str | None) -> SearchHit:
    """Minimal SearchHit factory used only by the snippet-priority tests."""
    return SearchHit(
        file_id=1, file_path="/a.mp4", ts_ms=0, duration_ms=2000,
        snippet=snippet, source=source, score=0.5,
    )


def test_pick_best_snippet_empty_bucket_returns_none():
    """Edge: empty input → None (caller falls back to its own placeholder)."""
    assert _pick_best_snippet([]) is None


def test_pick_best_snippet_prefers_transcript_over_ocr_and_visual():
    """Transcript wins — it's typically a full sentence with context, far more
    scannable than an OCR fragment or a visual placeholder. Pins the priority
    order (transcript < ocr < visual numerically) so a future "let me alphabet-
    sort the priority dict" refactor breaks loudly."""
    bucket = [
        _hit("ocr", "MOUNTAIN VIEW"),
        _hit("transcript", "and then we drove past the mountain"),
        _hit("visual", "[visual match at 12s]"),
    ]
    assert _pick_best_snippet(bucket) == "and then we drove past the mountain"


def test_pick_best_snippet_ocr_wins_when_no_transcript():
    """No transcript → OCR is the best text evidence available. The visual
    placeholder must NOT outrank OCR even if it's the only other hit."""
    bucket = [
        _hit("visual", "[visual match at 12s]"),
        _hit("ocr", "Series A Funding · Slide 3"),
    ]
    assert _pick_best_snippet(bucket) == "Series A Funding · Slide 3"


def test_pick_best_snippet_visual_only_returns_none_to_drop_placeholder():
    """A visual-only bucket has nothing meaningful to show as text — the raw
    `[visual match at Ns]` is implementation detail leaking into the UI. Return
    None and let the frontend render its own "Visual match" affordance.
    Without this guard the user would see literal `[visual match at 12s]`
    text in the result list — looks broken."""
    bucket = [_hit("visual", "[visual match at 12s]")]
    assert _pick_best_snippet(bucket) is None


def test_pick_best_snippet_skips_hits_with_empty_snippet():
    """A SearchHit with snippet=None / "" must not be chosen even at higher
    priority — there's nothing to display. Forces the function to fall through
    to the next-best hit that actually has text."""
    bucket = [
        _hit("transcript", None),       # missing snippet, skipped
        _hit("transcript", ""),         # empty snippet, also skipped (falsy)
        _hit("ocr", "Slide title here"),
    ]
    assert _pick_best_snippet(bucket) == "Slide title here"


def test_pick_best_snippet_multi_treated_like_unknown_falls_below_visual():
    """A bucket-merged "multi"-source hit isn't in the priority table (which
    only lists the input channels: transcript / ocr / visual). So it gets
    priority 99 — below visual — but still gets returned as a fallback when
    no other source has a snippet. The visual-placeholder drop only fires
    when the LITERAL source string is "visual", so a multi-source snippet
    isn't accidentally swallowed by that guard."""
    bucket = [_hit("multi", "post-merge representative snippet")]
    assert _pick_best_snippet(bucket) == "post-merge representative snippet"


# ─── _fetch_size — per-channel fetch ceiling math ──────────────────────
# SearchEngine fetches MORE than `query.limit` from each channel because:
#   1. Hits later get deduped by (file_id, ts_bucket) — multiple raw hits
#      collapse into one.
#   2. _apply_per_file_cap=4 narrows further (one densely-matching video
#      can't dominate).
#   3. The visual noise floor drops gibberish-query hits.
# So a 30-result query needs ~12× more raw hits per channel to survive
# fusion and still produce a full page. _fetch_size encodes this with two
# guards: a 40-hit floor (so single-hit limit queries don't fetch <40,
# which would over-narrow before fusion picks the best ones) and a 300-hit
# cap (so a hostile limit=100 doesn't request 1200 hits/channel which
# would burn DB CPU + Chroma quota with no upside).

@pytest.mark.parametrize("limit,expected", [
    (1,    40),    # 1*12=12 → floor at 40 (1-hit query still fetches enough to pick the best)
    (3,    40),    # 3*12=36 → still under floor, clamps to 40
    (4,    48),    # 4*12=48 → exactly at the floor's exit point
    (10,   120),   # typical mid-limit case
    (25,   300),   # 25*12=300 → exact cap
    (30,   300),   # 30*12=360 → cap kicks in
    (100,  300),   # hostile case: limit=100 doesn't fetch 1200/channel
])
def test_fetch_size_floor_and_cap(limit, expected):
    """_fetch_size(limit) = min(max(limit*12, 40), 300). The 40-floor
    prevents over-narrow fetches before fusion (a 1-hit query still
    needs 40 candidates to pick the best one through dedupe + per-file
    cap + noise filter). The 300-cap prevents a hostile or buggy
    limit=100 from requesting 1200 hits per channel and burning DB
    + Chroma CPU with no benefit (4× per-file cap × 30 result slots
    + noise headroom = ~300 ceiling)."""
    eng = SearchEngine.__new__(SearchEngine)
    assert eng._fetch_size(limit) == expected, (
        f"_fetch_size({limit}) = {eng._fetch_size(limit)}, expected {expected}"
    )


def test_fetch_size_constants_in_sane_band():
    """Defense against accidental constant drift. If a future refactor
    bumps _FETCH_MULTIPLIER above 20 or _FETCH_CAP below 100, the
    fusion + per-file-cap pipeline starves and the user sees fewer
    results than requested even on plentiful matches."""
    assert SearchEngine._FETCH_MULTIPLIER >= 8, (
        f"_FETCH_MULTIPLIER={SearchEngine._FETCH_MULTIPLIER} too low — "
        "fusion needs headroom to dedupe + per-file-cap"
    )
    assert SearchEngine._FETCH_MULTIPLIER <= 20, (
        f"_FETCH_MULTIPLIER={SearchEngine._FETCH_MULTIPLIER} unnecessarily high — "
        "wastes DB/Chroma CPU"
    )
    assert 100 <= SearchEngine._FETCH_CAP <= 1000, (
        f"_FETCH_CAP={SearchEngine._FETCH_CAP} out of sane band"
    )


def test_per_file_cap_constant_in_sane_band():
    """_PER_FILE_CAP controls how many hits per file_id survive the
    fusion pass — the diversity guard that keeps a densely-matching
    single file from filling the entire result page. Sanity bounds:

      - >=2: at least 2 hits per file or `mountain lake` matched in
        two separate scenes of the same video has no way to show
        both (which is the whole point of timecoded search).
      - <=8: above 8, a single file CAN crowd out other files'
        moments — the cap was added to PREVENT exactly that.

    Previously the constant lived at 4 with no sanity test;
    a future refactor that sets it to 1 (over-correcting diversity)
    or 50 (forgetting the cap) would silently degrade ranking
    quality with no test signal."""
    assert 2 <= SearchEngine._PER_FILE_CAP <= 8, (
        f"_PER_FILE_CAP={SearchEngine._PER_FILE_CAP} out of sane band [2, 8]"
    )


def test_visual_noise_floor_constants_in_sane_band():
    """The three visual-noise-floor tuning constants gate which visual
    hits survive past the gibberish-detector + relative-score filter.
    Drift in any direction silently degrades search quality with no
    test signal — pin them in defensible bands.

    Constants + bands:
      - _VISUAL_ABS_MIN: absolute noise floor. Below this, SigLIP-2
        cosine is statistical noise (~0.01-0.05 on a random keyframe).
        Bounds [0.05, 0.15]: <0.05 lets pure-noise through; >0.15
        kills legitimate weak matches like picsum_beach.jpg at 0.09.
      - _VISUAL_ABS_GOOD: "definitely a real match" threshold above
        which the gibberish-detector's flat-noise pattern can't fire.
        Bounds [0.10, 0.25]: <0.10 too lax (gibberish queries score
        ~0.10); >0.25 too strict (kills weak real matches).
      - _VISUAL_REL_FACTOR: fraction of the top visual score that the
        floor scales to. Bounds [0.30, 0.70]: <0.30 lets too many
        mediocre hits through; >0.70 only the top-quartile survive,
        which would cull a legitimate broad "any mountain photo"
        query into a single-hit result.

    Bands intentionally generous so a defensible tuning experiment
    can move within them; only catastrophic drift (typo, 0.5→5.0
    bump) fails the test."""
    assert 0.05 <= SearchEngine._VISUAL_ABS_MIN <= 0.15, (
        f"_VISUAL_ABS_MIN={SearchEngine._VISUAL_ABS_MIN} out of sane band [0.05, 0.15]"
    )
    assert 0.10 <= SearchEngine._VISUAL_ABS_GOOD <= 0.25, (
        f"_VISUAL_ABS_GOOD={SearchEngine._VISUAL_ABS_GOOD} out of sane band [0.10, 0.25]"
    )
    assert 0.30 <= SearchEngine._VISUAL_REL_FACTOR <= 0.70, (
        f"_VISUAL_REL_FACTOR={SearchEngine._VISUAL_REL_FACTOR} out of sane band [0.30, 0.70]"
    )
    # ABS_GOOD MUST be strictly > ABS_MIN — the flat-noise detector's
    # "top_visual_raw < ABS_GOOD" branch assumes ABS_GOOD is the
    # higher of the two. A refactor that swaps them would silently
    # disable the gibberish filter on real signal.
    assert SearchEngine._VISUAL_ABS_GOOD > SearchEngine._VISUAL_ABS_MIN, (
        f"_VISUAL_ABS_GOOD ({SearchEngine._VISUAL_ABS_GOOD}) must be > "
        f"_VISUAL_ABS_MIN ({SearchEngine._VISUAL_ABS_MIN}) for the gibberish "
        f"detector to function — see _passes(h) in SearchEngine.search"
    )


def test_filename_boost_constant_in_sane_band():
    """_FILENAME_BOOST is the multiplicative bump applied to hits whose
    basename contains a query token. Bounds [1.05, 1.50]: <1.05 too
    weak to dethrone a weak SigLIP match on a wrongly-named file;
    >1.50 lets filename matches outrank strong-signal transcript or
    OCR hits. The current 1.20 sits squarely in the middle —
    defensible-but-not-domineering."""
    assert 1.05 <= SearchEngine._FILENAME_BOOST <= 1.50, (
        f"_FILENAME_BOOST={SearchEngine._FILENAME_BOOST} out of sane band [1.05, 1.50]"
    )


def test_filename_hit_does_not_raise_the_visual_noise_floor():
    """A filename match must not silently delete the semantic channel.

    search_filename scores a flat synthetic 0.50 ("the basename matched"),
    which is an order of magnitude above what SigLIP-2 base actually emits
    for a genuine match (~0.10-0.15). That score used to be folded into
    visual_raw_by_bucket, which is what top_visual_raw — and therefore the
    RELATIVE noise floor, top * _VISUAL_REL_FACTOR — is computed from. One
    filename match pushed the floor to 0.25 and culled every real cosine
    hit in the result set.

    Field symptom: an archive containing `picsum_mountain.jpg` answered
    "snowy mountain peak" with that file alone (matched on its NAME) while
    discarding the photo that actually shows a snowy peak at cosine 0.1379.
    The better answer was thrown away because a worse one was named well.
    """
    from tern.models import SearchQuery, SearchHit

    eng = SearchEngine.__new__(SearchEngine)

    class _Store:
        def search_transcript(self, *a, **kw):
            return []
        def search_ocr(self, *a, **kw):
            return []
        def search_visual(self, *a, **kw):
            # file 2 is the genuinely-correct frame: a real, healthy cosine
            # that sits comfortably above _VISUAL_ABS_MIN (0.07) but far
            # below half of the filename channel's synthetic 0.50.
            return [SearchHit(file_id=2, file_path="/archive/DSC_3844.jpg",
                              ts_ms=0, duration_ms=2000,
                              snippet="[visual match at 0s]",
                              source="visual", score=0.1379)]
        def search_filename(self, *a, **kw):
            # file 1 matches on its name only.
            return [SearchHit(file_id=1, file_path="/archive/picsum_mountain.jpg",
                              ts_ms=0, duration_ms=2000,
                              snippet="[filename match]",
                              source="visual", score=0.50)]

    class _Embedder:
        def embed_text(self, q):
            class _A:
                def tolist(self_inner): return [0.0] * 768
            return _A()

    eng.store = _Store()
    eng.embedder = _Embedder()

    results = eng.search(SearchQuery(query="snowy mountain peak", limit=10,
                                     sources=["visual"],
                                     weights={"visual": 1.0}))
    returned = {h.file_id for h in results}

    assert 2 in returned, (
        "the real cosine hit (0.1379) was culled by the noise floor because a "
        "filename match set top_visual_raw to 0.50 -> threshold 0.25. Real "
        f"SigLIP-2 cosines never reach that. Got file_ids={sorted(returned)}"
    )
    assert 1 in returned, (
        "the filename hit must still survive — it is exempt from the floor "
        f"via filename_buckets. Got file_ids={sorted(returned)}"
    )


# ─── search() subset sources — no embed thread when "visual" not requested ───
#
# search() kicks off SigLIP embed_text on a
# background thread CONCURRENTLY with the FTS5 channels. The branch that
# creates the ThreadPoolExecutor is gated on `if "visual" in query.sources`
# — if the caller only wants transcript+ocr, no thread is created (and no
# call to self.embedder.embed_text fires).
#
# This test pins that the embedder is NEVER called when visual is omitted,
# even though embedder is wired up. A future refactor that always-on calls
# embed_text (e.g., "let's precompute it for the future visual channel
# bonus") would silently re-introduce the cost AND fail this test loudly.


def test_search_with_no_visual_does_not_call_embedder(monkeypatch):
    """When query.sources omits 'visual', the embedder is never touched.
    The gate is explicit: the ThreadPoolExecutor is only created when
    'visual' is requested. The contract is: no visual source = no embed
    call = no SigLIP overhead. Pin it so a future refactor doesn't
    re-introduce a precompute-just-in-case that ships unnecessary
    MPS forward passes per search."""
    from tern.models import SearchQuery, SearchHit
    eng = SearchEngine.__new__(SearchEngine)

    class _Store:
        def search_transcript(self, *a, **kw):
            return [SearchHit(file_id=1, file_path="/a.mp3", ts_ms=0,
                              duration_ms=1000, snippet="t", source="transcript",
                              score=0.5)]
        def search_ocr(self, *a, **kw):
            return []
        def search_visual(self, *a, **kw):
            raise AssertionError("search_visual must NOT be called when 'visual' not in sources")
        def search_filename(self, *a, **kw):
            raise AssertionError("search_filename runs INSIDE the visual block — must NOT be called when 'visual' not in sources")

    embed_call_count = [0]
    class _Embedder:
        def embed_text(self, q):
            embed_call_count[0] += 1
            class _A:
                def tolist(self_inner): return [0.0] * 768
            return _A()

    eng.store = _Store()
    eng.embedder = _Embedder()

    q = SearchQuery(query="anything", limit=10,
                    sources=["transcript", "ocr"],
                    weights={"transcript": 1.0, "ocr": 1.0})
    results = eng.search(q)

    # Surviving transcript hit comes back
    assert any(h.source == "transcript" for h in results), (
        "transcript channel should still return its hit when visual is omitted"
    )
    # Embedder never called — the MPS forward pass cost is saved
    assert embed_call_count[0] == 0, (
        f"embed_text called {embed_call_count[0]} times; expected 0 when "
        "'visual' is not in query.sources (the embed "
        "executor is gated on this check)"
    )


def test_search_with_only_visual_still_dispatches_embed(monkeypatch):
    """Inverse of the above — when visual IS the only source, the
    embed thread MUST still fire and search_visual MUST run, even
    though transcript+ocr branches are skipped. Catches a refactor
    that accidentally inverts the gate (e.g., 'optimize away the
    executor when ONLY visual is requested' — would break the
    perf optimization's purpose since visual queries are the
    dominant slow case)."""
    from tern.models import SearchQuery, SearchHit
    eng = SearchEngine.__new__(SearchEngine)

    embed_call_count = [0]
    visual_call_count = [0]

    class _Store:
        def search_transcript(self, *a, **kw):
            raise AssertionError("search_transcript must NOT be called when 'transcript' not in sources")
        def search_ocr(self, *a, **kw):
            raise AssertionError("search_ocr must NOT be called when 'ocr' not in sources")
        def search_visual(self, query_emb, *a, **kw):
            visual_call_count[0] += 1
            return [SearchHit(file_id=1, file_path="/p.mp4", ts_ms=0,
                              duration_ms=2000, snippet="v", source="visual",
                              score=0.30)]
        def search_filename(self, *a, **kw):
            return []

    class _Embedder:
        def embed_text(self, q):
            embed_call_count[0] += 1
            class _A:
                def tolist(self_inner): return [0.0] * 768
            return _A()

    eng.store = _Store()
    eng.embedder = _Embedder()

    q = SearchQuery(query="cat", limit=10,
                    sources=["visual"],
                    weights={"visual": 1.0})
    results = eng.search(q)

    # Visual hit comes back
    assert any(h.source == "visual" for h in results), (
        "visual channel should return its hit when only 'visual' in sources"
    )
    # Embedder called exactly once
    assert embed_call_count[0] == 1, (
        f"embed_text called {embed_call_count[0]} times; expected exactly 1 "
        "(the visual channel needs one text embed to query Chroma)"
    )
    assert visual_call_count[0] == 1, (
        f"search_visual called {visual_call_count[0]} times; expected 1"
    )


# ─── SearchEngine.search() per-channel failure swallowing + logging ─────
#
# Each of the four channels (transcript FTS, OCR FTS, visual cosine,
# filename match) is wrapped in try/except so a single failing source
# doesn't kill the whole search — the user still sees results from the
# channels that worked. PREVIOUSLY the swallow was silent: a transcript
# FTS5 regression looked exactly like "no matches" with nothing logged.
# Now logger.exception() lands the traceback in tern-debug.log without
# changing the user-facing fallback.


def test_search_swallows_transcript_channel_failure_and_logs(monkeypatch, caplog):
    """A failing transcript channel must not bring down the whole
    SearchEngine.search() call — visual / OCR results still flow back.
    AND the failure must be observable in tern-debug.log so the next
    "user reports 0 hits" support thread has an exception trace to
    chase."""
    import logging as _logging
    from tern.models import SearchQuery, SearchHit
    eng = SearchEngine.__new__(SearchEngine)

    class _Store:
        def search_transcript(self, *a, **kw):
            raise RuntimeError("FTS5 unterminated string")  # synthetic crash
        def search_ocr(self, *a, **kw):
            return []
        def search_visual(self, *a, **kw):
            # Return a visual hit so we can verify the surviving-channel result
            return [SearchHit(
                file_id=1, file_path="/p.mp4", ts_ms=1000, duration_ms=2000,
                snippet="[visual match at 1s]", source="visual", score=0.20,
            )]
        def search_filename(self, *a, **kw):
            return []

    class _Embedder:
        def embed_text(self, q):
            class _A:
                def tolist(self_inner): return [0.0] * 768
            return _A()

    eng.store = _Store()
    eng.embedder = _Embedder()

    q = SearchQuery(query='"unterminated', limit=10,
                    sources=["transcript", "ocr", "visual"],
                    weights={"transcript": 1.0, "ocr": 1.0, "visual": 1.0})
    with caplog.at_level(_logging.ERROR, logger="tern.search"):
        results = eng.search(q)

    # 1) Surviving channels' hits still come back
    assert any(h.source == "visual" for h in results), \
        "visual hit must survive a transcript channel crash"
    # 2) The transcript crash got logged at ERROR (logger.exception → ERROR)
    transcript_log_records = [
        r for r in caplog.records
        if r.name == "tern.search" and "search_transcript failed" in r.getMessage()
    ]
    assert transcript_log_records, (
        "expected logger.exception entry for search_transcript failure; "
        f"got records: {[(r.name, r.getMessage()) for r in caplog.records]}"
    )
    # exc_info is attached by logger.exception so the traceback survives
    assert transcript_log_records[0].exc_info is not None, \
        "logger.exception should attach exc_info for traceback rendering"


# Sister-tests for the other 3 channels — same isolation contract: ANY single
# channel can crash without taking down the whole search, AND the crash MUST
# leave an exception trace in tern-debug.log. Previously only transcript
# was pinned; an OCR / visual / filename regression could ship silently
# (returning fewer hits than expected with no error trace).

@pytest.mark.parametrize("failing_channel,log_marker", [
    # OCR fails — visual + filename should still produce hits
    ("ocr",      "search_ocr failed"),
    # Visual fails (e.g. ChromaDB unavailable) — transcript + filename survive
    ("visual",   "search_visual failed"),
    # Filename fails — transcript + ocr + visual still flow back
    ("filename", "search_filename failed"),
])
def test_search_swallows_other_channel_failures_and_logs(monkeypatch, caplog, failing_channel, log_marker):
    """Pin the isolation contract for the 3 non-transcript channels.
    Mirror of test_search_swallows_transcript_channel_failure_and_logs
    but parametrized so a single failure pattern covers ocr / visual /
    filename without 3× code duplication.

    Each parametrize row: stub ONE channel to raise, leave the others
    returning a synthetic SearchHit, run search(), assert that (a) the
    surviving channels' hits still come back AND (b) the failed channel
    landed a logger.exception trace with exc_info attached."""
    import logging as _logging
    from tern.models import SearchQuery, SearchHit

    eng = SearchEngine.__new__(SearchEngine)
    # Each channel returns a unique-source-marked synthetic hit when called
    # successfully. The failing channel raises instead. The post-search
    # assertion checks that the failing channel's marker is ABSENT (because
    # the channel crashed) while the other markers are PRESENT (survived).
    _markers = {
        "transcript": SearchHit(file_id=1, file_path="/a.mp3", ts_ms=0, duration_ms=1000,
                                snippet="t-marker", source="transcript", score=0.5),
        "ocr":        SearchHit(file_id=2, file_path="/b.mp4", ts_ms=0, duration_ms=1000,
                                snippet="o-marker", source="ocr",        score=0.5),
        "visual":     SearchHit(file_id=3, file_path="/c.mp4", ts_ms=0, duration_ms=1000,
                                snippet="v-marker", source="visual",     score=0.5),
        "filename":   SearchHit(file_id=4, file_path="/d_marker.jpg", ts_ms=0, duration_ms=1000,
                                snippet="f-marker", source="visual",     score=0.5),
    }

    def _make(channel):
        if channel == failing_channel:
            def _raise(*a, **kw):
                raise RuntimeError(f"synthetic {channel} crash")
            return _raise
        return lambda *a, **kw: [_markers[channel]]

    class _Store:
        search_transcript = staticmethod(_make("transcript"))
        search_ocr        = staticmethod(_make("ocr"))
        search_visual     = staticmethod(_make("visual"))
        search_filename   = staticmethod(_make("filename"))

    class _Embedder:
        def embed_text(self, q):
            class _A:
                def tolist(self_inner): return [0.0] * 768
            return _A()

    eng.store = _Store()
    eng.embedder = _Embedder()

    q = SearchQuery(query="anything", limit=10,
                    sources=["transcript", "ocr", "visual"],
                    weights={"transcript": 1.0, "ocr": 1.0, "visual": 1.0})
    with caplog.at_level(_logging.ERROR, logger="tern.search"):
        results = eng.search(q)

    # 1) The failed channel's marker MUST NOT appear in results
    snippets = [h.snippet for h in results]
    failing_marker = {"transcript": "t-marker", "ocr": "o-marker",
                      "visual": "v-marker", "filename": "f-marker"}[failing_channel]
    assert not any(failing_marker in (s or "") for s in snippets), (
        f"{failing_channel} channel crashed; its hit must NOT survive. "
        f"snippets={snippets}"
    )

    # 2) The crash MUST be logged at ERROR with exc_info (logger.exception).
    # Search runs many channels and may emit other log lines (e.g., other
    # successful channels logging at INFO); we only check that OUR target
    # log marker appears and has the traceback attached.
    target_records = [
        r for r in caplog.records
        if r.name == "tern.search" and log_marker in r.getMessage()
    ]
    assert target_records, (
        f"expected logger.exception entry containing {log_marker!r}; "
        f"got records: {[(r.name, r.getMessage()) for r in caplog.records]}"
    )
    assert target_records[0].exc_info is not None, (
        f"logger.exception for {failing_channel} must attach exc_info "
        f"for traceback rendering"
    )
