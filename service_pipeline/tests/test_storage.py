"""Tests for tern.storage module."""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from tern.models import FileRecord, Keyframe, OCRSegment, SearchHit, TranscriptSegment
from tern.storage import Store


@pytest.fixture
def temp_store():
    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)
        store = Store(db_path=td_path / "tern.db", chroma_path=td_path / "chroma")
        yield store
        store.close()


# ─── Composite-index usage ─────────────────────────────────────────────
# Three composite indexes were added alongside the original
# single-column ones so hot-path queries could skip the sort step
# entirely. The new indexes are silently useful — a future refactor
# that renamed the index or changed the query shape would still pass
# tests (correctness unchanged, just slower). EXPLAIN QUERY PLAN
# assertions pin the planner's choice so the regression is loud.

def test_composite_index_used_for_transcript_window_query(temp_store):
    """The /api/transcript/window query
    (`WHERE file_id=? ORDER BY start_ms ASC`) must use the composite
    idx_transcript_file_start, NOT the single-column file_id index +
    a separate sort step."""
    rec = FileRecord(path="/tmp/x.mp3", mime="audio/mpeg",
                     duration_ms=60_000, size_bytes=1, mtime=1.0)
    fid = temp_store.upsert_file(rec)
    temp_store.insert_transcript_segments([
        TranscriptSegment(file_id=fid, start_ms=i*1000, end_ms=i*1000+500,
                          text=f"seg {i}", confidence=1.0)
        for i in range(5)
    ])
    plan = temp_store.conn.execute(
        "EXPLAIN QUERY PLAN SELECT start_ms, text FROM transcript_segments "
        "WHERE file_id=? ORDER BY start_ms ASC", (fid,),
    ).fetchall()
    detail = " | ".join(r["detail"] for r in plan)
    assert "idx_transcript_file_start" in detail, (
        f"transcript window query should use composite index; plan: {detail}"
    )
    # And no separate sort step — the index supplies sorted order.
    assert "USE TEMP B-TREE FOR ORDER BY" not in detail, (
        f"unexpected SORT in plan: {detail}"
    )


def test_composite_index_used_for_transcript_window_count_subquery(temp_store):
    """Commit 53274bf refactored /api/transcript/window from a full-file
    load + Python `abs()` scan into a correlated-subquery COUNT that
    finds matched_idx without materializing all rows. The new query
    shape (NOT covered by the old EXPLAIN test which pins the simple
    WHERE+ORDER BY form):

        SELECT COUNT(*) FROM transcript_segments
        WHERE file_id = ?
          AND start_ms < (
            SELECT start_ms FROM transcript_segments
            WHERE file_id = ? ORDER BY abs(start_ms - ?) LIMIT 1
          )

    The outer COUNT is a `(file_id, start_ms < ?)` range — must use the
    composite idx_transcript_file_start. The inner closest-by-abs is
    file_id-filtered + LIMIT 1, so it walks the file_id partition
    (idx_transcript_file_start again) and tracks the running min in
    SQLite's scan loop — no Python materialization.

    Without an EXPLAIN pin here, a future "let's drop the composite
    index to save 16 bytes/row" PR would silently shift the outer
    COUNT to a full scan (3600 rows for a 6-hour audiobook) and we'd
    only notice via end-to-end latency regression. This test catches
    it loud."""
    rec = FileRecord(path="/tmp/y.mp3", mime="audio/mpeg",
                     duration_ms=60_000, size_bytes=1, mtime=1.0)
    fid = temp_store.upsert_file(rec)
    temp_store.insert_transcript_segments([
        TranscriptSegment(file_id=fid, start_ms=i*1000, end_ms=i*1000+500,
                          text=f"seg {i}", confidence=1.0)
        for i in range(10)
    ])
    plan = temp_store.conn.execute(
        "EXPLAIN QUERY PLAN "
        "SELECT COUNT(*) AS idx FROM transcript_segments "
        "WHERE file_id = ? "
        "  AND start_ms < ("
        "    SELECT start_ms FROM transcript_segments "
        "    WHERE file_id = ? ORDER BY abs(start_ms - ?) LIMIT 1"
        "  )",
        (fid, fid, 5000),
    ).fetchall()
    detail = " | ".join(r["detail"] for r in plan)
    # BOTH legs must hit the composite index. If either fell back to
    # idx_transcript_file (single-column), the SQL would still work
    # but pay an extra sort step on the file_id partition.
    assert "idx_transcript_file_start" in detail, (
        f"transcript_window count subquery must use composite index "
        f"on both legs; plan: {detail}"
    )


def test_composite_index_used_for_keyframe_first_per_file(temp_store):
    """search_filename + /api/files batched first-keyframe lookup use
    `ROW_NUMBER() OVER (PARTITION BY file_id ORDER BY ts_ms)`. The
    composite idx_keyframes_file_ts skips the per-partition sort."""
    from tern.models import Keyframe
    rec = FileRecord(path="/tmp/v.mp4", mime="video/mp4",
                     duration_ms=10_000, size_bytes=1, mtime=1.0)
    fid = temp_store.upsert_file(rec)
    temp_store.insert_keyframes_batch([
        (Keyframe(file_id=fid, ts_ms=ts,
                  thumbnail_path=f"/tmp/kf_{ts}.jpg", embedding_dim=2),
         [0.1, 0.2])
        for ts in (0, 5000, 9000)
    ])
    plan = temp_store.conn.execute(
        "EXPLAIN QUERY PLAN SELECT file_id, ts_ms FROM ("
        "  SELECT file_id, ts_ms, ROW_NUMBER() OVER (PARTITION BY file_id ORDER BY ts_ms) AS rn"
        "  FROM keyframes WHERE file_id IN (?)"
        ") WHERE rn = 1", (fid,),
    ).fetchall()
    detail = " | ".join(r["detail"] for r in plan)
    assert "idx_keyframes_file_ts" in detail, (
        f"keyframe window-fn query should use composite index; plan: {detail}"
    )


def test_store_init_sets_performance_pragmas(temp_store):
    """Store(...) must apply WAL + NORMAL synchronous + 64 MB
    cache + in-memory temp store on every connection. A future refactor
    that drops the executescript block would silently regress search
    latency (no error, just slower) — this test fails loudly instead."""
    c = temp_store.conn
    assert c.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert c.execute("PRAGMA synchronous").fetchone()[0] == 1   # NORMAL
    # cache_size negative = KB; positive = pages. We set -64000.
    assert c.execute("PRAGMA cache_size").fetchone()[0] == -64000
    assert c.execute("PRAGMA temp_store").fetchone()[0] == 2   # MEMORY


def test_store_init_raises_hnsw_ef_search(temp_store):
    """A fresh keyframes collection must be built with our ef_search, not
    Chroma's default of 100.

    At 100 the HNSW traversal does not reliably reach every vector. Measured
    on a healthy 430-keyframe index: the one true match for "sushi" (cosine
    0.1280) came back in 1 of 5 fresh processes at the production fetch size.
    The other four never had it as a candidate, so the visual noise floor saw
    nothing above threshold and the search returned zero hits — a query that
    works on one machine and silently fails on another."""
    from tern.storage import HNSW_EF_SEARCH

    cfg = (temp_store.vectors.configuration_json or {}).get("hnsw", {})
    assert cfg.get("ef_search") == HNSW_EF_SEARCH, (
        f"new collection has ef_search={cfg.get('ef_search')}, expected "
        f"{HNSW_EF_SEARCH}; Chroma's default 100 loses true nearest neighbours"
    )


def test_store_init_builds_the_keyframe_index_in_cosine_space(temp_store):
    """Passing `configuration` to get_or_create_collection makes Chroma 1.x
    ignore the legacy "hnsw:space" metadata, so the ef_search change briefly
    built every NEW collection in l2. Visual scores became 1 - L2 distance,
    the noise floor never engaged, and a fresh index answered almost any
    query with thirty unrelated frames. Existing indexes kept cosine, which
    is why it only showed up on a clean install."""
    cfg = (temp_store.vectors.configuration_json or {}).get("hnsw", {})
    assert cfg.get("space") == "cosine", (
        f"new keyframes collection uses space={cfg.get('space')!r}; the "
        f"visual channel's scores and noise floor assume cosine"
    )


def test_store_init_migrates_ef_search_on_an_existing_collection(tmp_path):
    """`get_or_create_collection(configuration=...)` only applies the config
    when it CREATES. Every workspace indexed before this landed — the dev
    demo, and any archive already sitting on a customer's Mac — would keep
    ef_search=100 forever and never see the fix. Opening the Store has to
    raise it in place, which is a metadata write rather than a re-index."""
    from tern.storage import HNSW_EF_SEARCH

    db, chroma = tmp_path / "tern.db", tmp_path / "chroma"
    first = Store(db_path=db, chroma_path=chroma)
    # Simulate a collection built by the older code.
    first.vectors.modify(configuration={"hnsw": {"ef_search": 100}})
    first.close()

    reopened = Store(db_path=db, chroma_path=chroma)
    try:
        cfg = (reopened.vectors.configuration_json or {}).get("hnsw", {})
        assert cfg.get("ef_search") == HNSW_EF_SEARCH, (
            f"reopening left ef_search={cfg.get('ef_search')}; an archive "
            "indexed by an older build keeps the recall hole forever"
        )
    finally:
        reopened.close()


def test_upsert_and_get_file(temp_store):
    record = FileRecord(
        path="/tmp/test.mp4",
        mime="video/mp4",
        duration_ms=60000,
        size_bytes=1024,
        mtime=1234567890.0,
        status="pending",
    )
    file_id = temp_store.upsert_file(record)
    assert file_id > 0

    retrieved = temp_store.get_file_by_path("/tmp/test.mp4")
    assert retrieved is not None
    assert retrieved.path == "/tmp/test.mp4"
    assert retrieved.duration_ms == 60000


def test_upsert_idempotent(temp_store):
    """Upsert with same path should not duplicate."""
    record = FileRecord(
        path="/tmp/test.mp4", mime="video/mp4", duration_ms=60000,
        size_bytes=1024, mtime=1234567890.0,
    )
    id1 = temp_store.upsert_file(record)
    id2 = temp_store.upsert_file(record)
    assert id1 == id2

    files = temp_store.list_files()
    assert len(files) == 1


def test_set_file_status(temp_store):
    record = FileRecord(
        path="/tmp/test.mp4", mime="video/mp4", duration_ms=60000,
        size_bytes=1024, mtime=1234567890.0, status="pending",
    )
    file_id = temp_store.upsert_file(record)
    temp_store.set_file_status(file_id, "done")
    retrieved = temp_store.get_file(file_id)
    assert retrieved.status == "done"


def test_set_file_metadata_round_trips_dict(temp_store):
    """Photo indexing uses set_file_metadata to stash HEIC EXIF (date_taken,
    make, model, GPS, dims) keyed to a file row. The metadata column stores
    JSON; verify the round-trip preserves the structure so the detail-pane
    chips render the same dict the ingester put in."""
    rec = FileRecord(path="/tmp/photo.heic", mime="image/heic",
                     duration_ms=0, size_bytes=1, mtime=1.0)
    fid = temp_store.upsert_file(rec)
    meta = {
        "date_taken": "2024-01-15T10:30:00",
        "make": "Apple",
        "model": "iPhone 15 Pro",
        "gps_lat": 52.5200,
        "gps_lon": 13.4050,
        "width": 4032,
        "height": 3024,
    }
    temp_store.set_file_metadata(fid, meta)
    retrieved = temp_store.get_file(fid)
    # _row_to_file_record JSON-parses the column back to a dict
    assert retrieved.metadata == meta


def test_set_file_metadata_empty_dict_stores_null(temp_store):
    """The `if metadata else None` branch in set_file_metadata stores NULL
    for an empty dict. `_row_to_file_record` reads NULL as Python None.
    Pin this so a future refactor that swaps `if metadata` to
    `if metadata is not None` (which would store the empty dict as `{}`
    instead of NULL) is caught — the difference matters for the
    /api/files response shape which the detail-pane reads."""
    rec = FileRecord(path="/tmp/empty.heic", mime="image/heic",
                     duration_ms=0, size_bytes=1, mtime=1.0)
    fid = temp_store.upsert_file(rec)
    temp_store.set_file_metadata(fid, {})
    retrieved = temp_store.get_file(fid)
    assert retrieved.metadata is None, (
        f"empty-dict metadata should store as NULL → None on read, "
        f"got {retrieved.metadata!r}"
    )


def test_set_file_metadata_preserves_unicode(temp_store):
    """HEIC EXIF may contain non-ASCII fields — Cyrillic camera makes
    (`Цифровая камера`), CJK locations, accented camera models. The
    JSON encoder MUST NOT mojibake-escape these to `\\uXXXX` (which is
    technically valid JSON but bloats the column and looks awful when
    a user inspects the file row directly). json.dumps defaults preserve
    Unicode in Python 3; this pin catches a regression where someone
    added ensure_ascii=True or .encode().decode() somewhere."""
    rec = FileRecord(path="/tmp/unicode.heic", mime="image/heic",
                     duration_ms=0, size_bytes=1, mtime=1.0)
    fid = temp_store.upsert_file(rec)
    meta = {
        "make": "Цифровая камера",   # Cyrillic
        "model": "相机 1",             # CJK
        "city": "São Paulo",          # diacritic
    }
    temp_store.set_file_metadata(fid, meta)
    # Round-trip via parsed metadata is fine — but ALSO check the raw
    # stored bytes don't have \uXXXX escapes (which would mean ensure_ascii
    # leaked back in). Query the raw column.
    raw = temp_store.conn.execute(
        "SELECT metadata FROM files WHERE id=?", (fid,)
    ).fetchone()["metadata"]
    assert "\\u" not in raw, (
        f"unicode metadata should not be \\uXXXX-escaped in the JSON "
        f"column; got: {raw!r}"
    )
    # And the round-trip yields the same dict
    retrieved = temp_store.get_file(fid)
    assert retrieved.metadata == meta


def test_upsert_file_preserves_unicode_metadata(temp_store):
    """Twin defect to set_file_metadata's ensure_ascii=False (commit
    f1cdbc3). upsert_file ALSO json.dumps the metadata column for the
    initial INSERT path — and was using the same default-ASCII-escape
    serializer. Without this pin, a regression that drops ensure_ascii
    on EITHER call site stays silent (since /api/files reads still work
    via json.loads) but the stored row gets ugly.

    Mirrors the test_set_file_metadata_preserves_unicode shape: assert
    the raw stored bytes contain NO `\\u` escape sequences AND the
    round-trip yields the same dict."""
    rec = FileRecord(
        path="/tmp/insert_unicode.heic", mime="image/heic",
        duration_ms=0, size_bytes=1, mtime=1.0,
        metadata={
            "make": "Цифровая камера",  # Cyrillic
            "model": "相机 1",            # CJK
            "city": "São Paulo",         # diacritic
        },
    )
    fid = temp_store.upsert_file(rec)
    raw = temp_store.conn.execute(
        "SELECT metadata FROM files WHERE id=?", (fid,)
    ).fetchone()["metadata"]
    assert "\\u" not in raw, (
        f"upsert_file should not \\uXXXX-escape unicode metadata; got: {raw!r}"
    )
    retrieved = temp_store.get_file(fid)
    assert retrieved.metadata == rec.metadata


def test_insert_and_search_transcript(temp_store):
    record = FileRecord(
        path="/tmp/podcast.mp3", mime="audio/mpeg", duration_ms=60000,
        size_bytes=1024, mtime=1234567890.0,
    )
    file_id = temp_store.upsert_file(record)

    segments = [
        TranscriptSegment(file_id=file_id, start_ms=0, end_ms=5000, text="Welcome to the show"),
        TranscriptSegment(file_id=file_id, start_ms=5000, end_ms=10000, text="Today we talk about pricing strategy"),
        TranscriptSegment(file_id=file_id, start_ms=10000, end_ms=15000, text="The key insight is value pricing"),
    ]
    temp_store.insert_transcript_segments(segments)

    hits = temp_store.search_transcript("pricing strategy")
    assert len(hits) >= 1
    assert any("pricing" in h.snippet.lower() for h in hits)


def test_search_transcript_file_filter_restricts_results(temp_store):
    """The `file_filter=[id1, id2, ...]` parameter must restrict
    transcript hits to ONLY files whose id is in the list. This
    parameter is load-bearing for the workspace-switcher / folder-
    limit feature path on /api/search — without it, a "limit to
    folder X" search would still pull transcript hits from outside
    that folder and post-filter in Python (slow + correctness-
    fragile under pagination). Untested before now, despite the
    SQL `AND t.file_id IN (...)` clause being trivially regressable
    if a refactor consolidates the conditional-WHERE branches."""
    rec_a = FileRecord(path="/tmp/podcast_a.mp3", mime="audio/mpeg",
                       duration_ms=60000, size_bytes=1, mtime=1.0)
    rec_b = FileRecord(path="/tmp/podcast_b.mp3", mime="audio/mpeg",
                       duration_ms=60000, size_bytes=1, mtime=1.0)
    rec_c = FileRecord(path="/tmp/podcast_c.mp3", mime="audio/mpeg",
                       duration_ms=60000, size_bytes=1, mtime=1.0)
    fid_a = temp_store.upsert_file(rec_a)
    fid_b = temp_store.upsert_file(rec_b)
    fid_c = temp_store.upsert_file(rec_c)
    # Same matching token "stanford" in all three files — so an
    # unfiltered search would return 3 hits, and the filter is the
    # ONLY thing narrowing to one.
    for fid in (fid_a, fid_b, fid_c):
        temp_store.insert_transcript_segments([
            TranscriptSegment(file_id=fid, start_ms=0, end_ms=5000,
                              text="I went to stanford university"),
        ])

    # Unfiltered: all three files match.
    hits_all = temp_store.search_transcript("stanford")
    file_ids_all = {h.file_id for h in hits_all}
    assert file_ids_all == {fid_a, fid_b, fid_c}, (
        f"unfiltered search should hit all 3 files; got {file_ids_all}"
    )

    # Filtered to just A + C: must skip B even though it matches.
    hits_filtered = temp_store.search_transcript("stanford", file_filter=[fid_a, fid_c])
    file_ids_filtered = {h.file_id for h in hits_filtered}
    assert file_ids_filtered == {fid_a, fid_c}, (
        f"file_filter=[A, C] should return ONLY A + C; got {file_ids_filtered} "
        f"(B leaked through = AND t.file_id IN (?) clause broken)"
    )

    # Filter to a non-existent file_id: zero hits, no SQL error.
    hits_none = temp_store.search_transcript("stanford", file_filter=[999_999])
    assert hits_none == [], f"filter to unknown file_id should return []; got {hits_none}"


def test_search_ocr_file_filter_restricts_results(temp_store):
    """Same file_filter contract for OCR as for transcript — untested
    before. The SQL `AND o.file_id IN (...)` clause is structurally
    identical so the same regression class applies."""
    from tern.models import OCRSegment
    rec_a = FileRecord(path="/tmp/slide_a.mp4", mime="video/mp4",
                       duration_ms=60000, size_bytes=1, mtime=1.0)
    rec_b = FileRecord(path="/tmp/slide_b.mp4", mime="video/mp4",
                       duration_ms=60000, size_bytes=1, mtime=1.0)
    fid_a = temp_store.upsert_file(rec_a)
    fid_b = temp_store.upsert_file(rec_b)
    # Same matching OCR text in both files.
    for fid in (fid_a, fid_b):
        temp_store.insert_ocr_segments([
            OCRSegment(file_id=fid, frame_ts_ms=1000,
                       text="Q3 Pricing Strategy",
                       confidence=0.95, bbox=[0.1, 0.1, 0.5, 0.2]),
        ])

    # Unfiltered: both files.
    hits_all = temp_store.search_ocr("pricing")
    file_ids_all = {h.file_id for h in hits_all}
    assert file_ids_all == {fid_a, fid_b}

    # Filter to A only.
    hits_filtered = temp_store.search_ocr("pricing", file_filter=[fid_a])
    file_ids_filtered = {h.file_id for h in hits_filtered}
    assert file_ids_filtered == {fid_a}, (
        f"file_filter=[A] should return ONLY A; got {file_ids_filtered}"
    )


def test_search_filename_file_filter_strict_filter(temp_store):
    """Extends the existing search_filename file_filter coverage (a single
    happy-path case at line 477) to assert that a filter naming a
    NON-MATCHING file_id returns [] even when the query token would
    otherwise match other files. Previously only the positive case
    was pinned; this fires if a refactor accidentally drops the filter
    when no file in the filter matches the query."""
    rec_match = FileRecord(path="/tmp/mountain.jpg", mime="image/jpeg",
                           duration_ms=0, size_bytes=1, mtime=1.0)
    rec_other = FileRecord(path="/tmp/office.jpg", mime="image/jpeg",
                           duration_ms=0, size_bytes=1, mtime=1.0)
    fid_match = temp_store.upsert_file(rec_match)
    fid_other = temp_store.upsert_file(rec_other)

    # Filter to a file whose name does NOT contain the query token —
    # must return [] even though the OTHER file would match the query.
    hits = temp_store.search_filename("mountain", file_filter=[fid_other])
    assert hits == [], (
        f"filter=[fid_other] (no 'mountain' in name) should return []; got {hits}"
    )

    # Sanity: filter=[fid_match] returns the match (proves the filter
    # isn't ignored — just that it correctly excludes the non-matcher).
    hits = temp_store.search_filename("mountain", file_filter=[fid_match])
    assert len(hits) == 1 and hits[0].file_id == fid_match


def test_stats(temp_store):
    record = FileRecord(
        path="/tmp/test.mp4", mime="video/mp4", duration_ms=60000,
        size_bytes=1024, mtime=1234567890.0, status="done",
    )
    file_id = temp_store.upsert_file(record)
    temp_store.insert_transcript_segments([
        TranscriptSegment(file_id=file_id, start_ms=0, end_ms=5000, text="test segment"),
    ])

    stats = temp_store.stats()
    assert stats["files_total"] == 1
    assert stats["files_done"] == 1
    assert stats["transcript_segments"] == 1
    assert stats["total_duration_ms"] == 60000


def test_stats_per_type_breakdown_only_counts_done(temp_store):
    """Pin the conditional-aggregation behavior after the 9→4 query
    consolidation: per-type counts must include `status='done'` filter,
    so a pending/errored file does NOT inflate files_video/audio/image.
    Also total_duration_ms must EXCLUDE pending/errored.

    Without this test, the consolidation could quietly drop the
    status filter on one of the SUM(CASE WHEN ...) branches and the
    only existing assertion (single done video) wouldn't catch it."""
    # 2 done videos (one with duration), 1 pending video (should NOT
    # count in files_video, files_done, or total_duration_ms).
    temp_store.upsert_file(FileRecord(
        path="/tmp/v1.mp4", mime="video/mp4", duration_ms=10000,
        size_bytes=1, mtime=1.0, status="done"))
    temp_store.upsert_file(FileRecord(
        path="/tmp/v2.mp4", mime="video/mp4", duration_ms=5000,
        size_bytes=1, mtime=1.0, status="done"))
    temp_store.upsert_file(FileRecord(
        path="/tmp/v_pending.mp4", mime="video/mp4", duration_ms=99999,
        size_bytes=1, mtime=1.0, status="indexing"))
    # 1 done audio + 1 errored audio (errored must not count as done).
    temp_store.upsert_file(FileRecord(
        path="/tmp/a1.mp3", mime="audio/mpeg", duration_ms=3000,
        size_bytes=1, mtime=1.0, status="done"))
    temp_store.upsert_file(FileRecord(
        path="/tmp/a_err.mp3", mime="audio/mpeg", duration_ms=1000,
        size_bytes=1, mtime=1.0, status="error"))
    # 1 done image (images have duration_ms=0).
    temp_store.upsert_file(FileRecord(
        path="/tmp/p1.jpg", mime="image/jpeg", duration_ms=0,
        size_bytes=1, mtime=1.0, status="done"))

    s = temp_store.stats()
    # 6 total, 4 done (2 video + 1 audio + 1 image), 2 not-done (pending video + errored audio)
    assert s["files_total"] == 6, f"files_total wrong: {s['files_total']}"
    assert s["files_done"] == 4,  f"files_done should exclude pending+errored: {s['files_done']}"
    assert s["files_video"] == 2, f"pending video leaked into files_video: {s['files_video']}"
    assert s["files_audio"] == 1, f"errored audio leaked into files_audio: {s['files_audio']}"
    assert s["files_image"] == 1, f"files_image wrong: {s['files_image']}"
    # Duration sums only done: 10000 + 5000 + 3000 + 0 = 18000. Must NOT
    # include pending video's 99999 or errored audio's 1000.
    assert s["total_duration_ms"] == 18000, f"duration leak: {s['total_duration_ms']}"


def test_ocr_insert_and_search(temp_store):
    record = FileRecord(
        path="/tmp/video.mp4", mime="video/mp4", duration_ms=60000,
        size_bytes=1024, mtime=1234567890.0,
    )
    file_id = temp_store.upsert_file(record)

    ocr_segments = [
        OCRSegment(
            file_id=file_id, frame_ts_ms=1000,
            text="Salesforce Dashboard - Q3 Pipeline",
            confidence=0.95, bbox=[0.1, 0.1, 0.5, 0.05],
        ),
    ]
    temp_store.insert_ocr_segments(ocr_segments)

    hits = temp_store.search_ocr("Salesforce")
    assert len(hits) >= 1
    assert "salesforce" in hits[0].snippet.lower() or "Salesforce" in hits[0].snippet


def test_ocr_snippet_includes_keyframe_context(temp_store):
    """When the matched OCR line is part of a larger keyframe (Apple Vision
    splits a slide into multiple lines), the snippet should stitch in the
    other lines so the user actually understands the slide."""
    record = FileRecord(path="/tmp/yc.mp4", mime="video/mp4", duration_ms=60000,
                        size_bytes=1024, mtime=1.0)
    file_id = temp_store.upsert_file(record)
    # All four OCR rows share the SAME frame_ts_ms — they're lines of one slide.
    temp_store.insert_ocr_segments([
        OCRSegment(file_id=file_id, frame_ts_ms=5000, text="Y Combinator", confidence=0.95, bbox=[0,0,1,0.1]),
        OCRSegment(file_id=file_id, frame_ts_ms=5000, text="Lecture 1", confidence=0.95, bbox=[0,0.1,1,0.1]),
        OCRSegment(file_id=file_id, frame_ts_ms=5000, text="Stanford", confidence=0.95, bbox=[0,0.2,1,0.1]),
        OCRSegment(file_id=file_id, frame_ts_ms=5000, text="2014", confidence=0.95, bbox=[0,0.3,1,0.1]),
    ])
    hits = temp_store.search_ocr("Stanford")
    assert len(hits) == 1
    snip = hits[0].snippet
    # The matched word is wrapped in <mark>
    assert "<mark>Stanford</mark>" in snip
    # And surrounding lines from the same keyframe come along for context
    assert "Y Combinator" in snip
    assert "2014" in snip


def test_highlight_all_query_words():
    """Multi-word queries should mark EVERY query token in the snippet, not
    just the one FTS5 picked. Used by the search engine after fusion."""
    from tern.storage import _highlight_all_query_words
    snip = _highlight_all_query_words(
        "I was a <mark>Stanford</mark> student that year",
        "stanford student",
    )
    # Existing mark preserved, plus a fresh one around `student`
    assert "<mark>Stanford</mark>" in snip
    assert "<mark>student</mark>" in snip
    # Doesn't double-wrap inside existing marks
    assert "<mark><mark>" not in snip
    # Doesn't mark inside HTML tags
    assert snip.count("<mark>") == 2


def test_search_filename_matches_basename_tokens(temp_store):
    """The filename channel returns a representative hit for every indexed
    file whose basename contains a query token (>=3 chars, separator-aware).
    Compensates for SigLIP's weak grounding on single-word visual queries."""
    rec1 = FileRecord(path="/tmp/picsum_mountain.jpg", mime="image/jpeg",
                      duration_ms=0, size_bytes=1, mtime=1.0)
    rec2 = FileRecord(path="/tmp/picsum_office.jpg", mime="image/jpeg",
                      duration_ms=0, size_bytes=1, mtime=1.0)
    rec3 = FileRecord(path="/tmp/IMG_iphone_15_holiday.heic", mime="image/heic",
                      duration_ms=0, size_bytes=1, mtime=1.0)
    rec4 = FileRecord(path="/tmp/totally_unrelated.mp4", mime="video/mp4",
                      duration_ms=0, size_bytes=1, mtime=1.0)
    f_mountain = temp_store.upsert_file(rec1)
    f_office = temp_store.upsert_file(rec2)
    f_iphone = temp_store.upsert_file(rec3)
    f_other = temp_store.upsert_file(rec4)

    # whole-word match
    hits = temp_store.search_filename("mountain")
    assert len(hits) == 1
    assert hits[0].file_id == f_mountain
    assert hits[0].score == 0.50
    assert hits[0].source == "visual"

    # substring match within tokenized basename ("iphone" appears in
    # "img_iphone_15_holiday" as a token)
    hits = temp_store.search_filename("iphone")
    assert len(hits) == 1
    assert hits[0].file_id == f_iphone

    # short tokens (< 3 chars) are ignored to avoid false positives
    hits = temp_store.search_filename("a")
    assert hits == []

    # query with no basename match returns nothing
    hits = temp_store.search_filename("zxqv_no_match")
    assert hits == []

    # file_filter restricts the candidate set
    hits = temp_store.search_filename("picsum", file_filter=[f_office])
    assert len(hits) == 1
    assert hits[0].file_id == f_office


def test_search_filename_cjk_2char_query_matches_basename(temp_store):
    """Commit 63ff96b added the CJK 2-char-minimum carve-out to
    `SearchEngine._apply_filename_boost` (the BOOST layer). That fix was
    INCOMPLETE because `storage.py search_filename` (the FETCH layer)
    still applied a flat 3-char minimum — so a CJK 2-char query like
    `会議` was silently filtered to zero candidates BEFORE the boost
    code could even see them. Net, previously:
        query="会議"
          → search_filename returns [] (filtered at line 735)
          → _apply_filename_boost has no hits to boost
        Japanese / Chinese / Korean users got NO filename hits for
        the most common search terms in their language.

    This pins the FETCH-layer carve-out so a 2-char CJK query lifts
    the namesake file. Three positive cases (Japanese ideograph,
    Hiragana, Hangul) + one negative (a 2-char Latin token must STILL
    be rejected — the carve-out doesn't relax Latin's 3-char floor)."""
    rec_kaigi = FileRecord(
        path="/tmp/会議録_2024.mp4",  # "kaigiroku" = meeting record
        mime="video/mp4", duration_ms=0, size_bytes=1, mtime=1.0,
    )
    rec_yosan = FileRecord(
        path="/tmp/予算_review.mp4",  # "yosan" = budget
        mime="video/mp4", duration_ms=0, size_bytes=1, mtime=1.0,
    )
    rec_hoesa = FileRecord(
        path="/tmp/회사_meeting.mp4",  # Hangul "hoesa" = company
        mime="video/mp4", duration_ms=0, size_bytes=1, mtime=1.0,
    )
    rec_unrelated = FileRecord(
        path="/tmp/random_video.mp4",
        mime="video/mp4", duration_ms=0, size_bytes=1, mtime=1.0,
    )
    f_kaigi = temp_store.upsert_file(rec_kaigi)
    f_yosan = temp_store.upsert_file(rec_yosan)
    f_hoesa = temp_store.upsert_file(rec_hoesa)
    temp_store.upsert_file(rec_unrelated)

    # 2-char CJK ideograph query MUST match the file with the same chars.
    hits = temp_store.search_filename("会議")
    assert len(hits) == 1, (
        f"2-char CJK ideograph query should match the namesake file; got {hits}"
    )
    assert hits[0].file_id == f_kaigi

    # 2-char Japanese ideograph (different — using 予算 = budget).
    hits = temp_store.search_filename("予算")
    assert len(hits) == 1 and hits[0].file_id == f_yosan

    # 2-char Hangul query.
    hits = temp_store.search_filename("회사")
    assert len(hits) == 1 and hits[0].file_id == f_hoesa

    # Negative: 2-char Latin tokens STILL get filtered (the carve-out
    # is CJK-only). If a future "relax all length filters" refactor
    # ever drops the Latin floor, this test fires.
    hits = temp_store.search_filename("re")  # "re" appears in many basenames as a substring
    assert hits == [], (
        f"2-char Latin token should NOT match — the CJK carve-out is "
        f"script-specific. Got: {hits}"
    )


def test_search_filename_directory_name_does_not_false_positive(temp_store):
    """Critical contract: search_filename matches BASENAME tokens, NOT
    full-path tokens. Commit 7d58e0c added a coarse SQL
    `LOWER(path) LIKE '%token%'` pre-filter — that would match files
    whose DIRECTORY contains the token (not just the basename). The
    Python word-boundary loop after the SQL filter is what enforces
    the basename-only semantic. Without this test, a refactor could
    silently drop the Python loop (e.g., 'SQL is fast enough now')
    and every file under /Users/alice/Podcasts/ would falsely match
    a query for 'podcasts' or 'alice' or 'users'."""
    inside = FileRecord(
        path="/tmp/MyPodcasts/episode_one.mp3",
        mime="audio/mpeg", duration_ms=0, size_bytes=1, mtime=1.0,
    )
    inside_id = temp_store.upsert_file(inside)

    # 'mypodcasts' appears in the DIRECTORY but NOT in the basename
    # ('episode_one'). search_filename must NOT return this file.
    hits = temp_store.search_filename("mypodcasts")
    assert hits == [], (
        f"search_filename leaked a directory-name match: query 'mypodcasts' "
        f"should NOT return /tmp/MyPodcasts/episode_one.mp3 since the token "
        f"is in the directory, not the basename. Got: {hits}"
    )

    # Sanity: the same file IS returned for a real basename token.
    hits2 = temp_store.search_filename("episode")
    assert len(hits2) == 1 and hits2[0].file_id == inside_id, (
        "basename-token match broken — episode_one.mp3 should match 'episode'"
    )


def test_search_filename_returns_one_hit_per_file_with_keyframe(temp_store):
    """When a file HAS keyframes (videos / images do; audio doesn't),
    the returned hit's ts_ms comes from the LOWEST-ts_ms keyframe and
    thumbnail_path matches that keyframe's path. Before commit 7d58e0c
    it used an N+1 per-match query; the fix batched it with ROW_NUMBER
    OVER PARTITION. This test pins both the correctness (lowest
    ts_ms wins) AND that thumbnail_path is populated, since the
    empty.js + row.js render path depends on it."""
    from tern.models import Keyframe
    rec = FileRecord(
        path="/tmp/cat_video.mp4",
        mime="video/mp4", duration_ms=10000, size_bytes=1, mtime=1.0,
    )
    fid = temp_store.upsert_file(rec)
    # Insert THREE keyframes out of order so we can verify ROW_NUMBER
    # really picks the lowest-ts_ms one (a wrong ORDER BY in the
    # window query would silently return whatever).
    temp_store.insert_keyframes_batch([
        (Keyframe(file_id=fid, ts_ms=5000, thumbnail_path="/tmp/kf_mid.jpg",
                  embedding_dim=2), [0.1, 0.2]),
        (Keyframe(file_id=fid, ts_ms=0,    thumbnail_path="/tmp/kf_first.jpg",
                  embedding_dim=2), [0.3, 0.4]),
        (Keyframe(file_id=fid, ts_ms=9000, thumbnail_path="/tmp/kf_last.jpg",
                  embedding_dim=2), [0.5, 0.6]),
    ])

    hits = temp_store.search_filename("cat")
    assert len(hits) == 1
    h = hits[0]
    assert h.ts_ms == 0, f"should pick lowest-ts_ms keyframe, got ts_ms={h.ts_ms}"
    assert h.thumbnail_path == "/tmp/kf_first.jpg", (
        f"thumbnail_path should match the lowest-ts_ms keyframe, "
        f"got {h.thumbnail_path}"
    )


# ─── search_visual — multi-file results + N+1 perf guard ──────────────
# Before the N+1 fix, search_visual issued one
# `SELECT path FROM files WHERE id=?` per ChromaDB hit. With the
# default limit=50, that's 50 SQL queries on the search hot path —
# ~10 ms of pointless serialization even on fast SSDs, more after
# idle when the cache is cold. The fix batches into a single
# `IN (?, ?, ?…)` join, mirroring search_filename's 7d58e0c pattern.
# Pin both correctness (multi-file results join right) and the perf
# property (only ONE files-table query, no matter the result count).

def test_search_visual_returns_paths_across_multiple_files(temp_store):
    """Visual hits spanning multiple files must all resolve to the
    correct file_path — catches a bad zip / wrong dict lookup that
    would silently drop hits or mismatch paths."""
    fids = []
    for i, name in enumerate(["alpha.mp4", "beta.mp4", "gamma.mp4"]):
        rec = FileRecord(
            path=f"/tmp/{name}", mime="video/mp4",
            duration_ms=10000, size_bytes=1, mtime=float(i),
        )
        fids.append(temp_store.upsert_file(rec))
    # Two keyframes per file → 6 visual vectors, each tied to its file.
    items = []
    for fid in fids:
        for ts in (0, 5000):
            items.append((
                Keyframe(file_id=fid, ts_ms=ts,
                         thumbnail_path=f"/tmp/file_{fid}/kf_{ts}.jpg",
                         embedding_dim=4),
                [0.1 * fid, 0.1 * fid + 0.01 * ts, 0.0, 0.0],
            ))
    temp_store.insert_keyframes_batch(items)

    hits = temp_store.search_visual([0.2, 0.0, 0.0, 0.0], limit=10)
    assert len(hits) == 6, f"expected all 6 visual hits, got {len(hits)}"
    # Every hit's file_path must match its file_id's row in `files`.
    path_by_fid = {f"/tmp/alpha.mp4", "/tmp/beta.mp4", "/tmp/gamma.mp4"}
    seen_paths = {h.file_path for h in hits}
    assert seen_paths == path_by_fid, f"path mismatch: {seen_paths}"


def test_search_visual_skips_orphan_vectors(temp_store):
    """A vector whose `files` row was deleted (cleanup_file race,
    manual prune) must be silently skipped — NOT raise, NOT poison
    the result list with None paths. Real bug we'd hit if a user
    cancelled indexing partway."""
    rec_keep = FileRecord(path="/tmp/keep.mp4", mime="video/mp4",
                          duration_ms=10000, size_bytes=1, mtime=1.0)
    rec_orphan = FileRecord(path="/tmp/orphan.mp4", mime="video/mp4",
                            duration_ms=10000, size_bytes=1, mtime=2.0)
    keep_id = temp_store.upsert_file(rec_keep)
    orphan_id = temp_store.upsert_file(rec_orphan)
    temp_store.insert_keyframes_batch([
        (Keyframe(file_id=keep_id, ts_ms=0,
                  thumbnail_path="/tmp/kf_keep.jpg", embedding_dim=2),
         [0.5, 0.5]),
        (Keyframe(file_id=orphan_id, ts_ms=0,
                  thumbnail_path="/tmp/kf_orphan.jpg", embedding_dim=2),
         [0.6, 0.4]),
    ])
    # Manually delete the orphan's `files` row — simulates the race.
    temp_store.conn.execute("DELETE FROM files WHERE id=?", (orphan_id,))
    temp_store.conn.commit()

    hits = temp_store.search_visual([0.55, 0.45], limit=10)
    # The keep file's vector must come back; orphan must be skipped.
    assert len(hits) == 1
    assert hits[0].file_id == keep_id
    assert hits[0].file_path == "/tmp/keep.mp4"


def test_search_visual_uses_single_batched_files_query(temp_store):
    """Perf regression guard: 5 visual hits across 5 files must hit
    the `files` table exactly ONCE, not five times. Uses SQLite's
    trace callback to count actual SQL statements executed."""
    fids = []
    for i in range(5):
        rec = FileRecord(
            path=f"/tmp/perf_{i}.mp4", mime="video/mp4",
            duration_ms=1000, size_bytes=1, mtime=float(i),
        )
        fids.append(temp_store.upsert_file(rec))
    temp_store.insert_keyframes_batch([
        (Keyframe(file_id=fid, ts_ms=0,
                  thumbnail_path=f"/tmp/kf_{fid}.jpg", embedding_dim=2),
         [0.1 * fid, 0.0])
        for fid in fids
    ])

    queries: list[str] = []
    temp_store.conn.set_trace_callback(lambda sql: queries.append(sql))
    try:
        hits = temp_store.search_visual([0.2, 0.0], limit=10)
    finally:
        temp_store.conn.set_trace_callback(None)
    assert len(hits) == 5

    files_lookups = [q for q in queries
                     if "FROM files" in q and "WHERE id" in q]
    assert len(files_lookups) == 1, (
        f"search_visual should batch file_path lookups into ONE query, "
        f"got {len(files_lookups)}: {files_lookups}"
    )


def test_heic_exif_extraction_recovers_iphone_metadata(tmp_path):
    """iPhone photos are .heic by default (iOS 11+). Without
    pillow_heif registration + the modern getexif() API, every .heic indexed
    by Tern dropped its date_taken/make/model metadata silently. This test
    builds a synthetic iPhone-tagged HEIC and asserts the round-trip."""
    pytest.importorskip("pillow_heif", reason="pillow-heif is the whole point of this test")
    from pillow_heif import register_heif_opener
    register_heif_opener()
    from PIL import Image
    from PIL.ExifTags import TAGS
    from tern.vision import extract_image_exif

    # Build a tiny 8x8 RGB HEIC with iPhone-shaped EXIF
    img = Image.new("RGB", (8, 8), (128, 64, 200))
    exif = img.getexif()
    tags = {v: k for k, v in TAGS.items()}
    exif[tags["Make"]] = "Apple"
    exif[tags["Model"]] = "iPhone 15 Pro"
    exif[tags["DateTimeOriginal"]] = "2024:08:15 14:23:11"
    out = tmp_path / "iphone.heic"
    img.save(out, format="HEIF", exif=exif.tobytes())

    meta = extract_image_exif(out)
    assert meta.get("make") == "Apple"
    assert meta.get("model") == "iPhone 15 Pro"
    assert meta.get("date_taken") == "2024-08-15 14:23:11"


def test_remove_folder_wipes_files_under_prefix(temp_store):
    """Removing a folder from the index drops every file whose path
    starts with the prefix, plus its transcript/OCR/keyframe rows. Files at
    sibling paths must survive."""
    # Three files: two inside /tmp/lib, one outside
    inside_a = FileRecord(path="/tmp/lib/a.mp4", mime="video/mp4", duration_ms=1000,
                          size_bytes=1, mtime=1.0)
    inside_b = FileRecord(path="/tmp/lib/sub/b.mp4", mime="video/mp4", duration_ms=1000,
                          size_bytes=1, mtime=1.0)
    outside = FileRecord(path="/tmp/lib_other/c.mp4", mime="video/mp4", duration_ms=1000,
                         size_bytes=1, mtime=1.0)
    a_id = temp_store.upsert_file(inside_a)
    b_id = temp_store.upsert_file(inside_b)
    c_id = temp_store.upsert_file(outside)
    # Stick a transcript on the inside-A file so we can confirm it gets purged.
    temp_store.insert_transcript_segments([
        TranscriptSegment(file_id=a_id, start_ms=0, end_ms=1000, text="hello world"),
    ])

    result = temp_store.remove_folder("/tmp/lib")
    assert result["files_removed"] == 2
    assert set(result["ids"]) == {a_id, b_id}
    assert temp_store.get_file(a_id) is None
    assert temp_store.get_file(b_id) is None
    # Sibling-prefix file must NOT have been removed (substring-vs-path-prefix
    # check). `/tmp/lib_other/...` does NOT start with `/tmp/lib/`.
    assert temp_store.get_file(c_id) is not None
    # Transcript rows for the deleted file are gone
    rows = temp_store.conn.execute(
        "SELECT COUNT(*) AS n FROM transcript_segments WHERE file_id=?", (a_id,)
    ).fetchone()
    assert rows["n"] == 0


def test_remove_folder_wipes_thumbnail_dirs_on_disk(temp_store):
    """End-to-end pin on the commit 891ed7e fix: remove_folder's per-file
    cleanup_file loop must propagate the on-disk thumbnail dir wipe.
    Before the fix, remove_folder cleaned SQLite + Chroma but left
    `db/thumbnails/file_<id>/` directories orphaned — a user removing
    a 60 GB podcast folder kept ~600 MB of stale JPEGs. The original
    test_remove_folder_wipes_files_under_prefix covered DB rows only;
    this one covers the disk side, scoped to ONLY the matching files
    (sibling folders must keep their thumbs)."""
    # Two indexed files in the target folder + one outside
    inside_a = FileRecord(path="/tmp/podcasts/a.mp4", mime="video/mp4",
                          duration_ms=1000, size_bytes=1, mtime=1.0)
    inside_b = FileRecord(path="/tmp/podcasts/b.mp4", mime="video/mp4",
                          duration_ms=1000, size_bytes=1, mtime=1.0)
    outside  = FileRecord(path="/tmp/other/c.mp4",    mime="video/mp4",
                          duration_ms=1000, size_bytes=1, mtime=1.0)
    a_id = temp_store.upsert_file(inside_a)
    b_id = temp_store.upsert_file(inside_b)
    c_id = temp_store.upsert_file(outside)
    # Seed real on-disk thumbnail dirs for all three
    thumbs_root = temp_store.db_path.parent / "thumbnails"
    for fid in (a_id, b_id, c_id):
        d = thumbs_root / f"file_{fid}"
        d.mkdir(parents=True, exist_ok=True)
        (d / "kf_00001.jpg").write_bytes(b"\xff\xd8\xff\xd9")
    a_dir = thumbs_root / f"file_{a_id}"
    b_dir = thumbs_root / f"file_{b_id}"
    c_dir = thumbs_root / f"file_{c_id}"
    assert a_dir.exists() and b_dir.exists() and c_dir.exists()

    temp_store.remove_folder("/tmp/podcasts")

    # Both inside-folder thumbnail dirs gone from disk
    assert not a_dir.exists(), f"inside-A dir survived: {a_dir}"
    assert not b_dir.exists(), f"inside-B dir survived: {b_dir}"
    # Sibling-folder dir UNTOUCHED — substring-vs-prefix safety
    assert c_dir.exists(), f"sibling dir wrongly removed: {c_dir}"
    assert (c_dir / "kf_00001.jpg").exists()


def test_remove_folder_trailing_slash_is_equivalent_to_no_slash(temp_store):
    """`remove_folder("/tmp/lib/")` and `remove_folder("/tmp/lib")` MUST
    behave identically — the docstring promises trailing-slash
    normalization (`prefix.rstrip("/")`). A future refactor that drops
    the normalize step would silently change behavior for any caller
    that copy-pastes a path with the trailing slash (Finder
    "Copy Pathname", drag-and-drop in some shells, Tauri's openDialog
    in some configurations). Pin both forms return identical results."""
    inside = FileRecord(path="/tmp/lib/x.mp4", mime="video/mp4",
                        duration_ms=1000, size_bytes=1, mtime=1.0)
    outside = FileRecord(path="/tmp/library2/y.mp4", mime="video/mp4",
                         duration_ms=1000, size_bytes=1, mtime=1.0)
    fid_in = temp_store.upsert_file(inside)
    fid_out = temp_store.upsert_file(outside)

    # Trailing-slash form
    result = temp_store.remove_folder("/tmp/lib/")
    assert result["files_removed"] == 1
    assert result["ids"] == [fid_in]
    assert result["errors"] == []
    # Sibling file with substring-but-not-prefix path survives — this is the
    # same guarantee as the no-slash form (regression: a careless
    # `prefix + "%"` instead of `prefix + "/%"` would match /tmp/library2/...)
    assert temp_store.get_file(fid_out) is not None


def test_remove_folder_returns_empty_result_when_no_match(temp_store):
    """`remove_folder` called for a folder that has no indexed files MUST
    return cleanly with `files_removed=0` and an empty `ids`/`errors` list
    — not raise, not return None. The API layer (/api/folders/remove)
    surfaces this dict to the user verbatim ("removed 0 files"); a raise
    here would 500 the request for what's actually a benign "nothing to
    do" case (user clicks Remove on a folder they already cleaned, or
    types a path that was never indexed)."""
    # Seed an unrelated file so the store isn't completely empty
    # (catches a regression where 0-row early-return would mask a
    # SELECT that broke on non-empty tables).
    temp_store.upsert_file(FileRecord(
        path="/tmp/unrelated/z.mp4", mime="video/mp4",
        duration_ms=1000, size_bytes=1, mtime=1.0,
    ))

    result = temp_store.remove_folder("/tmp/never_indexed")
    assert result == {"files_removed": 0, "ids": [], "errors": []}, (
        f"unexpected no-match result: {result!r}"
    )


def test_cleanup_file_wipes_derived_data_but_keeps_file_row(temp_store):
    """cleanup_file's docstring: 'Wipe all per-file derived data —
    transcripts, OCR, keyframe rows, AND ChromaDB embeddings'.

    This is the regression test for the bug that motivated cleanup_file's
    existence: re-indexing a video without cleanup was DOUBLING transcript
    + keyframe rows and leaving orphan ChromaDB vectors pointing at deleted
    SQLite IDs. cleanup_file is the gate that prevents that — it MUST:
      1. Delete every transcript_segments row for the file_id.
      2. Delete every ocr_segments row.
      3. Delete every keyframes row.
      4. NOT delete the files row itself (caller is about to re-populate).
      5. Survive a ChromaDB error gracefully (collection unavailable, etc.)
         without rolling back the SQLite deletes — those are independent.

    Without this test, a future refactor could silently break the dedupe
    guarantee — symptom would be 2× or 3× duplicate search hits per file
    after the user re-indexes, hard to trace back to storage.py."""
    f = FileRecord(path="/tmp/clip.mp4", mime="video/mp4", duration_ms=10000,
                   size_bytes=1, mtime=1.0)
    file_id = temp_store.upsert_file(f)

    # Populate all three derived tables.
    temp_store.insert_transcript_segments([
        TranscriptSegment(file_id=file_id, start_ms=0, end_ms=2000, text="first"),
        TranscriptSegment(file_id=file_id, start_ms=2000, end_ms=4000, text="second"),
    ])
    temp_store.insert_ocr_segments([
        OCRSegment(file_id=file_id, frame_ts_ms=1000, text="logo",
                   confidence=0.9, bbox=[0, 0, 0.1, 0.1]),
    ])
    temp_store.insert_keyframes_batch([
        (Keyframe(file_id=file_id, ts_ms=0, thumbnail_path="/tmp/k0.jpg",
                  embedding_dim=4), [0.1, 0.2, 0.3, 0.4]),
        (Keyframe(file_id=file_id, ts_ms=5000, thumbnail_path="/tmp/k1.jpg",
                  embedding_dim=4), [0.5, 0.6, 0.7, 0.8]),
    ])

    # Sanity: all three populated
    def _count(table: str) -> int:
        return temp_store.conn.execute(
            f"SELECT COUNT(*) AS n FROM {table} WHERE file_id=?", (file_id,)
        ).fetchone()["n"]
    assert _count("transcript_segments") == 2
    assert _count("ocr_segments") == 1
    assert _count("keyframes") == 2

    # Act.
    temp_store.cleanup_file(file_id)

    # Derived tables: all gone.
    assert _count("transcript_segments") == 0, "transcripts not cleaned"
    assert _count("ocr_segments") == 0, "OCR not cleaned"
    assert _count("keyframes") == 0, "keyframes not cleaned"

    # File row: still there (caller is about to repopulate it).
    assert temp_store.get_file(file_id) is not None, \
        "cleanup_file MUST NOT delete the files row — that's remove_folder's job"


def test_cleanup_file_wipes_thumbnail_dir_on_disk(temp_store, tmp_path):
    """cleanup_file must remove the on-disk thumbnail directory
    (`<db_path.parent>/thumbnails/file_<id>/`) so remove_folder's
    cleanup-per-file loop doesn't leak ~MB-per-video of orphan JPEGs.
    Prior to this commit the rmtree lived inline in ingest.index_file's
    force-re branch; remove_folder only called cleanup_file and so
    leaked the on-disk dirs forever (a user who Removed a 60 GB podcast
    folder kept ~600 MB of thumbnails)."""
    # Insert a file row + seed a real on-disk thumbnail dir at the
    # location cleanup_file derives (db_path.parent / "thumbnails" / file_X)
    fid = temp_store.upsert_file(FileRecord(
        path="/tmp/with_thumbs.mp4", mime="video/mp4", duration_ms=1000,
        size_bytes=1, mtime=1.0,
    ))
    kf_dir = temp_store.db_path.parent / "thumbnails" / f"file_{fid}"
    kf_dir.mkdir(parents=True, exist_ok=True)
    (kf_dir / "kf_00001.jpg").write_bytes(b"\xff\xd8\xff\xd9")
    (kf_dir / "kf_00002.jpg").write_bytes(b"\xff\xd8\xff\xd9")
    assert kf_dir.exists() and any(kf_dir.iterdir()), "test seed failed"

    temp_store.cleanup_file(fid)

    assert not kf_dir.exists(), (
        f"cleanup_file MUST rmtree the on-disk thumbnail dir; "
        f"still present at {kf_dir}"
    )


def test_cleanup_file_handles_missing_thumbnail_dir_gracefully(temp_store):
    """No thumbnail dir on disk (image-only files, never-extracted videos,
    fresh re-index on a wiped workspace) → cleanup_file must NOT raise.
    The rmtree call is guarded by `if kf_dir.exists()` AND wrapped in
    try/except so an mid-rmtree permission error on a network-mounted
    workspace doesn't abort the surrounding remove_folder loop."""
    fid = temp_store.upsert_file(FileRecord(
        path="/tmp/no_thumbs.mp3", mime="audio/mpeg", duration_ms=1000,
        size_bytes=1, mtime=1.0,
    ))
    # No thumbnail dir created
    kf_dir = temp_store.db_path.parent / "thumbnails" / f"file_{fid}"
    assert not kf_dir.exists()
    # Must not raise
    temp_store.cleanup_file(fid)


def test_cleanup_file_only_affects_target_file(temp_store):
    """Two files with derived data; cleanup_file(A) must NOT touch B's
    rows. Pins the WHERE file_id=? predicate against an accidental
    DELETE-everything refactor."""
    fa = FileRecord(path="/tmp/a.mp4", mime="video/mp4", duration_ms=1000,
                    size_bytes=1, mtime=1.0)
    fb = FileRecord(path="/tmp/b.mp4", mime="video/mp4", duration_ms=1000,
                    size_bytes=1, mtime=1.0)
    a_id = temp_store.upsert_file(fa)
    b_id = temp_store.upsert_file(fb)

    temp_store.insert_transcript_segments([
        TranscriptSegment(file_id=a_id, start_ms=0, end_ms=500, text="alpha"),
        TranscriptSegment(file_id=b_id, start_ms=0, end_ms=500, text="bravo"),
    ])

    temp_store.cleanup_file(a_id)

    # A's transcript gone, B's preserved.
    a_count = temp_store.conn.execute(
        "SELECT COUNT(*) AS n FROM transcript_segments WHERE file_id=?", (a_id,)
    ).fetchone()["n"]
    b_count = temp_store.conn.execute(
        "SELECT COUNT(*) AS n FROM transcript_segments WHERE file_id=?", (b_id,)
    ).fetchone()["n"]
    assert a_count == 0
    assert b_count == 1, "cleanup_file(A) wrongly purged B's transcripts"


# ─── list_files() — base behavior + status filter + limit arg ──────────
# /api/files (and indirectly the sidebar + empty state + filters folder
# dropdown) depends on this function. The limit arg was added in 98c3707
# for the empty-state perf win; the ORDER BY id was added in the same
# commit to make pagination stable. Both deserve regression coverage.

def _seed_n(store, n: int, status: str = "done", mime: str = "audio/mpeg"):
    """Helper: drop N files into the store with deterministic paths +
    one alternating status if status='mixed'."""
    ids = []
    for i in range(n):
        st = status if status != "mixed" else ("done" if i % 2 == 0 else "indexing")
        rec = FileRecord(
            path=f"/tmp/list_files_test_{i:03d}.mp3",
            mime=mime,
            duration_ms=1000,
            size_bytes=1,
            mtime=float(i),
            status=st,
        )
        ids.append(store.upsert_file(rec))
    return ids


def test_list_files_returns_all_by_default(temp_store):
    """list_files() with no args returns every file regardless of status."""
    _seed_n(temp_store, 5, status="done")
    out = temp_store.list_files()
    assert len(out) == 5


def test_list_files_status_filter_excludes_others(temp_store):
    """status='done' must skip indexing/error files. Without this filter,
    the sidebar would show half-processed files as if they were ready
    to search."""
    _seed_n(temp_store, 6, status="mixed")  # 3 done + 3 indexing
    done = temp_store.list_files(status="done")
    indexing = temp_store.list_files(status="indexing")
    assert len(done) == 3, f"expected 3 done, got {len(done)}"
    assert len(indexing) == 3, f"expected 3 indexing, got {len(indexing)}"
    assert all(f.status == "done" for f in done)


def test_list_files_limit_caps_returned_rows(temp_store):
    """limit pushes LIMIT into SQL. 98c3707's perf win — for a 5k-file
    workspace, the empty-state's limit=12 saves 4988 FileRecord
    constructs + 4988 metadata-JSON parses."""
    _seed_n(temp_store, 10)
    capped = temp_store.list_files(limit=3)
    assert len(capped) == 3, f"limit=3 returned {len(capped)} rows"


def test_list_files_returns_stable_order(temp_store):
    """ORDER BY id is required so the empty-state demo file list doesn't
    shuffle on every poll. Without explicit ORDER BY, SQLite returns
    rows in insertion order for a fresh table — but VACUUM, ANALYZE, or
    a future schema migration could re-order them silently."""
    _seed_n(temp_store, 10)
    out1 = temp_store.list_files()
    out2 = temp_store.list_files()
    assert [f.id for f in out1] == [f.id for f in out2], "list_files order is unstable"
    # And IDs should be monotonically increasing (matches ORDER BY id).
    ids = [f.id for f in out1]
    assert ids == sorted(ids), f"list_files not in id order: {ids}"


def test_list_files_limit_combines_with_status(temp_store):
    """Both filters apply together: status='done' first, then LIMIT
    cuts the result. Important for /api/files?limit=N&status=done."""
    _seed_n(temp_store, 10, status="mixed")  # 5 done + 5 indexing
    out = temp_store.list_files(status="done", limit=3)
    assert len(out) == 3
    assert all(f.status == "done" for f in out)


def test_list_files_limit_zero_or_none_returns_all(temp_store):
    """limit=None and limit=0 both bypass the LIMIT clause (the impl
    uses `if limit is not None and limit > 0`). Pins this behavior so
    a future caller passing 0 as a sentinel doesn't get an empty list."""
    _seed_n(temp_store, 4)
    assert len(temp_store.list_files(limit=None)) == 4
    assert len(temp_store.list_files(limit=0)) == 4


# ─── _refine_transcript_ts — word-position timestamp refinement ────────
# Whisper segments span 5-15 s and pack multiple words. A hit on
# "Stanford" inside "I'm Sam Altman ... I was a Stanford student..." should
# open the player NEAR the actual word, not at the segment start (which
# would play ~5 s of unrelated audio before the matched word).
#
# _refine_transcript_ts handles this via linear word-position interpolation.
# It's reached by every transcript search hit's ts_ms computation (see
# storage.search_transcript line 544) — a regression silently degrades
# every transcript-channel result by adding pre-roll audio. Untested
# directly until now; integration tests in test_search.py only exercise
# the happy path implicitly.


def test_refine_transcript_ts_empty_text_falls_back_to_start():
    """Edge: empty full_text → no haystack to search → return start_ms."""
    from tern.storage import _refine_transcript_ts
    assert _refine_transcript_ts("query", "", 10_000, 15_000) == 10_000


def test_refine_transcript_ts_inverted_window_falls_back():
    """Edge: end_ms <= start_ms (corrupt Whisper output, schema mismatch)
    → return start_ms unchanged rather than computing a negative duration."""
    from tern.storage import _refine_transcript_ts
    assert _refine_transcript_ts("hello", "hello world", 5000, 5000) == 5000
    assert _refine_transcript_ts("hello", "hello world", 5000, 4000) == 5000


def test_refine_transcript_ts_query_too_short_falls_back():
    """Queries with no token ≥ 2 chars (just 'a', '!', etc.) → fall back.
    Refinement requires anchoring on a real word position; a single-letter
    token would match every other character and produce noise."""
    from tern.storage import _refine_transcript_ts
    out = _refine_transcript_ts("a", "I'm a Stanford student now", 10_000, 20_000)
    assert out == 10_000


def test_refine_transcript_ts_word_at_segment_start_clamped_to_start():
    """The match landing at character 0 would compute refined = start - 500
    (the 0.5 s lead-in). The function must clamp at start_ms (no negative
    audio offsets, no pre-segment seek)."""
    from tern.storage import _refine_transcript_ts
    out = _refine_transcript_ts("hello", "hello world", 10_000, 15_000)
    assert out == 10_000


def test_refine_transcript_ts_word_mid_segment_interpolates():
    """The whole point: a hit on a word at ~50 % through the segment text
    should land at ~50 % through the segment time window, minus the 0.5 s
    lead-in (clamped to start_ms minimum)."""
    from tern.storage import _refine_transcript_ts
    # text length: ~40 chars, "stanford" starts at index ~24 → ratio ~0.60
    text = "I'm Sam Altman and I went to Stanford in 2005"
    start_ms = 10_000
    end_ms = 20_000  # 10 s window
    out = _refine_transcript_ts("stanford", text, start_ms, end_ms)
    # Expected position: 10_000 + (24/45) * 10_000 - 500 ≈ 14_833. Allow
    # some slack since the exact ratio depends on character count.
    assert 13_500 <= out <= 16_500, f"expected ~14_833, got {out}"
    # Must NOT exceed end_ms - 500 (the upper clamp)
    assert out <= end_ms - 500


def test_refine_transcript_ts_uses_earliest_token_position():
    """Multi-word query → use EARLIEST matching token so we don't seek past
    the user's intended landing. 'sam stanford' against text where 'sam'
    appears first should anchor on 'sam', not 'stanford'."""
    from tern.storage import _refine_transcript_ts
    text = "I'm Sam Altman who went to Stanford in 2005"
    # 'sam' is at index 4, 'stanford' at index 27. Should anchor on sam.
    out_sam_first = _refine_transcript_ts("sam stanford", text, 0, 10_000)
    out_only_stan = _refine_transcript_ts("stanford", text, 0, 10_000)
    # The first call must produce an EARLIER timestamp than the second
    # (since 'sam' lands earlier in the segment than 'stanford').
    assert out_sam_first < out_only_stan, (
        f"expected sam-anchored ts < stanford-only ts, got {out_sam_first} vs {out_only_stan}"
    )


def test_refine_transcript_ts_no_match_falls_back():
    """Query tokens not present (FTS5 hit was via stemmed form, but the
    literal substring isn't there) → return start_ms unchanged. Required
    so a Porter-stemmed match on 'pric' against 'pricing' still produces
    a sensible ts when the literal 'pric' isn't a separate word."""
    from tern.storage import _refine_transcript_ts
    text = "we discussed our pricing strategy at length"
    # Query is the stem; the literal substring IS present here ("pric"
    # is inside "pricing"), so this would find it. Use a truly absent
    # token to test the fall-back path.
    out = _refine_transcript_ts("zzzqxv", text, 10_000, 20_000)
    assert out == 10_000
