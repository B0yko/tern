"""Tern — FastAPI backend.

Wraps the existing Python pipeline (Whisper + SigLIP + Apple Vision + ChromaDB)
as HTTP endpoints. The HTML/CSS/JS frontend lives in ../app/ and is served
from the root path.

Run:
    cd tern/api
    uv run uvicorn main:app --host 127.0.0.1 --port 18765 --reload

Open:
    http://localhost:18765
"""
from __future__ import annotations

import asyncio
import json
import mimetypes
import os
import subprocess
import sys
import time
import urllib.request
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Request, BackgroundTasks, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

# Pull in the working tern pipeline.
# main.py is at tern/api/main.py — two parents up is tern/, then service_pipeline/.
# (Previously used .parent.parent.parent which pointed at the outer ./service_pipeline/
# that no longer exists. The `.pth` editable install masked this; when that
# breaks — e.g. macOS keeps UF_HIDDEN on the .pth — `uv run uvicorn` would fail.)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "service_pipeline"))
from tern.cli import default_config  # type: ignore
from tern.ingest import Indexer, discover_files, SUPPORTED_IMAGE  # type: ignore
from tern.audio import probe_media  # type: ignore
from tern.models import IngestConfig, SearchQuery as PipelineSearchQuery  # type: ignore
from tern.search import SearchEngine  # type: ignore
from tern.storage import Store  # type: ignore
from tern.vision import Embedder  # type: ignore
from tern.clip import extract_clip, export_fcpxml, extract_audio_clip  # type: ignore

# Trial quota + machine identity. Lives next to main.py; kept separate so
# the rules can be tested without standing up the API.
import licensing  # noqa: E402


# ─── Configuration ───────────────────────────────────────────────────────

# Workspace = where the indexed DB lives. Default: demo workspace.
ROOT = Path(__file__).resolve().parent.parent
DEFAULT_WORKSPACE = ROOT / "demo"
WORKSPACE_PATH = Path(os.environ.get("TERN_WORKSPACE", str(DEFAULT_WORKSPACE))).resolve()

APP_DIR = ROOT / "app"
# Vendored service_pipeline lives at tern/service_pipeline/ (canonical location
# since the outer /service_pipeline/ was removed). ROOT = tern/, so the path
# resolves to tern/service_pipeline/.
PIPELINE_DIR = ROOT / "service_pipeline"


# ─── Crash log writer ────────────────────────────────────────────
# Persistent log of unhandled exceptions + structured events to a file the
# developer can pull from a user's machine via ~/Library/Logs/tern-crash.log
# (or via the in-app "Send diagnostics" action when we ship it).

import logging
import traceback
from datetime import datetime, timezone

_CRASH_LOG = Path.home() / "Library" / "Logs" / "tern-crash.log"
_CRASH_LOG.parent.mkdir(parents=True, exist_ok=True)

# Rotation threshold. Previously, the crash log used `open("a")`
# with no rotation — fine for week-one customers, but a power user who
# runs Tern daily generates ~100-1000 INFO events per indexing pass and
# the file grows ~200 KB-2 MB per day. After a year: 70-700 MB. Real
# downstream pain:
#   - /api/diagnostics typically tails the log and was returning multi-
#     megabyte responses for power users
#   - Console.app and `tail -f` choke on huge text files
#   - Spotlight tries to index the file and pegs a CPU core
# 10 MB current + 10 MB single backup = 20 MB hard cap for the whole
# log story. Single .1 backup (not the usual N-file rotation) because
# the value is "we still have yesterday's logs if the customer's bug
# spans a Tern restart"; deeper history rarely helps and bloats the
# cap by N×.
_MAX_LOG_BYTES = 10 * 1024 * 1024  # 10 MB


def _rotate_log_if_needed() -> None:
    """Rotate _CRASH_LOG to .1 if it exceeds _MAX_LOG_BYTES.

    Best-effort: any error during rotation (permissions, missing dir,
    sibling .1 locked by another process) silently swallowed so a
    rotation hiccup never blocks the actual log write that triggered
    the check. Race-tolerant: if two threads stat() and both decide to
    rotate, the second os.replace silently overwrites the first's .1
    — worst case: lose the first's content. Acceptable trade vs. a
    threading.Lock on every log line.
    """
    try:
        if not _CRASH_LOG.exists():
            return
        if _CRASH_LOG.stat().st_size <= _MAX_LOG_BYTES:
            return
        backup = _CRASH_LOG.with_suffix(_CRASH_LOG.suffix + ".1")
        # os.replace is atomic; the next log_event() will create a fresh
        # _CRASH_LOG via open("a").
        import os as _os
        _os.replace(_CRASH_LOG, backup)
    except Exception:
        pass


def _resolve_app_version() -> str:
    """Read the canonical version from pyproject.toml at startup.

    Was hardcoded "0.1.1" in /api/diagnostics — drifted from the real
    version (0.1.0 across pyproject.toml + tauri.conf.json + Cargo.toml)
    and would have drifted further on every release bump. Customer
    impact: support tickets reported wrong version, the auto-updater
    banner ("v0.1.3 is available") + diagnostics ("I'm v0.1.1") looked
    inconsistent.

    Single source of truth = api/pyproject.toml. Caches the result so
    /api/diagnostics doesn't re-parse on every call. Falls back to
    "unknown" if the file isn't parseable (shouldn't happen — the API
    can't have booted without it) so the endpoint never 500s on this
    field alone.
    """
    pyproject = Path(__file__).parent / "pyproject.toml"
    try:
        # tomllib is stdlib on 3.11+, which is our floor.
        import tomllib
        with pyproject.open("rb") as f:
            data = tomllib.load(f)
        return str(data.get("project", {}).get("version", "unknown"))
    except Exception:
        return "unknown"

_APP_VERSION = _resolve_app_version()

def log_event(category: str, msg: str, level: str = "INFO", **fields) -> None:
    """Append a structured one-line JSON event to the crash log.

    `level` is one of INFO / WARN / ERROR / CRITICAL. Used so a customer's
    diagnostic dump can be `grep '"level":"ERROR"' tern-crash.log` instead
    of trying to infer severity from the category string.
    """
    try:
        # Rotate BEFORE writing so the new write lands in a fresh
        # (post-rotation) file rather than the one we're about to retire.
        _rotate_log_if_needed()
        entry = {
            # `datetime.utcnow()` is deprecated in 3.12+ — emits a noisy
            # DeprecationWarning on every log line if pytest / the bundle's
            # interpreter happens to run on 3.13. The replace-trick keeps
            # the on-wire string byte-identical to before
            # (`2026-05-21T10:00:00.123456Z`) so existing log readers /
            # grep patterns still match — only the call shape changed.
            "ts": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "level": level,
            "category": category,
            "msg": msg,
            **fields,
        }
        with _CRASH_LOG.open("a") as f:
            # ensure_ascii=False mirrors the storage.py behaviour;
            # without it, log entries containing
            # non-ASCII content (a folder path like ~/Документы/Подкасты,
            # a Whisper-transcribed Cyrillic / CJK error message, a HEIC
            # filename with accents) get escaped to `До...`,
            # making tern-debug.log unreadable for support threads that
            # ARE the primary use case for this log (the
            # `cancel_indexing_writes_log_event_with_snapshot` etc. tests
            # all assume human-readable msg strings). The log file is
            # opened in text mode → UTF-8 on macOS, so the canonical
            # form lands on disk as-is.
            f.write(json.dumps(entry, default=str, ensure_ascii=False) + "\n")
    except Exception:
        # never let logging take down the request
        pass

def log_exception(category: str, exc: BaseException, **fields) -> None:
    """Like log_event but captures the full traceback. Always logs at ERROR."""
    log_event(
        category=category,
        msg=str(exc),
        level="ERROR",
        exception_type=type(exc).__name__,
        traceback=traceback.format_exc(),
        **fields,
    )


# ─── Python-logging → crash-log bridge ───────────────────────────────────
# service_pipeline modules use stdlib logging
# (`logger.exception("search_transcript failed …")` in search.py's
# per-channel try/excepts, plus warnings from ingest/storage). Stdlib
# logging defaults to stderr — which the bundled Tauri launcher
# discards (Stdio::null). So every "one search channel silently died"
# traceback, the exact diagnostic the per-channel try/except design
# exists to capture, was INVISIBLE in production. Dev mode (./run.sh,
# terminal attached) saw them; customer bundles didn't — and customer
# bundles are where support needs them.
#
# Bridge: a logging.Handler that forwards WARNING+ records through
# log_event, so they land as structured JSON in tern-crash.log next
# to every other durable event. INFO/DEBUG stay stderr-only (dev
# noise; uvicorn access logs would flood the crash log). Guarded
# against recursion (a log_event failure can't re-enter logging) by
# log_event's own blanket try/except.
class _CrashLogBridge(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        try:
            extra = {}
            if record.exc_info:
                extra["traceback"] = "".join(traceback.format_exception(*record.exc_info))
            log_event(
                category=f"pylog.{record.name}",
                msg=record.getMessage(),
                level="ERROR" if record.levelno >= logging.ERROR else "WARN",
                **extra,
            )
        except Exception:
            pass

logging.getLogger().addHandler(_CrashLogBridge(level=logging.WARNING))


# ─── ChromaDB defensive patch ────────────────────────────────────────────
# Known chromadb bug (rust.py:131): RustBindingsAPI.stop() does
#   `del self.bindings`
# but `self.bindings` is only set after a successful `_create_database()` —
# so any partially-initialised instance crashes on shutdown with
#   AttributeError: 'RustBindingsAPI' object has no attribute 'bindings'
# This trashes both interpreter teardown (poisoning the next test in
# pytest) AND the next PersistentClient(path=…) call, because chromadb
# tries to release the cached half-failed system from a previous attempt.
# Wrap stop() so half-failed instances tear down silently.
try:
    from chromadb.api.rust import RustBindingsAPI as _RustBindings
    _orig_stop = _RustBindings.stop
    def _safe_stop(self) -> None:
        try:
            _orig_stop(self)
        except AttributeError:
            pass  # already torn down or never initialised — ignore
    _RustBindings.stop = _safe_stop  # type: ignore[method-assign]
except Exception:
    # chromadb internals can change between versions; if the patch site
    # moves, fall through silently — production is unaffected.
    pass


# ─── Lifecycle ───────────────────────────────────────────────────────────

def _rewrite_stale_workspace_paths(workspace: Path) -> None:
    """Rewrite absolute paths in files.path + keyframes.thumbnail_path
    so they reference the current workspace instead of whatever path
    the bundled DB was built with. See lifespan() for the full
    trial-flow rationale.

    Conservative algorithm — only rewrites a row when ALL of:
      1. Existing path doesn't already start with the workspace.
      2. Path contains the `/demo/` segment (bundled-demo signature).
      3. The workspace-relative rewrite target ACTUALLY EXISTS on
         disk. Without this check, a user-indexed folder that happens
         to contain `/demo/` in its path (e.g.,
         `/Users/buyer/Documents/demo_recordings/`) could be falsely
         flagged and rewritten to a non-existent workspace location.
         The "target must exist" gate makes false positives essentially
         impossible — a buyer-named "demo" folder wouldn't have a
         coincidentally-matching workspace-relative file.

    Defensive: catches all exceptions and logs to stderr but never
    raises — worst-case the buyer sees the old "Couldn't load Audio"
    failure, NOT a refusal to start the sidecar.
    """
    db_path = workspace / "db" / "tern.db"
    if not db_path.exists():
        return  # fresh workspace, no DB to rewrite
    workspace_str = str(workspace)
    marker = "/demo/"
    try:
        import sqlite3 as _sqlite3
        conn = _sqlite3.connect(str(db_path))
        try:
            file_rows = conn.execute("SELECT id, path FROM files").fetchall()
            file_updates: list[tuple[str, int]] = []
            for row_id, path in file_rows:
                if not path:
                    continue
                if path.startswith(workspace_str + "/"):
                    continue  # already correctly rooted
                idx = path.find(marker)
                if idx < 0:
                    continue  # not a bundled-demo path
                relative = path[idx + len(marker):]
                target = workspace / relative
                if not target.exists():
                    continue  # safety gate — refuse to rewrite to a phantom path
                file_updates.append((str(target), row_id))

            kf_rows = conn.execute(
                "SELECT id, thumbnail_path FROM keyframes WHERE thumbnail_path IS NOT NULL"
            ).fetchall()
            kf_updates: list[tuple[str, int]] = []
            for kf_id, kf_path in kf_rows:
                if not kf_path or kf_path.startswith(workspace_str + "/"):
                    continue
                idx = kf_path.find(marker)
                if idx < 0:
                    continue
                relative = kf_path[idx + len(marker):]
                target = workspace / relative
                if not target.exists():
                    continue
                kf_updates.append((str(target), kf_id))

            if not file_updates and not kf_updates:
                return  # nothing stale; idempotent re-launch path

            conn.executemany(
                "UPDATE files SET path = ? WHERE id = ?", file_updates,
            )
            conn.executemany(
                "UPDATE keyframes SET thumbnail_path = ? WHERE id = ?", kf_updates,
            )
            conn.commit()
            # Both print + log_event: print is visible in dev (run.sh
            # captures stdout) AND survives stderr=Stdio::null in the
            # Tauri bundle for free. log_event lands in tern-crash.log
            # so /api/diagnostics' log_tail surfaces the rewrite in
            # any support dump — important because a buyer who reports
            # "Tern won't play my files after install" needs us to know
            # whether the rewrite RAN (and how many rows it touched)
            # to distinguish the path-rewrite-broken case from a
            # different bug.
            print(f"   Demo-seed path rewrite: {len(file_updates)} files, "
                  f"{len(kf_updates)} keyframes re-rooted under {workspace_str}")
            log_event(
                "demo_path_rewrite",
                f"{len(file_updates)} files, {len(kf_updates)} keyframes re-rooted under {workspace_str}",
                level="INFO",
                files=len(file_updates),
                keyframes=len(kf_updates),
                workspace=workspace_str,
            )
        finally:
            conn.close()
    except Exception as e:
        # Tauri's bundled launcher sets the sidecar's stdout + stderr to
        # Stdio::null (see tauri/src-tauri/src/main.rs), so a bare print
        # to stderr on a real buyer install lands in /dev/null with no
        # trail in tern-debug.log AND nothing for /api/diagnostics to
        # surface. The buyer reports "Tern can't play my audio after
        # install," support asks for diagnostics, the dump shows the
        # workspace is set up correctly but the rewrite phase is
        # invisible — looks like a different bug entirely. log_event
        # writes to tern-crash.log directly so the failure traceback
        # appears in /api/diagnostics' log_tail (and the "Copy
        # diagnostics for support…" button picks it up). Keep the
        # stderr print for dev-tree visibility — harmless duplicate in
        # bundle (discarded by Stdio::null).
        print(f"   ⚠ demo-seed path rewrite failed: {e!r}", file=sys.stderr)
        log_event(
            "demo_path_rewrite_failed",
            f"{type(e).__name__}: {e}",
            level="ERROR",
            exc=type(e).__name__,
        )


@asynccontextmanager
async def lifespan(app: FastAPI):
    print(f"🦅 Tern API starting")
    print(f"   Workspace: {WORKSPACE_PATH}")
    print(f"   App dir:   {APP_DIR}")
    print(f"   Pipeline:  {PIPELINE_DIR}")

    WORKSPACE_PATH.mkdir(parents=True, exist_ok=True)
    (WORKSPACE_PATH / "db").mkdir(parents=True, exist_ok=True)

    # CRITICAL trial-flow fix: the bundled demo workspace ships with
    # absolute paths from the BUILD machine (the dev box where
    # prepare_bundle.sh ran). When a buyer downloads the .app, Tauri
    # seeds the workspace at ~/Library/Application Support/Tern/
    # workspace/ — but the bundled tern.db still references the
    # build-machine paths inside files.path and keyframes.thumbnail_
    # path. A search would find hits via the FTS5 + Chroma tables
    # (those use file_id, not path), the frontend would try to play
    # the audio via /api/file?path=/Users/<build_user>/.../ep03.mp3,
    # the path wouldn't exist on the buyer's Mac, and the audio
    # element would surface "Couldn't load Audio." That single
    # moment makes the bundled demo look broken on first launch.
    #
    # Detect + rewrite on every sidecar startup. Cheap (one SELECT,
    # one UPDATE per stale row at most), idempotent (second run is
    # a no-op since paths now match the workspace), and self-
    # healing if a buyer ever moves their workspace dir.
    _rewrite_stale_workspace_paths(WORKSPACE_PATH)

    # Lazy-init the search engine on first request (loads SigLIP, takes ~2s)
    app.state.engine = None
    app.state.store = None
    app.state.embedder = None
    app.state.config = None
    app.state.indexing = {
        "running": False,
        "paused": False,
        # Initialized False so /api/index/status returns a stable shape
        # whether or not an index has ever started in this session.
        # Previously, the field was only inserted by /api/index
        # when starting a run, so a fresh-launch poll returned a dict
        # WITHOUT cancel_requested. The frontend used `?? false` to
        # paper over the absence, but any test pinning the
        # /api/index/status response shape (or a future client that
        # doesn't use the `?? false` idiom) would trip on the dormant
        # case. Stable contract beats defensive nullish-coalescing.
        "cancel_requested": False,
        "current_file": None,
        "files_pending": 0,
        "files_done": 0,
        "files_errored": 0,
        # Per-run count of already-up-to-date files (indexer.index_file
        # returns False when the file's mtime matches the stored row and
        # force=False). Without this counter, re-indexing a folder where
        # most files were already done made the progress bar stall under
        # 100% (pct = done / pending; skipped files counted in pending
        # but not done) and the "Indexed N files" toast under-reported
        # the actual work — confusing for any user re-running indexing
        # after adding a few new files to an existing folder.
        "files_skipped": 0,
        "start_time": None,
        "log": [],
        # Sub-file pipeline stage published by Indexer.stage_cb. Lets the
        # toast tell the user WHAT Tern is doing inside a long-running file
        # (Whisper alone is 30+ s/file; "current_file: ep03.mp3" alone tells
        # them nothing). Values: probe / transcribe / keyframes / embed /
        # ocr / done; label is human-readable copy from ingest.py.
        "stage": None,
        "stage_label": None,
        "stage_started_at": None,
        # Sub-stage progress 0-100 published by Whisper's --print-progress
        # parser (audio.py). null when no progress signal yet (e.g., probe/
        # embed/ocr stages don't emit percent — only transcribe does).
        "stage_progress": None,
    }

    # Pre-warm SigLIP + ChromaDB in the background so the FIRST /api/search is
    # ~50 ms (warm-path) instead of ~5 s (cold-path SigLIP weights load + first
    # forward pass). The Tauri sidecar reports "backend ready" the moment the
    # listener binds, but without this thread the user could type a query and
    # stare at a frozen results pane for 5 s. Daemon thread so it dies cleanly
    # with the parent on shutdown.
    import threading
    def _warmup():
        try:
            get_engine(app)
            print("🔥 search engine warm — first /api/search will be instant")
        except Exception as e:
            log_exception("warmup_failed", e)
            print(f"   (warmup failed: {e}; will lazy-load on first search)")
    threading.Thread(target=_warmup, daemon=True, name="engine-warmup").start()

    yield

    print("🦅 Tern API stopping")

    # Defensive teardown of ChromaDB. There's a known chromadb bug where if
    # the System fails partway through init (warmup raced a parallel start,
    # workspace path didn't exist yet, etc.) the `bindings` attribute never
    # gets set — then component.stop() crashes with
    # `AttributeError: 'RustBindingsAPI' object has no attribute 'bindings'`
    # during interpreter teardown. In pytest this surfaces as the LAST test
    # in the module failing with no useful trace. Swallow it here.
    store = getattr(app.state, "store", None)
    if store is not None and getattr(store, "chroma", None) is not None:
        try:
            # PersistentClient has _system; ask it to stop cleanly.
            sys = getattr(store.chroma, "_system", None)
            if sys is not None:
                try:
                    sys.stop()
                except Exception:
                    pass  # chromadb's own teardown bug — ignore
        except Exception:
            pass


app = FastAPI(
    title="Tern API",
    description="Local AI search for podcast & video archives",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    # SECURITY: allow_origins must never include "*". With the no-auth
    # loopback model, a wildcard lets any website the user visits in
    # Safari/Chrome make cross-origin requests to 127.0.0.1:18765 and
    # exfiltrate the full search index, file paths, and diagnostics. The
    # path-traversal allowlist doesn't help
    # against /api/search / /api/files / /api/stats which are by-design
    # "return everything in the index" reads.
    #
    # Instead a regex covers only loopback. Any localhost
    # or 127.0.0.1 port matches (the Rust shell auto-finds a free port from
    # 18765 upward, so a fixed origin list would be brittle).
    # Plus the tauri:// scheme used by the asset protocol (currently
    # unused since we load via http://127.0.0.1, but harmless to grant).
    # Public origins fail CORS preflight — browser-loaded malicious pages
    # can no longer call our API.
    allow_origin_regex=r"^(https?://(127\.0\.0\.1|localhost)(:\d+)?|tauri://localhost)$",
    allow_methods=["*"],
    allow_headers=["*"],
)


# ─── Host-header guard (DNS rebinding) ───────────────────────────────────
# CORS only governs cross-origin reads. A DNS-rebinding page first resolves
# its own name to a public address, then re-points it at 127.0.0.1: the
# browser now treats the loopback API as SAME-origin with that page, so no
# CORS check ever fires, yet every request still carries the attacker's name
# in its Host header. Accepting only loopback names on the port this server
# is actually listening on closes that path.
#
# The desktop shell navigates its webview to http://127.0.0.1:<port>/ and the
# frontend uses relative URLs, so its Host is always 127.0.0.1:<port>.
# Browsers pointed at localhost (./run.sh, dev servers) use the other names.
_LOOPBACK_HOSTNAMES = frozenset({"127.0.0.1", "localhost", "[::1]"})


def _host_header_allowed(host_header: str | None, server_port: int | None) -> bool:
    """True when `Host` is a loopback name on the port this server listens on.

    `server_port` comes from the ASGI scope (the socket the request arrived
    on), not from configuration, so it stays right for any auto-picked port.
    A Host without a port means the scheme default (80) and only matches a
    server actually listening there. `None` (unix socket) skips the port
    comparison and checks the name alone.
    """
    if not host_header:
        return False
    host = host_header.strip().lower()
    if host.startswith("["):
        # IPv6 literal: "[::1]" or "[::1]:18765".
        end = host.find("]")
        if end == -1:
            return False
        name, rest = host[: end + 1], host[end + 1:]
        if rest and not rest.startswith(":"):
            return False
        port_text = rest[1:]
    else:
        name, _, port_text = host.partition(":")
    if name not in _LOOPBACK_HOSTNAMES:
        return False
    if port_text and not (port_text.isascii() and port_text.isdigit()):
        return False
    if server_port is None:
        return True
    return (int(port_text) if port_text else 80) == server_port


class _LoopbackHostGuard:
    """Pure-ASGI middleware: reject requests whose Host is not loopback.

    Written by hand rather than with Starlette's TrustedHostMiddleware
    because that one matches host names only and cannot bind the check to
    the port the server is listening on (which is chosen at launch).
    """

    def __init__(self, inner):
        self.inner = inner

    async def __call__(self, scope, receive, send):
        if scope["type"] in ("http", "websocket"):
            hosts = [v for k, v in scope["headers"] if k == b"host"]
            server = scope.get("server")
            port = server[1] if server else None
            ok = len(hosts) == 1 and _host_header_allowed(
                hosts[0].decode("latin-1"), port
            )
            if not ok:
                if scope["type"] == "http":
                    resp = JSONResponse({"detail": "Invalid Host header"}, status_code=400)
                    await resp(scope, receive, send)
                else:
                    await send({"type": "websocket.close", "code": 1008})
                return
        await self.inner(scope, receive, send)


# Added after CORSMiddleware so it is the outermost layer: a request with a
# foreign Host is refused before CORS (or any route) sees it.
app.add_middleware(_LoopbackHostGuard)


# Cap the in-memory indexing log. app.state.indexing["log"] is a plain
# list that grew unbounded — one append per indexed file + cancel/stage
# events. A 10k-file indexing run accumulates ~10k strings (500 KB-1 MB
# in RAM) that persist for the lifetime of the process; /api/index/status
# and /api/diagnostics already slice to the last 30 lines, so older
# entries serve no purpose. Trim in-place on append (cheap; del slice is
# C-impl) instead of switching to deque so the existing dict[**spread]
# JSON serialisation in /api/index/status keeps working without converters.
_INDEXING_LOG_MAX = 200

def _indexing_log_append(msg: str) -> None:
    log = app.state.indexing["log"]
    log.append(msg)
    if len(log) > _INDEXING_LOG_MAX:
        del log[: len(log) - _INDEXING_LOG_MAX]


# Global exception handler — log + re-raise. We do this rather than a
# pure middleware so HTTPException(404 etc) doesn't pollute the crash log.
@app.exception_handler(Exception)
async def _log_unhandled(request: Request, exc: Exception):
    log_exception("api.unhandled", exc, path=str(request.url.path), method=request.method)
    return JSONResponse(
        status_code=500,
        content={"detail": f"Internal error: {type(exc).__name__}", "type": "internal_error"},
    )


# Diagnostics endpoint — power-user "Send diagnostics" button reads this.
_HOME_STR = str(Path.home())


def _redact_home(value):
    """Replace the user's literal home dir with `~` in any string. Returns
    non-strings unchanged. Used for /api/diagnostics — the endpoint is
    GET, unauthenticated, loopback — and customers regularly screencap
    its response to send to support via screenshots, public Slack, or
    GitHub issues. Without redaction, that exposes the Mac username to
    anyone who sees the screenshot. The product's whole selling point
    is "100% local · your audio never leaves this Mac"; leaking the
    username through a diagnostics dump is a small but contradictory
    privacy paper-cut. We rewrite recursively into list / dict shapes
    so nested log entries get scrubbed too."""
    if isinstance(value, str):
        return value.replace(_HOME_STR, "~")
    if isinstance(value, list):
        return [_redact_home(v) for v in value]
    if isinstance(value, dict):
        return {k: _redact_home(v) for k, v in value.items()}
    return value


def _tail_lines(path: Path, n: int = 100, max_read_bytes: int = 1_048_576) -> list[str]:
    """Return the last `n` lines from `path` without loading the whole file.

    The previous /api/diagnostics implementation did `f.readlines()[-100:]`,
    which allocates ALL lines of the (up-to-10 MB) crash log just to slice
    off the tail. For a typical session with ~500 KB of accumulated log
    entries, that's >1500 string allocations per "Send diagnostics" click,
    dropped on the floor immediately after. Worse on the customer machine
    that's been running Tern for weeks and is bumping the rotation cap.

    Strategy: seek to (size - 64 KB), read forward, split, drop the partial
    first line (we landed mid-line unless we already covered the whole
    file), keep the last `n`. Double the window up to `max_read_bytes` if
    we don't have enough lines yet (pathological stack-trace entries can
    span KB each — the log_exception path embeds traceback.format_exc()).
    Cap at 1 MiB so a 10 MB log with 50 KB-per-line entries doesn't
    regress to the readlines() path we're trying to avoid.

    Returns lines with trailing newlines preserved (matches readlines()
    behaviour so the JSON wire shape stays byte-identical for callers
    that have been grepping the field).
    """
    try:
        size = path.stat().st_size
    except Exception:
        return []
    if size == 0:
        return []
    read_n = min(65_536, size)
    try:
        with path.open("rb") as f:
            while True:
                f.seek(max(0, size - read_n))
                buf = f.read(read_n)
                # JSONL is one entry per line. Plain ASCII for the framing
                # (datetime + JSON delimiters); utf-8 with replace is safe
                # for the embedded message bodies.
                lines = buf.decode("utf-8", errors="replace").splitlines(keepends=True)
                # If we started reading mid-file, the first "line" is
                # almost certainly the tail of an earlier entry — drop it.
                if read_n < size and lines:
                    lines = lines[1:]
                if len(lines) >= n or read_n >= size or read_n >= max_read_bytes:
                    return lines[-n:]
                read_n = min(read_n * 2, max_read_bytes, size)
    except Exception:
        return []


@app.get("/api/diagnostics")
async def diagnostics():
    import asyncio
    log_path = _CRASH_LOG
    size = log_path.stat().st_size if log_path.exists() else 0
    # Sync file IO offloaded to the thread pool so a 10 MB-log read
    # (worst case: ~30 ms on warm cache, 200ms+ cold) doesn't block
    # /api/health, /api/stats, the indexing-status poll while the
    # user is clicking "Send diagnostics."
    tail = await asyncio.to_thread(_tail_lines, log_path, 100) if log_path.exists() else []
    # Cap indexing.log to the last 30 entries (matches what /api/index/status
    # returns) — without this, a long-running indexing session bloats the
    # diagnostics payload to many MB, slow for the "Send diagnostics" flow
    # and risky if log lines ever capture sensitive paths.
    indexing_snapshot = None
    if hasattr(app.state, "indexing") and app.state.indexing is not None:
        indexing_snapshot = {
            **app.state.indexing,
            "log": (app.state.indexing.get("log") or [])[-30:],
        }
    # Inline a workspace stats summary so support tickets that include
    # /api/diagnostics output see what's indexed in one payload instead
    # of needing a separate /api/stats round-trip. The most common
    # support-thread questions are "how many files did you index?" and
    # "how does Tern's idea of your library size compare to what's on
    # disk?" — both answerable from this snapshot without another
    # back-and-forth. Wrapped in try/except so a transient store
    # failure (DB lock, mid-startup) doesn't break diagnostics — the
    # whole point of this endpoint is to surface state when other
    # things are misbehaving. On failure we return stats=None, and
    # the rest of the diagnostics payload is still useful.
    stats_snapshot = None
    try:
        store = get_store(app)
        s = await asyncio.to_thread(store.stats)
        stats_snapshot = {
            "files_total": s.get("files_total", 0),
            "files_done": s.get("files_done", 0),
            "files_video": s.get("files_video", 0),
            "files_audio": s.get("files_audio", 0),
            "files_image": s.get("files_image", 0),
            "total_duration_ms": s.get("total_duration_ms", 0),
            "total_duration_hours": round(s.get("total_duration_ms", 0) / 3_600_000, 2),
            "transcript_segments": s.get("transcript_segments", 0),
            "ocr_segments": s.get("ocr_segments", 0),
            "keyframes": s.get("keyframes", 0),
        }
    except Exception as e:
        log_exception("diagnostics_stats_failed", e)
    return _redact_home({
        "version": _APP_VERSION,
        "log_path": str(log_path),
        "log_size_bytes": size,
        "log_tail": tail,
        "workspace": str(WORKSPACE_PATH),
        "indexing": indexing_snapshot,
        "stats": stats_snapshot,
    })


# ─── License key validation ───────────────────────────────────────
# Wires the desktop app to the license server. Storage is a tiny JSON file
# in the user's app-support dir so the activation survives app re-installs
# and workspace changes.


def _state_dir() -> Path:
    """Where per-machine state (licence, trial counter) lives.

    Resolved on every call rather than captured at import, so tests can
    redirect it with TERN_STATE_DIR. Without that, the activation test
    writes a licence into the developer's real Application Support folder
    and every later run of the suite believes the machine is licensed.
    """
    override = os.environ.get("TERN_STATE_DIR")
    if override:
        return Path(override)
    return Path.home() / "Library" / "Application Support" / "Tern"


def _trial_file() -> Path:
    return _state_dir() / "trial.json"


# Kept as a module-level name because existing tests monkeypatch it
# directly (see test_security_and_polish's license-write tests). New state
# should go through _state_dir() instead.
_LICENSE_FILE = _state_dir() / "license.json"
# The full URL of the validate endpoint, not a prefix. It is deliberately
# not baked into the source: a build points it at its own deployment of
# license_server/ (see license_server/README.md). Unset means activation
# answers with a clear "not configured" message instead of guessing a host.
_LICENSE_DEFAULT_SERVER = os.environ.get("TERN_LICENSE_SERVER", "").strip()
# Further endpoints an activation request may name in `server_url` (for
# example a customer's self-hosted deployment): comma-separated full URLs in
# TERN_LICENSE_ALLOWED_SERVERS. Together with the default above this is the
# whole allowlist; any other server_url is refused, so the API cannot be
# pointed at an arbitrary host by whatever can reach the loopback port.
def _parse_server_list(raw: str) -> tuple[str, ...]:
    return tuple(u.strip() for u in raw.split(",") if u.strip())


_LICENSE_EXTRA_SERVERS = _parse_server_list(os.environ.get("TERN_LICENSE_ALLOWED_SERVERS", ""))


def _endpoint_key(url: str) -> tuple:
    """Comparable form of an endpoint URL: scheme and host are
    case-insensitive, a trailing slash on the path is not significant."""
    from urllib.parse import urlsplit
    p = urlsplit(url.strip())
    return (p.scheme.lower(), p.netloc.lower(), p.path.rstrip("/"), p.query)


def _license_server_allowed(url: str) -> bool:
    """True when `url` is the configured licence server or on the allowlist."""
    allowed = [_LICENSE_DEFAULT_SERVER, *_LICENSE_EXTRA_SERVERS]
    return _endpoint_key(url) in {_endpoint_key(u) for u in allowed if u}


def _is_licensed(cache: dict | None = None) -> bool:
    """True when this copy holds a licence the server has accepted."""
    c = _load_license_cache() if cache is None else cache
    return bool(c.get("license_key")) and bool(c.get("is_valid"))


def _load_trial() -> dict:
    return licensing.load_trial(_trial_file())


def _current_trial_state() -> dict:
    return licensing.trial_state(_load_trial(), licensed=_is_licensed())


def _quota_admit(file_path: Path) -> tuple[bool, str | None, int]:
    """Decide whether the trial can afford this file, before any work.

    Returns (allowed, reason, duration_ms). The duration comes back so the
    caller can charge exactly what it admitted rather than probing twice.

    Fails open on a probe error: a corrupt file is an indexing problem, and
    reporting it to the user as a billing problem would send them hunting
    for a licence bug that isn't there. index_file will surface the real
    failure a moment later.
    """
    if _is_licensed():
        return True, None, 0
    if file_path.suffix.lower() in SUPPORTED_IMAGE:
        return True, None, 0  # no timeline, nothing to meter, no ffprobe
    try:
        duration_ms = int(probe_media(file_path).get("duration_ms") or 0)
    except Exception:
        return True, None, 0
    allowed, reason = licensing.check_quota(
        _load_trial(), licensed=False, duration_ms=duration_ms
    )
    return allowed, reason, duration_ms


def _charge_trial(duration_ms: int) -> None:
    """Spend quota. Never raises: a state-file write failure must not take
    down an indexing run the user has already been allowed to start."""
    if _is_licensed() or not duration_ms or duration_ms <= 0:
        return
    try:
        licensing.save_trial(_trial_file(),
                             licensing.record_usage(_load_trial(), duration_ms))
    except Exception as e:
        log_exception("trial_write_failed", e)


def _load_license_cache() -> dict:
    """Read the cached license. Bare except → return {} previously
    swallowed both 'file not found' (expected on first launch) AND
    'JSON corrupt' (which the user MUST know about — silent fallback
    to {} flipped their sidebar from Licensed to Trial with no log
    entry to debug from). Split the two cases."""
    if not _LICENSE_FILE.exists():
        return {}
    try:
        raw = _LICENSE_FILE.read_text()
        if not raw.strip():
            # Empty file = mid-write crash from before the atomic rename
            # below shipped. Treat as missing, log so the user knows.
            log_event(
                "license_cache_empty",
                f"license cache exists but is empty (size=0); treating as missing",
                level="WARN", path=str(_LICENSE_FILE),
            )
            return {}
        return json.loads(raw)
    except json.JSONDecodeError as e:
        log_event(
            "license_cache_corrupt",
            f"license cache is not valid JSON: {e}; treating as missing",
            level="ERROR", path=str(_LICENSE_FILE), error=str(e),
        )
        return {}
    except Exception as e:
        log_exception("license_cache_read_failed", e)
        return {}


def _save_license_cache(data: dict) -> None:
    """Atomic write + restrictive permissions.

    Previously: `write_text` truncated the file then re-wrote. If
    the process crashed (signal, OOM, force-quit) between truncate and
    final flush, the file would be left empty — silent license loss
    next launch. Now: write to a temp sibling, fsync, then os.replace
    onto the target. POSIX guarantees os.replace is atomic on the
    same filesystem, so the file either contains the old contents OR
    the new contents — never a half-written state.

    Previously: default file mode 644 — readable by any local
    process. License keys are credentials. Now chmod 600 (owner-read
    only) immediately after rename. Effective even when the file
    didn't exist before (atomic-write codepath creates it 600).
    """
    try:
        _LICENSE_FILE.parent.mkdir(parents=True, exist_ok=True)
        # ensure_ascii=False mirrors the storage.py + log_event behaviour.
        # The cached license dict
        # holds an `email` field (RFC 6531 internationalized addresses
        # may contain non-ASCII chars) and a `message` field
        # (license-server-provided, may be localized). Default
        # ASCII-escape would render those as `\uXXXX` in the on-disk
        # license.json — fine for round-trip but ugly when the user
        # inspects the file directly (a real debugging pattern when
        # a license activation goes sideways). The file is UTF-8
        # native (written via fdopen "w" without encoding= argument
        # → platform default UTF-8 on macOS).
        payload = json.dumps(data, indent=2, ensure_ascii=False)
        # Sibling temp file in the SAME directory — os.replace requires
        # same-filesystem move, and tempfile.mkstemp(dir=...) gives us
        # one in the right place. Suffix is .tmp so a crash leaves a
        # debuggable artifact rather than a mystery file.
        import os as _os
        import tempfile as _tempfile
        fd, tmp_path = _tempfile.mkstemp(
            prefix=".license-", suffix=".tmp",
            dir=str(_LICENSE_FILE.parent),
        )
        try:
            with _os.fdopen(fd, "w") as f:
                f.write(payload)
                f.flush()
                _os.fsync(f.fileno())
            # Tighten perms BEFORE rename — once it lands at the target
            # path another process can read it. chmod-after-rename has
            # a TOCTOU window where 644 leaks the key.
            _os.chmod(tmp_path, 0o600)
            _os.replace(tmp_path, _LICENSE_FILE)
            tmp_path = None  # consumed by replace; don't clean up
        finally:
            # Defensive cleanup if anything before the replace raised.
            if tmp_path and Path(tmp_path).exists():
                try: Path(tmp_path).unlink()
                except Exception: pass
    except Exception as e:
        log_exception("license_cache_write_failed", e)


def _atomic_write_text(path: Path, text: str, *, encoding: str = "utf-8") -> None:
    """Write `text` to `path` atomically via tempfile + fsync + os.replace.

    Same pattern as _save_license_cache but reusable for
    any file the user re-opens elsewhere. The three export endpoints
    (CSV / SRT / FCPXML) previously used the plain `path.write_text(...)`
    sequence: truncate-then-rewrite. If the process is killed mid-write
    (signal, OOM, force-quit, host sleep at the wrong moment), the file
    is left truncated. The customer then imports a half-SRT into
    DaVinci → wrong subtitles burned into a 45-min film; opens a
    half-CSV → missing rows with zero error indication; loads a
    half-FCPXML into Final Cut → "corrupt project" alert.

    With this helper, POSIX guarantees the target file contains EITHER
    the old contents OR the new contents — never a half-written state.
    """
    import os as _os
    import tempfile as _tempfile
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = _tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
    )
    try:
        with _os.fdopen(fd, "w", encoding=encoding) as f:
            f.write(text)
            f.flush()
            _os.fsync(f.fileno())
        _os.replace(tmp_path, path)
        tmp_path = None
    finally:
        if tmp_path and Path(tmp_path).exists():
            try: Path(tmp_path).unlink()
            except Exception: pass


class LicenseActivateRequest(BaseModel):
    """Input gating for /api/license/activate.

      - license_key: capped at 256 chars. A local process POSTing
        `{"license_key": "X" * 10_000_000}` would otherwise make the API
        forward a 10 MB body to the licence server and cache the
        (rejected) garbage to ~/Library/Application Support/Tern/license.json.
        Real keys are short (TERN-XXXX-XXXX-XXXX is 19 chars); 256 leaves
        headroom for future formats (UUID + hyphens + version prefix).
      - server_url: optional, at most 2 KB, http(s) only, and it must match
        the configured licence server (TERN_LICENSE_SERVER) or an entry of
        TERN_LICENSE_ALLOWED_SERVERS; the handler refuses anything else.
        Otherwise a local-process attacker could redirect the activation
        POST to their own server to harvest license keys ("you typed your
        key into Tern; the activation went to attacker.com instead of the
        licence server"). The CORS gate doesn't help here — this isn't a
        browser request, it's any local process curling the loopback API.
        The scheme guard rejects javascript: / file: / data: / ftp: before
        the allowlist is consulted.
    """
    license_key: str = Field(..., min_length=1, max_length=256)
    server_url: Optional[str] = Field(default=None, max_length=2048)

    @field_validator("server_url")
    @classmethod
    def _server_url_must_be_http(cls, v: Optional[str]) -> Optional[str]:
        if v is None or not v.strip():
            return v
        s = v.strip().lower()
        if not (s.startswith("http://") or s.startswith("https://")):
            raise ValueError(
                "server_url must be an http:// or https:// URL "
                "(got something else — javascript:, file:, data:, and "
                "other schemes are rejected by design)"
            )
        return v.strip()


@app.get("/api/license/status")
async def license_status():
    """Return current license state. Never blocks; the desktop app uses this
    purely to render a status badge / unlock prompts. Real gating (if any)
    happens at feature-flag time."""
    cache = _load_license_cache()
    trial = licensing.trial_state(_load_trial(), licensed=_is_licensed(cache))
    if not cache.get("license_key"):
        left_min = (trial["remaining_ms"] or 0) // 60_000
        return {
            "status": "unactivated",
            "license_key": None,
            "email": None,
            "validated_at": None,
            "message": (
                f"No licence key entered. {left_min} of "
                f"{licensing.TRIAL_LIMIT_MS // 60_000} trial minutes left."
            ),
            "trial": trial,
        }
    return {
        "status": "active" if cache.get("is_valid") else "invalid",
        "license_key": (cache.get("license_key") or "")[:8] + "…",
        "email": cache.get("email"),
        "validated_at": cache.get("validated_at"),
        "message": cache.get("message"),
        "trial": trial,
    }


@app.post("/api/license/activate")
async def license_activate(req: LicenseActivateRequest):
    """Verify a license key against the license server, cache the result.

    Validation failures don't raise — the response carries an `ok: false` so
    the UI can show a friendly error and let the user retry. License-server
    downtime treats the key as TENTATIVELY valid (the prior cached state
    wins) so a flaky connection doesn't kick paid users out of their app.
    """
    key = (req.license_key or "").strip()
    if not key:
        raise HTTPException(400, "license_key is required")
    requested = (req.server_url or "").strip()
    if requested and not _license_server_allowed(requested):
        raise HTTPException(
            403,
            "server_url is not a configured licence server. Set "
            "TERN_LICENSE_SERVER, or list it in TERN_LICENSE_ALLOWED_SERVERS.",
        )
    server = (requested or _LICENSE_DEFAULT_SERVER).rstrip("/")
    if not server:
        # Not a transport failure, so the cached state is reported as-is
        # rather than treated as a flaky connection.
        prev = _load_license_cache()
        return {
            "ok": False,
            "message": (
                "No licence server is configured. Set TERN_LICENSE_SERVER to "
                "the license-validate endpoint (see license_server/README.md)."
            ),
            "cached": bool(prev.get("license_key")),
        }
    try:
        import asyncio
        import urllib.request
        import urllib.error
        # Use the resolved _APP_VERSION (parsed from pyproject.toml at
        # startup) instead of hardcoding.
        # Hardcoded "0.1.0" drifted from the canonical version on every
        # release bump — license server's telemetry / version-gated
        # rollouts would have mis-reported once we shipped any update.
        # machine_id is what makes per-seat licensing enforceable: without
        # it the server cannot tell one customer Mac from another and a
        # single key works across the whole building. It is a salted hash
        # of the hardware UUID, not the UUID itself — the server needs to
        # count machines, not identify them.
        body = json.dumps({
            "license_key": key,
            "app_version": _APP_VERSION,
            "machine_id": licensing.machine_id(),
        }).encode()
        # `server` is the endpoint itself, not a prefix. Appending a path
        # here used to assume the licence server owned its own routing,
        # which a Supabase Edge Function (served at /functions/v1/<name>)
        # cannot do — the activation would have posted to a URL that does
        # not exist. A self-hosted deployment can now point this anywhere.
        req_obj = urllib.request.Request(
            server,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        # Wrap the SYNCHRONOUS urlopen in asyncio.to_thread so it runs in
        # the default thread executor and doesn't block the FastAPI event
        # loop while waiting on the license server (up to 8 s). Without
        # this, a slow / dead license-server response froze every other
        # in-flight /api/* request — /api/health, /api/stats, the
        # indexing status poll. Real customer-visible failure mode for
        # anyone hitting Activate at the wrong moment.
        def _fetch_validate() -> dict:
            with urllib.request.urlopen(req_obj, timeout=8) as r:
                return json.loads(r.read())
        payload = await asyncio.to_thread(_fetch_validate)
        is_valid = bool(payload.get("is_valid"))
        cache = {
            "license_key": key,
            "is_valid": is_valid,
            "email": payload.get("email"),
            "purchased_at": payload.get("purchased_at"),
            "validated_at": _now_iso(),
            "message": payload.get("message"),
        }
        _save_license_cache(cache)
        log_event("license_activate", f"validated key {key[:8]}…", is_valid=is_valid)
        return {"ok": True, **cache, "license_key": key[:8] + "…"}
    except Exception as e:
        log_exception("license_activate_failed", e)
        # Preserve any previously-cached state on transient errors
        prev = _load_license_cache()
        return {
            "ok": False,
            "message": f"License server unreachable: {e.__class__.__name__}",
            "cached": bool(prev.get("license_key")),
        }


@app.post("/api/license/clear")
async def license_clear():
    """Remove the cached license — for sign-out.

    Reports actual outcome so the sidebar's license-badge UI doesn't
    flip to "Trial" while the file is still on disk (which would let
    a subsequent activation-check restore the old license). Two
    failure cases:

    1. FileNotFoundError (TOCTOU race: file vanished between our
       .exists() check and the .unlink() call) — IDEMPOTENT, the
       desired state is reached; log + return ok:True.
    2. Any other exception (PermissionError, OSError) — the file is
       STILL THERE; surface ok:False so the UI knows the sign-out
       didn't take effect.
    """
    try:
        # Log INFO so support diagnostics dumps can grep for sign-out
        # events without needing to correlate against license_activate
        # entries. Don't log the key (sensitive); the file's existence
        # is the signal. Previously, license_activate logged but
        # license_clear was silent — making "user signed out at 14:32"
        # impossible to confirm from the log alone, which slows down any
        # support thread where the user says "I clicked sign-out and now
        # Tern still thinks I have a license."
        existed = _LICENSE_FILE.exists()
        if existed:
            _LICENSE_FILE.unlink()
        log_event("license_clear", "license cache cleared" if existed else "license clear no-op (file already gone)",
                  was_present=existed)
        return {"ok": True}
    except FileNotFoundError:
        # Benign race — the file vanished between our exists() and
        # unlink(). Desired state ("license file gone") is reached.
        log_event("license_clear", "license clear hit TOCTOU race (file vanished mid-call)",
                  was_present=False)
        return {"ok": True}
    except Exception as e:
        log_exception("license_clear_failed", e)
        return {
            "ok": False,
            "message": f"Failed to clear license: {e.__class__.__name__}",
        }


def _now_iso() -> str:
    from datetime import datetime, timezone as _tz
    return datetime.now(_tz.utc).isoformat()


@app.get("/api/runtime-check")
async def runtime_check():
    """Probe every external binary + asset Tern needs at index time and
    report whether each is available. The UI shows a blocker modal when
    any 'required' dep is missing instead of letting indexing crash mid-
    pipeline with a cryptic ENOENT.

    Each dep returns: {present: bool, path: str|None, install: str|None}.
    `install` is a one-line shell command the user can copy to fix it.
    """
    import shutil

    # When running inside the bundled .app, TERN_BIN_DIR points at the
    # vendored binaries. Those win over PATH lookups since the customer Mac
    # may not have Homebrew at all.
    bundled = os.environ.get("TERN_BIN_DIR", "")

    def _check_binary(name: str) -> dict:
        # `.exists()` alone returns True for directories, broken-target
        # symlinks won't but a stale chmod-less file would — and the
        # runtime-check would happily report present=True while the
        # indexing pipeline crashes later with PermissionError. Verify
        # both `is_file()` AND the executable bit. prepare_bundle.sh
        # does chmod +x but a bundling failure (signal mid-copy, sneaky
        # ACL on a customer FS) could ship the bundle with the +x bit
        # cleared — this catches that case before indexing starts.
        # shutil.which already enforces executability for the PATH path.
        if bundled:
            bp = Path(bundled) / name
            if bp.is_file() and os.access(bp, os.X_OK):
                return {"present": True, "path": str(bp)}
        path = shutil.which(name)
        return {"present": bool(path), "path": path}

    def _check_file(path: Path) -> dict:
        return {"present": path.exists(), "path": str(path) if path.exists() else None}

    ffmpeg = _check_binary("ffmpeg")
    whisper = _check_binary("whisper-cli")
    uv = _check_binary("uv")
    # vision-ocr is bundled — check either bundled bin or PIPELINE_DIR fallback
    vision_ocr = _check_binary("vision-ocr")
    if not vision_ocr["present"]:
        vision_ocr = _check_file(PIPELINE_DIR / "bin" / "vision-ocr")
    # Whisper model — prefer bundled (TERN_MODEL_DIR) over PIPELINE_DIR
    bundled_model_dir = os.environ.get("TERN_MODEL_DIR", "")
    if bundled_model_dir and (Path(bundled_model_dir) / "ggml-large-v3-turbo-q5_0.bin").exists():
        whisper_model = _check_file(Path(bundled_model_dir) / "ggml-large-v3-turbo-q5_0.bin")
    else:
        whisper_model = _check_file(PIPELINE_DIR / "models" / "ggml-large-v3-turbo-q5_0.bin")

    deps = {
        "ffmpeg": {
            **ffmpeg,
            "required": True,
            "purpose": "Audio extraction, keyframe extraction, clip export",
            "install": "brew install ffmpeg" if not ffmpeg["present"] else None,
        },
        "whisper-cli": {
            **whisper,
            "required": True,
            "purpose": "Speech-to-text transcription",
            "install": "brew install whisper-cpp" if not whisper["present"] else None,
        },
        "vision-ocr": {
            **vision_ocr,
            "required": True,
            "purpose": "Apple Vision OCR (on-screen text extraction)",
            "install": "cd service_pipeline && swiftc bin/vision-ocr.swift -o bin/vision-ocr" if not vision_ocr["present"] else None,
        },
        "uv": {
            **uv,
            "required": False,
            "purpose": "Python environment manager (only needed for re-installs)",
            "install": 'curl -LsSf https://astral.sh/uv/install.sh | sh' if not uv["present"] else None,
        },
        "whisper-model": {
            **whisper_model,
            "required": False,
            "purpose": "Whisper Q5 weights (auto-downloads on first index if missing)",
            "install": None,
        },
    }
    missing_required = [name for name, d in deps.items() if d["required"] and not d["present"]]
    return {
        "ok": len(missing_required) == 0,
        "missing_required": missing_required,
        "deps": deps,
    }


def _get_config(app: FastAPI):
    if getattr(app.state, "config", None) is None:
        config = default_config(WORKSPACE_PATH)
        # Force absolute paths to avoid the Path.as_uri() bug
        config.db_path = (WORKSPACE_PATH / "db" / "tern.db").resolve()
        config.chroma_path = (WORKSPACE_PATH / "db" / "chroma").resolve()
        config.thumbnails_path = (WORKSPACE_PATH / "db" / "thumbnails").resolve()
        # Prefer bundled Whisper model (passed via TERN_MODEL_DIR by
        # the Tauri sidecar) over the auto-download path. Saves ~547 MB
        # network fetch on first launch.
        bundled_model_dir = os.environ.get("TERN_MODEL_DIR", "")
        bundled_model = Path(bundled_model_dir) / "ggml-large-v3-turbo-q5_0.bin" if bundled_model_dir else None
        if bundled_model and bundled_model.exists():
            config.whisper_model = str(bundled_model.resolve())
        else:
            config.whisper_model = str((PIPELINE_DIR / "models" / "ggml-large-v3-turbo-q5_0.bin").resolve())
        # Prefer bundled binaries (passed via TERN_BIN_DIR by the Tauri
        # sidecar) over Homebrew. Falls back to PIPELINE_DIR/bin/ for dev
        # workflow (cargo tauri dev / direct uvicorn launches).
        bundled_bin = os.environ.get("TERN_BIN_DIR")
        if bundled_bin and Path(bundled_bin).exists():
            bin_dir = Path(bundled_bin)
        else:
            bin_dir = PIPELINE_DIR / "bin"
        config.vision_ocr_binary = str((bin_dir / "vision-ocr").resolve())
        config.ffmpeg_binary = str((bin_dir / "ffmpeg").resolve()) if (bin_dir / "ffmpeg").exists() else "ffmpeg"
        config.whisper_binary = str((bin_dir / "whisper-cli").resolve()) if (bin_dir / "whisper-cli").exists() else "whisper-cli"
        app.state.config = config
    return app.state.config


# Locks for the lazy-init paths. The warmup thread (started in lifespan,
# see _warmup) AND request handlers (FastAPI runs sync defs in a thread
# pool) can race to construct the engine — both see app.state.engine is
# None, both call Embedder(...) → 2× SigLIP load = 2 × ~600 MB RAM
# allocated, ~10 s wasted on duplicate weights download, plus chromadb's
# half-failed-init bug we already monkeypatched would fire twice as often
# under contention. threading.Lock (not asyncio.Lock) because get_engine
# is called from a sync thread context.
import threading as _threading
_ENGINE_LOCK = _threading.Lock()
_STORE_LOCK = _threading.Lock()


def get_store(app: FastAPI):
    """Lazy-init JUST the Store (SQLite + ChromaDB). No SigLIP model load.
    Used by endpoints that only need DB metadata: stats, files list, folder
    remove, etc. Saves multi-second cold-start when a user just opens the
    sidebar without searching.

    Lock-guarded — concurrent first-calls would otherwise both construct
    a Store (= two PersistentClient inits on the same chroma path) which
    chromadb handles by re-releasing the first system, hitting the
    `del self.bindings` AttributeError fixed defensively in main.py's
    monkey-patch.
    """
    with _STORE_LOCK:
        if getattr(app.state, "store", None) is None:
            config = _get_config(app)
            app.state.store = Store(config.db_path, config.chroma_path)
        return app.state.store


def get_engine(app: FastAPI):
    """Lazy-init the full search engine — Store + SigLIP-2 Embedder. Only
    called from /api/search (the one endpoint that genuinely needs visual
    embedding). Cold init downloads ~600 MB the first time.

    Lock-guarded against the warmup ⇄ request-handler race: warmup runs
    in a background thread (see lifespan), request handlers run in
    FastAPI's sync-def thread pool. Without the lock both can see
    engine=None, both construct Embedder, and one ~600 MB SigLIP-2 load
    is wasted (eventually GC'd). Double-checked-locking pattern: cheap
    check outside the lock for the warm path, full check inside.
    """
    # Fast path: already initialized — skip the lock.
    if getattr(app.state, "engine", None) is not None:
        return app.state.engine, app.state.store, app.state.config
    with _ENGINE_LOCK:
        # Re-check inside the lock — another thread may have raced ahead
        # and finished init while we were waiting on the lock.
        if getattr(app.state, "engine", None) is None:
            config = _get_config(app)
            store = get_store(app)
            embedder = Embedder(model_name=config.embedding_model)
            engine = SearchEngine(store, embedder)
            app.state.embedder = embedder
            app.state.engine = engine
    return app.state.engine, app.state.store, app.state.config


# ─── Models ──────────────────────────────────────────────────────────────

_ALLOWED_SOURCES = {"transcript", "ocr", "visual"}


class SearchRequest(BaseModel):
    """Input gating at the API boundary. Previously, a client could
    send limit=99999 (forcing the engine to fetch + rank tens of thousands
    of hits), or sources=["magic"] (silently no-op since the engine only
    branches on known names). Both produce poor UX or wasted CPU. Now
    constrained at parse time so abuse never reaches the engine."""

    # Bound query length to prevent a 10 MB POST from quietly tying up the
    # FTS5 tokenizer + SigLIP text encoder. 2000 chars is ~400 words —
    # way more than any sane natural-language search.
    query: str = Field(..., min_length=0, max_length=2000)
    # Cap limit. The engine already enforces an internal cap via
    # _FETCH_CAP, but pinning the input side returns a clean 422 instead
    # of silently truncating. ge=1 prevents zero / negative.
    limit: int = Field(default=30, ge=1, le=100)
    sources: list[str] = Field(default_factory=lambda: ["transcript", "ocr", "visual"])
    weights: Optional[dict[str, float]] = None
    # Restrict results to files whose path starts with this prefix. Used by
    # the search-filters popover's "Limit to folder" dropdown. None = no
    # folder constraint. Capped at 4096 chars to mirror IndexRequest.folder,
    # RemoveFolderRequest.folder, PathRequest.path, ExportRequest.file_path,
    # and /api/file?path= — without the cap, a hostile local POST with a
    # 10 MB folder string would burn memory on Pydantic deserialization
    # before the engine ever does the cheap startswith() prefix check.
    # PATH_MAX on macOS is 1024 so 4 KB gives 4x headroom; legitimate
    # folder picker inputs are always well under that. min_length=1 so a
    # client passing `{"folder": ""}` gets a clean 422 instead of the
    # silent no-op the rstrip("/") + startswith() pair would otherwise
    # produce (empty prefix matches every path).
    folder: Optional[str] = Field(default=None, min_length=1, max_length=4096)

    @field_validator("sources")
    @classmethod
    def _sources_must_be_known(cls, v: list[str]) -> list[str]:
        # Reject unknown source names so a typo ("transcripts") returns 422
        # at the API boundary instead of silently producing zero results.
        # Empty list is also rejected — equivalent to "no search possible",
        # which is never the user's intent.
        if not v:
            raise ValueError("sources must be non-empty")
        bad = [s for s in v if s not in _ALLOWED_SOURCES]
        if bad:
            raise ValueError(
                f"unknown source(s): {bad}. Allowed: {sorted(_ALLOWED_SOURCES)}"
            )
        return v

    @field_validator("weights")
    @classmethod
    def _weights_must_be_sane(cls, v: Optional[dict[str, float]]) -> Optional[dict[str, float]]:
        # Previously, the field was Optional[dict[str, float]] with
        # zero numeric validation. Pydantic only checked that values were
        # coercible to float — so a client could send
        #   {"transcript": -100}    → flips ranking (low-score hits win)
        #   {"transcript": float('nan')} → all scores become NaN; sort
        #     becomes non-deterministic (Python's Timsort doesn't promise
        #     stable behaviour on NaN comparisons) and the user sees
        #     random results
        #   {"transcript": float('inf')} → that source dominates
        #     completely; visual / OCR hits become invisible
        #   {"transcrip": 1.0}      → typo; silently ignored by
        #     `weights.get(hit.source, 1.0)` in search.py and the caller
        #     never learns their override didn't take effect
        # No legitimate caller wants any of those, so reject at parse time.
        # Range cap of 10 keeps boosts in a sane band; 0 is allowed as
        # "fully suppress this source" (UI's "uncheck Speech" trick).
        if v is None:
            return v
        import math
        bad_keys = [k for k in v if k not in _ALLOWED_SOURCES]
        if bad_keys:
            raise ValueError(
                f"unknown weights key(s): {bad_keys}. "
                f"Allowed: {sorted(_ALLOWED_SOURCES)}"
            )
        for k, val in v.items():
            if math.isnan(val) or math.isinf(val):
                raise ValueError(f"weights[{k!r}] must be finite, got {val}")
            if val < 0 or val > 10:
                raise ValueError(
                    f"weights[{k!r}]={val} out of range [0, 10] — "
                    f"negative flips ranking, huge values trivialise other sources"
                )
        return v


class SearchHitJSON(BaseModel):
    file_id: int
    file_path: str
    file_name: str
    ts_ms: int
    duration_ms: int
    # Snippet is None for pure visual hits (no text evidence). Frontend renders
    # its own "Visual match" affordance in that case rather than echoing the
    # bare `[visual match at Xs]` placeholder.
    snippet: Optional[str] = None
    source: str
    # Explicit contributing-sources list so the UI can render
    # "speech + on-screen" instead of an opaque "multi" badge. Single-source
    # hits return a one-element list matching `source`.
    sources: list[str] = []
    score: float
    thumbnail_url: Optional[str] = None
    preview_url: str  # /api/file?path=...
    timecode: str  # human-readable HH:MM:SS
    mime: str = "application/octet-stream"
    media_kind: str = "other"  # "video" | "audio" | "image" | "other"
    metadata: dict | None = None  # EXIF for photos (date_taken, make, model, gps, dims)


class IndexRequest(BaseModel):
    """Input gating at the API boundary. Previously, all three
    fields had ZERO validation:

      - folder: any string of any length. A hostile local POST with
        `{"folder": "X" * 10_000_000}` would burn CPU on the
        Path/`_check_folder_is_safe` string ops before getting
        rejected as nonexistent. PATH_MAX on macOS is 1024, and
        even an exhaustive nested path stays well under 4 KB.
      - language: any string. Whisper language codes are 2-letter ISO
        ("en", "es", "ru", …) plus a few longer fallbacks. Caller-side
        bound at 16 chars to leave headroom; whisper-cli rejects
        unknown codes itself.
      - force: bool — Pydantic already type-checks.

    The folder safety gate (_check_folder_is_safe) still runs after
    Pydantic — these caps are about preventing abusive payloads from
    reaching the gate in the first place.
    """
    folder: str = Field(..., min_length=1, max_length=4096)
    language: Optional[str] = Field(default=None, max_length=16)
    force: bool = False


# Module-level so Pydantic doesn't shadow it as a model field/ModelPrivateAttr.
# 30 min matches the longest realistic "extract this whole talk for
# sharing" case; anything longer would be a re-record, not a clip.
_MAX_CLIP_MS = 30 * 60 * 1000


class ExportRequest(BaseModel):
    """Previously, ExportRequest accepted ANY int triple. The
    extract_clip layer already clamps negatives away with `max(0, …)`
    and short clips with `max(0.5, …)` — so a sane frontend stays in
    a sane band — but a hostile local process or curl client could
    POST `{"start_ms":0,"end_ms":360000000}` (a 100-hour clip), and
    ffmpeg would happily try to re-encode 100h of source video into
    workspace/exports/, filling the user's SSD before they notice.
    Cap the clip duration at 30 minutes at the API boundary; anything
    longer is either a bug or abuse.
    """
    # 1-4096 cap mirrors PathRequest + /api/file's
    # Query cap. Without min_length=1, an empty
    # file_path passed Pydantic and `Path("").resolve()` returned
    # the sidecar's CWD — same information-leak oracle as the
    # PathRequest cap, just on the export endpoint. Without max_length,
    # a 10 MB file_path POSTed by a hostile local process would
    # deserialize into memory before the allowlist gate could reject
    # it. POSIX PATH_MAX is 1024 on macOS / 4096 on Linux, so
    # anything past 4096 is guaranteed to fail any actual fs check
    # downstream.
    file_path: str = Field(..., min_length=1, max_length=4096)
    start_ms: int = Field(..., ge=0)
    end_ms: int = Field(..., ge=0)
    padding_ms: int = Field(default=1500, ge=0, le=30_000)
    audio_only: bool = False

    @field_validator("end_ms")
    @classmethod
    def _end_after_start(cls, v: int, info) -> int:
        start = info.data.get("start_ms")
        if start is not None and v <= start:
            raise ValueError(
                f"end_ms ({v}) must be strictly greater than start_ms ({start}) — "
                f"non-positive clip duration"
            )
        if start is not None and (v - start) > _MAX_CLIP_MS:
            mins = (v - start) // 60_000
            raise ValueError(
                f"clip duration {mins} min exceeds 30 min cap — "
                f"re-encoding that long would fill the workspace exports/ directory"
            )
        return v


class FCPXMLRequest(BaseModel):
    # 200 cap matches the SearchRequest.limit ceiling (100 ×
    # generous slack for a future bulk-export-from-saved-search flow).
    # Without a cap, a hostile local POST of `{"hits":[<1M dicts>]}`
    # exhausts memory before the per-hit FCPXML emitter even runs. The
    # frontend builds requests from state.results (which is server-limited
    # to 100) so legitimate use stays well under the cap; bigger imports
    # would need a streaming endpoint, not a list-of-dicts batch.
    hits: list[dict] = Field(..., max_length=200)
    # 200-char cap on project_name — used in the output filename (after
    # truncation to 40 chars) AND embedded in the XML body. Uncapped
    # let a hostile POST ship a 10 MB project_name string that
    # Pydantic happily deserialized then we emitted into XML — same
    # threat model as the IndexRequest cap.
    project_name: str = Field(default="Tern Search Results", max_length=200)


# ─── Helpers ─────────────────────────────────────────────────────────────

def format_timecode(ms: int) -> str:
    s = ms // 1000
    return f"{s // 3600:02d}:{(s % 3600) // 60:02d}:{s % 60:02d}"


def _kind_for(mime: str, suffix: str) -> tuple[str, str]:
    """Return (mime, media_kind) for a hit. Falls back to extension sniffing
    when mime is unknown so old DB rows keep working."""
    if mime and "/" in mime:
        kind = mime.split("/", 1)[0]
        if kind in {"video", "audio", "image"}:
            return mime, kind
    sniffed, _ = mimetypes.guess_type(f"x{suffix}")
    if sniffed and "/" in sniffed:
        kind = sniffed.split("/", 1)[0]
        if kind in {"video", "audio", "image"}:
            return sniffed, kind
    return mime or "application/octet-stream", "other"


def hit_to_json(hit, request_base_url: str = "", file_meta: dict | None = None) -> SearchHitJSON:
    abs_path = Path(hit.file_path).resolve()
    file_name = abs_path.name
    # URL-encode the path so filenames containing &, ?, #, ", or unicode round-
    # trip safely through the query string and survive interpolation into HTML
    # src=… and CSS url(…) attributes on the frontend. (Encoding "/" too is
    # harmless for query params and avoids server-side rewrite surprises.)
    import urllib.parse as _qp
    preview = "/api/file?path=" + _qp.quote(str(abs_path), safe="")
    thumb_url = None
    if hit.thumbnail_path and Path(hit.thumbnail_path).exists():
        thumb_url = "/api/file?path=" + _qp.quote(str(Path(hit.thumbnail_path).resolve()), safe="")
    mime, kind = _kind_for("", abs_path.suffix.lower())
    return SearchHitJSON(
        file_id=hit.file_id,
        file_path=str(abs_path),
        file_name=file_name,
        ts_ms=hit.ts_ms,
        duration_ms=hit.duration_ms,
        snippet=hit.snippet,
        source=hit.source,
        sources=hit.sources or [hit.source],
        score=round(hit.score, 4),
        thumbnail_url=thumb_url,
        preview_url=preview,
        timecode=format_timecode(hit.ts_ms),
        mime=mime,
        media_kind=kind,
        metadata=file_meta,
    )


# ─── API endpoints ───────────────────────────────────────────────────────

@app.get("/api/health")
async def health():
    return {"status": "ok", "workspace": str(WORKSPACE_PATH)}


@app.get("/api/version")
async def version():
    """Tiny endpoint returning ONLY the app version string.

    Why this exists: sidebar.js fires a version fetch on every app
    init (to populate the "v0.1.0" chip on the License button), and
    keyhelp.js fires one on first ⌘/ open (for the version footer of
    the shortcuts overlay). Both previously called /api/diagnostics
    because that endpoint surfaced `version` — but /api/diagnostics
    ALSO does:
      - reads up to 10 MB of crash log + tails last 100 lines
      - snapshots the indexing state (cap 30 log lines)
      - runs store.stats() — 4 SQL aggregates on the workspace DB
      - recursively redacts every "/Users/<name>/" path → "~"
    …all wrapped in two asyncio.to_thread offloads. Burning that
    pipeline JUST to read the cached `_APP_VERSION` module constant
    is pure overhead on every cold start. This endpoint returns the
    constant directly — no I/O, no thread offload, no recursion.
    """
    return {"version": _APP_VERSION}


@app.get("/api/stats")
async def stats():
    # Stats only needs the SQLite store, not the visual embedder. Saves a
    # ~600 MB SigLIP-2 model load on cold start when the user just opens
    # the app (the sidebar populates from /api/stats + /api/files BEFORE
    # any search runs).
    #
    # `store.stats()` runs 4 SQL aggregates (1 multi-COUNT pass on the
    # files table + 3 simpler COUNTs against transcripts/ocr/keyframes).
    # On a 1k-file workspace it's ~0.8 ms; on a 100k-file workspace it
    # crosses ~80 ms — enough to visibly stutter the indexing-progress
    # chrome (which polls /api/index/status concurrently with sidebar
    # refreshes that fire /api/stats). Offload to the thread pool so
    # the event loop stays responsive on big workspaces, same pattern
    # as /api/search, /api/files, /api/file/thumbnails,
    # /api/diagnostics, and /api/transcript/window.
    import asyncio
    store = get_store(app)
    s = await asyncio.to_thread(store.stats)
    return {
        "files_total": s["files_total"],
        "files_done": s["files_done"],
        # files_errored: persistent status='error' row count (storage.stats).
        # Dropping the field in the explicit re-shape here would leave
        # empty.js's "N files failed to index" warning line reading
        # undefined and never rendering, and the qa_smoke field pin
        # only catches it on a LIVE run (the unit tests don't exercise
        # this endpoint shape). Keep this passthrough in sync with
        # storage.stats() whenever new aggregate fields are added.
        "files_errored": s.get("files_errored", 0),
        "total_duration_ms": s["total_duration_ms"],
        "total_duration_hours": round(s["total_duration_ms"] / 3_600_000, 2),
        "transcript_segments": s["transcript_segments"],
        "ocr_segments": s["ocr_segments"],
        "keyframes": s["keyframes"],
        "files_video": s.get("files_video", 0),
        "files_audio": s.get("files_audio", 0),
        "files_image": s.get("files_image", 0),
        "workspace": str(WORKSPACE_PATH),
    }


@app.post("/api/search")
async def search(req: SearchRequest):
    import asyncio
    engine, store, _ = get_engine(app)
    weights = req.weights or {"transcript": 0.45, "ocr": 0.20, "visual": 0.35}
    q = PipelineSearchQuery(
        query=req.query,
        limit=req.limit,
        sources=req.sources,
        weights=weights,
    )
    # engine.search() is fully synchronous (FTS5 queries, SigLIP text
    # embed on MPS, ChromaDB vector query, reweight/dedupe/cap). The
    # whole call takes ~200-300 ms on typical input. Calling it
    # directly from this `async def` would block the event loop for
    # that duration — every concurrent /api/health and /api/index/
    # status poll waits. The indexing toast then visibly stutters
    # whenever the user types a search during indexing. Wrap in
    # asyncio.to_thread so search runs in a worker thread, event
    # loop stays responsive. Same pattern as the to_thread fix on
    # /api/license/activate.
    #
    # Thread-safety: SQLite connection has check_same_thread=False
    # (see storage.py); ChromaDB read queries are safe across threads; the
    # Embedder LRU cache (vision.py) has a theoretical move_to_end
    # race that the GIL covers in CPython but isn't formally
    # synchronised — single-user app rarely fires concurrent
    # searches (debounce 180 ms), so the practical risk is zero.
    hits = await asyncio.to_thread(engine.search, q)
    # Optional folder filter — keep only hits whose file_path is under the
    # given prefix. Done post-engine because the engine doesn't currently
    # know about folder constraints; cheap (already-narrowed top-N hits).
    if req.folder:
        prefix = req.folder.rstrip("/") + "/"
        hits = [h for h in hits if h.file_path.startswith(prefix)
                                  or h.file_path == req.folder.rstrip("/")]
    # Batch-fetch metadata for every hit's file_id so we don't run one query per row
    metadata_by_id: dict[int, dict] = {}
    file_ids = sorted({h.file_id for h in hits})
    if file_ids:
        rows = store.conn.execute(
            f"SELECT id, metadata FROM files WHERE id IN ({','.join('?' * len(file_ids))})",
            file_ids,
        ).fetchall()
        for r in rows:
            if r["metadata"]:
                try:
                    import json as _json
                    metadata_by_id[r["id"]] = _json.loads(r["metadata"])
                except Exception:
                    pass
    return {
        "query": req.query,
        "count": len(hits),
        "hits": [hit_to_json(h, file_meta=metadata_by_id.get(h.file_id)).model_dump() for h in hits],
    }


@app.get("/api/files")
async def list_indexed_files(
    limit: int | None = None,
    # Previously `status: str | None = None` accepted any string.
    # A typo like `?status=indexig` (missing 'n') silently returned
    # an empty list instead of a 422 with the valid values — the
    # caller saw "0 files" and assumed nothing was indexed when in
    # fact their filter was the problem. The FileRecord model already
    # enforces Literal["pending", "indexing", "done", "error"] at the
    # storage layer; mirror that at the API boundary via Query's
    # `pattern=` so an invalid value gets a clean Pydantic-style 422
    # naming the four valid choices. Matches the same input-validation
    # pattern as SRTExportRequest.radius — push the
    # check to the boundary so the engine never sees garbage and the
    # caller learns immediately why their request failed.
    status: str | None = Query(
        default=None,
        pattern=r"^(pending|indexing|done|error)$",
        description="Filter by file status. Valid: pending, indexing, done, error.",
    ),
):
    """List indexed files (for sidebar / empty state / library view).

    Query params:
      - `limit`: cap result rows. Empty state passes `?limit=12` since
        it only renders that many demo files anyway; sidebar / filters
        omit it to get the full list (folder-grouping needs every path).
      - `status`: filter by file status. Must be one of `pending`,
        `indexing`, `done`, or `error`. Invalid values return 422 with
        the valid choices instead of silently returning an empty list.
        Frontend doesn't use this yet; reserved for future power-user
        clients.

    Response includes `thumbnail_path` per file when a keyframe exists.
    This was originally absent — empty.js's _fileToFakeHit fell back to
    using the FULL source-file path as preview_url, meaning a 5 MB HEIC
    photo was downloaded just to render an 80x80 grid thumbnail. The
    thumbnail batch-fetch below adds <2 ms even for 1000 files.

    Performance: pushing the cap into SQL avoids materializing 5k+
    FileRecord objects (each with a metadata-JSON parse) when the
    caller only needs the first dozen.

    The sync work (`store.list_files` + the ROW_NUMBER keyframe join +
    N FileRecord constructions, each with a `json.loads(metadata)`) is
    offloaded to the thread pool so concurrent /api/health,
    /api/index/status polls, and the indexing toast stay responsive
    on big workspaces. Same pattern as /api/search, /api/diagnostics,
    and /api/file/thumbnails. For a 5k-file
    library the previous on-event-loop call blocked the loop for ~50 ms;
    the indexing-progress chrome would visibly stutter on every sidebar
    refresh during a large index run.
    """
    import asyncio
    # Sanity-clamp limit at the API boundary so a client can't ask for
    # a million rows. Matches the SearchRequest pattern.
    if limit is not None:
        if limit < 1:
            raise HTTPException(400, "limit must be >= 1")
        if limit > 10000:
            limit = 10000
    store = get_store(app)

    def _gather():
        files = store.list_files(status=status, limit=limit)
        # Batch-fetch first-keyframe-per-file in one query (same window-fn
        # pattern as search_filename). N+1-style per-file queries
        # would balloon /api/files latency on big workspaces.
        thumbnail_by_file: dict[int, str] = {}
        if files:
            ids = [f.id for f in files]
            placeholders = ",".join("?" * len(ids))
            kf_rows = store.conn.execute(
                f"""
                SELECT file_id, thumbnail_path FROM (
                  SELECT file_id, thumbnail_path,
                         ROW_NUMBER() OVER (PARTITION BY file_id ORDER BY ts_ms) AS rn
                  FROM keyframes
                  WHERE file_id IN ({placeholders})
                ) WHERE rn = 1
                """,
                ids,
            ).fetchall()
            for r in kf_rows:
                thumbnail_by_file[r["file_id"]] = r["thumbnail_path"]
        return files, thumbnail_by_file

    files, thumbnail_by_file = await asyncio.to_thread(_gather)
    return {
        "count": len(files),
        "files": [
            {
                "id": f.id,
                "path": f.path,
                "name": Path(f.path).name,
                "mime": f.mime,
                "duration_ms": f.duration_ms,
                "duration_str": format_timecode(f.duration_ms),
                "size_bytes": f.size_bytes,
                "status": f.status,
                "thumbnail_path": thumbnail_by_file.get(f.id),
            }
            for f in files
        ],
    }


class TranscriptLine(BaseModel):
    ts_ms: int
    text: str


class TranscriptWindowResponse(BaseModel):
    file_id: int
    matched_index: int
    lines: list[TranscriptLine]


@app.get("/api/file/thumbnails")
async def file_thumbnails(file_id: int = Query(..., ge=1)):
    """Return pre-extracted keyframe thumbnails for a file.

    Powers the player's filmstrip timeline (CapCut-style). The Indexer's
    KeyframeExtractor already saved 12-300 keyframes per video to
    workspace/db/thumbnails/file_<id>/ during indexing — including
    scene-change-aware sampling for sparse content.
    Surfacing them via this endpoint replaces the client-side seek-and-
    canvas extraction pattern that the trim UI used before, which:
      - took ~600-1500 ms (each seek is async + WebKit serialises them)
      - sometimes hung indefinitely on Safari for malformed files
      - produced one fewer cache hit since every player-open re-extracted

    Returns sorted by ts_ms so the client can render the filmstrip in
    timeline order without further sorting. Each item's `url` is a
    /api/file?path=... URL (URL-encoded server-side) so it round-trips
    through the existing _is_allowed_serve_path allowlist gate.

    Performance: the existence check + Path.resolve runs up to 300 stat
    syscalls per call. Warm cache ~1 ms; cold cache (just-launched app,
    network-mounted workspace, large indexed corpus) can hit 50-200 ms.
    Auto-preview steps through video hits one at a time on the ↓ key, so
    even with the frontend's per-file_id cache (player.js _vtrimThumbsCache),
    the FIRST hit on each video pays the cost. Offload the stat loop to
    the thread pool so /api/health, /api/index/status, and any in-flight
    /api/search the user fires while scrolling stays responsive. Same
    pattern as /api/search and /api/diagnostics.
    """
    import asyncio
    import urllib.parse as _qp
    store = get_store(app)
    # Validate the file exists before we trust the file_id (so an
    # enumerator can't probe the keyframes table for arbitrary IDs and
    # learn workspace layout from the response shape).
    cur = store.conn.execute("SELECT id FROM files WHERE id = ?", (file_id,)).fetchone()
    if cur is None:
        raise HTTPException(404, f"file {file_id} not found")
    rows = store.conn.execute(
        "SELECT ts_ms, thumbnail_path FROM keyframes WHERE file_id = ? ORDER BY ts_ms ASC",
        (file_id,),
    ).fetchall()
    # Snapshot the row data so the worker thread doesn't reach back into
    # the sqlite Row objects (which may not be safe across threads in
    # all pythons). sqlite3.Row supports indexed access and is cheap to
    # snapshot — convert to plain tuples here.
    raw = [(r["ts_ms"], r["thumbnail_path"]) for r in rows]

    def _build():
        items: list[dict] = []
        for ts_ms, p in raw:
            if not p:
                continue
            try:
                abs_p = Path(p).resolve()
            except Exception:
                continue
            # Skip thumbs deleted off disk since indexing — <img> would
            # 404 + flicker. Frontend treats absent items as "no tile."
            if not abs_p.exists():
                continue
            items.append({
                "ts_ms": ts_ms,
                "url": "/api/file?path=" + _qp.quote(str(abs_p), safe=""),
            })
        return items

    out = await asyncio.to_thread(_build)
    return {"file_id": file_id, "count": len(out), "thumbnails": out}


@app.get("/api/transcript/window", response_model=TranscriptWindowResponse)
async def transcript_window(
    file_id: int = Query(..., ge=1),
    ts_ms: int = Query(..., ge=0),
    # ±N transcript segments around ts_ms. ge=0 because 0 = "just the
    # matched line, no surrounding context" is a legitimate use case.
    # le=30 because a 60-min podcast has ~600 segments; 30-line cap
    # gives a 61-line max-window — sane for a single render. Pydantic-
    # side enforcement so a request with radius=100 returns a clean
    # 422 instead of silent clamp to 30 (same migration as the change that
    # added ge=1,le=30 to SRTExportRequest.radius — see that model's
    # docstring for the threat-model write-up).
    radius: int = Query(default=3, ge=0, le=30),
):
    """Return the transcript segments around a given timestamp.

    Used by the redesigned detail pane to show the matched line + N lines of
    surrounding context. `radius` is bounded to [0, 30] at the Pydantic
    boundary (was a defensive endpoint-side clamp before; moved to
    the Query validator so out-of-range requests get a clean 422 instead
    of silent clamping).

    Cap was previously 10 — but the detail.js "Show more context ↓" button
    asks for 12 to give the user ~25 lines on click. The 10-cap silently
    clamped down to 21, making the expand button feel weak. 30 leaves
    headroom while still well below "dump everything".

    Performance: was loading ALL transcript_segments rows for the file
    (a 6-hour audiobook = 3600 rows) just to find the row closest to
    `ts_ms` by Python `abs()` and slice a 7-row window. Now two cheap
    indexed queries: (1) `SELECT COUNT(*) WHERE start_ms < (closest
    segment's start_ms)` = matched_idx, (2) `LIMIT 2*radius+1 OFFSET
    matched_idx-radius` for the window. 3600 rows × 24-byte text columns
    + 3600 Python Row materializations drops to ~7 rows materialized.
    Bench on the demo audiobook: 28 ms → 1.4 ms (20× speedup). Wrap in
    `asyncio.to_thread` so the still-sync sqlite calls don't block the
    event loop during the indexing-status poll storm. Same pattern as
    /api/search, /api/diagnostics and /api/files.
    """
    import asyncio
    # Defensive clamp removed — Query(ge=0, le=30) above enforces it now.
    store = get_store(app)
    cur = store.conn.execute(
        "SELECT id FROM files WHERE id = ?", (file_id,)
    ).fetchone()
    if cur is None:
        raise HTTPException(404, f"file {file_id} not found")

    def _gather():
        # Find the closest-by-timestamp segment's start_ms AND count how
        # many segments come before it (= matched_idx in the file's
        # time-sorted order) in one query via a correlated subquery.
        # The inner SELECT runs once per file_id partition; both
        # statements use the (file_id, start_ms) composite index.
        idx_row = store.conn.execute(
            """
            SELECT COUNT(*) AS idx
            FROM transcript_segments
            WHERE file_id = ?
              AND start_ms < (
                SELECT start_ms FROM transcript_segments
                WHERE file_id = ? ORDER BY abs(start_ms - ?) LIMIT 1
              )
            """,
            (file_id, file_id, ts_ms),
        ).fetchone()
        # SELECT COUNT(*) ALWAYS returns exactly one row in SQLite — even
        # when zero source rows match the WHERE clause, COUNT returns 0
        # (the row is {idx: 0}, not None). The genuinely-empty case (no
        # segments for this file_id at all, e.g. image-only file or
        # indexing-in-progress) is handled by the LIMIT query below
        # returning [] rows, which the outer caller treats as empty.
        # The previous `if idx_row is None: return [], 0` defensive
        # branch was dead — a maintainer reading it would assume that
        # branch fires under some condition. It doesn't. Removed.
        matched_idx = idx_row["idx"]
        offset = max(0, matched_idx - radius)
        # LIMIT 2*radius+1 covers the symmetric window; SQLite naturally
        # returns fewer rows if we hit the end of the file (same behavior
        # as the old in-Python slice).
        rows = store.conn.execute(
            """
            SELECT start_ms, text
            FROM transcript_segments
            WHERE file_id = ?
            ORDER BY start_ms ASC
            LIMIT ? OFFSET ?
            """,
            (file_id, 2 * radius + 1, offset),
        ).fetchall()
        # matched_index is relative to the returned window, not the
        # full file — frontend uses it to know which row to highlight.
        return rows, matched_idx - offset

    rows, matched_in_window = await asyncio.to_thread(_gather)
    if not rows:
        return TranscriptWindowResponse(
            file_id=file_id, matched_index=0, lines=[]
        )
    return TranscriptWindowResponse(
        file_id=file_id,
        matched_index=matched_in_window,
        lines=[
            TranscriptLine(ts_ms=r["start_ms"], text=r["text"])
            for r in rows
        ],
    )


# Shared blacklist of "broad" filesystem paths that we never let a client
# operate on. Used by /api/folders/remove (don't wipe arbitrary index data)
# AND /api/index (don't silently ingest /Users/<victim> into the searchable
# DB when a malicious local process posts to the loopback API). Min-depth
# enforcement happens at the call site.
_DANGEROUS_FOLDER_PREFIXES = {
    "/", "/Applications", "/System", "/Library", "/private",
    "/etc", "/var", "/tmp", "/bin", "/sbin", "/usr",
    "/Users", "/Volumes",
    # macOS symlink-resolved equivalents — /etc → /private/etc, etc.
    "/private/etc", "/private/var", "/private/tmp",
}


def _check_folder_is_safe(folder_path: Path, literal_input: str, min_depth: int = 2) -> tuple[bool, str]:
    """Returns (allowed, reason). Centralises the "is this folder
    overly-broad?" decision used by /api/index + /api/folders/remove.

    - Refuses if the literal input OR the symlink-resolved form is in
      the danger blacklist (defends against /etc → /private/etc tricks).
    - Refuses the user's HOME root explicitly (computed at call time
      since Path.home() depends on env).
    - Refuses anything fewer than `min_depth` path components below /.
    """
    literal = literal_input.rstrip("/") or "/"
    resolved = str(folder_path)
    parts = [p for p in folder_path.parts if p and p != "/"]
    blacklist = _DANGEROUS_FOLDER_PREFIXES | {str(Path.home())}
    if literal in blacklist or resolved in blacklist:
        return False, f"refused blacklisted prefix: {folder_path}"
    if len(parts) < min_depth:
        return False, f"refused shallow path ({len(parts)} parts, need ≥{min_depth}): {folder_path}"
    return True, ""


def _is_allowed_serve_path(file_path: Path) -> bool:
    """Path-traversal allowlist for /api/file.

    A local attacker (other browser tab on localhost, another process curl'ing
    127.0.0.1:18765, etc.) could otherwise pull /etc/passwd, ~/.ssh/id_rsa,
    anything. Allow only:
      1. Files under the writable workspace (db/, thumbnails/, exports/).
      2. Files explicitly added to the index — i.e., paths that match a row
         in the `files` table. (User-chosen folders during indexing.)
    Symlink games are blocked by resolve() upstream.
    """
    p = file_path.resolve()
    ws = WORKSPACE_PATH.resolve()
    # Anything inside the workspace dir (db/, thumbnails/, exports/, etc.)
    try:
        p.relative_to(ws)
        return True
    except ValueError:
        pass
    # DB-indexed file
    try:
        store = get_store(app)
        cur = store.conn.execute(
            "SELECT 1 FROM files WHERE path = ? LIMIT 1", (str(p),)
        ).fetchone()
        if cur:
            return True
    except Exception:
        pass
    return False


@app.get("/api/file")
async def serve_file(path: str = Query(..., min_length=1, max_length=4096)):
    """Serve a media or thumbnail file from disk (for the HTML video/audio player).

    Security: allowlisted via `_is_allowed_serve_path` so a local process
    cannot use this endpoint as a generic file-read primitive. Query
    param is length-capped at 4096 (POSIX PATH_MAX-ish; macOS is 1024,
    Linux 4096) so a hostile local GET with `?path=A*10_000_000` 422s
    at the Pydantic boundary instead of burning CPU on
    `Path(...).resolve()` + the allowlist string-op + the DB lookup.
    Mirrors the PathRequest cap used by /api/reveal /
    /api/open / /api/quicklook.

    Caching: thumbnails under workspace/db/thumbnails/ are effectively
    immutable per (file_id, ts_ms) — they only change when the user
    explicitly re-indexes the source file. Add Cache-Control: max-age
    so the browser skips the round-trip on the empty-state grid + any
    sidebar redraw. Source media files (audio/video) get a SHORTER
    max-age since the user could edit them externally in disk.
    """
    file_path = Path(path).resolve()
    # Allowlist BEFORE exists() — otherwise the response distinguishes
    # `/path/that/exists/but/is/blocked` (403) from `/path/that/doesn't/
    # exist` (404), which lets a local probe enumerate the filesystem
    # outside the allowlist by reading status codes. All four sibling
    # endpoints (/api/export/clip, /api/reveal, /api/open, /api/quicklook)
    # already check in this order; /api/file was the odd one out.
    if not _is_allowed_serve_path(file_path):
        log_event(
            "serve_file_denied",
            f"refused path outside allowlist: {file_path}",
            level="WARN",
            path=str(file_path),
        )
        raise HTTPException(status_code=403, detail="Path not in allowlist")
    if not file_path.exists():
        raise HTTPException(status_code=404, detail=f"File not found: {file_path}")
    # Guess mime
    mime_type, _ = mimetypes.guess_type(str(file_path))
    if mime_type is None:
        mime_type = "application/octet-stream"

    # Cache policy based on path. Thumbnails are derivatives of indexed
    # files; they only change on re-index, and re-index produces a NEW
    # path (file_<new_id>/...) so cache invalidation isn't an issue.
    # Source media gets a shorter cache since the user might edit-in-place.
    is_thumbnail = "db/thumbnails/" in str(file_path)
    if is_thumbnail:
        # 1-day cache. private because workspace is per-user; immutable
        # because we never rewrite a thumbnail at the same path.
        cache_header = "private, max-age=86400, immutable"
    else:
        # Source media — 1-hour cache, must-revalidate so an external
        # edit becomes visible on next reload.
        cache_header = "private, max-age=3600, must-revalidate"
    return FileResponse(file_path, media_type=mime_type, headers={"Cache-Control": cache_header})


@app.post("/api/export/clip")
async def export_clip_endpoint(req: ExportRequest):
    """Extract a clip via ffmpeg. Returns the output path + a download URL.

    Security: allowlisted — see the /api/file allowlist gate. Without
    this check, a local attacker could `curl /api/export/clip` with an
    arbitrary file_path, ffmpeg would extract bytes (audio/video stream)
    from that file into workspace/exports/, and then download the bytes
    via /api/file (which legitimately serves workspace contents). Effective
    cross-process file exfiltration. The /api/file gate alone doesn't
    block this — exports are by-design inside the workspace allowlist.
    """
    src = Path(req.file_path).resolve()
    if not _is_allowed_serve_path(src):
        log_event("export_clip_denied", f"refused source outside allowlist: {src}",
                  level="WARN", path=str(src))
        raise HTTPException(403, f"Source path not allowed: {src}")
    if not src.exists():
        raise HTTPException(404, f"Source not found: {src}")

    out_dir = WORKSPACE_PATH / "exports"
    out_dir.mkdir(exist_ok=True)
    ext = ".mp3" if req.audio_only else ".mp4"
    base = f"{src.stem}_{req.start_ms}_{req.end_ms}{ext}"
    out_path = out_dir / base

    # extract_clip / extract_audio_clip both shell out to ffmpeg via
    # subprocess.run(check=True) — sync, blocks for 5-60s depending on
    # clip duration + re-encode load. Run in the thread pool so the
    # event loop stays responsive to /api/health, /api/index/status,
    # and the in-flight /api/search the user might fire while the
    # export crunches. Same pattern as the /api/search engine.search wrap.
    import asyncio
    # Use the ffmpeg the app ships with, not whatever PATH turns up. The
    # bundled one is the LGPL build; PATH on a developer Mac finds the
    # Homebrew GPL build instead, and on a customer Mac without Homebrew
    # finds nothing at all — the export then died on ENOENT despite the
    # binary sitting inside the .app. get_engine() already resolved this
    # path from TERN_BIN_DIR at startup.
    _, _, _cfg = get_engine(app)
    _ffmpeg = getattr(_cfg, "ffmpeg_binary", "ffmpeg")
    try:
        if req.audio_only:
            result = await asyncio.to_thread(
                extract_audio_clip, src, out_path, req.start_ms, req.end_ms,
                req.padding_ms, _ffmpeg
            )
        else:
            result = await asyncio.to_thread(
                extract_clip, src, out_path, req.start_ms, req.end_ms,
                req.padding_ms, True, _ffmpeg
            )
    except subprocess.CalledProcessError as e:
        raise HTTPException(500, f"ffmpeg failed: {e.stderr.decode() if e.stderr else e}")
    except subprocess.TimeoutExpired as e:
        # clip.py puts 120s / 300s ceilings on ffmpeg.
        # Without this catch, a hung ffmpeg (corrupted source, stalled SMB
        # mount) would propagate the raw Python TimeoutExpired up through
        # the asyncio thread pool as an unhandled 500 — the user's
        # "Crop & Save" toast sees a generic error and they can't tell
        # the difference between "ffmpeg crashed" and "ffmpeg hung". This
        # catch surfaces a precise message so the troubleshooting doc's
        # "Export timed out — source may be corrupted" section is actually
        # findable from the toast text.
        log_event("export_clip_timeout",
                  f"ffmpeg exceeded {e.timeout}s ceiling on {src.name}",
                  level="WARN",
                  source=str(src), timeout_s=e.timeout, audio_only=req.audio_only)
        raise HTTPException(504, f"Export timed out after {int(e.timeout)}s — source may be corrupted or on a stalled mount")

    abs_result = Path(result).resolve()
    # Log success for support-diagnostics parity with /api/export/srt
    # (which already logs every success). Previously, the export
    # endpoints were inconsistent: SRT logged success, clip/csv/fcpxml
    # logged only failure cases. A support thread asking "when did this
    # user export the deleted-clip-that's-now-on-disk?" couldn't be
    # answered from the log alone for the most common export path.
    log_event("export_clip", f"{abs_result.name} ({abs_result.stat().st_size} bytes)",
              source=str(src), audio_only=req.audio_only,
              duration_ms=req.end_ms - req.start_ms)
    return {
        "ok": True,
        "path": str(abs_result),
        "filename": abs_result.name,
        "size_bytes": abs_result.stat().st_size,
        "download_url": f"/api/file?path={abs_result}",
        "reveal_command": f"open -R '{abs_result}'",
    }


class CSVExportRequest(BaseModel):
    # Same 200 cap as FCPXMLRequest — see that model's comment for the
    # rationale. CSV is more forgiving than FCPXML (no XML emitter to
    # exhaust), but uncapped `hits` still lets a hostile local POST
    # exhaust memory before _csv_safe() loops over the rows.
    hits: list[dict] = Field(..., max_length=200)
    # Same 200-char project_name cap as FCPXMLRequest — see that model.
    project_name: str = Field(default="Tern Search Results", max_length=200)


# CSV-formula-injection guard. When a cell value starts with `=`, `+`, `-`,
# `@`, tab (0x09), or CR (0x0D), Excel / Numbers / Google Sheets interpret
# it as a formula on open — so an indexed file named
# `=HYPERLINK("https://atk/?"&A1,"click")` or a transcript line starting
# with `=cmd|'/c calc'!A0` becomes executable on someone else's machine
# the moment they double-click the CSV we generated. Standard OWASP-listed
# fix: prefix any such cell with a single quote so the spreadsheet treats
# it as literal text. Apply to every stringy cell — filename, snippet,
# file_path are the obvious attacker-controlled sinks but the same rule
# is cheap to apply universally.
_CSV_INJECTION_LEAD = ("=", "+", "-", "@", "\t", "\r")
def _csv_safe(v):
    s = "" if v is None else str(v)
    if not s:
        return s
    # Two checks because the lead set MIXES visible chars (`=`, `+`, `-`,
    # `@`) with invisible chars (TAB, CR) that are themselves injection
    # leads per OWASP. Naive lstrip would silently bypass the invisible-
    # char cases (a lone TAB-leading filename would become "injected" with
    # the TAB stripped — NO prefix — but the on-disk CSV still has the
    # raw TAB which Excel formula-evaluates).
    #   1. Literal first char in lead set → unambiguous formula attack
    #      (catches `=...`, `\t...`, `\r...`, etc.)
    #   2. First non-space char in lead set → caller may have padded
    #      with leading spaces (transcript line starting with " =", a
    #      file name `" =SUM(A1)"`); Excel / Numbers / Sheets strip
    #      leading SPACES before evaluating the formula parser, so
    #      space-padded `=` is just as dangerous as a literal `=`
    if s[0] in _CSV_INJECTION_LEAD:
        return "'" + s
    spaced = s.lstrip(" ")
    if spaced and spaced[0] in _CSV_INJECTION_LEAD:
        return "'" + s
    return s


@app.post("/api/export/csv")
async def export_csv_endpoint(req: CSVExportRequest):
    """Dump the current result list as CSV for spreadsheets or
    other tools. Columns: file, timecode, ts_ms, source, score, snippet."""
    import csv
    import io
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["file_name", "timecode", "ts_ms", "source", "media_kind", "score", "snippet", "file_path"])
    for h in req.hits:
        snippet = (h.get("snippet") or "").replace("<mark>", "").replace("</mark>", "").replace("\n", " ")
        w.writerow([
            _csv_safe(h.get("file_name", "")),
            _csv_safe(h.get("timecode", "")),
            _csv_safe(h.get("ts_ms", "")),
            _csv_safe(h.get("source", "")),
            _csv_safe(h.get("media_kind", "")),
            _csv_safe(h.get("score", "")),
            _csv_safe(snippet),
            _csv_safe(h.get("file_path", "")),
        ])
    out_dir = WORKSPACE_PATH / "exports"
    out_dir.mkdir(exist_ok=True)
    safe = "".join(c if c.isalnum() else "_" for c in req.project_name)[:40]
    out_path = out_dir / f"{safe}_{int(time.time())}.csv"
    _atomic_write_text(out_path, buf.getvalue())
    abs_path = out_path.resolve()
    # Log success — diagnostic-parity with /api/export/srt and the
    # /api/export/clip log added in the same change.
    log_event("export_csv", f"{abs_path.name} ({len(req.hits)} hits)",
              hits=len(req.hits))
    return {
        "ok": True,
        "path": str(abs_path),
        "filename": abs_path.name,
        "size_bytes": abs_path.stat().st_size,
        "download_url": f"/api/file?path={abs_path}",
        "reveal_command": f"open -R '{abs_path}'",
    }


@app.post("/api/export/fcpxml")
async def export_fcpxml_endpoint(req: FCPXMLRequest):
    """Generate FCPXML from search hits."""
    # Convert dict hits back to SearchHit-like objects
    from tern.models import SearchHit
    hits = [SearchHit(**{k: v for k, v in h.items() if k in SearchHit.model_fields}) for h in req.hits]

    out_dir = WORKSPACE_PATH / "exports"
    out_dir.mkdir(exist_ok=True)
    safe_name = "".join(c if c.isalnum() else "_" for c in req.project_name)[:40]
    out_path = out_dir / f"{safe_name}_{int(time.time())}.fcpxml"

    # export_fcpxml shells out to ffprobe per unique source file
    # (_probe_video_format AND probe_media) to discover fps / width /
    # height / duration. For a 50-hit FCPXML spanning 10 source videos
    # that's ~20 sync ffprobe calls. Wrap in to_thread so the event
    # loop stays responsive — same rationale as the extract_clip
    # wrap above and the /api/search engine.search wrap.
    #
    # probe_media propagates subprocess.TimeoutExpired if any source
    # file is on a stalled mount (with a 30s
    # ceiling). _probe_video_format's own try/except swallows it and
    # returns the safe (1920, 1080, 30) fallback, so it doesn't reach
    # us here — but probe_media doesn't have that fallback because
    # the duration field is load-bearing for FCPXML (a 0-duration
    # asset breaks the timeline math on import to FCP / DaVinci).
    # Catch TimeoutExpired explicitly and return 504 so the toast
    # message points the user at the actual cause instead of dumping
    # a raw Python traceback. Same pattern + reasoning as the
    # /api/export/clip TimeoutExpired translation.
    import asyncio
    try:
        result = await asyncio.to_thread(
            export_fcpxml, hits, req.project_name, out_path
        )
    except subprocess.TimeoutExpired as e:
        log_event("fcpxml_export_timeout",
                  f"ffprobe exceeded {e.timeout}s ceiling during FCPXML export",
                  level="WARN",
                  timeout_s=e.timeout, hit_count=len(hits))
        raise HTTPException(504, f"FCPXML export timed out after {int(e.timeout)}s probing a source — one of the asset files may be on a stalled mount or corrupted")
    except Exception as e:
        raise HTTPException(500, f"FCPXML export failed: {e}")

    abs_result = Path(result).resolve()
    # Log success — diagnostic-parity with /api/export/srt and the
    # /api/export/clip + /api/export/csv logs added in the same change.
    log_event("export_fcpxml", f"{abs_result.name} ({len(hits)} hits)",
              hits=len(hits))
    return {
        "ok": True,
        "path": str(abs_result),
        "filename": abs_result.name,
        "size_bytes": abs_result.stat().st_size,
        "download_url": f"/api/file?path={abs_result}",
        "reveal_command": f"open -R '{abs_result}'",
    }


class SRTExportRequest(BaseModel):
    """Input gating at the API boundary. Previously the int fields
    had ZERO bounds — a hostile POST could send file_id=-1 (silent
    no-match → 404 path), ts_ms=-1 (negative timecode breaks the
    base_ms math), or radius=100000 (the endpoint then clamped silently
    to 30, leaving the caller wondering why the parameter was ignored).

    Pydantic-side bounds give a clean 422 with a specific message
    instead of silent clamping or 404, matching the
    Query(..., ge=1) treatment of /api/file/thumbnails + /api/transcript/
    window file_id params. The endpoint's own defensive `radius = max(1,
    min(req.radius, 30))` clamp is not needed: it
    would be dead code once Pydantic guarantees the input is in range,
    and leaving it out lets the user see a clean validation error for
    radius=0 instead of silent promotion to 1."""
    # file_ids are positive auto-increment SQLite IDs. ge=1 mirrors
    # /api/transcript/window's Query(ge=1) and the
    # /api/file/thumbnails param. Without it, file_id=-1 silently
    # routed through the COUNT-rows-with-id=-1 query (returns 0) and
    # the endpoint raised 404 — but the 422 path is a faster, clearer
    # rejection that doesn't even touch the DB.
    file_id: int = Field(..., ge=1)
    # Transcript timestamps are never negative. ge=0 catches negative
    # poll values that would otherwise feed into base_ms = max(0,
    # window[0]["start_ms"] - 1500) — currently the max() papers over
    # negative inputs but the rejection is cleaner.
    ts_ms: int = Field(..., ge=0)
    # ±N transcript segments around ts_ms (defaults to ~30s). The
    # endpoint previously clamped to [1, 30]; Pydantic now enforces
    # the same bounds so the user sees a clean 422 for radius=0 or
    # radius=100 instead of silent promotion / demotion. Upper bound
    # of 30 picked when it was raised from 10 — a 60-min podcast
    # has ~600 segments, 30-line cap gives a 61-line max-window
    # which is sane for a single SRT file.
    radius: int = Field(default=8, ge=1, le=30)
    # 200-char project_name cap matches FCPXML + CSV export requests —
    # see FCPXMLRequest for the threat-model rationale (used in output
    # filename + SRT body; uncapped lets a hostile local POST ship MB
    # of string for Pydantic to deserialize then for us to emit).
    project_name: str = Field(default="Tern Subtitle", max_length=200)


def _fmt_srt_time(ms: int) -> str:
    """SRT timestamp format: HH:MM:SS,mmm"""
    s, ms_part = divmod(int(ms), 1000)
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    return f"{h:02d}:{m:02d}:{s:02d},{ms_part:03d}"


@app.post("/api/export/srt")
async def export_srt_endpoint(req: SRTExportRequest):
    """Generate an .srt subtitle file for a window around a single hit.

    Useful when the user finds a moment they want to reuse with subtitles
    burned in (e.g. for a clip cut later in DaVinci / Premiere). Output
    timestamps are RELATIVE to the matched line so the .srt drops onto the
    exported clip directly (export_clip starts ~1.5 s before the match).
    """
    import asyncio
    # Defensive radius clamp removed in the same change as SRTExportRequest
    # got Field(ge=1, le=30) — Pydantic now enforces the bounds at the
    # API boundary so the clamp is dead code. A request with radius=0
    # or radius=100 now returns a clean 422 instead of being silently
    # promoted/demoted.
    radius = req.radius
    store = get_store(app)
    # Validate file exists
    cur = store.conn.execute("SELECT path FROM files WHERE id = ?", (req.file_id,)).fetchone()
    if cur is None:
        raise HTTPException(404, f"file {req.file_id} not found")
    file_path = cur["path"]

    # Pull only the window we need. Was loading ALL transcript_segments
    # for the file (a 6-hour audiobook = 3600 rows) just to find the
    # closest-by-ts_ms row via Python `abs()` and slice a small window
    # (the pattern /api/transcript/window avoids).
    # Two indexed queries: (1) COUNT rows before the closest segment's
    # start_ms (= matched_idx), (2) LIMIT 2*radius+1 OFFSET matched_idx-radius
    # for the window. Bench on demo audiobook: 28 ms → 1.4 ms.
    def _gather():
        idx_row = store.conn.execute(
            """
            SELECT COUNT(*) AS idx
            FROM transcript_segments
            WHERE file_id = ?
              AND start_ms < (
                SELECT start_ms FROM transcript_segments
                WHERE file_id = ? ORDER BY abs(start_ms - ?) LIMIT 1
              )
            """,
            (req.file_id, req.file_id, req.ts_ms),
        ).fetchone()
        # COUNT(*) always returns one row (idx=0 when no matches).
        # Empty-window case is handled by the LIMIT query below returning
        # zero rows, which the endpoint's outer `if not window` check
        # translates to a clean 404. Removed the dead defensive `if
        # idx_row is None` branch — see /api/transcript/window for the
        # full rationale.
        best_idx = idx_row["idx"]
        offset = max(0, best_idx - radius)
        # Window query returns end_ms too (needed for SRT cue duration —
        # this is the bit the transcript_window endpoint doesn't need).
        rows = store.conn.execute(
            """
            SELECT start_ms, end_ms, text
            FROM transcript_segments
            WHERE file_id = ?
            ORDER BY start_ms ASC
            LIMIT ? OFFSET ?
            """,
            (req.file_id, 2 * radius + 1, offset),
        ).fetchall()
        return rows, best_idx - offset

    window, _matched_in_window = await asyncio.to_thread(_gather)
    if not window:
        raise HTTPException(404, f"no transcript segments for file {req.file_id}")

    # Shift timestamps so the window starts at 00:00:00,000 — drops onto the
    # exported clip directly (clip starts 1.5 s before matched line; SRT offset
    # accounts for that padding too).
    base_ms = max(0, window[0]["start_ms"] - 1500)  # clip padding from export_clip

    cues = []
    for i, r in enumerate(window, start=1):
        start_rel = max(0, r["start_ms"] - base_ms)
        end_rel = max(start_rel + 200, r["end_ms"] - base_ms)
        # Strip any <mark> wrappers from the snippet — SRT players don't speak HTML.
        text = r["text"].replace("<mark>", "").replace("</mark>", "").strip()
        cues.append(f"{i}\n{_fmt_srt_time(start_rel)} --> {_fmt_srt_time(end_rel)}\n{text}\n")
    srt_body = "\n".join(cues)

    out_dir = WORKSPACE_PATH / "exports"
    out_dir.mkdir(exist_ok=True)
    safe = "".join(c if c.isalnum() else "_" for c in req.project_name)[:40]
    src_stem = Path(file_path).stem
    out_path = out_dir / f"{src_stem}_{req.ts_ms}_{safe}.srt"
    _atomic_write_text(out_path, srt_body, encoding="utf-8")
    abs_path = out_path.resolve()
    log_event("export_srt", f"{out_path.name} ({len(cues)} cues)", file_id=req.file_id, cues=len(cues))
    return {
        "ok": True,
        "path": str(abs_path),
        "filename": abs_path.name,
        "size_bytes": abs_path.stat().st_size,
        "cues": len(cues),
        "download_url": f"/api/file?path={abs_path}",
        "reveal_command": f"open -R '{abs_path}'",
    }


@app.post("/api/index")
async def start_indexing(req: IndexRequest, background: BackgroundTasks):
    """Start indexing a folder in background. Returns immediately.

    Security: gated by `_check_folder_is_safe` so a local attacker can't
    `curl -d '{"folder":"/Users/victim"}'` and silently pull arbitrary
    file paths into Tern's searchable DB. Same blacklist + min-depth
    check as /api/folders/remove.
    """
    folder = Path(req.folder).resolve()
    if not folder.exists():
        raise HTTPException(404, f"Folder not found: {folder}")

    ok, reason = _check_folder_is_safe(folder, req.folder)
    if not ok:
        log_event("index_refused", reason, level="WARN", folder=str(folder))
        raise HTTPException(
            400,
            "Folder path is too broad. Specify at least two levels deep "
            "(e.g. /Users/you/Documents/Podcasts) — refusing to index "
            "an entire system mount or user home root."
        )

    if app.state.indexing["running"]:
        raise HTTPException(409, "Indexing already in progress")

    # Trial gate. Checked before the binary check and before any walking so
    # a spent trial fails instantly with an actionable message rather than
    # starting a run that dies on the first file. Licensed copies skip it.
    # 402 rather than 403: this is "pay for it", not "you may never".
    _quota_ok, _quota_reason = licensing.check_quota(
        _load_trial(), licensed=_is_licensed(), duration_ms=1
    )
    if not _quota_ok:
        log_event("index_refused_trial", _quota_reason, level="WARN",
                  folder=str(folder))
        raise HTTPException(402, _quota_reason)

    # Refuse to start indexing if any required binary is missing — saves the
    # user from a mid-pipeline ENOENT crash 30 seconds later. Mirrors the
    # frontend runtime-check modal but enforces server-side too (so curl or
    # third-party clients hitting the API also fail gracefully).
    # Honors TERN_BIN_DIR (bundled binaries inside the .app) before
    # falling back to PATH lookup.
    import shutil as _shutil
    bundled = os.environ.get("TERN_BIN_DIR", "")
    def _binary_present(name: str) -> bool:
        if bundled and (Path(bundled) / name).exists():
            return True
        return bool(_shutil.which(name))
    missing = []
    if not _binary_present("ffmpeg"):       missing.append("ffmpeg")
    if not _binary_present("whisper-cli"):  missing.append("whisper-cli")
    if not _binary_present("vision-ocr") and not (PIPELINE_DIR / "bin" / "vision-ocr").exists():
        missing.append("vision-ocr")
    if missing:
        # Log at WARN so this lands in tern-debug.log — without it, a user
        # reporting "Tern won't index my folder" leaves the support thread
        # debugging blind (the only signal the operator had was the user's
        # screenshot of the modal). Now the log line names the exact missing
        # binary AND the folder the user was trying to index — enough to
        # diagnose "you installed Tern but skipped Homebrew" in one
        # round-trip.
        log_event(
            "index_refused_missing_binaries",
            f"refused index: missing {', '.join(missing)}",
            level="WARN",
            folder=str(folder),
            missing=missing,
        )
        raise HTTPException(
            424,  # Failed Dependency — semantically right
            f"Missing required binaries: {', '.join(missing)}. "
            f"Install with `brew install ffmpeg whisper-cpp` (and check "
            f"GET /api/runtime-check for the full list)."
        )

    engine, store, config = get_engine(app)

    # Discover files first to set total count.
    #
    # Offload to the default thread pool. discover_files() walks the
    # supplied directory with os.walk + stat — sync work that can run
    # for many seconds on a deep tree (10k+ files on a slow SSD, even
    # longer on a network-mounted volume, longer still if the user
    # picked a TCC-protected ancestor whose subdirs each cost a
    # PermissionError round-trip). Calling it directly from this
    # `async def` blocks the event loop for that duration — every
    # concurrent /api/health, /api/stats, /api/index/status poll
    # waits, and the indexing-status toast visibly stalls before
    # showing "running" because the prep call hasn't returned yet.
    # Same pattern as /api/search, /api/files,
    # /api/file/thumbnails and /api/diagnostics.
    import asyncio
    files = await asyncio.to_thread(discover_files, folder)
    if not files:
        return {"ok": False, "message": "No supported media files found in folder", "folder": str(folder)}

    app.state.indexing.update({
        "running": True,
        "paused": False,
        "cancel_requested": False,  # user-cancel flag, checked between files
        "current_file": None,
        "files_pending": len(files),
        "files_done": 0,
        "files_errored": 0,
        "files_skipped": 0,
        "start_time": time.time(),
        "log": [f"Found {len(files)} files in {folder}"],
    })

    # Pass the discovered file list to the background task so it doesn't
    # have to walk the filesystem AGAIN — that second discover_files()
    # was (a) wasted ~50 ms on a 1000-file folder (more on network
    # mounts), and (b) a real correctness bug: files dropped INTO the
    # folder between the API response and the bg task starting caused
    # the iterated count to DIVERGE from files_pending, so the toast
    # showed "100 / 100" while actually processing 150 and the progress
    # bar silently overflowed 100%. Snapshot-at-click-time is the right
    # semantic anyway — the user's intent is "index THIS folder, what's
    # in it RIGHT NOW", not "include anything that lands later mid-run".
    background.add_task(_run_indexing_task, files, req.language, req.force)
    return {
        "ok": True,
        "started": True,
        "folder": str(folder),
        "files_to_process": len(files),
    }


@app.post("/api/index/cancel")
async def cancel_indexing():
    """Request cancellation of the in-flight index pass.

    Two-layer cancel:
      - BETWEEN files: the loop checks `cancel_requested` before each
        next file → cleanest exit, current-file derived data is
        committed normally.
      - MID-file (during whisper-cli transcription specifically): the
        WhisperTranscriber polls cancel_cb at 1 Hz inside its wait
        loop and proc.kill()s on True → current file is marked errored,
        loop continues to next-file check which short-circuits and exits.

    Sub-second responsiveness instead of the "wait up to wait_timeout
    seconds (max(300, wav_seconds))" delay of the pre-8ca4d91
    between-files-only design. extract_audio (ffmpeg) + the OCR /
    embed stages aren't yet wired for mid-stage cancel; those finish
    naturally then the between-files check fires. In practice whisper
    is the long pole (8-15× realtime → minutes per file; ffmpeg PCM
    remux is seconds), so this is the right tier to short-circuit.

    UI shows "stopping…" until the in-flight stage either notices the
    cancel poll or finishes naturally — typically <1 s for transcribe,
    a few seconds for other stages.
    """
    if not app.state.indexing.get("running"):
        return {"ok": True, "message": "Nothing to cancel (no index running)"}
    # Idempotent: a second cancel POST (impatient user clicking Stop
    # again after the failed-cancel re-enable, or a curl
    # script double-tapping) MUST NOT re-append to the in-memory log
    # (toast would render two "Cancel requested" lines) NOR re-fire
    # the log_event (support threads would see two
    # index_cancel_requested entries for one cancel sequence and
    # double-count cancels in any operator dashboard). Caller still
    # gets ok:True so the UI flow is unchanged.
    if app.state.indexing.get("cancel_requested"):
        return {"ok": True, "message": "Cancel already requested; still stopping."}
    app.state.indexing["cancel_requested"] = True
    current_file = app.state.indexing.get("current_file")
    files_done = app.state.indexing.get("files_done", 0)
    files_pending = app.state.indexing.get("files_pending", 0)
    _indexing_log_append("⏸ Cancel requested — stopping current file…")
    # Log to tern-debug.log so support threads can see "user cancelled
    # at HH:MM with N/M files done" without asking for a screenshot of
    # the in-memory indexing-log shown in the toast. Mirror the
    # observability pattern from index_refused_missing_binaries.
    log_event(
        "index_cancel_requested",
        f"user cancelled with {files_done}/{files_pending} files done",
        current_file=current_file,
        files_done=files_done,
        files_pending=files_pending,
    )
    return {"ok": True, "message": "Cancel requested; stopping current file."}


def _run_indexing_task(files: list, language: Optional[str], force: bool):
    """Background task that runs indexing and updates app.state.

    `files` is the snapshot-at-click-time list from the /api/index handler
    — passing it through avoids a redundant `discover_files()` here (the
    handler already walked the tree to populate files_pending) AND keeps
    the iterated set in sync with the count the user was shown on click.
    See the comment in start_indexing() for the correctness rationale."""
    try:
        engine, store, config = get_engine(app)
        indexer = Indexer(config)

        # Wire Indexer's stage_cb to write into app.state so /api/index/status
        # surfaces sub-file pipeline progress (transcribing vs embedding vs
        # OCR). Each transition resets stage_started_at so the frontend can
        # render "Transcribing 23s" for stages exceeding ~5s. Also resets
        # stage_progress to None so a stale Whisper percent from the previous
        # file doesn't leak into the new stage.
        def _on_stage(stage_id: str, label: str, file_name: str) -> None:
            app.state.indexing["stage"] = stage_id
            app.state.indexing["stage_label"] = label
            app.state.indexing["stage_started_at"] = time.time()
            app.state.indexing["stage_progress"] = None
        indexer.stage_cb = _on_stage
        # Whisper sub-progress (parsed from --print-progress stderr in
        # audio.py). Only the transcribe stage emits these; other stages
        # leave stage_progress at None.
        def _on_stage_progress(pct: int) -> None:
            app.state.indexing["stage_progress"] = pct
        indexer.stage_progress_cb = _on_stage_progress
        # Mid-file cancel: WhisperTranscriber polls this at 1 Hz inside
        # its wait loop. Pre-this-wire, Cancel only took effect between
        # files — a user clicking Stop on a 4-hour podcast transcription
        # would have to wait the full transcription out (or the
        # wait_timeout ceiling of `max(300, dur)`
        # seconds — could still be HOURS). Reading from
        # app.state.indexing.cancel_requested means the same Cancel
        # button that already gates the between-files loop now also
        # short-circuits the current file.
        indexer.cancel_cb = lambda: bool(app.state.indexing.get("cancel_requested"))

        cancelled = False
        for file_path in files:
            # Cooperative cancellation — exit before starting the
            # next file when the user has clicked Cancel.
            if app.state.indexing.get("cancel_requested"):
                _indexing_log_append("Cancelled by user — stopped before next file.")
                cancelled = True
                break
            app.state.indexing["current_file"] = str(file_path)

            # Trial gate, per file. The upfront check in start_indexing only
            # proves the quota wasn't already spent; a forty-episode queue
            # still has to stop at the file where it runs out. Skip rather
            # than abort, so the files that DO fit still get indexed and the
            # log says exactly which ones didn't and why.
            _admitted, _why, _duration_ms = _quota_admit(file_path)
            if not _admitted:
                app.state.indexing["files_skipped"] += 1
                app.state.indexing["trial_blocked"] = True
                _indexing_log_append(f"⊘ {file_path.name}: {_why}")
                log_event("index_file_refused_trial", _why, level="WARN",
                          file_path=str(file_path))
                continue

            try:
                indexed = indexer.index_file(file_path, language=language, force=force)
                if indexed:
                    app.state.indexing["files_done"] += 1
                    # Charge only what was actually indexed. A file skipped
                    # as already-up-to-date costs the user nothing.
                    _charge_trial(_duration_ms)
                    _indexing_log_append(f"✓ {file_path.name}")
                else:
                    app.state.indexing["files_skipped"] += 1
                    _indexing_log_append(f"⊝ skipped {file_path.name} (already indexed)")
            except Exception as e:
                app.state.indexing["files_errored"] += 1
                _indexing_log_append(f"✗ {file_path.name}: {e}")
                # Previously, per-file index failures landed ONLY in
                # the in-memory toast log (app.state.indexing["log"]).
                # That log is bounded to 30 lines in /api/index/status,
                # cleared on app close, and never persisted — so a user
                # reporting "indexing skipped 5 of 100 files, what went
                # wrong?" had no diagnostic trail at all. The "Send
                # diagnostics" button (which dumps the structured
                # ~/Library/Logs/tern-debug.log) returned silence on
                # indexing problems, even though every other error path
                # in the codebase (license_clear, fcpxml_export_timeout,
                # export_clip_timeout, etc.) routes through log_event /
                # log_exception. Close the parity gap: log the exception
                # at ERROR with the file path so support diagnostics
                # dumps and grep-by-category both see indexing failures.
                log_exception("index_file_failed", e, file_path=str(file_path))
        # Only emit "Indexing complete." when the loop ran to natural
        # exhaustion. After a cancel the log would otherwise show both
        # "Cancelled by user…" and "Indexing complete." — misleading
        # ("complete" implies success). Customer impact: confusing
        # toast on the cancel path.
        if not cancelled:
            _indexing_log_append("Indexing complete.")
    except Exception as e:
        _indexing_log_append(f"FATAL: {e}")
        # FATAL covers everything OUTSIDE the per-file try/except —
        # connection-pool death, DB lock, Indexer construction failure,
        # ChromaDB collection corruption, OOM. Same diagnostic-parity
        # rationale as the per-file index_file_failed branch above: the
        # in-memory toast log gets a "FATAL: …" line, but without
        # log_exception the structured log file shows nothing. A user
        # filing "indexing never started" or "indexing died halfway" has
        # no traceback to send — support has to guess at the cause.
        log_exception("index_run_fatal", e)
    finally:
        app.state.indexing["running"] = False
        app.state.indexing["current_file"] = None
        # Defense-in-depth: clear cancel_requested too. /api/index's
        # start-of-run update() already resets this to False, but a
        # future code path that reads the flag BEFORE the next run
        # starts (a diagnostics dump, a status poll that misses the
        # window) shouldn't see a stale True.
        app.state.indexing["cancel_requested"] = False
        app.state.indexing["stage"] = None
        app.state.indexing["stage_label"] = None
        app.state.indexing["stage_started_at"] = None
        app.state.indexing["stage_progress"] = None


@app.get("/api/index/status")
async def indexing_status():
    return {
        **app.state.indexing,
        "log": app.state.indexing["log"][-30:],  # last 30 lines
        "elapsed_s": (time.time() - app.state.indexing["start_time"]) if app.state.indexing["start_time"] else 0,
    }


class RemoveFolderRequest(BaseModel):
    """Same length cap as IndexRequest.folder — see that model's
    docstring for the threat-model reasoning."""
    folder: str = Field(..., min_length=1, max_length=4096)


@app.post("/api/folders/remove")
async def remove_folder(req: RemoveFolderRequest):
    """Remove all indexed data for files under a given folder prefix.

    Important: this NEVER touches the user's source files on disk — only the
    derived index (transcripts, OCR rows, keyframes, embeddings, thumbnails).
    After removal, search will no longer return hits from this folder until
    the user re-indexes it.

    Rejects if an index pass is currently running so we don't yank data out
    from under a worker.
    """
    if app.state.indexing.get("running"):
        raise HTTPException(409, "Indexing in progress — please wait until it finishes before removing folders.")
    prefix = (req.folder or "").strip()
    if not prefix or not prefix.startswith("/"):
        raise HTTPException(400, "Folder path must be an absolute path starting with /")

    # Refuse overly-broad prefixes — a malicious local process could otherwise
    # wipe the entire index via {"folder": "/"} or {"folder": "/Users"}.
    # Shared with /api/index — see _check_folder_is_safe for the rules.
    prefix_path = Path(prefix).resolve()
    ok, reason = _check_folder_is_safe(prefix_path, prefix)
    if not ok:
        log_event("remove_folder_refused", reason, level="WARN",
                  folder=str(prefix_path))
        raise HTTPException(
            400,
            "Folder path is too broad. Specify at least two levels deep "
            "(e.g. /Users/you/Documents/Podcasts) — refusing to remove an "
            "index that could span the whole disk."
        )
    store = get_store(app)
    # remove_folder loops cleanup_file per matched file (3 SQL DELETEs
    # + 1 Chroma vector delete + 1 on-disk thumbnail rmtree per file).
    # A 200-file folder = ~600 SQL ops + 200 rmtrees touching potentially
    # thousands of JPEGs. Running synchronously on the event loop made
    # /api/health, /api/index/status, and the live indexing-status poll
    # stall for the full duration (seconds on big folders). Offload to
    # the thread pool — same pattern as /api/search, /api/diagnostics
    # and /api/file/thumbnails.
    import asyncio
    result = await asyncio.to_thread(store.remove_folder, prefix)
    log_event(
        "folder_removed",
        f"removed {result['files_removed']} file(s) under {prefix}",
        folder=prefix,
        **{k: v for k, v in result.items() if k != "ids"},  # ids list is noisy
    )
    return {"ok": True, "folder": prefix, **result}


class PathRequest(BaseModel):
    """Shared request shape for the three file-action endpoints
    (/api/reveal, /api/open, /api/quicklook). Previously each took
    `req: dict` directly and ran `Path(req.get("path",""))`. A malformed
    POST like `{"path": ["a","b"]}` would feed a list into Path() and
    raise TypeError → unhandled 500 with a stack trace in the log
    (noise) instead of the clean 422 every other validated endpoint
    returns. Pydantic now enforces `path: str` at the boundary.

    `min_length=1`: without it, `{"path": ""}` passed Pydantic and
    `Path("").resolve()` returned the server's CWD. The allowlist gate
    that follows would then either accept or deny depending on what
    directory the sidecar happened to be launched in — undefined
    behavior, and an information-leak oracle if CWD happens to fall
    under the workspace prefix. Match the IndexRequest/RemoveFolderRequest
    `min_length=1` pattern so this is a clean 422 at the boundary."""
    path: str = Field(..., min_length=1, max_length=4096)


@app.post("/api/reveal")
async def reveal_in_finder(req: PathRequest):
    """Run `open -R` to reveal a file in Finder.

    Security: allowlisted via `_is_allowed_serve_path` so a local process
    (curl on the loopback, another browser tab on localhost) can't trick
    Tern into revealing arbitrary files like /etc/passwd. Same allowlist
    rules as /api/file — workspace contents OR an indexed file's path.
    """
    path = Path(req.path).resolve()
    if not _is_allowed_serve_path(path):
        log_event("reveal_denied", f"refused path outside allowlist: {path}",
                  level="WARN", path=str(path))
        raise HTTPException(403, f"Path not allowed: {path}")
    if not path.exists():
        raise HTTPException(404, f"Not found: {path}")
    subprocess.Popen(["open", "-R", str(path)])
    return {"ok": True, "path": str(path)}


@app.post("/api/open")
async def open_file(req: PathRequest):
    """Open a file with the default app.

    Security: allowlisted — see /api/reveal docstring. The risk here is
    higher than /api/file (which only reads bytes): /api/open EXECUTES
    the file via LaunchServices. Without the gate, a local attacker could
    `curl -d '{"path":"/path/to/script.sh"}'` and Tern would run it.
    """
    path = Path(req.path).resolve()
    if not _is_allowed_serve_path(path):
        log_event("open_denied", f"refused path outside allowlist: {path}",
                  level="WARN", path=str(path))
        raise HTTPException(403, f"Path not allowed: {path}")
    if not path.exists():
        raise HTTPException(404, f"Not found: {path}")
    subprocess.Popen(["open", str(path)])
    return {"ok": True, "path": str(path)}


@app.post("/api/quicklook")
async def quicklook(req: PathRequest):
    """Open a file in macOS QuickLook (the same overlay you get with the
    space-bar in Finder). Lighter-weight than launching the file's default app —
    perfect for skimming a photo, peeking at a PDF, or hearing 10 s of an audio
    clip. `qlmanage -p` launches the QuickLook preview panel synchronously and
    returns when the user closes it; we run it detached so the API call stays
    instant.

    Security: allowlisted — see /api/reveal docstring. Quick Look is read-only
    so the blast radius is smaller than /api/open, but it can still leak
    arbitrary file contents to the screen.
    """
    path = Path(req.path).resolve()
    if not _is_allowed_serve_path(path):
        log_event("quicklook_denied", f"refused path outside allowlist: {path}",
                  level="WARN", path=str(path))
        raise HTTPException(403, f"Path not allowed: {path}")
    if not path.exists():
        raise HTTPException(404, f"Not found: {path}")
    subprocess.Popen(
        ["qlmanage", "-p", str(path)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    return {"ok": True, "path": str(path)}


# ─── Frontend (HTML/CSS/JS) — mount LAST so /api routes win ──────────────

class _CachedStaticFiles(StaticFiles):
    """StaticFiles subclass that emits Cache-Control on long-lived assets.

    FastAPI's default StaticFiles sends ETag + Last-Modified but no
    Cache-Control header. Without Cache-Control the browser falls back
    to a heuristic — typically "10% of (now - Last-Modified)" — which
    is effectively zero for files shipped today. Every WKWebView /
    browser launch does a conditional GET (If-Modified-Since) for
    every CSS / JS / woff2 the page references, gets 304 back, fine
    but pure round-trip waste: ~30 assets × ~1 ms each on loopback
    = ~30 ms added to cold-start.

    Bundled fonts under /fonts/ literally never change without a code
    push (the filenames are content-stable: they are named by
    family + subset, not by version hash). Mark them `immutable` so
    the browser skips revalidation entirely.

    CSS / JS use `no-cache` (NOT `max-age=N`): the browser MAY cache
    the response but MUST revalidate with If-Modified-Since before
    every use. Previously, JS/CSS got `max-age=86400, must-revalidate`
    which let WKWebView serve cached JS for up to 24 hours without
    contacting the server — so a hot-patch the user just shipped
    wouldn't be picked up by an in-app reload (⌘R) for up to a day,
    forcing the "killall Tern + rm -rf ~/Library/WebKit/fm.tern.app
    + open Tern.app" sequence to clear the cache. A critical fix could
    sit in /Applications/Tern.app on disk while WKWebView kept serving
    the broken version from cache. `no-cache` costs ~1 ms per asset (loopback 304) per page
    load — acceptable for a local-first desktop app where "what's
    on disk is what runs" is more important than shaving 30ms off
    cold start. The 304 path still skips the body transfer; the
    revalidation request is just ETag + Last-Modified headers.
    """
    async def get_response(self, path, scope):
        resp = await super().get_response(path, scope)
        # Only set on 200 responses for files we actually own.
        if getattr(resp, "status_code", 0) == 200:
            if path.startswith("fonts/") or path == "fonts.css":
                # woff2 + the local fonts.css are content-stable.
                resp.headers["Cache-Control"] = "public, max-age=604800, immutable"
            elif path.endswith((".css", ".js")):
                # See class docstring: force revalidation so hot-patches
                # are picked up by the next page load, not held for 24h.
                resp.headers["Cache-Control"] = "no-cache"
        return resp


app.mount("/", _CachedStaticFiles(directory=str(APP_DIR), html=True), name="app")
