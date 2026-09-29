"""Hybrid search combining transcript FTS, OCR FTS, and visual embedding similarity."""
from __future__ import annotations

import logging
import re
from collections import defaultdict
from pathlib import Path

from .models import SearchHit, SearchQuery
from .storage import Store, _highlight_all_query_words, _token_has_cjk
from .vision import Embedder


logger = logging.getLogger("tern.search")


# Strip characters that break FTS5 MATCH syntax (keep letters/digits/underscore,
# whitespace, and quote).
#
# `-` USED to be preserved here, but FTS5 interprets `-token` as the NOT
# operator (and `word-word` as `word - word`) — so common real inputs like
#   "covid-19", "back-end", "tax-free", "pricing -free"
# raise `no such column: 19` / `no such column: free`. search.py's per-channel
# try/except swallows the OperationalError, so the user sees ZERO transcript
# and OCR hits for any hyphenated phrase, with no error surfaced — the visual
# channel still runs because it doesn't go through FTS5, so the result list
# isn't empty enough to tip the user off. Stripping `-` (just like `&`, `*`,
# `(`, `:`, etc.) turns the dash into a space so the words AND together,
# which matches the natural user expectation: "covid-19" finds rows mentioning
# both "covid" and "19".
FTS5_STRIP = re.compile(r'[^\w\s"]')


def sanitize_fts_query(q: str) -> str:
    """Turn a user query into a safe FTS5 MATCH expression.

    Behaviours:
    * Strip out FTS5 syntax characters (`*`, `(`, `:`, etc.) that would otherwise
      raise a syntax error.
    * Preserve quoted phrases. `"customer success"` searches for the exact phrase
      "customer success", not the union of the two words.
    * Append a `*` prefix-wildcard to the LAST word when the user looks like
      they're still typing (>= 2 chars, no closing quote, no trailing space).
      That makes search-as-you-type return useful hits for `Stanf` (→ Stanford),
      `Sam Alt` (→ Sam Altman), `pricin` (→ pricing) before they finish.
    """
    raw = q
    sanitized = FTS5_STRIP.sub(" ", raw)
    sanitized = re.sub(r"\s+", " ", sanitized).strip()
    if not sanitized:
        return '""'

    # Preserve any user-supplied quoted phrases verbatim. If quotes are present,
    # don't add prefix wildcards — the user has signalled they want an exact match.
    if '"' in raw:
        # Balance unterminated quotes so search-as-you-type works while the
        # user is mid-typing a phrase. Without this, the moment they hit `"`
        # FTS5 raises `unterminated string` (verified in sqlite3 directly);
        # search.py's try/except swallows the error and the transcript / OCR
        # buckets return ZERO hits. The visual channel still runs (it doesn't
        # go through FTS5), so the user sees a partial result list and has no
        # idea their phrase typo killed text search. Append a closing quote
        # when the quote count is odd — that's the entire fix.
        if sanitized.count('"') % 2 == 1:
            sanitized = sanitized + '"'
        return sanitized

    # Add a prefix wildcard to the trailing token. We OR it with the bare form
    # so that words that get Porter-stemmed at index time (e.g. "pricing" →
    # "pric") still match — the bare form lets FTS5 apply the same stem to the
    # query side. FTS5 quirk: implicit AND (just space) doesn't compose with
    # parenthesized OR, so we use explicit AND between all preceding tokens.
    if not raw.endswith(" ") and not raw.endswith("\t"):
        tokens = sanitized.split()
        if tokens and len(tokens[-1]) >= 2 and not tokens[-1].endswith("*"):
            last = tokens[-1]
            head_tokens = tokens[:-1]
            tail = f"({last}* OR {last})"
            if head_tokens:
                head = " AND ".join(head_tokens)
                sanitized = f"{head} AND {tail}"
            else:
                sanitized = tail

    return sanitized


class SearchEngine:
    """Combines transcript, OCR, and visual search with weighted ranking."""

    def __init__(self, store: Store, embedder: Embedder):
        self.store = store
        self.embedder = embedder

    # How many raw hits to fetch per source before fusion.
    # Has to be >> limit because one file with many densely-matching keyframes can
    # otherwise crowd out higher-scoring hits from other files.
    _FETCH_MULTIPLIER = 12
    _FETCH_CAP = 300

    # How many hits per file to keep after fusion. Prevents a single video with
    # many similar-looking scenes from filling the entire result list.
    _PER_FILE_CAP = 4

    # Visual relevance filtering. SigLIP-2 base returns top-N by cosine sim
    # regardless of absolute score, so for a query no file actually matches
    # we still get back random keyframes at ~0.01-0.10. Three-stage filter:
    #
    #   1. ABS_MIN: kill anything below 0.07 — true noise on this model.
    #      (Used to be 0.10 but that killed legitimate weak matches: e.g.
    #      `picsum_beach.jpg` scored 0.0946 for query "beach" and was
    #      filtered as noise. 0.07 keeps weak-but-real matches.)
    #   2. REL_FACTOR: when there IS a strong top match, drop hits below
    #      half its score (cat-on-butterfly trap).
    #   3. SIGNAL_GAP: gibberish queries like `zxqv_no_match` hallucinate
    #      uniformly mediocre scores ~0.10 across a single file's keyframes
    #      (the model maps OOV tokens to a centroid that happens to be near
    #      one cluster). Detect that: if top visual is below ABS_GOOD and
    #      the distribution is flat (top - p50 < 0.02), there's no real
    #      signal — drop all visual hits.
    _VISUAL_ABS_MIN = 0.07
    _VISUAL_ABS_GOOD = 0.12       # above this, definitely a real match
    _VISUAL_REL_FACTOR = 0.50
    _VISUAL_GAP_MIN = 0.02        # required top-vs-median gap when score < GOOD

    def _fetch_size(self, limit: int) -> int:
        return min(max(limit * self._FETCH_MULTIPLIER, 40), self._FETCH_CAP)

    def search(self, query: SearchQuery) -> list[SearchHit]:
        all_hits: list[SearchHit] = []
        fts_query = sanitize_fts_query(query.query)
        fetch_n = self._fetch_size(query.limit)

        # Each channel is wrapped in try/except so a single failing source
        # (e.g. an FTS5 quirk on transcript, ChromaDB unavailable for visual)
        # doesn't take down the whole search — the user still sees results
        # from healthy channels. But the swallow USED to be silent: a
        # transcript-FTS regression looked identical to "no matches" in the
        # UI, with nothing in the log either. logger.exception() now lands
        # the traceback in tern-debug.log (Python's root logger is routed
        # there by the API host) without changing the user-facing fallback.

        # Perf: when visual is in sources, embed_text dominates the search
        # latency budget (~50-150 ms on warm SigLIP, vs ~10-30 ms per
        # FTS5 channel). Kick off the embed on a background thread so it
        # OVERLAPS with the FTS5 channels' SQLite work instead of running
        # serially after them. The thread joins right before search_visual
        # needs the embedding. Saves ~50-100 ms per search on the typical
        # "all 3 sources" case. Pure latency win — no semantic change.
        # ThreadPoolExecutor with max_workers=1 because we only need one
        # background slot; the context-manager shutdown waits on the
        # future so we don't leak the thread even on exceptions.
        embed_future = None
        embed_executor = None
        if "visual" in query.sources:
            from concurrent.futures import ThreadPoolExecutor
            embed_executor = ThreadPoolExecutor(max_workers=1,
                                                thread_name_prefix="tern-embed")
            embed_future = embed_executor.submit(self.embedder.embed_text, query.query)

        if "transcript" in query.sources:
            try:
                all_hits.extend(self.store.search_transcript(fts_query, limit=fetch_n, file_filter=query.file_filter))
            except Exception:
                logger.exception("search_transcript failed for query=%r", query.query)

        if "ocr" in query.sources:
            try:
                all_hits.extend(self.store.search_ocr(fts_query, limit=fetch_n, file_filter=query.file_filter))
            except Exception:
                logger.exception("search_ocr failed for query=%r", query.query)

        # Track raw visual scores (pre-weighting) per-bucket so we can apply the
        # min-score threshold AFTER merging — visual-only hits below the noise
        # floor get dropped, but a low-visual-sim keyframe that has a matching
        # OCR or transcript signal in the same bucket survives as a multi hit.
        visual_raw_by_bucket: dict[tuple[int, int], float] = {}
        # Buckets that came from the filename channel are exempt from the
        # visual noise floor (they have explicit user-intent grounding).
        filename_buckets: set[tuple[int, int]] = set()
        if "visual" in query.sources:
            try:
                # Join the embed thread started above. By the time we get
                # here, FTS5 transcript + OCR channels above have already
                # consumed ~30-60 ms of wall-clock; the embed has had that
                # time to compute concurrently. future.result() raises if
                # the embed itself failed (model load error, OOM); the
                # outer except converts that into a logged warning + visual
                # channel skipped, matching the earlier behavior.
                query_emb = embed_future.result().tolist()
                visual_hits = self.store.search_visual(query_emb, limit=fetch_n, file_filter=query.file_filter)
                for h in visual_hits:
                    bucket = (h.file_id, h.ts_ms // 5000)
                    if h.score > visual_raw_by_bucket.get(bucket, 0.0):
                        visual_raw_by_bucket[bucket] = h.score
                all_hits.extend(visual_hits)
            except Exception:
                logger.exception("search_visual failed for query=%r", query.query)
            finally:
                # Shutdown the per-search executor. wait=False because the
                # future is already resolved (we just called .result()); if
                # the embed failed mid-flight, the thread is still alive but
                # will exit on its own. The interpreter cleanup on process
                # exit handles any dangling threads.
                if embed_executor is not None:
                    embed_executor.shutdown(wait=False)
                    embed_executor = None
            # Filename channel: compensates for SigLIP base's weak grounding on
            # single-word queries (e.g. "mountain" doesn't reliably retrieve
            # picsum_mountain.jpg via cosine alone). Adds a guaranteed hit per
            # filename-match.
            try:
                fname_hits = self.store.search_filename(query.query, limit=fetch_n, file_filter=query.file_filter)
                for h in fname_hits:
                    bucket = (h.file_id, h.ts_ms // 5000)
                    filename_buckets.add(bucket)
                    # Deliberately NOT folded into visual_raw_by_bucket.
                    #
                    # search_filename's score is a synthetic flat 0.50, not a
                    # cosine — it means "the basename matched", not "this frame
                    # looks 50% like the query". visual_raw_by_bucket feeds
                    # top_visual_raw, which sets the RELATIVE noise floor
                    # (top * _VISUAL_REL_FACTOR) for every other hit. Folding
                    # 0.50 in pushed that floor to 0.25, and real SigLIP-2 base
                    # cosines top out around 0.15 — so a single filename match
                    # silently culled the entire semantic channel. Observed:
                    # "snowy mountain peak" returned only picsum_mountain.jpg
                    # (name match, wrong photo) and dropped the actual snowy
                    # peak at cosine 0.1379.
                    #
                    # These buckets don't need the entry to survive: _passes()
                    # short-circuits on `bucket in filename_buckets` before it
                    # ever consults visual_raw_by_bucket.
                all_hits.extend(fname_hits)
            except Exception:
                logger.exception("search_filename failed for query=%r", query.query)

        # Weighted scoring + dedupe near-duplicates (same file, close timestamps)
        weighted = self._reweight_and_dedupe(all_hits, query.weights)
        # Filename boost: SigLIP-2 base is mediocre on single-word visual queries
        # ("mountain" → picsum_office.jpg ranks above picsum_mountain.jpg). When
        # the query token appears in the file basename, give the file a +20%
        # score nudge. Token-level so "mountain lake" boosts files with either
        # word; substring match so "iphone" boosts IMG_iphone_15.heic. Cheap,
        # interpretable, and complements (doesn't replace) the visual signal.
        self._apply_filename_boost(weighted, query.query)
        weighted.sort(key=lambda h: h.score, reverse=True)
        # Apply per-file diversity cap so one densely-matching file can't fill the page
        diverse = self._apply_per_file_cap(weighted, self._PER_FILE_CAP)
        # Drop noise: visual-only hits whose raw cosine was below the noise floor.
        # Multi-source hits always pass — they have additional evidence (OCR
        # or transcript matched at the same timecode).
        if visual_raw_by_bucket and diverse:
            raws = sorted(visual_raw_by_bucket.values(), reverse=True)
            top_visual_raw = raws[0]
            # Look at the TOP cluster only: gibberish queries score uniformly
            # mediocre across one file's keyframes (e.g. all 0.100-0.104).
            # If we sample the full distribution including unrelated low-
            # scoring files (0.03-0.05), the gap looks wide and we'd miss
            # the noise pattern. Use the top-5 mean as the "what's actually
            # competing here" floor.
            head = raws[: min(5, len(raws))]
            head_mean = sum(head) / len(head) if head else 0.0
            head_spread = top_visual_raw - (head[-1] if head else 0.0)
            # Gibberish detector: low absolute scores AND the top results
            # are tightly clustered → no real signal, drop all visual.
            flat_noise = (top_visual_raw < self._VISUAL_ABS_GOOD
                          and head_spread < self._VISUAL_GAP_MIN)
            threshold = max(self._VISUAL_ABS_MIN, top_visual_raw * self._VISUAL_REL_FACTOR)
            def _passes(h):
                if h.source != "visual":
                    return True
                bucket = (h.file_id, h.ts_ms // 5000)
                # Filename-channel hits bypass the noise floor — they have
                # explicit user-intent grounding (basename matched a token).
                if bucket in filename_buckets:
                    return True
                if flat_noise:
                    return False
                return visual_raw_by_bucket.get(bucket, 0.0) >= threshold
            diverse = [h for h in diverse if _passes(h)]
        # Final pass: ensure every query token in the snippet is highlighted,
        # not just the ones FTS5's snippet() picked. Helps multi-word queries
        # where only the first matched word would otherwise be marked.
        final = diverse[: query.limit]
        for h in final:
            if h.snippet:
                h.snippet = _highlight_all_query_words(h.snippet, query.query)
        return final

    _FILENAME_BOOST = 1.20  # multiplicative bonus for hits whose file basename matches a query token

    @staticmethod
    def _token_has_cjk(token: str) -> bool:
        """Thin alias around storage._token_has_cjk.

        Kept as a class-level staticmethod because:
          - Tests reference SearchEngine._token_has_cjk directly
            (see test_token_has_cjk_script_block_coverage).
          - _apply_filename_boost below calls SearchEngine._token_has_cjk(t)
            — switching that to the module-level import would work fine
            but keeping the class-bound name documents intent: "this
            check belongs to the search-engine's filter logic."

        Single source of truth lives in storage.py since storage is the
        lower-level module; search.py imports it from there rather
        than keeping a duplicate definition. The _CJK_RANGES constant + the per-char range scan
        live there too; this is a pure delegation.
        """
        return _token_has_cjk(token)

    @staticmethod
    def _apply_filename_boost(hits: list[SearchHit], query: str) -> None:
        """Nudge results whose file basename contains a query token. In-place.

        Implementation detail: we match against the basename WITHOUT extension,
        lower-cased, against query tokens. The length minimum is **3 chars for
        Latin-alphabet tokens** (because `if`, `to`, `or`, `is` would falsely
        boost every English file), but **2 chars for tokens containing any
        CJK / Japanese / Korean character** (because 会議 = "meeting",
        予算 = "budget", 株式 = "stock", 회사 = "company" are full standalone
        words at 2 chars — the Latin-noise-floor rationale doesn't apply to
        morpheme-per-character scripts). Without this carve-out, Japanese /
        Chinese / Korean users would silently get NO filename boost for the
        most common search terms in their language.

        We strip common separators (`_`, `-`, `.`, space) before substring
        search so `picsum_mountain.jpg` matches `mountain`. Doesn't penalize
        anything; only adds an upward nudge. Multiplicative so it's
        proportional to the underlying signal — strong base hits get a
        stronger boost.
        """
        if not query:
            return
        # Length filter: 3+ chars OR (2+ chars AND contains CJK character).
        # See SearchEngine._token_has_cjk docstring for the script blocks
        # we recognize as CJK.
        tokens = [
            t for t in re.findall(r"\w+", query.lower())
            if len(t) >= 3 or (len(t) >= 2 and SearchEngine._token_has_cjk(t))
        ]
        if not tokens:
            return
        for h in hits:
            try:
                base = Path(h.file_path).stem.lower()
            except Exception:
                continue
            # Normalize separators so picsum_mountain matches "mountain"
            base_norm = re.sub(r"[_\-.\s]+", " ", base)
            base_words = set(base_norm.split())
            # Match if ANY query token is either a whole word in the basename
            # OR appears as substring (catches img_iphone15 case).
            if any(t in base_words or t in base_norm for t in tokens):
                h.score *= SearchEngine._FILENAME_BOOST

    @staticmethod
    def _apply_per_file_cap(hits: list[SearchHit], cap: int) -> list[SearchHit]:
        """Keep at most `cap` hits per file_id while preserving the existing
        score-descending order."""
        if cap <= 0:
            return hits
        counts: dict[int, int] = defaultdict(int)
        out: list[SearchHit] = []
        for h in hits:
            if counts[h.file_id] >= cap:
                continue
            counts[h.file_id] += 1
            out.append(h)
        return out

    @staticmethod
    def _reweight_and_dedupe(
        hits: list[SearchHit], weights: dict[str, float], dedupe_window_ms: int = 5000
    ) -> list[SearchHit]:
        # Apply weights
        for hit in hits:
            hit.score = hit.score * weights.get(hit.source, 1.0)

        # Group by (file_id, ts_bucket)
        buckets: dict[tuple[int, int], list[SearchHit]] = defaultdict(list)
        for hit in hits:
            bucket = (hit.file_id, hit.ts_ms // dedupe_window_ms)
            buckets[bucket].append(hit)

        merged: list[SearchHit] = []
        for bucket_hits in buckets.values():
            # Pick best hit per bucket. Use max(score) for the base so multiple visual
            # keyframes in the same 5s window don't artificially out-rank a single
            # higher-scoring hit (e.g. a photo). Then add a *modest* bonus when the
            # bucket has multiple distinct sources — that's the real signal we want
            # to reward: speech + on-screen + visual all agreeing.
            best = max(bucket_hits, key=lambda h: h.score)
            sources = {h.source for h in bucket_hits}
            base = best.score
            multi_bonus = 0.0
            if len(sources) > 1:
                # Boost by half of the next-best source's contribution, capped.
                other_scores = sorted((h.score for h in bucket_hits if h.source != best.source), reverse=True)
                if other_scores:
                    multi_bonus = min(other_scores[0] * 0.5, base * 0.5)
            # Pick the richest snippet by source priority. Transcript wins because
            # it tends to be a full sentence with context; OCR is short (often one
            # word per slide line); visual is just a [visual match at Xs] placeholder
            # which is useless when we already have text evidence.
            best.score = base + multi_bonus
            best.snippet = _pick_best_snippet(bucket_hits)
            best.sources = sorted(sources)
            best.source = best.source if len(sources) == 1 else "multi"
            merged.append(best)
        return merged


_SNIPPET_PRIORITY = {"transcript": 0, "ocr": 1, "visual": 2}


def _pick_best_snippet(bucket_hits: list[SearchHit]) -> str | None:
    """Choose the most informative snippet from a bucket of co-located hits.

    Order of preference: transcript > OCR > visual. The visual placeholder
    is only returned if no text evidence exists for the bucket — otherwise we
    drop it because it's a useless `[visual match at Xs]` string when we
    already have actual matched text to show.
    """
    by_priority = sorted(
        (h for h in bucket_hits if h.snippet),
        key=lambda h: _SNIPPET_PRIORITY.get(h.source, 99),
    )
    if not by_priority:
        return None
    top = by_priority[0]
    # Visual-only bucket: drop the placeholder so the frontend can render its
    # own "Visual match" affordance instead of leaking implementation detail.
    if top.source == "visual":
        return None
    return top.snippet
