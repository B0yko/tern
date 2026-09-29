// app/modules/bookmarks.js — per-moment bookmarks.
// Different from Saved Searches (which save a query string): a bookmark
// pins a SPECIFIC moment (file_id + ts_ms) so you can jump back to it.
// localStorage: tern.bookmarks.v1.

import { state } from "/modules/state.js";

const KEY = "tern.bookmarks.v1";
const LIMIT = 100;

function _load() {
  // Shape-guard — see recents.js comment. localStorage values surviving
  // the try/catch can still be non-array (`"null"`, `"true"`, `"{}"`)
  // and would crash downstream `.some/.findIndex/.unshift` calls.
  try {
    const arr = JSON.parse(localStorage.getItem(KEY) || "[]");
    return Array.isArray(arr) ? arr : [];
  } catch { return []; }
}
function _persist(arr) {
  const trimmed = arr.slice(0, LIMIT);
  // Same diagnostic-trail rationale as saved.js _persist — otherwise
  // a QuotaExceededError or SecurityError vanishes into a bare
  // catch{}. The in-memory state.bookmarks stayed current so the
  // session looked fine, but bookmarks silently disappeared on next
  // app launch. Surface the failure to WebKit devtools so support
  // diagnostics ("open Inspector → Console") has something to read.
  // Matches empty.js + saved.js.
  try { localStorage.setItem(KEY, JSON.stringify(trimmed)); }
  catch (e) { console.error("bookmark persist failed", e); }
  state.bookmarks = trimmed;
}

export function loadBookmarks() {
  const arr = _load();
  state.bookmarks = arr;
  return arr;
}

export function isBookmarked(file_id, ts_ms) {
  if (!file_id) return false;
  return _load().some(b => b.file_id === file_id && Math.abs(b.ts_ms - ts_ms) < 1000);
}

export function toggle(hit) {
  if (!hit || !hit.file_id) return false;
  const arr = _load();
  const idx = arr.findIndex(b =>
    b.file_id === hit.file_id && Math.abs(b.ts_ms - hit.ts_ms) < 1000
  );
  if (idx >= 0) {
    arr.splice(idx, 1);
    _persist(arr);
    return false;
  }
  arr.unshift({
    file_id: hit.file_id,
    file_path: hit.file_path,
    file_name: hit.file_name,
    ts_ms: hit.ts_ms,
    timecode: hit.timecode,
    snippet: hit.snippet,
    media_kind: hit.media_kind,
    preview_url: hit.preview_url,
    thumbnail_url: hit.thumbnail_url,
    label: null,                                    // user can rename later (v1.1)
    ts: Date.now(),
  });
  _persist(arr);
  return true;
}

export function remove(file_id, ts_ms) {
  const arr = _load().filter(b =>
    !(b.file_id === file_id && Math.abs(b.ts_ms - ts_ms) < 1000)
  );
  _persist(arr);
}
