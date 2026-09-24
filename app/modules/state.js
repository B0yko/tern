// app/modules/state.js
// Tiny pub-sub store. Single source of truth for UI state.
// Subscribers are called synchronously after each set().

const _state = {
  query: "",
  results: [],         // SearchHitJSON[]
  selectedIndex: -1,
  selectedHit: null,
  sidebarOpen: false,
  isSearching: false,
  isIndexing: false,
  stats: null,         // /api/stats response
  recents: [],         // [{ query, hits, ts }]
  saved: [],           // [{ query, hits, ts }] — pinned (modules/saved.js)
  prefs: null,         // see modules/theme.js
  sources: ["transcript", "ocr", "visual"],  // active search scope (modules/filters.js)
  folderFilter: null,  // null = all folders; string prefix = limit search to that folder
  bookmarks: [],       // [{file_id, ts_ms, file_name, timecode, snippet, ...}] — see modules/bookmarks.js
  lastSearchMs: null,  // client-observed round-trip ms for the most recent /api/search — see modules/topbar.js _doSearch; rendered in the results-pill latency badge (modules/results.js) as proof of the homepage "3 seconds" promise
};

const _subs = new Set();

export const state = new Proxy(_state, {
  set(t, k, v) {
    t[k] = v;
    // Per-subscriber try/catch — without it, a throw in ONE subscriber
    // (e.g., a stale DOM ref in modules/sidebar.js after a re-render,
    // an unguarded localStorage access in modules/theme.js on a private-
    // browsing WKWebView) propagates through `forEach` and:
    //   1. silently skips subscribers later in the Set iteration order
    //      (visible as the indexing toast freezing, recents not
    //      refreshing, etc. depending on which subscriber threw)
    //   2. propagates the throw out of `set()` to the original
    //      `state.x = y` call site, where almost no caller has
    //      try/catch around an assignment — so the entire async
    //      handler that triggered the update stops mid-execution
    // logging at console.warn so a subscriber regression is visible in
    // DevTools without taking down the page.
    _subs.forEach(fn => {
      try { fn(k, v); }
      catch (e) { console.warn("[state] subscriber error on key", k, e); }
    });
    return true;
  },
});

export function subscribe(fn) {
  _subs.add(fn);
  return () => _subs.delete(fn);
}

export function snapshot() {
  return { ..._state };
}
