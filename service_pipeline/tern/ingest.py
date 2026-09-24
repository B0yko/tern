"""Main indexing pipeline orchestrator."""
from __future__ import annotations

import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

from rich.console import Console
from rich.progress import (
    BarColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
    TimeRemainingColumn,
)
from tqdm import tqdm

from .audio import WhisperTranscriber, probe_media
from .models import FileRecord, IngestConfig, Keyframe, OCRSegment
from .storage import Store
from .vision import Embedder, KeyframeExtractor, VisionOCR, extract_image_exif

logger = logging.getLogger("tern.ingest")

SUPPORTED_VIDEO = {".mp4", ".mov", ".mkv", ".m4v", ".webm", ".avi", ".flv", ".wmv"}
SUPPORTED_AUDIO = {".mp3", ".wav", ".m4a", ".flac", ".ogg", ".opus", ".aac"}
SUPPORTED_IMAGE = {".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif", ".bmp", ".tiff", ".tif", ".gif"}
SUPPORTED = SUPPORTED_VIDEO | SUPPORTED_AUDIO | SUPPORTED_IMAGE


def _mime_for(suffix: str) -> str:
    """Return a coarse MIME string for a supported file extension."""
    if suffix in SUPPORTED_VIDEO:
        return "video/mp4"
    if suffix in SUPPORTED_AUDIO:
        return "audio/mpeg"
    if suffix in SUPPORTED_IMAGE:
        # Map a few common image extensions; everything else gets image/octet
        return {
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".png": "image/png",
            ".webp": "image/webp",
            ".heic": "image/heic",
            ".heif": "image/heif",
            ".bmp": "image/bmp",
            ".tiff": "image/tiff",
            ".tif": "image/tiff",
            ".gif": "image/gif",
        }.get(suffix, "image/octet-stream")
    return "application/octet-stream"


# Directory names we never recurse into during discovery. These are either
# Tern's own derived artifacts (db/, exports/) or generic noise (.git, etc.)
# that the user would never want indexed even when they're nominally inside
# a folder they added.
_SKIP_DIRS = {
    "db",                # workspace/db/{tern.db, chroma, thumbnails}
    "exports",           # workspace/exports/clip_*.mp3
    "__pycache__",
    ".git",
    ".DS_Store",
    "node_modules",
    ".venv",
}


def discover_files(root: Path) -> list[Path]:
    """Walk a directory and return all supported media files.

    Robust against two real macOS issues that `Path.rglob("*")` doesn't
    handle:

    1. **TCC-protected subdirs** — `~/Library/Mail`, `~/Library/Calendars`,
       any `~/Documents/com.apple.*` folder, sandboxed app containers,
       etc. raise PermissionError on stat/readdir. Pathlib's rglob bubbles
       that up and aborts the WHOLE walk; partial results from sibling
       folders are lost. We use os.walk with an onerror handler that logs
       + skips the offending subdir but keeps walking.

    2. **Symlink loops** — pathlib's rglob follows symlinks by default
       (Python 3.13 has followlinks=False but we still target 3.11).
       A user folder containing a self-referential symlink (or a chain
       to a parent) would loop forever. os.walk defaults to
       followlinks=False — explicitly so here for clarity.

    Skips _SKIP_DIRS at any depth so we don't ingest our own derived
    thumbnails (workspace/db/thumbnails/file_*/*.jpg are media files by
    extension but they're keyframe artifacts of files we already indexed).
    """
    files: list[Path] = []
    skipped_perm_errors = 0

    def _onerror(e: OSError) -> None:
        # PermissionError is the common one on macOS TCC subdirs. Any other
        # OSError (FileNotFoundError if a folder vanished mid-walk, etc.)
        # also lands here. Don't print per-error — could be thousands in a
        # deep TCC-protected tree. Count + log a single summary at the end.
        nonlocal skipped_perm_errors
        skipped_perm_errors += 1

    for dirpath, dirnames, filenames in os.walk(root, topdown=True, onerror=_onerror, followlinks=False):
        # Mutate dirnames in-place to prune _SKIP_DIRS from the walk — this
        # both skips them AND avoids descending into them (cheaper than
        # filtering paths post-hoc in the old rglob version).
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        # Also reject if ANY ancestor was skipped (defensive — e.g. someone
        # passes root=/path/db and we're already inside a skip-dir).
        if any(p in _SKIP_DIRS for p in Path(dirpath).parts):
            dirnames[:] = []
            continue
        for fn in filenames:
            if Path(fn).suffix.lower() in SUPPORTED:
                files.append(Path(dirpath) / fn)

    if skipped_perm_errors:
        # One-line summary so the indexing log shows what got skipped
        # without flooding crash.log with per-subdir entries.
        logging.warning(
            "discover_files: skipped %d unreadable subdirectories under %s "
            "(TCC-protected / permission denied)",
            skipped_perm_errors, root,
        )

    return sorted(files)


class Indexer:
    """Orchestrates the full indexing pipeline."""

    def __init__(self, config: IngestConfig):
        self.config = config
        self.store = Store(config.db_path, config.chroma_path)
        self.transcriber = WhisperTranscriber(
            model_path=config.whisper_model,
            binary=config.whisper_binary,
        )
        self.keyframe_extractor = KeyframeExtractor(
            scene_threshold=config.scene_threshold,
            max_keyframes=config.max_keyframes_per_file,
            ffmpeg_binary=config.ffmpeg_binary,
        )
        self.embedder = Embedder(model_name=config.embedding_model)
        self.ocr = VisionOCR(binary=config.vision_ocr_binary)
        self.thumbnails_path = config.thumbnails_path
        self.thumbnails_path.mkdir(parents=True, exist_ok=True)

        # Optional progress callback — set by the API layer to surface
        # sub-file pipeline stages to the indexing toast. Signature:
        #   stage_cb(stage_id: str, label: str, file_name: str)
        # where stage_id ∈ {"probe","transcribe","keyframes","embed",
        # "ocr","exif","done"}. Set to None for CLI / pytest runs that
        # don't care about per-stage progress.
        self.stage_cb = None

        # Optional sub-stage percent callback — currently only Whisper
        # publishes (parses `--print-progress` stderr line-by-line). API
        # layer sets this to a closure that writes to
        # app.state.indexing["stage_progress"]. Signature: progress_cb(pct: int).
        self.stage_progress_cb = None
        # Forward into the transcriber so it can call us during transcribe().
        # Re-wired on every call since stage_progress_cb may change.
        def _whisper_progress(pct: int) -> None:
            if self.stage_progress_cb is not None:
                try:
                    self.stage_progress_cb(pct)
                except Exception:
                    pass
        self.transcriber.progress_cb = _whisper_progress

        # Optional cancel-check callback. Signature: cancel_cb() -> bool.
        # API layer sets this to a closure that reads
        # app.state.indexing["cancel_requested"]. Forwarded into
        # WhisperTranscriber so a mid-transcription cancel takes effect
        # within ~1 s instead of waiting for the file's wait_timeout.
        self.cancel_cb = None
        def _whisper_cancel() -> bool:
            if self.cancel_cb is None:
                return False
            try:
                return bool(self.cancel_cb())
            except Exception:
                return False
        self.transcriber.cancel_cb = _whisper_cancel

    def index_directory(self, root: Path, language: str | None = None, force: bool = False) -> dict:
        """Index all media files in a directory."""
        console = Console()
        files = discover_files(root)
        console.print(f"[bold cyan]Found {len(files)} media files in {root}[/bold cyan]")

        results = {"indexed": 0, "skipped": 0, "errors": 0, "elapsed_s": 0}
        start = time.time()

        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TextColumn("[bold]{task.completed}/{task.total}[/bold]"),
            TimeElapsedColumn(),
            TimeRemainingColumn(),
            console=console,
        ) as progress:
            task = progress.add_task("Indexing files", total=len(files))
            for file_path in files:
                try:
                    indexed = self.index_file(file_path, language=language, console=console, force=force)
                    if indexed:
                        results["indexed"] += 1
                    else:
                        results["skipped"] += 1
                except Exception as e:
                    logger.exception(f"Failed to index {file_path}")
                    console.print(f"[red]ERROR[/red] {file_path}: {e}")
                    results["errors"] += 1
                progress.advance(task)

        results["elapsed_s"] = time.time() - start
        return results

    def index_file(
        self, file_path: Path, language: str | None = None, console=None, force: bool = False
    ) -> bool:
        """Index a single file. Returns True if indexed, False if skipped."""
        c = console or Console()

        # Cache the stat() result — index_file used to call file_path.stat()
        # THREE times (mtime check below, then size + mtime in the upsert
        # FileRecord block) which was 3 syscalls per file. For a 1000-file
        # folder that's ~3000 syscalls; trivially small but caching also
        # makes it OBVIOUS that the size/mtime values are a consistent
        # snapshot (back-to-back .stat() calls would technically race on a
        # mid-walk re-encoded file). Single snapshot = atomic.
        file_stat = file_path.stat()

        # Check if already indexed
        existing = self.store.get_file_by_path(str(file_path))
        if existing and existing.status == "done" and not force:
            if abs(file_stat.st_mtime - existing.mtime) < 1.0:
                return False  # Already up to date

        suffix = file_path.suffix.lower()
        is_image = suffix in SUPPORTED_IMAGE
        mime = _mime_for(suffix)

        # Cheap stage-callback helper — no-ops when stage_cb is unset.
        def _stage(stage_id: str, label: str) -> None:
            if self.stage_cb is not None:
                try:
                    self.stage_cb(stage_id, label, file_path.name)
                except Exception:
                    pass  # progress callbacks must never break the pipeline

        # Probe duration — images have none, skip ffprobe entirely for them
        if is_image:
            info = {"duration_ms": 0, "bit_rate": 0, "format_name": suffix[1:]}
        else:
            _stage("probe", "Reading metadata")
            try:
                info = probe_media(file_path)
            except Exception as e:
                c.print(f"[yellow]Cannot probe {file_path.name}: {e}[/yellow]")
                return False

        # Insert/update file record
        record = FileRecord(
            path=str(file_path),
            mime=mime,
            duration_ms=info["duration_ms"],
            size_bytes=file_stat.st_size,
            mtime=file_stat.st_mtime,
            status="indexing",
        )
        file_id = self.store.upsert_file(record)

        # If we're force-re-indexing OR re-indexing after mtime change, wipe
        # the previous transcripts/OCR/keyframes/embeddings for this file_id
        # (incl. the on-disk thumbnail dir, now done inside cleanup_file
        # itself so remove_folder's loop benefits too) so we don't
        # accumulate duplicates or leave orphan vectors in chroma.
        if existing and (force or abs(file_path.stat().st_mtime - existing.mtime) >= 1.0):
            self.store.cleanup_file(file_id)

        if is_image:
            c.print(f"  [cyan]→[/cyan] {file_path.name} (image)")
            # Extract EXIF (date taken, camera, GPS, dimensions). Empty dict for
            # photos without EXIF (stock/web/screenshots) — that's fine, we just
            # don't show anything in the UI for them.
            exif = extract_image_exif(file_path)
            if exif:
                self.store.set_file_metadata(file_id, exif)
                interesting = ", ".join(f"{k}={v}" for k, v in exif.items() if k in ("date_taken", "make", "model"))
                if interesting:
                    c.print(f"    [green]✓[/green] EXIF: {interesting}")
        else:
            c.print(f"  [cyan]→[/cyan] {file_path.name} ({info['duration_ms']//60000} min)")

        try:
            # 1. For audio: transcribe directly. For VIDEO: kick off Whisper AND
            #    ffmpeg keyframe extraction in parallel — both are subprocess
            #    calls that release the GIL while waiting, so a ThreadPoolExecutor
            #    overlaps them and cuts video indexing time roughly in half on M-series.
            video_keyframes = None
            video_kf_dir = None
            if suffix in SUPPORTED_VIDEO:
                video_kf_dir = self.thumbnails_path / f"file_{file_id}"
                _stage("transcribe", "Transcribing speech + extracting keyframes")
                t_total = time.time()
                with ThreadPoolExecutor(max_workers=2) as ex:
                    fut_kf = ex.submit(self.keyframe_extractor.extract, file_path, video_kf_dir)
                    fut_tr = ex.submit(self.transcriber.transcribe, file_path, file_id=file_id, language=language)
                    segments = fut_tr.result()
                    video_keyframes = fut_kf.result()
                self.store.insert_transcript_segments(segments)
                c.print(
                    f"    [green]✓[/green] {len(segments)} transcript segments + "
                    f"{len(video_keyframes)} keyframes in {time.time()-t_total:.1f}s (parallel)"
                )
            elif not is_image:
                # audio-only
                _stage("transcribe", "Transcribing speech")
                t0 = time.time()
                segments = self.transcriber.transcribe(file_path, file_id=file_id, language=language)
                self.store.insert_transcript_segments(segments)
                c.print(f"    [green]✓[/green] {len(segments)} transcript segments in {time.time()-t0:.1f}s")

            # 2a. For images: one synthetic keyframe at ts=0 using a downscaled copy,
            # then embed + OCR identically to a video keyframe.
            if is_image:
                _stage("keyframes", "Preparing image")
                t0 = time.time()
                kf_dir = self.thumbnails_path / f"file_{file_id}"
                thumb_path = self.keyframe_extractor.snapshot_image(file_path, kf_dir)
                keyframes = [(0, thumb_path)] if thumb_path else []
                c.print(f"    [green]✓[/green] {len(keyframes)} keyframes in {time.time()-t0:.1f}s")

                if keyframes:
                    _stage("embed", "Embedding visual scene")
                    t0 = time.time()
                    paths = [p for _, p in keyframes]
                    # embed_images returns (surviving_paths, embeddings).
                    # Build a path → emb dict so the keyframe loop can skip
                    # any frame whose Image.open failed (corrupt JPEG,
                    # vanished mid-pipeline) — the file as a whole still
                    # finishes with its remaining good keyframes.
                    embedded_paths, embeddings = self.embedder.embed_images(paths)
                    emb_by_path = {p: e for p, e in zip(embedded_paths, embeddings)}
                    kf_items = []
                    for (ts_ms, tp) in keyframes:
                        emb = emb_by_path.get(tp)
                        if emb is None:
                            continue
                        kf = Keyframe(
                            file_id=file_id,
                            ts_ms=ts_ms,
                            thumbnail_path=str(tp),
                            embedding_dim=len(emb),
                        )
                        kf_items.append((kf, emb.tolist()))
                    self.store.insert_keyframes_batch(kf_items)
                    c.print(f"    [green]✓[/green] embedded {len(kf_items)}/{len(keyframes)} keyframes in {time.time()-t0:.1f}s")

                    _stage("ocr", "Reading on-screen text")
                    t0 = time.time()
                    # OCR runs on the FULL keyframe set (the Swift binary
                    # opens its own file handles and has its own per-file
                    # try/except). A frame that failed PIL.Image
                    # decode might still OCR fine, or vice versa — they're
                    # independent codecs.
                    ocr_map = self.ocr.ocr_batch(paths, mode=self.config.ocr_mode, confidence_threshold=self.config.ocr_confidence_threshold)
                    ocr_segments = []
                    for (ts_ms, tp) in keyframes:
                        for seg in ocr_map.get(tp, []):
                            seg.file_id = file_id
                            seg.frame_ts_ms = ts_ms
                            ocr_segments.append(seg)
                    if ocr_segments:
                        self.store.insert_ocr_segments(ocr_segments)
                    c.print(f"    [green]✓[/green] OCR: {len(ocr_segments)} text blocks in {time.time()-t0:.1f}s")

            # 2b. For videos: visual embeddings + OCR. Keyframes were already
            # extracted in parallel with Whisper above.
            elif suffix in SUPPORTED_VIDEO:
                keyframes = video_keyframes or []

                if keyframes:
                    # Embed in batches
                    _stage("embed", f"Embedding {len(keyframes)} visual frames")
                    t0 = time.time()
                    paths = [p for _, p in keyframes]
                    # embed_images returns (surviving_paths, embeddings) —
                    # see image-branch comment above. A 80-keyframe video
                    # with one truncated keyframe file (partial ffmpeg
                    # write) used to fail the WHOLE video's visual
                    # indexing; now it ships the 79 good ones.
                    embedded_paths, embeddings = self.embedder.embed_images(paths)
                    emb_by_path = {p: e for p, e in zip(embedded_paths, embeddings)}
                    kf_items = []
                    for (ts_ms, thumb_path) in keyframes:
                        emb = emb_by_path.get(thumb_path)
                        if emb is None:
                            continue
                        kf = Keyframe(
                            file_id=file_id,
                            ts_ms=ts_ms,
                            thumbnail_path=str(thumb_path),
                            embedding_dim=len(emb),
                        )
                        kf_items.append((kf, emb.tolist()))
                    self.store.insert_keyframes_batch(kf_items)
                    c.print(f"    [green]✓[/green] embedded {len(kf_items)}/{len(keyframes)} keyframes in {time.time()-t0:.1f}s")

                    # OCR pass — runs on the full keyframe set; OCR and
                    # PIL-embed failures are independent codec paths.
                    _stage("ocr", f"Reading text from {len(paths)} frames")
                    t0 = time.time()
                    ocr_map = self.ocr.ocr_batch(paths, mode=self.config.ocr_mode, confidence_threshold=self.config.ocr_confidence_threshold)
                    ocr_segments = []
                    for (ts_ms, thumb_path) in keyframes:
                        for seg in ocr_map.get(thumb_path, []):
                            seg.file_id = file_id
                            seg.frame_ts_ms = ts_ms
                            ocr_segments.append(seg)
                    if ocr_segments:
                        self.store.insert_ocr_segments(ocr_segments)
                    c.print(f"    [green]✓[/green] OCR: {len(ocr_segments)} text blocks across {len(paths)} frames in {time.time()-t0:.1f}s")

            self.store.set_file_status(file_id, "done")
            return True

        except Exception as e:
            self.store.set_file_status(file_id, "error", error=str(e))
            raise

    def stats(self) -> dict:
        return self.store.stats()

    def close(self) -> None:
        self.store.close()
