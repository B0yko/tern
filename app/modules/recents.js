// app/modules/recents.js — recent searches store, localStorage-backed.
import { state } from "/modules/state.js";

const KEY = "tern.recents.v1";
const LIMIT = 12;

export function loadRecents() {
  // Shape-guard the parse. `JSON.parse("null")` returns null (truthy
  // localStorage value bypassed our `|| "[]"` fallback); devtools /
  // extensions / iCloud Safari sync corruption have all been seen in the
  // wild writing non-array values under our key. Without the
  // Array.isArray check, every downstream `.filter/.unshift/.some` call
  // crashes on first interaction.
  try {
    const arr = JSON.parse(localStorage.getItem(KEY) || "[]");
    const safe = Array.isArray(arr) ? arr : [];
    state.recents = safe;
    return safe;
  } catch {
    state.recents = [];
    return [];
  }
}

export function addRecent(query, hits) {
  const q = query.trim();
  if (!q) return;
  // Drop any prior entry that's a STRICT PREFIX of the new query (case-
  // insensitive, length < new). Topbar's 180ms search debounce only swallows
  // characters typed within 180ms of each other; pause to think between
  // "pricing" and "strategy" and BOTH searches fire, leaving the recents
  // list polluted with the intermediate prefix. Removing prefixes when the
  // longer form lands collapses ["pricing", "pricing strategy"] down to
  // just the meaningful query. Strict (length < new) so deleting characters
  // doesn't wipe the longer historical query — the user shortening their
  // query isn't a signal to forget the previous longer one.
  // Also drops the exact-match (filter handles both via the `.toLowerCase()
  // + .startsWith()` check covering equal-length-and-equal too via the
  // exact-match branch).
  const qLower = q.toLowerCase();
  const arr = loadRecents().filter(r => {
    if (!r.query) return false;
    const rLower = r.query.toLowerCase();
    if (rLower === qLower) return false;
    // Drop strictly-shorter prefix of the new query
    if (rLower.length < qLower.length && qLower.startsWith(rLower + " ")) return false;
    return true;
  });
  arr.unshift({ query: q, hits, ts: Date.now() });
  const trimmed = arr.slice(0, LIMIT);
  // Wrap setItem because localStorage throws QuotaExceededError on
  // quota overflow (rare on macOS WKWebView at 5 MB default but
  // possible if some other module ever writes a giant payload to a
  // shared origin). Without the wrap, the throw bubbles out of
  // addRecent — called every successful search — and the search
  // flow breaks. Matches the pattern used by bookmarks.js (`_persist`),
  // prefs.js (`_persist`), and saved.js (`_persist`).
  // Diagnostic-trail addition (commit 8cef452 sibling): surface the
  // actual exception class to WebKit devtools. Recents is the
  // HIGHEST-frequency writer of the lot (fires on every successful
  // search), so it's the most likely to be the writer that finally
  // trips quota after months of unique searches — and the silent
  // catch made that exact failure mode invisible.
  try { localStorage.setItem(KEY, JSON.stringify(trimmed)); }
  catch (e) { console.error("recents persist failed", e); }
  state.recents = trimmed;
}

export function clearRecents() {
  try { localStorage.removeItem(KEY); }
  catch (e) { console.error("recents clear failed", e); }
  state.recents = [];
}

// Per-item remove — used by sidebar's right-click "Remove from Recents"
// menu item. Returns true if the entry was removed, false if it wasn't
// in the list. Idempotent; safe to call on a query that's already gone.
export function removeRecent(query) {
  const q = (query || "").trim();
  if (!q) return false;
  const arr = loadRecents();
  const next = arr.filter(r => (r.query || "").trim() !== q);
  if (next.length === arr.length) return false;
  try { localStorage.setItem(KEY, JSON.stringify(next)); }
  catch (e) { console.error("recents remove-one persist failed", e); }
  state.recents = next;
  return true;
}
