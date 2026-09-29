// app/modules/empty.js — empty state: day-1 hero with demo queries, day-N
// hero with recent searches. Both rendered into the SAME shell so the
// transition is content-only, not layout.
import { state, subscribe } from "/modules/state.js";
import { api } from "/modules/api.js";
import { renderRow } from "/modules/row.js";

// Escape HTML for safe interpolation into innerHTML AND attribute values.
// Previously the pill renderer (line ~71) only escaped `"` in attribute
// contexts and put `${p.query}` into innerHTML text with ZERO escaping —
// a user-typed recent like `<img src=x onerror=…>` would execute in the
// WKWebView with backend API access. _esc() closes that.
function _esc(s) {
  return String(s == null ? "" : s).replace(/[&<>"']/g, c =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

// DEMO_QUERIES used to seed the empty-workspace hero with clickable
// example pills. Removed because clicking ANY of
// them on a truly-empty workspace fires a search that returns 0 hits
// (no indexed files to match against). The buyer's path then: click
// pill → see 0 results → read the "index a folder" tip → click the
// CTA. Strictly worse than the path with no pills: read the hero
// subtext describing what Tern does → click the "+ Index your folder"
// CTA directly. When the user HAS recents (e.g., they indexed +
// removed a folder), the pills show real recents, which retain their
// semantic. The day-1 truly-empty state is now: headline + subtext +
// CTA + privacy line, with no fake example chips to mislead.

let _pane = null;
let _shell = null;

async function _renderList(listEl) {
  // Library view — used when the workspace has files (day-N). Fills the
  // entire results-pane width; no marketing hero, no pills. Counts come
  // straight from /api/files.
  //
  // Adds an inline "+ Add folder" button to the header label. An earlier
  // change removed the hero entirely on day-N, which also removed the only
  // visible affordance for indexing a new folder — leaving users to
  // either know ⇧⌘O or open the sidebar. This puts the action back in
  // the user's eyeline without bringing back the marketing slab.
  try {
    const r = await api.files({ limit: 12 });
    const files = (r.files || []);
    // Library-size label. The backend's `count` is
    // len(files) AFTER the SQL limit — so a 500-file library rendered
    // "12 files in library" directly above a stats-derived breakdown
    // ("300 audio · 200 video") that contradicted it. state.stats.
    // files_total is the real library size (unlimited aggregate);
    // fall back to r.count only while stats hasn't loaded yet.
    const totalFiles = (state.stats && state.stats.files_total) || r.count || files.length;
    const label = totalFiles === 1 ? "1 file in library" : `${totalFiles} files in library`;
    // Pull the kind breakdown from state.stats. A trial buyer staring
    // at "19 files in library" doesn't immediately register that Tern
    // handles audio + video + photos — a tiny "11 audio · 3 video · 5
    // images" line below the count makes the all-media-types
    // capability obvious at first glance, which is one of the biggest
    // differentiators vs Castmagic/Descript/Otter (audio-only). Stats
    // may not be loaded yet on first paint; fall back to no breakdown
    // rather than rendering "0 audio · 0 video · 0 images" garbage.
    const s = state.stats;
    const kindParts = [];
    if (s) {
      if ((s.files_audio || 0) > 0) kindParts.push(`${s.files_audio} audio`);
      if ((s.files_video || 0) > 0) kindParts.push(`${s.files_video} video`);
      if ((s.files_image || 0) > 0) kindParts.push(`${s.files_image} ${s.files_image === 1 ? "image" : "images"}`);
    }
    // Append a total-duration suffix when there's playable content
    // (audio + video). For a buyer mid-trial, "11 audio · 3 video · 5
    // images" tells them WHAT kinds Tern indexed; adding "· 4h 32m of
    // content" tells them HOW MUCH — a different and equally important
    // value-proof signal. "Tern has indexed 86h of my podcasts" lands
    // harder than "37 files." Skips photos in the duration math
    // (they're 0 ms) and the suffix entirely when the workspace is
    // image-only or stats are still loading.
    let durationSuffix = "";
    const durMs = (s && (s.total_duration_ms || 0)) || 0;
    if (durMs > 0) {
      const totalMin = Math.round(durMs / 60_000);
      const h = Math.floor(totalMin / 60);
      const m = totalMin % 60;
      const formatted = h > 0 ? `${h}h ${m}m` : `${m}m`;
      durationSuffix = ` · ${formatted} of content`;
    }
    const breakdown = kindParts.length >= 2
      ? `<div class="empty-list-breakdown" style="font-size:11px;color:var(--text-3);margin-top:2px;">${kindParts.join(" · ")}${durationSuffix}</div>`
      : "";
    // Surface the persistent indexing-error count (status='error' rows
    // in the files table, reported by /api/stats) as a
    // separate line below the breakdown. The PER-RUN errored count
    // surfaces in the indexing toast; this is the
    // ACCUMULATED across-runs count that the toast resets between
    // index passes. Without surfacing it here, a buyer with 3 failed
    // files from last week's indexing run has no in-app signal that
    // anything is wrong — they have to grep ~/Library/Logs/tern-
    // crash.log or call /api/files?status=error themselves. Renders
    // only when errors > 0 so the clean-state library view stays
    // uncluttered.
    const erroredCount = (s && (s.files_errored || 0)) || 0;
    const erroredLine = erroredCount > 0
      ? `<div class="empty-list-errored" style="font-size:11px;color:var(--err);margin-top:2px;" title="See ~/Library/Logs/tern-crash.log for per-file errors (search category index_file_failed)">⚠ ${erroredCount} ${erroredCount === 1 ? "file" : "files"} failed to index</div>`
      : "";
    listEl.innerHTML = `
      <div class="empty-list-label empty-list-header">
        <div style="display:flex;flex-direction:column;gap:2px;">
          <span>${label}</span>
          ${breakdown}
          ${erroredLine}
        </div>
        <button class="empty-list-add" id="empty-list-add"
                title="Index a folder (⇧⌘O)" aria-label="Index a folder (Command-Shift-O)">
          + Add folder
        </button>
      </div>
    `;
    listEl.querySelector("#empty-list-add").addEventListener("click", () => {
      document.dispatchEvent(new CustomEvent("tern:open-folder-modal"));
    });
    files.forEach((f) => {
      const fakeHit = _fileToFakeHit(f);
      const node = renderRow(fakeHit, -1, false);
      node.addEventListener("click", () => _openFromFile(f));
      listEl.appendChild(node);
    });
  } catch (e) {
    // Show the user-facing fallback (don't surface a toast on every
    // state.stats subscriber tick — this can fire 5+ times during a
    // backend hiccup and toast-spam erodes trust). But DO log to
    // console so devtools captures the actual failure for diagnostics
    // — previously the catch was a silent swallow, so a real
    // backend regression on /api/files (e.g., the
    // asyncio.to_thread wrap if it ever broke) looked identical to a
    // transient warmup race in the UI, with no diagnostic trace.
    console.error("empty-list /api/files failed", e);
    listEl.innerHTML = `<div class="empty-list-label">Library not ready</div>`;
  }
}

function _renderHero(heroEl) {
  // Hero — only shown when the workspace is TRULY empty (no files
  // indexed yet). Previously was always rendered alongside the file
  // list, taking ~62% of the results-pane width even when the user
  // had files and didn't need marketing material — user-reported
  // "the middle panel is useless, just takes half the screen".
  const hasRecents = state.recents.length > 0;
  const pills = hasRecents ? state.recents.slice(0, 6) : [];

  heroEl.innerHTML = `
    <h1>Find any moment<br/>in <span class="accent">3 seconds.</span></h1>
    <p>Search by what was said, what's on screen, or what's in the picture. Tern indexes everything locally on your Mac.</p>
    ${pills.length ? `<div class="empty-pills" role="list" aria-label="Recent searches">
      ${pills.map(p => `<button class="empty-pill" role="listitem" data-q="${_esc(p.query)}" aria-label="Run search: ${_esc(p.query)}">${_esc(p.query)}${p.hits ? ` <span style="opacity:.55;font-family:var(--font-mono);font-size:10px;margin-left:4px;">${_esc(p.hits)}</span>` : ""}</button>`).join("")}
    </div>` : ""}
    <button class="empty-cta" id="empty-add-folder" aria-label="Index a folder (Command-Shift-O)">+ Index your folder<span class="kbd">⇧⌘O</span></button>
    <div class="empty-privacy">100% local · Your audio never leaves this Mac</div>
  `;

  heroEl.querySelectorAll(".empty-pill").forEach(btn => {
    btn.addEventListener("click", () => {
      const q = btn.dataset.q;
      const input = document.getElementById("search-input");
      input.value = q;
      input.dispatchEvent(new Event("input", { bubbles: true }));
    });
  });
  heroEl.querySelector("#empty-add-folder").addEventListener("click", () => {
    document.dispatchEvent(new CustomEvent("tern:open-folder-modal"));
  });
}

function _fileToFakeHit(f) {
  const mime = f.mime || "";
  const kind = mime.startsWith("video") ? "video"
            : mime.startsWith("audio") ? "audio"
            : mime.startsWith("image") ? "image"
            : "other";
  // Prefer the small db/thumbnails/file_<id>/image_00000.jpg over the
  // full source file as the row's preview image. /api/files
  // now includes thumbnail_path per file,
  // so we don't blast through 5 MB HEIC files just to render an 80x80
  // grid thumbnail in the empty state.
  // %27-escape apostrophes on top of encodeURIComponent:
  // encodeURIComponent does NOT encode `'`, and row.js
  // interpolates these URLs into style="background-image:url('…')" —
  // a path like …/Sam's birthday.heic terminated the CSS string
  // (broken thumbnail for every apostrophe-named file), and a crafted
  // filename could inject CSS declarations including a remote url()
  // fetch — a privacy leak for a "100% local" app. Backend-built hit
  // URLs are safe (Python quote(safe="") encodes apostrophes); only
  // these frontend-constructed fake-hit URLs were exposed.
  const _encPath = (p) => encodeURIComponent(p).replace(/'/g, "%27");
  const thumbUrl = f.thumbnail_path
    ? `/api/file?path=${_encPath(f.thumbnail_path)}`
    : null;
  return {
    file_id: f.id, file_path: f.path, file_name: f.name,
    ts_ms: 0, duration_ms: f.duration_ms || 0,
    snippet: f.duration_str ? `${f.duration_str} · ${mime}` : (f.mime || ""),
    source: "transcript", sources: ["transcript"],
    score: 0,
    thumbnail_url: thumbUrl,
    preview_url: `/api/file?path=${_encPath(f.path)}`,
    timecode: f.duration_str || "",
    mime, media_kind: kind,
  };
}

function _openFromFile(f) {
  // Synthesize a hit for the detail pane so the user can preview the file
  const hit = _fileToFakeHit(f);
  state.selectedHit = hit;
}

function _show() {
  if (!_shell) {
    _shell = document.createElement("div");
    _shell.className = "empty-shell";
    _pane.appendChild(_shell);
  }
  _shell.hidden = false;

  // Decide: workspace empty (day-1) → hero only; workspace has files
  // (day-N) → library list only, no marketing panel. Stats may not be
  // loaded yet on first paint — fall back to "show list" since
  // /api/files is what we'd render anyway. The hero is reserved for
  // the explicit "no files indexed yet" case.
  const s = state.stats;
  const isEmptyWorkspace = s != null && (s.files_total ?? 0) === 0;

  if (isEmptyWorkspace) {
    _shell.classList.add("empty-shell--hero-only");
    _shell.classList.remove("empty-shell--list-only");
    _shell.innerHTML = `<div class="empty-hero" id="empty-hero"></div>`;
    _renderHero(_shell.querySelector("#empty-hero"));
  } else {
    _shell.classList.add("empty-shell--list-only");
    _shell.classList.remove("empty-shell--hero-only");
    _shell.innerHTML = `<div class="empty-list" id="empty-list"></div>`;
    _renderList(_shell.querySelector("#empty-list"));
  }
}

function _hide() {
  if (_shell) _shell.hidden = true;
}

export function initEmpty() {
  _pane = document.getElementById("results-pane");
  // Show on first load (empty query). Hide as soon as a query is set.
  if (!state.query) _show();

  subscribe((k) => {
    if (k === "query") {
      if (state.query.trim()) _hide(); else _show();
    }
    if ((k === "recents" || k === "stats" || k === "saved") && !state.query.trim()) _show();
  });
}
