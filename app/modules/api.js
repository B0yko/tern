// app/modules/api.js
// Thin REST client. One function per backend endpoint.

const BASE = "";  // same-origin

// Default 60 s timeout on every backend request. Without it, a wedged
// sidecar (Whisper subprocess hung mid-decode, ChromaDB lock contention
// after a hard crash, segfault recovery) hangs the frontend call forever
// — the user sees a spinner indefinitely with no feedback. The toast /
// search input flows surface AbortError → "timed out" so the user knows
// to retry or restart rather than wait. 60 s covers the slowest legitimate
// call shape: /api/search on cold start (SigLIP-2 load + first text embed
// = ~5–10 s on Apple Silicon) plus margin; /api/license/activate is
// already 8 s-capped server-side; /api/folders/remove for a 200-file
// folder is ~4–10 s on a healthy SSD. Callers can override via
// `opts.timeoutMs` if a specific endpoint needs longer.
const DEFAULT_TIMEOUT_MS = 60000;

async function _json(path, opts = {}) {
  const { timeoutMs = DEFAULT_TIMEOUT_MS, signal: extSignal, ...fetchOpts } = opts;
  const ctrl = new AbortController();
  // Track WHICH cause aborted us — needed because external aborts (user
  // typing supersedes a search) and timeout aborts both surface as
  // AbortError on fetch, but only the timeout should be re-thrown as
  // a friendly "timed out — backend may be wedged" message. External
  // aborts must propagate as plain AbortError so topbar.js's
  // silence-branch (`name === "AbortError"`) catches them and doesn't
  // toast-spam the user on every keystroke.
  let _timedOut = false;
  const timer = setTimeout(() => { _timedOut = true; ctrl.abort(); }, timeoutMs);
  // Link the caller-provided AbortSignal to ours so cancellation flows
  // through. Without this, the search-as-you-type abort in topbar.js
  // (commit 8217c1e) would be silently ignored — the older fetch would
  // run to completion on the backend, wasting a SigLIP embed + Chroma
  // query + transcript/OCR FTS per superseded keystroke. Adding-and-
  // firing pattern: if already aborted, abort immediately; else hook
  // the abort event once.
  if (extSignal) {
    if (extSignal.aborted) ctrl.abort();
    else extSignal.addEventListener("abort", () => ctrl.abort(), { once: true });
  }
  try {
    const r = await fetch(BASE + path, {
      headers: { "Content-Type": "application/json", ...(fetchOpts.headers || {}) },
      ...fetchOpts,
      signal: ctrl.signal,
    });
    if (!r.ok) {
      const detail = await r.json().catch(() => ({}));
      throw new Error(`${r.status} ${r.statusText}: ${detail.detail || ""}`);
    }
    return r.json();
  } catch (e) {
    // Only translate AbortError to "timed out" when OUR timer fired.
    // External-cause AbortErrors propagate as-is so callers can
    // detect-and-silence them (topbar.js does, on user-typed cancel).
    if (e?.name === "AbortError" && _timedOut) {
      throw new Error(`${path} timed out after ${(timeoutMs / 1000) | 0}s — backend may be wedged`);
    }
    throw e;
  } finally {
    clearTimeout(timer);
  }
}

export const api = {
  health:        () => _json("/api/health", { timeoutMs: 10000 }),
  stats:         () => _json("/api/stats"),
  files:         (opts = {}) => {
    // Query params: { limit?: number, status?: string }. Backward-compat —
    // no args returns the full list as before. Empty state passes limit=12;
    // sidebar/filters omit it (need every path for folder grouping).
    const qs = new URLSearchParams();
    if (opts.limit) qs.set("limit", String(opts.limit));
    if (opts.status) qs.set("status", opts.status);
    return _json("/api/files" + (qs.toString() ? "?" + qs : ""));
  },
  search:        (query, opts = {}) => _json("/api/search", {
    method: "POST",
    body: JSON.stringify({
      query,
      limit: opts.limit || 30,
      sources: opts.sources || ["transcript", "ocr", "visual"],
      ...(opts.folder ? { folder: opts.folder } : {}),
    }),
    // Pass through caller-provided AbortSignal so topbar.js can
    // cancel superseded search-as-you-type fetches mid-flight
    // instead of wasting SigLIP embed + Chroma query + FTS work
    // on the older keystroke's query.
    ...(opts.signal ? { signal: opts.signal } : {}),
  }),
  transcriptWindow: (file_id, ts_ms, radius = 3, opts = {}) =>
    _json(
      `/api/transcript/window?file_id=${file_id}&ts_ms=${ts_ms}&radius=${radius}`,
      // Pass-through signal so detail.js can abort superseded fetches
      // during rapid ↓ navigation. Same end-to-end abort plumbing as
      // /api/search (commit 8217c1e).
      opts.signal ? { signal: opts.signal } : {},
    ),
  // Export endpoints get bumped timeouts because the backend's subprocess
  // ceiling (commit 5de1191) is higher than _json's 60 s default. Without
  // the override, the frontend aborts at 60 s while the BACKEND continues
  // ffmpeg encoding for up to its own ceiling — the user sees a "timed
  // out" toast but the clip ACTUALLY GETS SAVED to workspace/exports/
  // (just with no UI feedback). Then on the next search the user thinks
  // the export failed but two copies exist. Both timeouts here = backend
  // ceiling + 60 s slack so a real backend timeout still gets a clean
  // 504 from commit bf2c0b5/6490a2d translations rather than a frontend
  // AbortError that masks it.
  exportClip:    (file_path, start_ms, end_ms, audio_only = false) => _json("/api/export/clip", {
    method: "POST",
    body: JSON.stringify({ file_path, start_ms, end_ms, audio_only, padding_ms: 1500 }),
    // Backend ceiling: 300 s for video (commit 5de1191) + 60 s slack
    // for asyncio scheduling / network round-trip / file IO. Audio
    // path is faster (120 s ceiling) but using the higher value is
    // simpler than branching on audio_only — extra wait only triggers
    // for the genuinely-stuck case which we want the backend's 504
    // to handle anyway.
    timeoutMs: 360_000,
  }),
  exportSrt:     (file_id, ts_ms, radius = 8) => _json("/api/export/srt", {
    method: "POST",
    body: JSON.stringify({ file_id, ts_ms, radius }),
    // SRT export doesn't shell out to ffmpeg — pure DB query + text
    // emit. 60 s default is fine, kept explicit so a future refactor
    // doesn't accidentally bump it without reason.
  }),
  exportFcpxml:  (hits, project_name) => _json("/api/export/fcpxml", {
    method: "POST",
    body: JSON.stringify({ hits, project_name }),
    // Backend probes ffprobe per UNIQUE source file (commit 6490a2d) at
    // 30 s ceiling each. A 50-hit FCPXML spanning 10 sources = up to
    // 300 s if all 10 hit the ceiling. + 60 s slack matches the
    // export/clip rationale. Real-world: completes in 1-5 s; the
    // 360 s is the worst-case safety net.
    timeoutMs: 360_000,
  }),
  exportCsv:     (hits, project_name) => _json("/api/export/csv", {
    method: "POST",
    body: JSON.stringify({ hits, project_name }),
    // CSV export is pure Python-side rowwriting — no ffprobe, no
    // model load, no thread offload beyond the standard FastAPI
    // event-loop work. 60 s default is generous; real-world completes
    // in single-digit ms for the 200-hit max payload.
  }),
  fileThumbnails: (file_id) => _json(`/api/file/thumbnails?file_id=${file_id}`),
  reveal:        (path) => _json("/api/reveal", { method: "POST", body: JSON.stringify({ path }) }),
  open:          (path) => _json("/api/open",   { method: "POST", body: JSON.stringify({ path }) }),
  quicklook:     (path) => _json("/api/quicklook", { method: "POST", body: JSON.stringify({ path }) }),
  indexFolder:   (folder, force = false) => _json("/api/index", {
    method: "POST",
    body: JSON.stringify({ folder, force }),
  }),
  indexStatus:   () => _json("/api/index/status"),
  indexCancel:   () => _json("/api/index/cancel", { method: "POST" }),
  removeFolder:  (folder) => _json("/api/folders/remove", { method: "POST", body: JSON.stringify({ folder }) }),
  licenseStatus: () => _json("/api/license/status"),
  licenseActivate: (license_key) => _json("/api/license/activate", {
    method: "POST",
    body: JSON.stringify({ license_key }),
  }),
  licenseClear:    () => _json("/api/license/clear", { method: "POST" }),
};
