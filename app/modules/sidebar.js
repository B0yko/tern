// app/modules/sidebar.js — sidebar render + ⌘B toggle.
import { state, subscribe } from "/modules/state.js";
import { api } from "/modules/api.js";
import { icons } from "/modules/icons.js";
import { showContextMenu } from "/modules/contextmenu.js";
import { flashToast } from "/modules/toast.js";

let _el = null;

// Cached app version for the license button's "vX.Y.Z" chip. Fetched
// ONCE at sidebar init from /api/diagnostics (the canonical source —
// /api/diagnostics reads from pyproject.toml at startup, single source
// of truth per commit 71d239e). Previously HARDCODED here
// as "v0.1.1" which drifted from the real version (0.1.0 across
// pyproject.toml, tauri.conf.json, Cargo.toml) — same defect class
// the 71d239e diagnostics fix corrected, just on a different surface.
// User saw "v0.1.1" in the sidebar while the ⌘/ keyhelp's About chip
// and /api/diagnostics correctly reported v0.1.0. Module-level cache
// because the version never changes during a process lifetime.
let _sidebarVersion = null;

// Escape HTML for safe interpolation into innerHTML AND attribute values.
// Previously we were doing inconsistent things — some sites stripped
// `[<>&]` entirely (lossy: "Foo & Bar.mp4" rendered as "Foo  Bar.mp4"),
// others escaped only `"` in attributes (insufficient: a folder path like
// `…/<img src=x onerror=alert(1)>/` interpolated as the truncated `label`
// on line 438 had ZERO escaping and would execute in the WKWebView with
// full backend access — real XSS surface). One helper, every site.
function _esc(s) {
  return String(s == null ? "" : s).replace(/[&<>"']/g, c =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function _fmtHours(ms) {
  if (!ms) return "0";
  const h = ms / 3_600_000;
  return h >= 10 ? h.toFixed(0) : h.toFixed(1);
}

function _stats() {
  const s = state.stats;
  if (!s) return "—";
  return `${s.files_total} files · ${_fmtHours(s.total_duration_ms)}h · ${s.transcript_segments?.toLocaleString?.() || s.transcript_segments} moments`;
}

function _render() {
  if (!_el) return;
  if (!state.sidebarOpen) {
    _el.innerHTML = "";
    return;
  }

  const recents = state.recents;

  _el.innerHTML = `
    <div class="sb">
      <div class="sb-head">
        <div class="sb-logo">
          <span class="bird">${icons.ternBird({ w: 16, h: 16 })}</span>
          <span>tern<span class="fm">.fm</span></span>
        </div>
        <div class="sb-stats">${_stats()}</div>
      </div>

      ${(state.bookmarks && state.bookmarks.length) ? `
        <div class="sb-sec" id="sb-bookmarks-sec">
          <div class="sb-sec-label">Bookmarks${state.bookmarks.length > 6 ? ` <span class="sb-sec-count">${state.bookmarks.length}</span>` : ""}</div>
          <div class="sb-sec-scroll">
          ${state.bookmarks.map(b => `
            <div class="sb-row sb-row-bookmark" data-bookmark-id="${_esc(b.file_id)}-${_esc(b.ts_ms)}" title="${_esc(b.file_name || '')} @ ${_esc(b.timecode || '')}">
              <span class="sb-row-glyph">${icons.starFilled({ w: 10, h: 10 })}</span>
              <span class="sb-row-text">${_esc(b.file_name || '')}</span>
              <span class="sb-row-meta" style="font-family:var(--font-mono);">${_esc(b.timecode || '')}</span>
              <button class="sb-row-remove" data-remove-bookmark="${_esc(b.file_id)}-${_esc(b.ts_ms)}" title="Remove bookmark" aria-label="Remove">${icons.close({ w: 10, h: 10 })}</button>
            </div>
          `).join("")}
          </div>
        </div>` : ""}

      ${(state.saved && state.saved.length) ? `
        <div class="sb-sec" id="sb-saved-sec">
          <div class="sb-sec-label">Saved${state.saved.length > 8 ? ` <span class="sb-sec-count">${state.saved.length}</span>` : ""}</div>
          <div class="sb-sec-scroll">
          ${state.saved.map(s => `
            <div class="sb-row sb-row-saved${state.query === s.query ? " active" : ""}" data-q="${_esc(s.query)}">
              <span class="sb-row-glyph">${icons.starFilled({ w: 10, h: 10 })}</span>
              <span class="sb-row-text">${_esc(s.query)}</span>
              <button class="sb-row-remove" data-remove-q="${_esc(s.query)}" title="Remove from Saved" aria-label="Remove">${icons.close({ w: 10, h: 10 })}</button>
            </div>
          `).join("")}
          </div>
        </div>` : ""}

      <div class="sb-sec" id="sb-recents-sec">
        <div class="sb-sec-label">Recent searches${recents.length > 5 ? ` <span class="sb-sec-count">${recents.length}</span>` : ""}</div>
        ${recents.length ? `<div class="sb-sec-scroll">${recents.map((r, i) => `
          <div class="sb-row${state.query === r.query ? " active" : ""}" data-q="${_esc(r.query)}">
            <span class="sb-row-glyph">${icons.search({ w: 11, h: 11 })}</span>
            <span class="sb-row-text">${_esc(r.query)}</span>
            <span class="sb-row-meta">${r.hits ?? ""}</span>
          </div>
        `).join("")}</div>` : `<div style="font-size:11px;color:var(--text-4);padding:4px 8px;">No recents yet — type to search.</div>`}
      </div>

      <div class="sb-sec" id="sb-folders-sec">
        <div class="sb-sec-label">
          Folders
          <span class="sb-sec-add" id="sb-add-folder" title="Add folder (⇧⌘O)">${icons.plus({ w: 12, h: 12 })}</span>
        </div>
        <div id="sb-folders-list">
          <div style="font-size:11px;color:var(--text-4);padding:4px 8px;">Loading…</div>
        </div>
      </div>

      <div class="sb-foot">
        <button class="sb-license" id="sb-license" type="button"
                title="${state.license?.status === "active"
                  ? 'Licensed — click to view or remove your key'
                  : 'Trial mode — click to activate a licence key or request one'}">
          <span class="sb-license-dot ${state.license?.status === "active" ? "active" : "trial"}"></span>
          <span style="flex:1;text-align:left;">${state.license?.status === "active"
            ? "Licensed"
            : 'Trial mode <span class="sb-license-cta">· Activate</span>'}</span>
          <span class="sb-license-version">${_sidebarVersion ? "v" + _esc(_sidebarVersion) : ""}</span>
        </button>
        <div class="sb-privacy">
          <span class="sb-privacy-icon">${icons.shield({ w: 12, h: 12 })}</span>
          <span><strong>100% local.</strong> Your audio never leaves this Mac.</span>
        </div>
      </div>
    </div>
  `;

  // Saved-row remove buttons — stop propagation so they don't trigger the
  // re-run-search click below.
  _el.querySelectorAll("[data-remove-q]").forEach(btn => {
    btn.addEventListener("click", async (ev) => {
      ev.stopPropagation();
      const q = btn.dataset.removeQ;
      const { remove } = await import("/modules/saved.js");
      remove(q);
      _render();
    });
  });
  // Bookmark rows: click = jump to that moment; X = remove.
  _el.querySelectorAll("[data-bookmark-id]").forEach(row => {
    row.addEventListener("click", () => {
      const bk = state.bookmarks.find(b => `${b.file_id}-${b.ts_ms}` === row.dataset.bookmarkId);
      if (bk) state.selectedHit = { ...bk, sources: [bk.source || "transcript"] };
    });
  });
  _el.querySelectorAll("[data-remove-bookmark]").forEach(btn => {
    btn.addEventListener("click", async (ev) => {
      ev.stopPropagation();
      const [fid, ts] = btn.dataset.removeBookmark.split("-").map(Number);
      const { remove } = await import("/modules/bookmarks.js");
      remove(fid, ts);
      _render();
    });
  });

  _el.querySelectorAll(".sb-row[data-q]").forEach(node => {
    node.addEventListener("click", () => {
      const q = node.dataset.q;
      // Saved rows: restore the scope snapshotted at save time so the
      // user gets the same result set they pinned. Recent rows skip
      // the scope restore — semantically "what did I search recently"
      // should re-run under current scope.
      if (node.classList.contains("sb-row-saved")) {
        _runSavedQuery(q);
      } else {
        _runQuery(q);
      }
    });
    // Right-click → context menu. Different actions depending on whether
    // this is a Saved or a Recent row (the saved ones have .sb-row-saved).
    node.addEventListener("contextmenu", (ev) => {
      ev.preventDefault();
      _onQueryRowContext(ev, node);
    });
  });

  // Bookmarks: right-click → context menu for jump / reveal / remove.
  _el.querySelectorAll("[data-bookmark-id]").forEach(node => {
    node.addEventListener("contextmenu", (ev) => {
      ev.preventDefault();
      _onBookmarkRowContext(ev, node);
    });
  });

  _el.querySelector("#sb-add-folder").addEventListener("click", () => {
    document.dispatchEvent(new CustomEvent("tern:open-folder-modal"));
  });
  const lic = _el.querySelector("#sb-license");
  if (lic) lic.addEventListener("click", () => {
    document.dispatchEvent(new CustomEvent("tern:open-license-modal"));
  });

  _renderFolders();
}

// ─── Helpers for sidebar row interactions ──────────────────────────────

function _runQuery(q) {
  const input = document.getElementById("search-input");
  if (!input) return;
  input.value = q;
  input.dispatchEvent(new Event("input", { bubbles: true }));
  input.focus();
}

// Re-run a Saved search WITH its snapshotted scope (sources + folder)
// restored. Mirrors the click-path logic above —
// extracted here so right-click "Run this search" goes through the
// same code as the row click. Falls through to plain _runQuery if
// the saved entry predates the scope-snapshot feature (legacy
// entries return null from getSavedScope).
//
// Ordering matters: setting state.sources / state.folderFilter
// triggers topbar's subscriber to re-run the CURRENT query immediately.
// We want that to be a no-op (because we haven't changed q yet), then
// _runQuery flips q → triggers the actual search under the restored
// scope. Net: one search, correct scope.
async function _runSavedQuery(q) {
  // Track whether scope actually changed so we can show a toast that
  // explains the visible result-set shift. Without this, the user
  // clicks "pricing" expecting their familiar Speech-only results;
  // gets them, but doesn't realize the scope flipped — then later
  // they search something else and wonder why visual hits are gone.
  let toastMsg = null;
  try {
    const savedMod = await import("/modules/saved.js");
    const scope = savedMod.getSavedScope(q);
    if (scope) {
      const labelMap = { transcript: "Speech", ocr: "On-screen", visual: "Visual" };
      const newSources = Array.isArray(scope.sources) && scope.sources.length
        ? [...scope.sources] : null;
      const newFolder = scope.folder || null;

      // Detect whether applying the scope is a no-op (same as current).
      const currentSources = Array.isArray(state.sources) ? state.sources : [];
      const sourcesChanged = newSources !== null && (
        newSources.length !== currentSources.length ||
        newSources.some(s => !currentSources.includes(s))
      );
      const folderChanged = (newFolder || null) !== (state.folderFilter || null);

      if (newSources !== null) state.sources = newSources;
      state.folderFilter = newFolder;

      // Compose a human message only when SOMETHING actually changed.
      if (sourcesChanged || folderChanged) {
        const parts = [];
        if (sourcesChanged && newSources && newSources.length < 3) {
          parts.push("only " + newSources.map(s => labelMap[s] || s).join(" + "));
        }
        if (folderChanged && newFolder) {
          const f = newFolder;
          parts.push("in " + (f.length > 24 ? "…" + f.slice(-22) : f));
        }
        if (parts.length) {
          toastMsg = "Restored scope: " + parts.join(" · ");
        }
      }
    }
  } catch (e) { console.warn("scope restore failed", e); }
  _runQuery(q);
  // Fire toast AFTER the search kicks off so the user sees both events
  // in order (results land first, then the explanation chip pops).
  if (toastMsg) {
    import("/modules/toast.js")
      .then(m => m.flashToast(toastMsg, { kind: "info", ttl: 2400 }))
      .catch(() => {});
  }
}

function _copyText(text) {
  if (!text) return;
  try {
    if (navigator.clipboard?.writeText) {
      navigator.clipboard.writeText(text).catch(() => _execCopy(text));
    } else {
      _execCopy(text);
    }
  } catch { _execCopy(text); }
}
function _execCopy(text) {
  const ta = document.createElement("textarea");
  ta.value = text; ta.style.position = "fixed"; ta.style.opacity = "0";
  document.body.appendChild(ta); ta.select();
  try { document.execCommand("copy"); } catch {}
  document.body.removeChild(ta);
}

// Right-click on a Saved or Recent search row.
async function _onQueryRowContext(ev, node) {
  const q = node.dataset.q;
  const isSavedRow = node.classList.contains("sb-row-saved");

  // Need to import dynamically because saved.js + recents.js export by name
  // and we only need these on right-click (rare path).
  const savedMod  = await import("/modules/saved.js");
  const recentsMod = await import("/modules/recents.js");
  const isSaved = savedMod.isSaved(q);

  const items = [
    {
      label: "Run this search",
      shortcut: "↵",
      // Mirror the row-CLICK path: saved rows restore their snapshotted
      // scope, recent rows don't. Without this branch the context-menu
      // Run was inconsistent with the row click — same query could
      // return different results depending on whether the user clicked
      // the row vs right-clicked + "Run this search".
      onClick: () => { isSavedRow ? _runSavedQuery(q) : _runQuery(q); },
    },
    {
      label: "Copy query",
      shortcut: "⌘C",
      onClick: () => { _copyText(q); flashToast("Query copied", { kind: "ok" }); },
    },
    { divider: true },
    isSavedRow
      ? {
          label: "Remove from Saved",
          danger: true,
          onClick: () => {
            // Snapshot the row before removal so Undo can restore it.
            const snap = (state.saved || []).find(s => s.query === q);
            savedMod.remove(q);
            _render();
            flashToast("Removed from Saved", {
              undo: () => { savedMod.toggle(q, snap?.hits ?? null); _render(); },
            });
          },
        }
      : {
          label: isSaved ? "Already in Saved" : "Pin to Saved",
          shortcut: isSaved ? "" : "⌘D",
          disabled: isSaved,
          onClick: () => { savedMod.toggle(q); _render(); flashToast("Saved", { kind: "ok" }); },
        },
    !isSavedRow && {
      label: "Remove from Recents",
      danger: true,
      onClick: () => {
        // Snapshot for Undo. recents.js doesn't have a re-add API so
        // restore by direct localStorage write (mirrors removeRecent).
        const snap = (state.recents || []).find(r => (r.query || "").trim() === q.trim());
        recentsMod.removeRecent(q);
        _render();
        flashToast("Removed from Recents", {
          undo: () => {
            if (!snap) return;
            try {
              const KEY = "tern.recents.v1";
              const arr = JSON.parse(localStorage.getItem(KEY) || "[]");
              arr.unshift(snap);
              localStorage.setItem(KEY, JSON.stringify(arr));
              state.recents = arr;
            } catch (e) { console.error("sidebar undo-remove-recent persist failed", e); }
            _render();
          },
        });
      },
    },
  ].filter(Boolean);

  showContextMenu(ev, items);
}

// Right-click on a Bookmark row.
async function _onBookmarkRowContext(ev, node) {
  const id = node.dataset.bookmarkId;
  const bk = (state.bookmarks || []).find(b => `${b.file_id}-${b.ts_ms}` === id);
  if (!bk) return;

  const bookmarksMod = await import("/modules/bookmarks.js");

  const items = [
    {
      label: "Jump to this moment",
      shortcut: "↵",
      onClick: () => { state.selectedHit = { ...bk, sources: [bk.source || "transcript"] }; },
    },
    bk.snippet && {
      label: "Copy quote",
      shortcut: "⌘C",
      onClick: () => {
        _copyText((bk.snippet || "").replace(/<\/?mark>/g, ""));
        flashToast("Quote copied", { kind: "ok" });
      },
    },
    {
      label: `Copy ${bk.file_name || ""} @ ${bk.timecode || "0:00"}`,
      onClick: () => {
        _copyText(`${bk.file_name || "?"} @ ${bk.timecode || "0:00"}`);
        flashToast("Reference copied", { kind: "ok" });
      },
    },
    bk.file_path && { divider: true },
    bk.file_path && {
      label: "Reveal in Finder",
      // Bookmarks survive file moves/deletes — the indexed path could be
      // stale by the time the user right-click → Reveal. Without the
      // toast, the menu just closed silently and the user assumed
      // either (a) Finder didn't focus or (b) the menu item was a
      // no-op. Surface the rejection so they know the file's gone.
      onClick: () => api.reveal(bk.file_path).catch(e =>
        flashToast(`Couldn't reveal: ${e?.message || e}`, { kind: "err", ttl: 3500 })
      ),
    },
    { divider: true },
    {
      label: "Remove bookmark",
      danger: true,
      onClick: () => {
        // Snapshot the full bookmark object before removal so Undo can
        // re-pin the exact same moment (file_id+ts_ms uniqueness is
        // bookmarks.toggle's idempotency key).
        const snap = { ...bk };
        bookmarksMod.remove(bk.file_id, bk.ts_ms);
        _render();
        flashToast("Bookmark removed", {
          undo: () => { bookmarksMod.toggle(snap); _render(); },
        });
      },
    },
  ].filter(Boolean);

  showContextMenu(ev, items);
}

// Extracted from the inline X-button click handler so the context menu can
// reuse the same flow (confirm prompt + DELETE call + sidebar refresh).
async function _removeFolder(folder) {
  if (!folder) return;
  if (!confirm(`Remove "${folder}" from the index?\n\nThis only deletes the search index (transcripts, OCR, embeddings). Your source files on disk are untouched.`)) return;
  try {
    // Use the api.removeFolder helper instead of raw fetch — this gets
    // the commit d252018 default 60s timeout (which matters here: a big
    // folder removal loops cleanup_file per matched file and rmtrees
    // potentially thousands of thumbnail JPEGs; a wedged backend or
    // network-mounted workspace mid-delete used to hang the call
    // forever with no user feedback). Backend returns 200 with
    // `{ok: true, files_removed: N}` on success or a thrown HTTP error
    // for 4xx (status 409 if indexing is in flight, 400 if the path
    // is too broad / not absolute). api.removeFolder THROWS on the
    // HTTP-error path → outer catch surfaces it.
    const r = await api.removeFolder(folder);
    if (r && r.ok) {
      _renderFolders();
      try { state.stats = await api.stats(); } catch {}
    } else {
      alert((r && r.detail) || "Remove failed.");
    }
  } catch (e) {
    alert("Backend error: " + (e.message || e));
  }
}

async function _renderFolders() {
  const list = _el.querySelector("#sb-folders-list");
  if (!list) return;
  try {
    const r = await api.files();
    // Group by directory
    const groups = new Map();
    (r.files || []).forEach(f => {
      const dir = f.path.split("/").slice(0, -1).join("/") || "/";
      if (!groups.has(dir)) groups.set(dir, []);
      groups.get(dir).push(f);
    });
    const folders = [...groups.entries()].map(([dir, files]) => ({ dir, count: files.length }));
    if (!folders.length) {
      list.innerHTML = `<div style="font-size:11px;color:var(--text-4);padding:4px 8px;">No folders indexed.</div>`;
      return;
    }
    list.innerHTML = folders.map(f => {
      const label = f.dir.length > 28 ? "…" + f.dir.slice(-26) : f.dir;
      // CRITICAL: f.dir is a filesystem path that the OS allows to contain
      // `<`, `>`, `&`, `"` (macOS happily creates these). Previously
      // `${label}` was interpolated with ZERO escaping — a folder
      // `~/foo/<img src=x onerror=alert(1)>/` would inject and execute in
      // the WKWebView with backend API access. _esc() now closes that.
      return `
        <div class="sb-row sb-row-folder" data-folder="${_esc(f.dir)}">
          <span class="sb-row-glyph">${icons.folder({ w: 11, h: 11 })}</span>
          <span class="sb-row-text" title="${_esc(f.dir)}">${_esc(label)}</span>
          <span class="sb-row-meta">${f.count}</span>
          <button class="sb-row-remove" data-remove-folder="${_esc(f.dir)}" title="Remove from index (source files untouched)" aria-label="Remove">${icons.close({ w: 10, h: 10 })}</button>
        </div>
      `;
    }).join("");
    // Wire remove buttons — stopPropagation so the folder-row click (no
    // current handler but might land later) doesn't fire too.
    list.querySelectorAll("[data-remove-folder]").forEach(btn => {
      btn.addEventListener("click", async (ev) => {
        ev.stopPropagation();
        const folder = btn.dataset.removeFolder;
        await _removeFolder(folder);
      });
    });
    // Right-click on a folder row → context menu (Reveal in Finder + Remove).
    list.querySelectorAll(".sb-row-folder").forEach(node => {
      node.addEventListener("contextmenu", (ev) => {
        ev.preventDefault();
        const folder = node.dataset.folder;
        showContextMenu(ev, [
          {
            label: "Reveal in Finder",
            // Folder rows can also go stale — user moved/renamed/deleted
            // the indexed folder outside Tern between page-load and
            // right-click. Toast surfaces the rejection (same pattern
            // as the bookmark Reveal above + every other Reveal site
            // in the app) so the user knows the row is dead and not
            // that Tern's Reveal feature is broken.
            onClick: () => api.reveal(folder).catch(e =>
              flashToast(`Couldn't reveal: ${e?.message || e}`, { kind: "err", ttl: 3500 })
            ),
          },
          {
            label: "Filter search to this folder",
            onClick: () => {
              state.folderFilter = folder;
              // If we have a query, the topbar subscriber re-runs it.
              if (!state.query?.trim()) {
                const input = document.getElementById("search-input");
                if (input) input.focus();
              }
            },
          },
          { divider: true },
          {
            label: "Remove folder from index",
            danger: true,
            onClick: () => _removeFolder(folder),
          },
        ]);
      });
    });
  } catch {
    list.innerHTML = `<div style="font-size:11px;color:var(--err);padding:4px 8px;">Backend unreachable.</div>`;
  }
}

// Persistence key for the sidebar open/closed state — survives reloads.
// Without this the sidebar always re-opens to its state.js default
// (currently false), wiping the user's last preference each launch.
const SIDEBAR_OPEN_KEY = "tern.sidebarOpen.v1";

export function initSidebar() {
  _el = document.getElementById("sidebar");

  // Restore last open/closed preference. Default to current state.js
  // value if missing (first launch / opted out of localStorage).
  try {
    const stored = localStorage.getItem(SIDEBAR_OPEN_KEY);
    if (stored !== null) state.sidebarOpen = stored === "1";
  } catch (e) { console.error("sidebar open-state restore failed", e); }

  const apply = () => _el.dataset.open = state.sidebarOpen ? "true" : "false";
  apply();
  // Initial render so the sidebar paints immediately if it starts open
  // (without this it stays empty until the first subscriber fire).
  if (state.sidebarOpen) _render();

  subscribe((k) => {
    if (k === "sidebarOpen") {
      apply();
      _render();
      // Persist on every change so toggling state via ⌘B or the topbar
      // button "sticks" across launches.
      try { localStorage.setItem(SIDEBAR_OPEN_KEY, state.sidebarOpen ? "1" : "0"); }
      catch (e) { console.error("sidebar open-state persist failed", e); }
    }
    if (k === "query")       _render();
    if (k === "stats")       _render();
    if (k === "recents")     _render();
    if (k === "saved")       _render();
    if (k === "bookmarks")   _render();
    if (k === "license")     _render();  // flip "Trial mode" → "Licensed" reactively after activation; was stuck until next unrelated re-render before this
  });

  // Fetch license status once at init. State is mostly static (server
  // validates on activation), so no poller is needed; if we ever need
  // staleness handling, switch to setInterval here.
  api.licenseStatus().then(l => { state.license = l; }).catch(() => {});

  // Fetch the canonical app version ONCE so the license button's
  // "vX.Y.Z" chip stops drifting from the real pyproject.toml value.
  // Version never changes during a process lifetime so no re-fetch
  // needed. On failure (network glitch at boot), the chip silently
  // renders empty — better than showing a stale hardcoded value.
  //
  // Previously, this hit /api/diagnostics — which reads up to 10 MB
  // of crash log + tails it + runs store.stats() + recursively
  // redacts every Users path, all in two asyncio.to_thread offloads,
  // JUST to surface the cached _APP_VERSION constant. Burning that
  // pipeline on every app cold-start before any user interaction was
  // pure overhead. The new /api/version endpoint returns ONLY the
  // version constant — no I/O, no thread offload, no recursion.
  fetch("/api/version")
    .then(r => r.ok ? r.json() : null)
    .then(d => {
      if (d && d.version) {
        _sidebarVersion = d.version;
        // Re-render so the button picks up the version string.
        if (state.sidebarOpen) _render();
      }
    })
    .catch(() => {});
}
