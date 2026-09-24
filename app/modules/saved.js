// app/modules/saved.js — pinned/saved searches store.
// Persisted in localStorage under tern.saved.v1; surfaced in the sidebar
// (sidebar.js) above Recent searches and toggled from the topbar star
// button (topbar.js).
import { state } from "/modules/state.js";

const KEY = "tern.saved.v1";
const LIMIT = 30;

function _load() {
  // Shape-guard — see recents.js comment. localStorage may hold a
  // truthy-but-non-array value (devtools / extension / sync
  // corruption); without the Array.isArray check, every downstream
  // `.findIndex/.filter/.some/.unshift` crashes on first interaction.
  try {
    const arr = JSON.parse(localStorage.getItem(KEY) || "[]");
    return Array.isArray(arr) ? arr : [];
  } catch {
    return [];
  }
}

function _persist(arr) {
  // Wrap setItem because localStorage throws QuotaExceededError on full
  // storage AND throws SecurityError in private-browsing WebViews (where
  // localStorage is sometimes wired through but quota-zero). Without the
  // try/catch, the throw propagates out of toggle() to the calling event
  // handler, which has no error surface — user clicks "Save search" and
  // nothing visible happens, no toast, no log. Matches the pattern used
  // by bookmarks.js, recents.js, filters.js, theme.js, and updater.js
  // (every other localStorage writer in the codebase had this catch
  // EXCEPT saved.js — the recents.js comment even claimed "Matches the
  // pattern used by ... saved.js (`_persist`)" but saved.js had drifted
  // since then). Update state.saved unconditionally so the in-memory
  // representation stays current even when the disk write fails — the
  // session continues working, only persistence-across-restart breaks.
  const trimmed = arr.slice(0, LIMIT);
  // Previously the catch was bare `catch {}` — silent. The
  // session kept working (state.saved updated unconditionally below)
  // but persistence-across-restart broke with ZERO trail. A support
  // thread with "my saved searches keep disappearing" had nothing
  // to diagnose against. console.error gives WebKit devtools (and
  // the macOS Web Inspector when attached) the QuotaExceededError /
  // SecurityError class name + stack so the user can read the
  // failure out loud during a support call. Matches the same
  // diagnostic-trail pattern empty.js gained in commit f8f2e19 for
  // its /api/files catch.
  try { localStorage.setItem(KEY, JSON.stringify(trimmed)); }
  catch (e) { console.error("saved-search persist failed", e); }
  state.saved = trimmed;
}

export function loadSaved() {
  const arr = _load();
  state.saved = arr;
  return arr;
}

export function isSaved(query) {
  const q = (query || "").trim();
  if (!q) return false;
  return _load().some(s => s.query === q);
}

export function toggle(query, hits = null, scope = null) {
  const q = (query || "").trim();
  if (!q) return false;
  let arr = _load();
  const idx = arr.findIndex(s => s.query === q);
  if (idx >= 0) {
    arr.splice(idx, 1);
    _persist(arr);
    return false; // now unsaved
  }
  // Snapshot the active scope filters so re-running this saved search
  // restores the user's intent. Without this, saving "pricing" with
  // Speech-only scope and re-running it tomorrow under default scope
  // would return DIFFERENT (more) results — the saved entry would be
  // a misleading label for whatever-the-current-scope-decides-today.
  // scope is optional + backward-compatible: missing scope on legacy
  // entries means "use whatever is currently active" at re-run time.
  const entry = { query: q, hits, ts: Date.now() };
  if (scope && typeof scope === "object") {
    if (Array.isArray(scope.sources)) entry.sources = [...scope.sources];
    if (scope.folder != null) entry.folder = scope.folder;
  }
  arr.unshift(entry);
  _persist(arr);
  return true; // now saved
}

export function remove(query) {
  const q = (query || "").trim();
  if (!q) return;
  const arr = _load().filter(s => s.query !== q);
  _persist(arr);
}

// Returns the snapshotted scope for a saved query, or null if either the
// query isn't saved OR the saved entry predates the scope-snapshot feature
// (legacy entries with only {query, hits, ts}). Callers should treat null
// as "no override — keep current scope".
export function getSavedScope(query) {
  const q = (query || "").trim();
  if (!q) return null;
  const entry = _load().find(s => s.query === q);
  if (!entry) return null;
  if (entry.sources == null && entry.folder == null) return null;
  return {
    sources: Array.isArray(entry.sources) ? [...entry.sources] : null,
    folder:  entry.folder ?? null,
  };
}
