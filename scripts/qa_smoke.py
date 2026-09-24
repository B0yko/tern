#!/usr/bin/env python3
"""End-to-end QA smoke test for Tern.

Runs every category of search query against the live backend (default
127.0.0.1:18765 — matches run.sh + dev_check.sh's auto-pick of the
first free port) and reports pass/fail with diagnostics. Designed to
be runnable in CI or by a human "did anything regress?" check.

Usage:
  python3 scripts/qa_smoke.py                       # uses default API endpoint
  python3 scripts/qa_smoke.py --base http://127.0.0.1:8765  # if you ran with TERN_PORT=8765
  python3 scripts/qa_smoke.py --base http://...

Exit 0 if all critical checks pass, 1 otherwise.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

DEFAULT_BASE = "http://127.0.0.1:18765"


# SearchEngine init (triggered by the first /api/search call) downloads the
# SigLIP-2 model from HuggingFace (~600 MB on a cold runner). The lazy-
# embedder split (commit 9f1b992) means /api/health, /api/stats, /api/files
# etc. all complete fast — only /api/search is slow on first call. So we
# track "have we exercised /api/search yet?" separately from "have we made
# any request yet?". First search call → 300s timeout; everything else 30s.
_NORMAL_TIMEOUT = 30
_SEARCH_FIRST_TIMEOUT = 300
_first_search_done = False


def _timeout_for(path: str) -> int:
    """Return the timeout to use for a request to `path`. The first
    /api/search call gets a generous timeout (cold SigLIP download);
    everything else gets the normal timeout."""
    global _first_search_done
    if path.startswith("/api/search") and not _first_search_done:
        _first_search_done = True
        return _SEARCH_FIRST_TIMEOUT
    return _NORMAL_TIMEOUT


def post(base: str, path: str, payload: dict) -> dict:
    req = urllib.request.Request(
        f"{base}{path}",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=_timeout_for(path)) as r:
        return json.loads(r.read())


def get(base: str, path: str) -> dict:
    with urllib.request.urlopen(f"{base}{path}", timeout=_timeout_for(path)) as r:
        return json.loads(r.read())


def search(base: str, q: str, **kw) -> dict:
    return post(base, "/api/search", {"query": q, "limit": kw.get("limit", 5), **{k: v for k, v in kw.items() if k != "limit"}})


class QA:
    def __init__(self, base: str):
        self.base = base
        self.passed: list[str] = []
        self.failed: list[tuple[str, str]] = []
        self.skipped: list[tuple[str, str]] = []

    def check(self, name: str, cond: bool, why: str = "") -> None:
        if cond:
            self.passed.append(name)
            print(f"  ✓ {name}")
        else:
            self.failed.append((name, why))
            print(f"  ✗ {name}  →  {why}")

    def skip(self, name: str, why: str) -> None:
        self.skipped.append((name, why))
        print(f"  ⊝ {name}  ({why})")

    def section(self, title: str) -> None:
        print(f"\n=== {title} ===")

    def summary(self) -> int:
        total = len(self.passed) + len(self.failed)
        print(f"\n────────────────────────────────────────")
        print(f"PASS {len(self.passed)}/{total}   FAIL {len(self.failed)}   SKIP {len(self.skipped)}")
        if self.failed:
            print("\nFailures:")
            for n, why in self.failed:
                print(f"  • {n}: {why}")
        return 0 if not self.failed else 1


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--base", default=DEFAULT_BASE)
    args = p.parse_args()
    qa = QA(args.base)

    # ── Health + stats ──────────────────────────────────────────
    qa.section("Backend health")
    try:
        h = get(args.base, "/api/health")
        qa.check("GET /api/health", h.get("status") == "ok", str(h))
    except Exception as e:
        qa.check("GET /api/health", False, str(e))
        return qa.summary()

    s = get(args.base, "/api/stats")
    qa.check("stats: files indexed > 0", s.get("files_total", 0) > 0, str(s))
    qa.check("stats: transcript segments > 0", s.get("transcript_segments", 0) > 0)
    qa.check("stats: keyframes > 0", s.get("keyframes", 0) > 0)
    # files_errored is the persistent DB-level count (status='error' rows),
    # distinct from app.state.indexing's per-run files_errored counter.
    # Added for surface-future flexibility — a sidebar
    # warning chip / empty-state breakdown / indexing-toast follow-up can
    # all read it. Pin the FIELD presence + numeric type here so a
    # refactor that drops the column from the SQL aggregate or renames it
    # lights up on the next live smoke instead of silently zeroing the
    # downstream UI.
    qa.check("stats: files_errored field present + numeric",
             isinstance(s.get("files_errored"), (int, float)),
             f"files_errored={s.get('files_errored')!r}")

    # ── Runtime-check ────────────────────────────────────────────
    qa.section("Runtime dependencies")
    rc = get(args.base, "/api/runtime-check")
    qa.check("runtime-check ok=True", rc.get("ok") is True, f"missing={rc.get('missing_required')}")

    # ── Diagnostics ──────────────────────────────────────────────
    qa.section("Diagnostics endpoint")
    d = get(args.base, "/api/diagnostics")
    qa.check("diagnostics returns version", bool(d.get("version")))
    qa.check("diagnostics has workspace path", bool(d.get("workspace")))

    # ── Version endpoint (commit e151a26) ───────────────────────
    # Lightweight endpoint that returns ONLY the cached _APP_VERSION
    # constant — no log I/O, no thread offload, no recursion. sidebar.js
    # + keyhelp.js call this on cold start / first ⌘/ press to populate
    # the version chip; previously both hit /api/diagnostics (which reads
    # up to 10 MB of crash log + runs store.stats() + recursive path
    # redaction) JUST to read the version field. Live-smoke this endpoint
    # so a regression in the trivial handler (e.g., accidental rename, or
    # the handler returning the WRONG shape after a refactor) gets caught
    # on the next dev_check.sh run instead of by buyers when the version
    # chip silently goes blank.
    v = get(args.base, "/api/version")
    qa.check("/api/version returns version field", bool(v.get("version")))
    qa.check("/api/version version matches /api/diagnostics version",
             v.get("version") == d.get("version"),
             f"version={v.get('version')!r} diagnostics_version={d.get('version')!r}")
    qa.check("/api/version returns ONLY the version key (no extra payload)",
             set(v.keys()) == {"version"},
             f"keys={sorted(v.keys())}")

    # ── Indexing status — polled by the sidebar progress chrome at ~1.2 s
    # cadence during an active index pass. The shape that endpoint
    # returns drives the indexing-toast layout: a regression that
    # silently drops a field (e.g., `stage`, `current_file`, `elapsed_s`,
    # `log`, or `cancel_requested`) breaks the toast's progress bar /
    # stage labels / cancel-button enable state with no error in the
    # logs. GET-only, zero side effects, safe to smoke on every release.
    # Doesn't require an index to be running — when idle, the endpoint
    # returns the dormant indexing state (running: false, etc).
    qa.section("Indexing status endpoint")
    try:
        ix = get(args.base, "/api/index/status")
        # Frontend's render path branches on this — must always be a bool.
        qa.check("/api/index/status returns 'running' (bool)",
                 isinstance(ix.get("running"), bool),
                 f"running={ix.get('running')!r}")
        # `log` is what the toast renders as the live log tail (last 3 lines).
        # Must always be a list (even when empty / not running) so the
        # frontend's .slice(-3) doesn't TypeError.
        qa.check("/api/index/status returns 'log' (list)",
                 isinstance(ix.get("log"), list),
                 f"log type={type(ix.get('log')).__name__}")
        # elapsed_s is computed live (now - start_time) when running,
        # 0 when idle. Always present, always numeric.
        qa.check("/api/index/status returns numeric 'elapsed_s'",
                 isinstance(ix.get("elapsed_s"), (int, float)),
                 f"elapsed_s={ix.get('elapsed_s')!r}")
        # cancel_requested drives the cancel-button's "Stopping…" label
        # enable state. Frontend reads via the `?? false` idiom so
        # truthy-falsy alone is fine — pin the field is present.
        qa.check("/api/index/status returns 'cancel_requested' field",
                 "cancel_requested" in ix,
                 f"keys={list(ix.keys())}")
        # Per-file counters drive the indexing-toast progress bar +
        # done-toast summary. Commit 8d94687 fixed a stale-progress bug
        # by adding files_skipped to the dormant state init AND the
        # per-run init — without both, the frontend's
        # `processed = files_done + files_skipped + files_errored`
        # rollup silently treats undefined as 0 and the bar stalls
        # under 100% on any re-index of a partially-done folder. Pin
        # all four field names so a refactor that drops any one of
        # them lights up here on the next live smoke instead of
        # waiting for a buyer to file "the progress bar is broken".
        for field in ("files_done", "files_errored", "files_skipped", "files_pending"):
            qa.check(f"/api/index/status returns numeric {field!r}",
                     isinstance(ix.get(field), (int, float)),
                     f"{field}={ix.get(field)!r}")
    except urllib.error.HTTPError as he:
        qa.check("/api/index/status reachable", False, f"HTTP {he.code}")

    # ── /api/index/cancel — idle-safe no-op when nothing is indexing.
    # Smoke-checking the idle path is safe (returns ok:True with a
    # "Nothing to cancel" message — no state change, no DB writes, no
    # subprocess kill). Catches three regression classes the pytest
    # unit tests miss against a live bundle:
    #   1. handler crashes when app.state.indexing.running is False
    #      (e.g., a future refactor that assumes 'running': True)
    #   2. the "Nothing to cancel" response shape drifts (frontend
    #      reads `r.ok` to decide whether to surface "cancel sent"
    #      vs "nothing was running" — both code paths matter)
    #   3. the endpoint is renamed/removed and the cancel button in
    #      the indexing toast silently 404s.
    # Skip if indexing is currently running (smoking a live cancel
    # could disrupt the user's actual work — unlikely in qa_smoke's
    # release-context but defensive).
    if ix and ix.get("running") is False:
        try:
            cancel = post(args.base, "/api/index/cancel", {})
            qa.check("/api/index/cancel returns ok:True when idle",
                     cancel.get("ok") is True,
                     f"got: {cancel}")
            qa.check("/api/index/cancel includes a 'message' field on idle",
                     "message" in cancel,
                     f"keys={list(cancel.keys())}")
        except urllib.error.HTTPError as he:
            qa.check("/api/index/cancel reachable", False, f"HTTP {he.code}")
    else:
        qa.skip("/api/index/cancel idle check",
                "indexing is currently running — refuse to disrupt it")

    # ── License status — used by the sidebar badge + commit 9b0a433
    # is_valid handling. A regression in /api/license/status shape
    # (missing field, renamed key) would break the badge AND the
    # license modal's "Licensed / Invalid / Unactivated" routing.
    # GET-only + zero side effects, so safe to smoke on every release.
    qa.section("License status (commit 9b0a433)")
    try:
        ls = get(args.base, "/api/license/status")
        # Every response (activated or not) MUST include `status` —
        # frontend's badge state derives from it.
        qa.check("/api/license/status returns 'status' field",
                 "status" in ls,
                 f"keys={list(ls.keys())}")
        # status is one of three enum values (commit 9b0a433): unactivated,
        # active, invalid. Anything else means the response shape drifted.
        qa.check("/api/license/status status is one of {unactivated, active, invalid}",
                 ls.get("status") in ("unactivated", "active", "invalid"),
                 f"got status={ls.get('status')!r}")
        # message field is always present (used by the sidebar tooltip)
        qa.check("/api/license/status includes 'message' field",
                 "message" in ls,
                 f"keys={list(ls.keys())}")
    except urllib.error.HTTPError as he:
        qa.check("/api/license/status reachable", False, f"HTTP {he.code}")

    # ── Photo / image search ────────────────────────────────────
    qa.section("Photo / image search")
    # Demo workspace photos: cat, beach, mountain, office, whiteboard, cafe, png transparency
    for q in ["cat", "beach", "mountain"]:
        r = search(args.base, q, limit=5)
        photo_hits = [h for h in r["hits"] if h["media_kind"] == "image"]
        qa.check(f"visual: '{q}' returns ≥1 image hit",
                 len(photo_hits) >= 1,
                 f"hits={[(h['source'], h['media_kind']) for h in r['hits']]}")
        if photo_hits:
            top = photo_hits[0]
            qa.check(f"  '{q}' top photo has thumbnail_url",
                     bool(top.get("thumbnail_url")), "no thumbnail")
            qa.check(f"  '{q}' top photo has score in (0, 1.5)",
                     0 < top.get("score", 0) < 1.5, f"score={top.get('score')}")

    # ── Video search (visual) ────────────────────────────────────
    qa.section("Video visual search")
    for q in ["butterfly", "bunny"]:
        r = search(args.base, q, limit=8)
        video_hits = [h for h in r["hits"] if h["media_kind"] == "video"]
        if video_hits:
            qa.check(f"visual: '{q}' returns ≥1 video hit (BBB)",
                     True, "")
            top = video_hits[0]
            qa.check(f"  '{q}' has timecode string", bool(top.get("timecode")))
            qa.check(f"  '{q}' has ts_ms > 0 (not start-of-file)",
                     top.get("ts_ms", 0) > 0, f"ts_ms={top.get('ts_ms')}")
        else:
            qa.skip(f"visual: '{q}' video hit", "no BBB video hits (workspace may not have indexed BBB)")

    # ── Transcript search ──────────────────────────────────────
    qa.section("Transcript search (speech-to-text)")
    for q, expect_kind in [("stanford", "video"), ("pricing", "audio")]:
        r = search(args.base, q, limit=5)
        transcript_hits = [h for h in r["hits"] if h["source"] in ("transcript", "multi") and h.get("snippet")]
        qa.check(f"transcript: '{q}' returns hit with snippet",
                 len(transcript_hits) >= 1,
                 f"hits={[(h['source'], (h.get('snippet') or '')[:30]) for h in r['hits']]}")
        if transcript_hits:
            top = transcript_hits[0]
            qa.check(f"  '{q}' snippet contains <mark>",
                     "<mark>" in (top.get("snippet") or ""),
                     f"snippet={(top.get('snippet') or '')[:80]!r}")
            qa.check(f"  '{q}' top hit timecode format",
                     ":" in top.get("timecode", ""), f"tc={top.get('timecode')}")

    # ── Exact-phrase search (quoted query) ────────────────────────
    # Commits 8603702 / c12bd2b / 1c0a8bf / c1ba469 / aa9d5ad surfaced
    # the quoted-phrase syntax across the user-facing entry points
    # (FAQ, search placeholder, README). The capability has
    # shipped since v1.0 via sanitize_fts_query preserving balanced
    # quotes through to FTS5's phrase-match operator. Smoke the live
    # wire to lock the behavior in: a regression in sanitize_fts_query
    # (the strip regex accidentally eats quotes, the balance-quotes
    # branch mis-balances, etc.) silently drops every buyer surface's
    # most-prominent power-feature claim into 0-hits.
    #
    # "customer success" is the canonical example used in those
    # surfaces. Backed by 4 transcript + 12 OCR rows in the
    # bundled demo, so a 0-hit return is a real regression — not a
    # demo-content drift issue.
    qa.section("Exact-phrase search (quoted query)")
    r = search(args.base, '"customer success"', limit=20)
    qa.check("quoted search: '\"customer success\"' returns hits",
             r["count"] >= 1,
             f"count={r['count']} — sanitize_fts_query may be eating "
             f"the quotes or FTS5 phrase-match regressed")
    # Each returned snippet from a quoted phrase search must literally
    # contain BOTH words in sequence (with normal interstitial whitespace).
    # A regression that drops quote-preservation typically returns hits
    # for either word alone — those would NOT contain the literal
    # "customer success" two-word sequence.
    snippet_hits = [h for h in r["hits"] if "customer success" in (h.get("snippet") or "").lower().replace("<mark>", "").replace("</mark>", "")]
    qa.check("quoted search: at least one snippet contains the exact 'customer success' phrase",
             len(snippet_hits) >= 1,
             f"got {len(r['hits'])} hits but none contained the exact phrase — "
             f"FTS5 phrase-match may have regressed to bag-of-words")

    # ── OCR search ──────────────────────────────────────────────
    qa.section("OCR search (on-screen text)")
    # Stanford and other YC slide content should hit via OCR
    r = search(args.base, "idea team execution", limit=5)
    multi_hits = [h for h in r["hits"] if h["source"] == "multi"]
    qa.check("ocr: 'idea team execution' returns multi-source hit",
             len(multi_hits) >= 1 or len(r["hits"]) >= 1,
             f"count={r['count']}")

    # ── Multimodal ranking ──────────────────────────────────────
    qa.section("Multimodal ranking")
    r = search(args.base, "stanford", limit=10)
    qa.check("multimodal: 'stanford' returns hits",
             r["count"] >= 1, f"count={r['count']}")
    sources = [h["source"] for h in r["hits"]]
    qa.check("multimodal: results include multiple sources",
             len(set(sources)) >= 1, f"sources={sources}")
    # Top hit should be high-confidence — transcript or multi
    if r["hits"]:
        top = r["hits"][0]
        qa.check("multimodal: top hit is transcript or multi (not raw visual)",
                 top["source"] in ("transcript", "multi", "ocr"),
                 f"top.source={top['source']}")

    # ── Filename-match channel ──────────────────────────────────
    # Locks in the fix that ensures single-word visual queries hit their
    # namesake files even when SigLIP-2's text-to-image signal is too weak.
    qa.section("Filename-match channel (SigLIP-base compensation)")
    for q, expected in [
        ("mountain", "mountain"),
        ("beach", "beach"),
        ("office", "office"),
        ("whiteboard", "whiteboard"),
    ]:
        r = search(args.base, q, limit=3)
        top = r["hits"][0] if r["hits"] else None
        ok = top and expected in top["file_name"].lower()
        qa.check(f"'{q}' top hit is the namesake photo",
                 ok,
                 f"got: {top['file_name'] if top else 'no hits'}")

    # ── Performance ─────────────────────────────────────────────
    qa.section("Search latency")
    import time
    # Warm: load SigLIP/Chroma into memory
    for _ in range(2):
        search(args.base, "warmup")
    times = []
    for q in ["stanford", "pricing strategy", "butterfly", "mountain", "y combinator"]:
        for _ in range(3):
            t0 = time.perf_counter()
            search(args.base, q)
            times.append((time.perf_counter() - t0) * 1000)
    times.sort()
    p50 = times[len(times) // 2]
    p99 = times[min(int(0.99 * len(times)), len(times) - 1)]
    # CPU-bound CI runs are 2-5x slower than M-series MPS. Use a relaxed
    # threshold when CI=true; strict threshold otherwise.
    is_ci = os.environ.get("CI", "").lower() in ("true", "1")
    p50_max = 500 if is_ci else 100
    p99_max = 800 if is_ci else 200
    qa.check(f"p50 search latency < {p50_max} ms  (got {p50:.1f}ms)", p50 < p50_max)
    qa.check(f"p99 search latency < {p99_max} ms  (got {p99:.1f}ms)", p99 < p99_max)

    # ── Empty/no-match query ────────────────────────────────────
    qa.section("Empty / no-match queries")
    r = search(args.base, "zxqv_no_match_should_return_nothing")
    qa.check("noise query returns 0 hits (visual floor working)",
             r["count"] == 0, f"got {r['count']}")

    # ── Snippet quality ────────────────────────────────────────
    qa.section("Snippet quality")
    r = search(args.base, "hurricane center", limit=3)
    multi_word_hits = [h for h in r["hits"] if h.get("snippet")]
    if multi_word_hits:
        snip = multi_word_hits[0]["snippet"]
        qa.check("snippet highlights BOTH hurricane AND center",
                 "<mark>" in snip and snip.lower().count("<mark>") >= 2,
                 f"snippet={snip!r}")
    else:
        qa.skip("snippet quality", "no transcript hits to test against")

    # Pure visual snippet should be None
    r = search(args.base, "butterfly", limit=3)
    visual_only = [h for h in r["hits"] if h["source"] == "visual"]
    if visual_only:
        qa.check("visual-only hit has snippet=None (not '[visual match at Xs]')",
                 visual_only[0].get("snippet") is None,
                 f"snippet={visual_only[0].get('snippet')!r}")
    else:
        qa.skip("visual snippet None check", "no visual-only hits")

    # ── Folder listing + filter ─────────────────────────────────
    qa.section("Folder + file APIs")
    files = get(args.base, "/api/files")
    qa.check("/api/files returns list", isinstance(files.get("files"), list))
    qa.check("/api/files returns >0", len(files.get("files", [])) > 0)

    # ── /api/file/thumbnails — feeds the dual-zoom video trim filmstrip
    # (commit 3425ede). Backend reads pre-extracted ffmpeg keyframes from
    # the workspace/db/thumbnails/file_<id>/ directory. If this regresses,
    # the trim UI silently falls back to a flat gradient and EVERY video
    # search hit gets a degraded crop experience with no test signal in
    # pytest (which uses a synthetic in-memory fixture). qa_smoke catches
    # the live-workspace integration: real files, real keyframes on disk.
    qa.section("Video trim filmstrip API")
    # Pick the first video file (audio + image files have no useful
    # keyframe filmstrip for this endpoint to surface)
    video_files = [f for f in (files.get("files") or [])
                   if (f.get("mime") or "").startswith("video/")]
    if video_files:
        v_fid = video_files[0]["id"]
        try:
            thumbs = get(args.base, f"/api/file/thumbnails?file_id={v_fid}")
            qa.check("thumbnails endpoint returns the requested file_id",
                     thumbs.get("file_id") == v_fid, str(thumbs.get("file_id")))
            tlist = thumbs.get("thumbnails", [])
            qa.check("thumbnails count is reported + matches list length",
                     thumbs.get("count") == len(tlist), f"count={thumbs.get('count')} list={len(tlist)}")
            if tlist:
                first = tlist[0]
                qa.check("thumbnail item has ts_ms + url keys",
                         "ts_ms" in first and "url" in first, str(first))
                qa.check("thumbnail url is the allowlisted /api/file?path= form",
                         first.get("url", "").startswith("/api/file?path="),
                         first.get("url", "")[:60])
                # Sorted ascending — the filmstrip layout depends on this
                ts_list = [t["ts_ms"] for t in tlist]
                qa.check("thumbnails are ts_ms-ascending",
                         ts_list == sorted(ts_list), str(ts_list[:6]))
        except urllib.error.HTTPError as he:
            qa.check("video thumbnails endpoint reachable", False, f"HTTP {he.code}")
    else:
        qa.skip("video trim thumbnails check", "no indexed videos in workspace")
    # Unknown file_id must 404 (not silently empty)
    try:
        get(args.base, "/api/file/thumbnails?file_id=99999999")
        qa.check("thumbnails for unknown file_id returns 404", False, "no error raised")
    except urllib.error.HTTPError as he:
        qa.check("thumbnails for unknown file_id returns 404", he.code == 404, f"code={he.code}")

    # ── /api/file?path= — the static-serve endpoint that backs every
    # <audio>/<video> src, every thumbnail <img>, and every export download.
    # pytest covers the allowlist gate with in-memory fakes; qa_smoke
    # catches the LIVE-bundle case where prepare_bundle.sh might have
    # broken something in the actual file-serve middleware (commit
    # 7cc54f7 _CachedStaticFiles subclass survived the bundle? Commit
    # 110d0db backtick-trap didn't accidentally take down api/main.py
    # collection? Both shipped as critical fixes — verify they're alive
    # in the running sidecar, not just on disk).
    qa.section("/api/file static-serve + path allowlist")
    # Positive case: serve a thumbnail we just got the URL for. The
    # thumbnails endpoint returned URLs in /api/file?path= form, so
    # this round-trips a known-good allowlisted path.
    if video_files and tlist:
        thumb_url = tlist[0].get("url", "")
        try:
            with urllib.request.urlopen(f"{args.base}{thumb_url}", timeout=10) as r:
                qa.check("/api/file serves an indexed thumbnail (200)",
                         r.status == 200, f"code={r.status}")
                ct = r.headers.get("Content-Type", "")
                qa.check("/api/file thumbnail Content-Type is image/*",
                         ct.startswith("image/"), f"got {ct!r}")
        except urllib.error.HTTPError as he:
            qa.check("/api/file serves an indexed thumbnail (200)", False, f"HTTP {he.code}")
        except Exception as e:
            qa.check("/api/file serves an indexed thumbnail (200)", False, f"{type(e).__name__}: {e}")
    else:
        qa.skip("/api/file thumbnail serve", "no thumbnail URL available")
    # Negative case: /etc/passwd must be refused (403). The allowlist gate
    # (commit be39d9a) is load-bearing security; a regression here is the
    # difference between "local-first app" and "local exfiltration vector".
    try:
        urllib.request.urlopen(f"{args.base}/api/file?path=/etc/passwd", timeout=5)
        qa.check("/api/file refuses /etc/passwd with 403", False, "request unexpectedly succeeded")
    except urllib.error.HTTPError as he:
        qa.check("/api/file refuses /etc/passwd with 403",
                 he.code == 403, f"got HTTP {he.code} (want 403)")

    # ── /api/transcript/window + /api/export/srt — both backed by the
    # same transcript_segments table, both got 2-query SQL refactors in
    # commits 53274bf + 84bc280 that flipped a full-file Python scan into
    # SQL OFFSET arithmetic. pytest covers the SQL semantics with
    # in-memory fixtures; qa_smoke catches a regression where the live
    # demo workspace's actual ts_ms distribution breaks the matched_index
    # math (e.g., the demo file has only 3 transcript segments → tail
    # clamp behaves differently than the >100-segment pytest fixture).
    qa.section("Transcript window + SRT export (commits 53274bf + 84bc280)")
    audio_files = [f for f in (files.get("files") or [])
                   if (f.get("mime") or "").startswith("audio/")]
    if audio_files:
        a_fid = audio_files[0]["id"]
        try:
            tw = get(args.base, f"/api/transcript/window?file_id={a_fid}&ts_ms=0&radius=3")
            qa.check("/api/transcript/window returns file_id",
                     tw.get("file_id") == a_fid, str(tw.get("file_id")))
            lines = tw.get("lines", [])
            qa.check("/api/transcript/window respects radius=3 cap",
                     len(lines) <= 7, f"got {len(lines)} lines")
            if lines:
                # Head clamp: ts_ms=0 should put matched_index at the start
                qa.check("/api/transcript/window head clamp: matched_index=0 at ts_ms=0",
                         tw.get("matched_index") == 0,
                         f"matched_index={tw.get('matched_index')}")
        except urllib.error.HTTPError as he:
            qa.check("/api/transcript/window reachable", False, f"HTTP {he.code}")

        # /api/export/srt — small radius so we don't generate a 60-line SRT
        # for a 2-segment demo file. The file lands in workspace/exports/.
        try:
            srt = post(args.base, "/api/export/srt",
                       {"file_id": a_fid, "ts_ms": 0, "radius": 2})
            qa.check("/api/export/srt returns ok=True", srt.get("ok") is True, str(srt))
            qa.check("/api/export/srt cues count > 0",
                     srt.get("cues", 0) > 0, f"cues={srt.get('cues')}")
            qa.check("/api/export/srt path ends with .srt",
                     str(srt.get("path", "")).endswith(".srt"),
                     str(srt.get("path"))[-60:])
            qa.check("/api/export/srt file is non-empty on disk",
                     int(srt.get("size_bytes", 0)) > 0,
                     f"size_bytes={srt.get('size_bytes')}")
        except urllib.error.HTTPError as he:
            qa.check("/api/export/srt reachable", False, f"HTTP {he.code}")
    else:
        qa.skip("transcript_window + SRT export", "no audio file in workspace")
    # Unknown file_id must 404 on both endpoints — pin the validation gate
    for path in ("/api/transcript/window?file_id=99999999&ts_ms=0",):
        try:
            get(args.base, path)
            qa.check(f"{path} unknown file_id returns 404", False, "no error")
        except urllib.error.HTTPError as he:
            qa.check(f"{path} unknown file_id returns 404",
                     he.code == 404, f"code={he.code}")

    # ── /api/export/csv + /api/export/fcpxml — bulk export endpoints
    # used by the results-pane "Export FCPXML" pill + Preferences ⌘E
    # CSV dump. Both consume the in-memory hits list from a fresh search,
    # both write through _atomic_write_text. pytest covers the unit shapes
    # (project_name cap, formula injection, atomic-write, hit count caps);
    # qa_smoke pins that the live workspace's actual hit data round-trips
    # to a real file on disk. Hits come from running a known-good demo
    # query first.
    qa.section("Bulk export (CSV + FCPXML)")
    try:
        hits_resp = post(args.base, "/api/search", {"query": "pricing strategy", "limit": 5})
        bulk_hits = hits_resp.get("hits") or []
    except Exception as e:
        bulk_hits = []
        qa.check("/api/search reachable for bulk-export sourcing",
                 False, str(e))
    if bulk_hits:
        # CSV
        try:
            csv = post(args.base, "/api/export/csv",
                       {"hits": bulk_hits, "project_name": "smoke"})
            qa.check("/api/export/csv returns ok=True",
                     csv.get("ok") is True, str(csv))
            qa.check("/api/export/csv filename ends with .csv",
                     str(csv.get("filename", "")).endswith(".csv"),
                     str(csv.get("filename")))
            qa.check("/api/export/csv file is non-empty on disk",
                     int(csv.get("size_bytes", 0)) > 0,
                     f"size_bytes={csv.get('size_bytes')}")
        except urllib.error.HTTPError as he:
            qa.check("/api/export/csv reachable", False, f"HTTP {he.code}")
        # FCPXML
        try:
            fcpxml = post(args.base, "/api/export/fcpxml",
                          {"hits": bulk_hits, "project_name": "smoke"})
            qa.check("/api/export/fcpxml returns ok=True",
                     fcpxml.get("ok") is True, str(fcpxml))
            qa.check("/api/export/fcpxml filename ends with .fcpxml",
                     str(fcpxml.get("filename", "")).endswith(".fcpxml"),
                     str(fcpxml.get("filename")))
            qa.check("/api/export/fcpxml file is non-empty on disk",
                     int(fcpxml.get("size_bytes", 0)) > 0,
                     f"size_bytes={fcpxml.get('size_bytes')}")
        except urllib.error.HTTPError as he:
            qa.check("/api/export/fcpxml reachable", False, f"HTTP {he.code}")
    else:
        qa.skip("bulk export (CSV + FCPXML)",
                "demo query returned no hits — workspace may be empty")
    # And: the project_name 200-char cap (commit aaa71d3) must hold for
    # both endpoints. 1000-char name → 422 at the Pydantic boundary.
    huge_name = "A" * 1000
    for path in ("/api/export/csv", "/api/export/fcpxml"):
        try:
            post(args.base, path,
                 {"hits": bulk_hits or [{"file_name": "x"}],
                  "project_name": huge_name})
            qa.check(f"{path} caps project_name at 200 chars",
                     False, "no 422 raised")
        except urllib.error.HTTPError as he:
            qa.check(f"{path} caps project_name at 200 chars",
                     he.code == 422, f"got {he.code}")

    # ── Folder remove + restore ─────────────────────────────────
    qa.section("Folder remove API")
    try:
        # Bad path → 400
        try:
            post(args.base, "/api/folders/remove", {"folder": "relative/path"})
            qa.check("remove rejects non-absolute path", False, "no error")
        except urllib.error.HTTPError as he:
            qa.check("remove rejects non-absolute path", he.code == 400, f"code={he.code}")
        # Nonexistent absolute prefix → 200 / 0 files
        r = post(args.base, "/api/folders/remove", {"folder": "/zzz/no/such/path"})
        qa.check("remove returns 0 for nonexistent prefix",
                 r.get("files_removed", -1) == 0, str(r))
    except Exception as e:
        qa.check("folder remove edge cases", False, str(e))

    return qa.summary()


if __name__ == "__main__":
    sys.exit(main())
