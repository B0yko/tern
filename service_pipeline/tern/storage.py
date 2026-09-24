"""SQLite + ChromaDB storage layer for Tern."""
from __future__ import annotations

import html
import json
import logging
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger("tern.storage")

#: HNSW search breadth for the keyframe vector index.
#:
#: Chroma's default is 100, and at that setting the graph does not reliably
#: reach every vector. Measured on a healthy 430-keyframe index: the query
#: "sushi" has exactly one true match at cosine 0.1280, and at the production
#: fetch size the ANN traversal returned it in **1 of 5 fresh processes** —
#: the other four surfaced an unrelated frame at 0.0471 and the real photo
#: was never a candidate, so the noise floor then discarded the whole visual
#: channel and the search came back empty. Which way it fell varied per
#: process, because the traversal entry point does. A search that works on
#: your machine and returns nothing on the buyer's is the worst shape a bug
#: can take in a demo.
#:
#: At 400 the same probe found it 5 of 5. The cost is a wider candidate scan
#: per query on an index that is tiny next to the model inference already in
#: the request. Raise it further if recall ever regresses on a large archive;
#: `max_neighbors` (16, fixed at build time) is the other lever and that one
#: needs a re-index.
HNSW_EF_SEARCH = 400


def _html_escape(s: str) -> str:
    """Escape ampersands/angle brackets for safe insertion into the result
    snippet HTML. We deliberately do NOT escape after wrapping in `<mark>`
    elsewhere — the caller is responsible for escaping content first."""
    return html.escape(s or "", quote=False)


_USER_TOKEN_RE = re.compile(r"\w{2,}")


# CJK Unified Ideographs (U+4E00–U+9FFF) + Hiragana (U+3040–U+309F) +
# Katakana (U+30A0–U+30FF) + Hangul Syllables (U+AC00–U+D7AF).
# Mirror of SearchEngine._CJK_RANGES — duplicated here (rather than imported
# from search.py) because storage.py is the lower-level module: search.py
# already imports from storage, so an `from .search import ...` here would
# create a circular import. The commit 63ff96b CJK 2-char carve-out lives
# at the BOOST layer (search.py); this brings the same carve-out to the
# FETCH layer (storage.py search_filename) so a CJK 2-char query no
# longer gets silently filtered to zero hits BEFORE the boost path even
# sees it. The duplication is intentional — DRY refactor can come later
# as a separate change.
_CJK_RANGES = (
    (0x4E00, 0x9FFF),
    (0x3040, 0x309F),
    (0x30A0, 0x30FF),
    (0xAC00, 0xD7AF),
)


def _token_has_cjk(token: str) -> bool:
    """True if any character is in a CJK / Japanese / Korean script block.

    Used as a length-filter carve-out in search_filename — see the
    63ff96b commit message for the design rationale: Latin scripts need
    ≥3 chars to filter stop-words ('if', 'to', 'or'), but CJK packs a full
    morpheme per character, so 2-char words like 会議 (meeting), 予算
    (budget), 회사 (company) are real standalone search terms that
    deserve the same filename-match treatment Latin tokens get.
    """
    for c in token:
        cp = ord(c)
        for lo, hi in _CJK_RANGES:
            if lo <= cp <= hi:
                return True
    return False


def _highlight_all_query_words(snippet: str, query: str) -> str:
    """Wrap every query token that appears in the snippet in `<mark>...</mark>`.

    FTS5's `snippet()` only highlights the tokens it considered for ranking,
    which for a multi-word query like "stanford student" may leave "student"
    un-marked even when both words appear in the matched segment. This pass
    catches the rest so the user can visually scan all matched terms.

    We skip tokens already inside a `<mark>` (to avoid double-wrapping) and
    use a simple case-insensitive word-boundary regex. Quoted phrases are
    stripped from the query before tokenization — phrase matches are an
    all-or-nothing thing already and FTS5 will have marked them whole.
    """
    if not snippet or not query:
        return snippet
    # Drop FTS-syntax noise (`*`, parentheses, AND/OR) before extracting words
    cleaned = re.sub(r'"[^"]*"', " ", query)
    cleaned = re.sub(r"[^\w\s]", " ", cleaned)
    tokens = {t.lower() for t in _USER_TOKEN_RE.findall(cleaned) if t.lower() not in {"and", "or", "not"}}
    if not tokens:
        return snippet
    # Build alternation pattern matching any token as a whole word, case-insensitive.
    pattern = re.compile(r"\b(" + "|".join(re.escape(t) for t in sorted(tokens, key=len, reverse=True)) + r")\b", re.IGNORECASE)

    # Split around existing <mark>...</mark> blocks so we don't mark inside them
    # nor inside the closing tag. We process the OUTSIDE portions only.
    parts = re.split(r"(<mark>.*?</mark>)", snippet)
    for i, part in enumerate(parts):
        if part.startswith("<mark>"):
            continue
        parts[i] = pattern.sub(r"<mark>\1</mark>", part)
    return "".join(parts)


def _refine_transcript_ts(query: str, full_text: str, start_ms: int, end_ms: int) -> int:
    """Refine the hit timestamp by approximate-word-position within the segment.

    Whisper segments span 5-15 s and pack multiple words. A hit on "Stanford"
    in a segment that starts with "I'm Sam Altman ... I was a Stanford
    student..." should land NEAR the actual word, not at the segment start
    (which would open the player 5 s before the matched word).

    Strategy: tokenize the query, find the EARLIEST query-token position in
    the segment text (case-insensitive substring), then linearly interpolate
    that character position to a millisecond offset within the segment's
    [start_ms, end_ms] range. Conservative: shift back by 0.5 s to give the
    user a hair of lead-in audio. Approximate but vastly better than the
    raw segment start.

    Falls back to start_ms unchanged when nothing matches (e.g. the FTS hit
    was on a stem and the literal substring isn't there)."""
    if not full_text or end_ms <= start_ms:
        return start_ms
    q_words = [w for w in re.findall(r"\w+", query.lower()) if len(w) >= 2]
    if not q_words:
        return start_ms
    haystack_lower = full_text.lower()
    earliest = None
    for w in q_words:
        idx = haystack_lower.find(w)
        if idx >= 0 and (earliest is None or idx < earliest):
            earliest = idx
    if earliest is None:
        return start_ms
    seg_len = max(1, len(full_text))
    rel = earliest / seg_len
    duration = end_ms - start_ms
    refined = start_ms + int(rel * duration) - 500  # -0.5 s lead-in
    return max(start_ms, min(end_ms - 500, refined))
from typing import Iterator

import chromadb
from chromadb.config import Settings

from .models import FileRecord, Keyframe, OCRSegment, SearchHit, TranscriptSegment


SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    path TEXT UNIQUE NOT NULL,
    mime TEXT NOT NULL,
    duration_ms INTEGER NOT NULL,
    size_bytes INTEGER NOT NULL,
    mtime REAL NOT NULL,
    indexed_at TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    error TEXT,
    metadata TEXT  -- JSON: EXIF for images, future extension for video/audio metadata
);

CREATE INDEX IF NOT EXISTS idx_files_status ON files(status);
CREATE INDEX IF NOT EXISTS idx_files_path ON files(path);

CREATE TABLE IF NOT EXISTS transcript_segments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    start_ms INTEGER NOT NULL,
    end_ms INTEGER NOT NULL,
    text TEXT NOT NULL,
    confidence REAL DEFAULT 1.0
);

CREATE INDEX IF NOT EXISTS idx_transcript_file ON transcript_segments(file_id);
-- Composite (file_id, start_ms) lets /api/transcript/window's
--   `WHERE file_id=? ORDER BY start_ms ASC` skip the sort step entirely
-- — SQLite reads matching rows in already-sorted index order. The single-
-- column idx_transcript_file above forced a separate sort of N segments
-- (N=600+ for a 60-min podcast). Kept side-by-side; query planner picks
-- whichever is more efficient. Storage cost: ~16 bytes/row.
CREATE INDEX IF NOT EXISTS idx_transcript_file_start
    ON transcript_segments(file_id, start_ms);

CREATE VIRTUAL TABLE IF NOT EXISTS transcript_fts USING fts5(
    text, content=transcript_segments, content_rowid=id, tokenize='porter unicode61'
);

CREATE TRIGGER IF NOT EXISTS transcript_fts_insert AFTER INSERT ON transcript_segments BEGIN
    INSERT INTO transcript_fts(rowid, text) VALUES (new.id, new.text);
END;

CREATE TRIGGER IF NOT EXISTS transcript_fts_delete AFTER DELETE ON transcript_segments BEGIN
    INSERT INTO transcript_fts(transcript_fts, rowid, text) VALUES('delete', old.id, old.text);
END;

CREATE TABLE IF NOT EXISTS ocr_segments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    frame_ts_ms INTEGER NOT NULL,
    text TEXT NOT NULL,
    confidence REAL NOT NULL,
    bbox TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_ocr_file ON ocr_segments(file_id);
-- Composite (file_id, frame_ts_ms) accelerates _build_ocr_context's
--   `WHERE file_id=? AND frame_ts_ms=?` lookup (called once per OCR
-- hit in the search result loop, so N+1 in result count).
CREATE INDEX IF NOT EXISTS idx_ocr_file_frame
    ON ocr_segments(file_id, frame_ts_ms);

CREATE VIRTUAL TABLE IF NOT EXISTS ocr_fts USING fts5(
    text, content=ocr_segments, content_rowid=id, tokenize='porter unicode61'
);

CREATE TRIGGER IF NOT EXISTS ocr_fts_insert AFTER INSERT ON ocr_segments BEGIN
    INSERT INTO ocr_fts(rowid, text) VALUES (new.id, new.text);
END;

CREATE TRIGGER IF NOT EXISTS ocr_fts_delete AFTER DELETE ON ocr_segments BEGIN
    INSERT INTO ocr_fts(ocr_fts, rowid, text) VALUES('delete', old.id, old.text);
END;

CREATE TABLE IF NOT EXISTS keyframes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    ts_ms INTEGER NOT NULL,
    thumbnail_path TEXT NOT NULL,
    embedding_dim INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_keyframes_file ON keyframes(file_id);
-- Composite (file_id, ts_ms) accelerates search_filename's window-fn
--   `ROW_NUMBER() OVER (PARTITION BY file_id ORDER BY ts_ms)` query
-- (commit 7d58e0c) — also the /api/files batched first-
-- keyframe lookup (commit a4bf079). Both did a per-file
-- scan + sort with the single-column index.
CREATE INDEX IF NOT EXISTS idx_keyframes_file_ts
    ON keyframes(file_id, ts_ms);
"""


class Store:
    """SQLite + Chroma combined store."""

    def __init__(self, db_path: Path, chroma_path: Path):
        self.db_path = db_path
        self.chroma_path = chroma_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        chroma_path.mkdir(parents=True, exist_ok=True)

        self.conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        # Performance pragmas. SQLite's defaults are conservative — designed
        # for embedded devices in 2004, not modern desktop apps with GB of
        # RAM. Tern's workload is read-heavy (every keystroke fires FTS5 +
        # window-fn queries), so the wins are concentrated:
        #
        #   journal_mode = WAL   — write-ahead logging. Major: lets readers
        #     and writers run concurrently (default DELETE journal serialises
        #     them). Also halves write latency on the indexing path. Creates
        #     `*.db-wal` + `*.db-shm` sidecar files — fine for a workspace.
        #
        #   synchronous = NORMAL — fsync on COMMIT only (default FULL also
        #     fsyncs the WAL after every page write). NORMAL+WAL is the
        #     SQLite-recommended combo for most apps; durability cost is
        #     "the last few transactions may be lost on power loss", which
        #     for a re-derivable local index is the right trade.
        #
        #   cache_size = -64000  — 64 MB page cache (default 2 MB). FTS5
        #     query plans on a 10k-segment table walk 30-50 pages per
        #     query; the default cache evicts them between every request,
        #     so each search does fresh disk I/O. 64 MB keeps the hot
        #     working set resident. Negative value = "KB" not "pages".
        #
        #   temp_store = MEMORY  — ORDER BY / GROUP BY temp tables live in
        #     RAM not /tmp. Tern's search.py does sort+window operations on
        #     up to _FETCH_CAP=120 candidate rows; staying in RAM removes
        #     the syscall + disk round-trip.
        #
        # All four are idempotent and survive a Store(...) re-open. mmap_size
        # is intentionally NOT set — macOS mmap + iCloud-synced paths
        # (~/Documents/ default) interact badly and can corrupt under sync.
        self.conn.executescript("""
            PRAGMA journal_mode = WAL;
            PRAGMA synchronous = NORMAL;
            PRAGMA cache_size = -64000;
            PRAGMA temp_store = MEMORY;
        """)
        self.conn.executescript(SCHEMA)
        self.conn.commit()

        self.chroma = chromadb.PersistentClient(
            path=str(chroma_path),
            settings=Settings(anonymized_telemetry=False),
        )
        self.vectors = self.chroma.get_or_create_collection(
            name="keyframes",
            metadata={"hnsw:space": "cosine"},
            # The space has to be named here as well. Chroma 1.x ignores the
            # legacy "hnsw:space" metadata once a `configuration` is passed,
            # so without it a NEW collection is built in l2. Scores then come
            # back as 1 - L2 distance, mostly negative, the noise floor never
            # engages, and nearly every query returns a page of unrelated
            # frames. Existing collections are unaffected: modify() below
            # keeps the space they were built with.
            configuration={"hnsw": {"space": "cosine", "ef_search": HNSW_EF_SEARCH}},
        )
        # Existing collections keep whatever ef_search they were built with —
        # get_or_create only applies `configuration` on create. Anyone who
        # indexed before this landed (including every dev workspace and any
        # archive a customer has already built) would silently keep the
        # Chroma default of 100 and the recall hole below. Nudge it on open;
        # it is a metadata write, not a re-index.
        try:
            current = (self.vectors.configuration_json or {}).get("hnsw", {})
            if current.get("ef_search", 0) < HNSW_EF_SEARCH:
                self.vectors.modify(
                    configuration={"hnsw": {"ef_search": HNSW_EF_SEARCH}}
                )
        except Exception:
            # Never let a tuning nudge stop the app from opening. Worst case
            # is the pre-existing recall behaviour, which is what shipped.
            logger.exception("could not raise hnsw:ef_search on the keyframes collection")

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        try:
            yield self.conn
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    # --- File operations ---

    def upsert_file(self, record: FileRecord) -> int:
        cur = self.conn.execute(
            """
            INSERT INTO files (path, mime, duration_ms, size_bytes, mtime, status, indexed_at, error, metadata)
            VALUES (:path, :mime, :duration_ms, :size_bytes, :mtime, :status, :indexed_at, :error, :metadata)
            ON CONFLICT(path) DO UPDATE SET
                mtime=excluded.mtime,
                size_bytes=excluded.size_bytes,
                status=excluded.status,
                indexed_at=excluded.indexed_at,
                error=excluded.error,
                metadata=COALESCE(excluded.metadata, files.metadata)
            RETURNING id
            """,
            {
                "path": record.path,
                "mime": record.mime,
                "duration_ms": record.duration_ms,
                "size_bytes": record.size_bytes,
                "mtime": record.mtime,
                "status": record.status,
                "indexed_at": record.indexed_at.isoformat() if record.indexed_at else None,
                "error": record.error,
                "metadata": (
                    # ensure_ascii=False mirrors the fix from commit f1cdbc3 on
                    # set_file_metadata — without it, HEIC EXIF with Cyrillic
                    # camera maker / CJK location / accented chars gets stored
                    # as `Ц...` escapes that bloat the column 3-6× and
                    # show as gibberish on direct sqlite3 inspection. Both
                    # call sites (this insert AND the in-place update) need
                    # the same flag — JSON round-trip via `json.loads` works
                    # in both forms, but the human-readable canonical form is
                    # what a UTF-8 SQLite column deserves.
                    json.dumps(record.metadata, separators=(",", ":"), ensure_ascii=False)
                    if record.metadata else None
                ),
            },
        )
        file_id = cur.fetchone()["id"]
        self.conn.commit()
        return file_id

    def set_file_metadata(self, file_id: int, metadata: dict) -> None:
        """Update the JSON metadata for a file in place.

        `ensure_ascii=False` because the SQLite text column is UTF-8 and
        HEIC EXIF can carry Cyrillic camera makes (`Цифровая камера`),
        CJK locations, accented chars (`São Paulo`). Default json.dumps
        ASCII-escapes those to `\\u0426...`, which:
          - bloats the column ~3-6× for non-ASCII content
          - shows as gibberish if a user inspects the file row via
            `sqlite3 db/tern.db "SELECT metadata FROM files…"`
          - hides FTS-searchable text from any future metadata index
        The round-trip via json.loads handles either form, so this
        change is purely about storing the human-readable canonical
        form rather than an escaped echo of it.
        """
        self.conn.execute(
            "UPDATE files SET metadata=? WHERE id=?",
            (json.dumps(metadata, separators=(",", ":"), ensure_ascii=False) if metadata else None, file_id),
        )
        self.conn.commit()

    def set_file_status(self, file_id: int, status: str, error: str | None = None) -> None:
        # datetime.utcnow() is deprecated in Python 3.12+. The replace-trick
        # below keeps the stored ISO string byte-identical to the old form
        # (no timezone suffix, naive-but-UTC-grounded) so existing reads
        # don't see a format change.
        ts = datetime.now(timezone.utc).isoformat().replace("+00:00", "")
        self.conn.execute(
            "UPDATE files SET status=?, error=?, indexed_at=? WHERE id=?",
            (status, error, ts, file_id),
        )
        self.conn.commit()

    def get_file_by_path(self, path: str) -> FileRecord | None:
        row = self.conn.execute("SELECT * FROM files WHERE path=?", (path,)).fetchone()
        if not row:
            return None
        return self._row_to_file_record(row)

    def get_file(self, file_id: int) -> FileRecord | None:
        row = self.conn.execute("SELECT * FROM files WHERE id=?", (file_id,)).fetchone()
        if not row:
            return None
        return self._row_to_file_record(row)

    def list_files(self, status: str | None = None, limit: int | None = None) -> list[FileRecord]:
        # Optional limit pushed into SQL so a 5k-file workspace doesn't pull
        # all 5k rows + parse 5k metadata-JSON blobs when the caller only
        # wants the first N. /api/files now threads `?limit=` through; the
        # empty-state caller asks for 12.
        sql = "SELECT * FROM files"
        params: list = []
        if status:
            sql += " WHERE status=?"
            params.append(status)
        # Stable ordering required by /api/files contract (deterministic
        # pagination, predictable empty-state demo-files order).
        sql += " ORDER BY id"
        if limit is not None and limit > 0:
            sql += " LIMIT ?"
            params.append(int(limit))
        rows = self.conn.execute(sql, params).fetchall()
        return [self._row_to_file_record(r) for r in rows]

    @staticmethod
    def _row_to_file_record(row) -> FileRecord:
        """Materialize a `files` row, parsing the JSON `metadata` column to a dict
        so Pydantic doesn't choke on the string. Empty / unparseable → None."""
        d = dict(row)
        raw = d.get("metadata")
        if isinstance(raw, str):
            try:
                d["metadata"] = json.loads(raw) if raw else None
            except Exception:
                d["metadata"] = None
        return FileRecord(**d)

    def stats(self) -> dict[str, int]:
        # Collapse 7 separate `files` table COUNTs into ONE pass with
        # conditional aggregation. Previously: 9 queries (7 against files,
        # 1 each against transcripts/ocr/keyframes). Each COUNT was a full
        # index scan — linear in file count, repeated 7 times.
        # Now: 1 single-pass scan against files + 3 against the smaller
        # derived tables. For a 1000-file workspace this drops total stats()
        # time from ~3.5 ms to ~0.8 ms (~4× speedup on the path that the
        # empty-state subscriber polls on every state change).
        files_row = self.conn.execute(
            """
            SELECT
              COUNT(*) AS files_total,
              SUM(CASE WHEN status='done' THEN 1 ELSE 0 END) AS files_done,
              SUM(CASE WHEN status='error' THEN 1 ELSE 0 END) AS files_errored,
              SUM(CASE WHEN status='done' AND mime LIKE 'video/%' THEN 1 ELSE 0 END) AS files_video,
              SUM(CASE WHEN status='done' AND mime LIKE 'audio/%' THEN 1 ELSE 0 END) AS files_audio,
              SUM(CASE WHEN status='done' AND mime LIKE 'image/%' THEN 1 ELSE 0 END) AS files_image,
              COALESCE(SUM(CASE WHEN status='done' THEN duration_ms ELSE 0 END), 0) AS total_duration_ms
            FROM files
            """
        ).fetchone()
        out: dict[str, int] = {
            "files_total":        files_row["files_total"]      or 0,
            "files_done":         files_row["files_done"]       or 0,
            # files_errored: persistent (status='error' in DB) count, separate
            # from the per-run files_errored counter in app.state.indexing.
            # The per-run counter resets on every /api/index/start; this one
            # accumulates across runs so support / sidebar / empty-state UI
            # can answer "do I have ANY indexing failures hanging around?"
            # without needing to scroll through `/api/files?status=error` or
            # grep ~/Library/Logs/tern-debug.log. Currently consumed by
            # qa_smoke for shape-coverage; future surface candidates: a
            # sidebar warning chip ("3 files failed to index"), an empty-
            # state library breakdown extension, an indexing-toast follow-up.
            "files_errored":      files_row["files_errored"]    or 0,
            "files_video":        files_row["files_video"]      or 0,
            "files_audio":        files_row["files_audio"]      or 0,
            "files_image":        files_row["files_image"]      or 0,
            "total_duration_ms":  files_row["total_duration_ms"] or 0,
        }
        # Derived tables are unrelated to `files` so they stay as separate
        # COUNTs (a JOIN/CTE here would be slower, not faster).
        out["transcript_segments"] = self.conn.execute("SELECT COUNT(*) FROM transcript_segments").fetchone()[0]
        out["ocr_segments"]        = self.conn.execute("SELECT COUNT(*) FROM ocr_segments").fetchone()[0]
        out["keyframes"]           = self.conn.execute("SELECT COUNT(*) FROM keyframes").fetchone()[0]
        return out

    # --- Transcript operations ---

    def insert_transcript_segments(self, segments: list[TranscriptSegment]) -> None:
        with self.transaction() as conn:
            conn.executemany(
                "INSERT INTO transcript_segments (file_id, start_ms, end_ms, text, confidence) "
                "VALUES (?, ?, ?, ?, ?)",
                [(s.file_id, s.start_ms, s.end_ms, s.text, s.confidence) for s in segments],
            )

    # --- OCR operations ---

    def insert_ocr_segments(self, segments: list[OCRSegment]) -> None:
        import json

        with self.transaction() as conn:
            conn.executemany(
                "INSERT INTO ocr_segments (file_id, frame_ts_ms, text, confidence, bbox) "
                "VALUES (?, ?, ?, ?, ?)",
                [(s.file_id, s.frame_ts_ms, s.text, s.confidence, json.dumps(s.bbox)) for s in segments],
            )

    # --- Keyframe operations ---

    def insert_keyframe(self, kf: Keyframe, embedding: list[float]) -> int:
        cur = self.conn.execute(
            "INSERT INTO keyframes (file_id, ts_ms, thumbnail_path, embedding_dim) VALUES (?, ?, ?, ?) RETURNING id",
            (kf.file_id, kf.ts_ms, kf.thumbnail_path, kf.embedding_dim),
        )
        kf_id = cur.fetchone()["id"]
        self.conn.commit()
        self.vectors.add(
            ids=[str(kf_id)],
            embeddings=[embedding],
            metadatas=[{
                "file_id": kf.file_id,
                "ts_ms": kf.ts_ms,
                "thumbnail_path": kf.thumbnail_path,
            }],
        )
        return kf_id

    def cleanup_file(self, file_id: int) -> None:
        """Wipe all per-file derived data — transcripts, OCR, keyframe rows,
        ChromaDB embeddings, AND the on-disk thumbnail directory.

        Call this BEFORE a force-reindex so we don't accumulate stale rows
        or orphan vectors. The original bug this fixes: re-indexing a video
        doubled keyframe + transcript counts and left embeddings in
        ChromaDB referencing deleted SQLite IDs.

        Thumbnail-dir cleanup was previously inline in ingest.index_file's
        force-reindex branch (a separate `shutil.rmtree`), which meant the
        OTHER caller — remove_folder's `cleanup_file` loop — leaked the
        on-disk thumbnail directories after every folder removal. A user
        who indexed a 60 GB podcast folder then clicked Remove got the DB
        cleaned but ~600 MB of thumbnails left on disk; repeated runs
        accumulated stale `file_<id>/` directories indefinitely. Moving
        the rmtree HERE makes both callers (index force-re, remove_folder)
        share the same on-disk cleanup contract.

        Derives the thumbnails root from `self.db_path.parent / "thumbnails"`
        — workspace layout convention from `default_config()` (db_path is
        always `workspace/db/tern.db`, thumbnails always `workspace/db/
        thumbnails`). Tests using a flat temp dir get the same relative
        layout (`tmp/thumbnails/file_<id>/`).
        """
        with self.transaction() as conn:
            conn.execute("DELETE FROM transcript_segments WHERE file_id=?", (file_id,))
            conn.execute("DELETE FROM ocr_segments WHERE file_id=?", (file_id,))
            conn.execute("DELETE FROM keyframes WHERE file_id=?", (file_id,))
        # ChromaDB: delete every vector whose metadata.file_id matches
        try:
            self.vectors.delete(where={"file_id": file_id})
        except Exception as e:
            import logging
            logging.getLogger("tern.storage").warning(
                "ChromaDB cleanup for file_id=%s failed: %s", file_id, e
            )
        # On-disk thumbnail dir — `ignore_errors=True` so a partial-
        # delete on a network-mounted workspace or a TCC-locked subdir
        # doesn't abort the surrounding remove_folder loop. The DB rows
        # are already gone; even an orphan dir on disk produces no
        # visible bug (just wasted bytes — eventually overwritten by
        # the next re-index of a file that lands at the same file_id).
        try:
            import shutil as _shutil
            kf_dir = self.db_path.parent / "thumbnails" / f"file_{file_id}"
            if kf_dir.exists():
                _shutil.rmtree(kf_dir, ignore_errors=True)
        except Exception as e:
            import logging
            logging.getLogger("tern.storage").warning(
                "thumbnail dir cleanup for file_id=%s failed: %s", file_id, e
            )

    def remove_folder(self, prefix: str) -> dict:
        """Remove every indexed file whose path begins with `prefix` from the
        index (DB rows + Chroma vectors + thumbnail files on disk). The actual
        source files on the user's disk are NEVER touched — only the derived
        index data Tern wrote.

        Returns a dict with the count of files removed and any errors so the
        caller can report back to the UI.
        """
        # Normalize prefix so "/Users/me/Video" doesn't accidentally match
        # "/Users/me/Videos2/...". We require a trailing slash for the
        # prefix-match comparison.
        normalized = prefix.rstrip("/")
        like = normalized + "/%"
        # Also match the prefix itself in case someone re-indexed a file at
        # exactly that path (rare; included for completeness).
        rows = self.conn.execute(
            "SELECT id, path FROM files WHERE path = ? OR path LIKE ?",
            (normalized, like),
        ).fetchall()
        removed: list[int] = []
        errors: list[str] = []
        for row in rows:
            fid = row["id"]
            try:
                self.cleanup_file(fid)
                # Drop the file row last so we still know what to clean up if
                # the per-file pass partially fails.
                self.conn.execute("DELETE FROM files WHERE id=?", (fid,))
                self.conn.commit()
                removed.append(fid)
            except Exception as e:
                errors.append(f"file_id={fid}: {e}")
        return {"files_removed": len(removed), "ids": removed, "errors": errors}

    def insert_keyframes_batch(self, items: list[tuple[Keyframe, list[float]]]) -> None:
        if not items:
            return
        kf_ids: list[int] = []
        with self.transaction() as conn:
            for kf, _ in items:
                cur = conn.execute(
                    "INSERT INTO keyframes (file_id, ts_ms, thumbnail_path, embedding_dim) VALUES (?, ?, ?, ?) RETURNING id",
                    (kf.file_id, kf.ts_ms, kf.thumbnail_path, kf.embedding_dim),
                )
                kf_ids.append(cur.fetchone()["id"])
        self.vectors.add(
            ids=[str(i) for i in kf_ids],
            embeddings=[emb for _, emb in items],
            metadatas=[
                {"file_id": kf.file_id, "ts_ms": kf.ts_ms, "thumbnail_path": kf.thumbnail_path}
                for kf, _ in items
            ],
        )

    # --- Search operations ---

    def search_transcript(self, query: str, limit: int = 50, file_filter: list[int] | None = None) -> list[SearchHit]:
        # FTS5 query with snippet. We also pull the segment's full text so we
        # can refine ts_ms by approximate-word-position below.
        sql = """
            SELECT
                t.file_id,
                t.start_ms,
                t.end_ms,
                t.text AS full_text,
                snippet(transcript_fts, 0, '<mark>', '</mark>', '...', 32) AS snippet,
                bm25(transcript_fts) AS score,
                f.path AS file_path
            FROM transcript_fts
            JOIN transcript_segments t ON t.id = transcript_fts.rowid
            JOIN files f ON f.id = t.file_id
            WHERE transcript_fts MATCH ?
        """
        params: list = [query]
        if file_filter:
            placeholders = ",".join("?" for _ in file_filter)
            sql += f" AND t.file_id IN ({placeholders})"
            params.extend(file_filter)
        sql += " ORDER BY score LIMIT ?"
        params.append(limit)
        rows = self.conn.execute(sql, params).fetchall()
        # Lower bm25 = better; convert to a 0-1 score
        hits: list[SearchHit] = []
        for r in rows:
            refined_ts = _refine_transcript_ts(
                query=query,
                full_text=r["full_text"] or "",
                start_ms=r["start_ms"],
                end_ms=r["end_ms"],
            )
            hits.append(SearchHit(
                file_id=r["file_id"],
                file_path=r["file_path"],
                ts_ms=refined_ts,
                duration_ms=max(2000, r["end_ms"] - refined_ts),
                snippet=r["snippet"],
                source="transcript",
                score=1.0 / (1.0 + abs(r["score"])),
            ))
        return hits

    def search_ocr(self, query: str, limit: int = 50, file_filter: list[int] | None = None) -> list[SearchHit]:
        sql = """
            SELECT
                o.file_id,
                o.frame_ts_ms,
                o.text AS matched_text,
                snippet(ocr_fts, 0, '<mark>', '</mark>', '...', 32) AS snippet,
                bm25(ocr_fts) AS score,
                f.path AS file_path
            FROM ocr_fts
            JOIN ocr_segments o ON o.id = ocr_fts.rowid
            JOIN files f ON f.id = o.file_id
            WHERE ocr_fts MATCH ?
        """
        params: list = [query]
        if file_filter:
            placeholders = ",".join("?" for _ in file_filter)
            sql += f" AND o.file_id IN ({placeholders})"
            params.extend(file_filter)
        sql += " ORDER BY score LIMIT ?"
        params.append(limit)
        rows = self.conn.execute(sql, params).fetchall()
        hits: list[SearchHit] = []
        for r in rows:
            # Expand the snippet with the rest of the OCR text from the same
            # keyframe (same file + same frame_ts_ms). This turns a bare-word
            # snippet like `<mark>Stanford</mark>` into something like
            # `Y Combinator | <mark>Stanford</mark> | Lecture 1 | 2014` so the
            # user actually understands what's on the slide.
            ctx = self._build_ocr_context(
                file_id=r["file_id"],
                frame_ts_ms=r["frame_ts_ms"],
                matched_text=r["matched_text"] or "",
            )
            hits.append(SearchHit(
                file_id=r["file_id"],
                file_path=r["file_path"],
                ts_ms=r["frame_ts_ms"],
                duration_ms=2000,
                snippet=ctx or r["snippet"],
                source="ocr",
                score=1.0 / (1.0 + abs(r["score"])),
            ))
        return hits

    def _build_ocr_context(
        self, file_id: int, frame_ts_ms: int, matched_text: str, max_chars: int = 140
    ) -> str | None:
        """Stitch together all OCR lines from the same keyframe and wrap the
        matched one in `<mark>...</mark>` so it stands out. Returns None if
        only the matched line exists (caller falls back to the raw snippet).

        We dedupe by line text (case-insensitive) so repeated lines like the
        slide footer don't bloat the snippet.
        """
        matched_norm = matched_text.strip().lower()
        if not matched_norm:
            return None
        # Pull ALL OCR rows at the same exact ts. Apple Vision returns one
        # row per detected line on the keyframe.
        rows = self.conn.execute(
            "SELECT text FROM ocr_segments WHERE file_id=? AND frame_ts_ms=? ORDER BY id",
            (file_id, frame_ts_ms),
        ).fetchall()
        if not rows or len(rows) <= 1:
            return None
        seen: set[str] = set()
        parts: list[str] = []
        for row in rows:
            t = (row["text"] or "").strip()
            if not t:
                continue
            key = t.lower()
            if key in seen:
                continue
            seen.add(key)
            if key == matched_norm:
                parts.append(f"<mark>{_html_escape(t)}</mark>")
            else:
                parts.append(_html_escape(t))
        if not parts:
            return None
        snippet = " · ".join(parts)
        if len(snippet) > max_chars:
            # Window around the matched line so we don't blow out the UI.
            try:
                idx = next(i for i, p in enumerate(parts) if p.startswith("<mark>"))
            except StopIteration:
                idx = 0
            # Greedy: take the matched line plus neighbours until we hit max_chars
            lo = hi = idx
            total = len(parts[idx])
            while total < max_chars and (lo > 0 or hi < len(parts) - 1):
                if lo > 0 and (hi == len(parts) - 1 or len(parts[lo - 1]) <= len(parts[hi + 1])):
                    lo -= 1
                    total += len(parts[lo]) + 3
                else:
                    hi += 1
                    total += len(parts[hi]) + 3
            window = parts[lo : hi + 1]
            prefix = "… " if lo > 0 else ""
            suffix = " …" if hi < len(parts) - 1 else ""
            snippet = prefix + " · ".join(window) + suffix
        return snippet

    def search_filename(self, query: str, limit: int = 50, file_filter: list[int] | None = None) -> list[SearchHit]:
        """Return one representative hit per file whose basename matches a
        query token. Compensates for SigLIP's weak grounding on single-word
        visual queries (e.g. "mountain" against picsum_mountain.jpg scores
        below the noise floor, so it never made the candidate set). The
        filename match guarantees the obviously-correct file shows up.

        Score is a flat 0.50 — high enough to clear noise floors, low enough
        that a real transcript/OCR match still wins. Source is "visual" so
        existing snippet/render logic Just Works; sources list also gets
        "filename" so the UI can later badge it differently if needed.
        """
        # Length filter: 3+ chars (Latin noise floor — `if`, `to`, `or`
        # would falsely match every English-named file at <3) OR 2+ chars
        # if the token contains any CJK / Japanese / Korean character
        # (each char is a full morpheme; 会議, 予算, 회사 are legitimate
        # standalone search terms). Mirrors the SearchEngine._apply_
        # filename_boost token filter from commit 63ff96b — pre-this-
        # commit that fix was incomplete because search_filename
        # (the FETCH path) dropped CJK 2-char tokens BEFORE the boost
        # path could even see them. Net: a Japanese user searching
        # "会議" got zero filename hits even when a file literally
        # named "会議録_2024.mp4" existed in the index.
        tokens = [
            t for t in re.findall(r"\w+", (query or "").lower())
            if len(t) >= 3 or (len(t) >= 2 and _token_has_cjk(t))
        ]
        if not tokens:
            return []

        # Coarse pre-filter at SQL level — LOWER(path) LIKE '%<token>%' for
        # each token, OR'd together. Without this, the OLD code pulled
        # `SELECT id, path FROM files` (ALL files in the workspace) and did
        # the filter in Python; 10k-file workspaces would allocate 10k row
        # objects then discard ~99% of them. SQL LIKE can't do word-boundary
        # matching so the Python loop below still applies the precise check
        # — but it's now operating on a small candidate set, not the world.
        where_parts = []
        params: list = []
        if file_filter:
            placeholders = ",".join("?" for _ in file_filter)
            where_parts.append(f"id IN ({placeholders})")
            params.extend(file_filter)
        # One LIKE clause per token. Coarse — matches substring anywhere in
        # the path, including extensions and dir names. Python loop refines.
        like_clauses = ["LOWER(path) LIKE ?" for _ in tokens]
        where_parts.append("(" + " OR ".join(like_clauses) + ")")
        params.extend([f"%{t}%" for t in tokens])
        sql = "SELECT id, path FROM files WHERE " + " AND ".join(where_parts)
        rows = self.conn.execute(sql, params).fetchall()

        # Collect matching file IDs first so we can batch-fetch their first
        # keyframes in ONE query instead of N+1. Old code ran one
        # SELECT FROM keyframes per match — 30 hits = 30 separate queries.
        matched: list[tuple[int, str]] = []
        for r in rows:
            base = Path(r["path"]).stem.lower()
            base_norm = re.sub(r"[_\-.\s]+", " ", base)
            base_words = set(base_norm.split())
            if not any(t in base_words or t in base_norm for t in tokens):
                continue
            matched.append((r["id"], r["path"]))
            if len(matched) >= limit:
                break
        if not matched:
            return []

        # Batch fetch: lowest-ts_ms keyframe per file_id. Uses a window-
        # function (SQLite 3.25+) so each file's first keyframe comes back
        # in one round-trip. Falls back to a correlated subquery on older
        # SQLite — but our floor is 3.40+ (macOS 14 ships 3.43+, our
        # Python wheel includes 3.45+). The ROW_NUMBER + PARTITION BY
        # pattern is what every modern SQLite tutorial recommends for
        # first-row-per-group.
        ids = [m[0] for m in matched]
        kf_placeholders = ",".join("?" for _ in ids)
        kf_rows = self.conn.execute(
            f"""
            SELECT file_id, ts_ms, thumbnail_path FROM (
              SELECT file_id, ts_ms, thumbnail_path,
                     ROW_NUMBER() OVER (PARTITION BY file_id ORDER BY ts_ms) AS rn
              FROM keyframes
              WHERE file_id IN ({kf_placeholders})
            ) WHERE rn = 1
            """,
            ids,
        ).fetchall()
        kf_by_file = {r["file_id"]: (r["ts_ms"], r["thumbnail_path"]) for r in kf_rows}

        hits: list[SearchHit] = []
        for fid, path in matched:
            ts_ms, thumb = kf_by_file.get(fid, (0, None))
            hits.append(SearchHit(
                file_id=fid,
                file_path=path,
                ts_ms=ts_ms,
                duration_ms=2000,
                snippet=None,
                source="visual",  # render path is identical; sources[] gets "filename"
                score=0.50,
                thumbnail_path=thumb,
            ))
        return hits

    def search_visual(
        self, query_embedding: list[float], limit: int = 50, file_filter: list[int] | None = None
    ) -> list[SearchHit]:
        where: dict | None = None
        if file_filter:
            where = {"file_id": {"$in": file_filter}}
        results = self.vectors.query(
            query_embeddings=[query_embedding],
            n_results=limit,
            where=where,
        )
        hits: list[SearchHit] = []
        if not results["ids"] or not results["ids"][0]:
            return hits
        triples = list(zip(
            results["ids"][0], results["distances"][0], results["metadatas"][0]
        ))
        # Pre-batch file_path resolution. The previous implementation issued
        # one `SELECT path FROM files WHERE id=?` per visual hit — N+1 on
        # the search hot path (~10 ms wasted serialization for the default
        # limit=50 even on a fast SSD; worse when SQLite hits the macOS
        # filesystem cache cold after a long idle). One IN-list query is
        # the same call pattern search_filename adopted in 7d58e0c.
        unique_ids = sorted({m["file_id"] for _, _, m in triples})
        path_by_id: dict[int, str] = {}
        if unique_ids:
            placeholders = ",".join("?" * len(unique_ids))
            for row in self.conn.execute(
                f"SELECT id, path FROM files WHERE id IN ({placeholders})",
                unique_ids,
            ).fetchall():
                path_by_id[row["id"]] = row["path"]
        for kf_id, dist, meta in triples:
            file_id = meta["file_id"]
            file_path = path_by_id.get(file_id)
            if not file_path:
                # File was deleted from `files` since this vector was indexed
                # (cleanup_file race, manual DELETE, etc.). Skip the orphan
                # vector — search continues with the remaining valid hits.
                continue
            hits.append(
                SearchHit(
                    file_id=file_id,
                    file_path=file_path,
                    ts_ms=meta["ts_ms"],
                    duration_ms=2000,
                    snippet=f"[visual match at {meta['ts_ms']//1000}s]",
                    source="visual",
                    score=1.0 - dist,  # cosine distance to similarity
                    thumbnail_path=meta.get("thumbnail_path"),
                )
            )
        return hits

    def close(self) -> None:
        """Release the SQLite connection and this Store's Chroma client.

        chromadb caches one System per persist directory at class level and
        reference-counts the clients on it. Only `Client.close()` gives the
        reference back; when the last client on a directory closes, the
        System stops and drops the Rust bindings and their open files.
        Closing just SQLite left every Store's Chroma files open for the
        life of the process, about six descriptors each, which is enough
        to hit macOS's default limit of 256 in a long test run. Another
        Store still open on the same directory keeps the shared System
        alive, so this is safe while the API server holds its own Store.
        Both calls are idempotent. Older chromadb releases have no
        `Client.close()`; there the old behaviour stays.
        """
        self.conn.close()
        close_chroma = getattr(self.chroma, "close", None)
        if callable(close_chroma):
            close_chroma()
