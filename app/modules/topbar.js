// app/modules/topbar.js
// Wires the search input + result count + sidebar toggle + gear button.
import { state, subscribe } from "/modules/state.js";
import { on, INTENTS } from "/modules/keyboard.js";
import { api } from "/modules/api.js";
import { addRecent } from "/modules/recents.js";
import { isSaved, toggle as toggleSaved } from "/modules/saved.js";
import { icons } from "/modules/icons.js";
import { toggleFilterPopover, isAllActive as isAllSourcesActive } from "/modules/filters.js";

let _searchTimer = null;
// Monotonic id used to discard results from an out-of-order in-flight search.
// Without this, typing "a" → "ab" rapidly can race: "ab" returns first, then
// "a" overwrites with stale results.
let _searchReqId = 0;

// Per-keystroke AbortController for in-flight search requests. Without
// this, search-as-you-type was wasteful: typing "pricing" fired 7
// requests, the _searchReqId guard discarded the 6 stale RESPONSES,
// but the backend STILL ran each one (SigLIP text embed + Chroma
// vector query + transcript/OCR FTS = ~200-400 ms backend CPU per
// fetch). With abort, the previous fetch is cancelled mid-flight;
// FastAPI/Starlette cancels the handler when the client disconnects,
// so subsequent awaits short-circuit and we stop burning backend
// cycles on the superseded query.
let _searchAbort = null;

async function _runSearch(query) {
  state.query = query;
  if (!query.trim()) {
    state.results = [];
    state.selectedIndex = -1;
    state.selectedHit = null;
    // Also abort any in-flight search — empty query means the user
    // cleared the input, the old result is gone, no point finishing.
    if (_searchAbort) { _searchAbort.abort(); _searchAbort = null; }
    return;
  }
  const myReq = ++_searchReqId;
  state.isSearching = true;
  // Abort the previous search before firing the new one. The
  // _searchReqId race-guard below is still needed (covers the case
  // where the previous fetch's headers landed BEFORE the abort
  // propagated, so the catch path runs without AbortError).
  if (_searchAbort) _searchAbort.abort();
  _searchAbort = new AbortController();
  const mySignal = _searchAbort.signal;
  // Round-trip timer for the results-pill latency badge. performance.
  // now() is monotonic and unaffected by wall-clock changes (the
  // request timeouts already lean on it). We measure CLIENT-OBSERVED
  // latency including the localhost fetch, JSON parse, and any
  // backend asyncio.to_thread offload — which is what the buyer
  // perceives as "search speed", not whatever ms the engine reports
  // internally. Stored as state.lastSearchMs (int) for results.js to
  // render in the bulk-results pill, reinforcing the homepage's
  // "find any moment in 3 seconds" claim with proof at the moment
  // of use.
  const t0 = performance.now();
  try {
    const r = await api.search(query, {
      sources: state.sources,
      folder: state.folderFilter,
      signal: mySignal,
    });
    if (myReq !== _searchReqId) return; // a newer query supersedes this one
    state.lastSearchMs = Math.round(performance.now() - t0);
    state.results = r.hits || [];
    state.selectedIndex = r.hits && r.hits.length ? 0 : -1;
    state.selectedHit = r.hits && r.hits.length ? r.hits[0] : null;
    if (r.hits && r.hits.length) addRecent(query, r.hits.length);
  } catch (e) {
    if (myReq !== _searchReqId) return;
    // AbortError is the expected outcome when the next keystroke
    // cancelled us. Don't toast — the user already moved on.
    if (e?.name === "AbortError" || /aborted/i.test(e?.message || "")) {
      return;
    }
    console.error("search failed", e);
    state.results = [];
    // Surface the failure as a toast so the user knows the empty
    // results pane is from a backend error, not "0 matches". Previously
    // results.js' empty-state message ("No moments matched X") was
    // misleading on backend errors. Lazy-import toast to keep
    // topbar.js' static graph free of optional deps.
    const msg = e?.message || String(e);
    import("/modules/toast.js")
      .then(m => m.flashToast(`Search failed: ${msg}`, { kind: "err", ttl: 3500 }))
      .catch(() => {});
  } finally {
    if (myReq === _searchReqId) state.isSearching = false;
  }
}

const PERSIST_KEY = "tern.lastQuery.v1";

// Rotating placeholder examples — hint at the kinds of queries Tern
// handles (speech / OCR / visual scene) so new users see what to try.
// Stops rotating the moment the user types or focuses the input;
// resumes if they clear it and click away.
// Each example MUST match the bundled demo workspace so a trial user
// who copies a placeholder into the search box always gets hits.
// "slide with $29/month" lived here for a while but matched ZERO content
// in the demo (no slide in the demo has that exact pricing text) — so
// a trial buyer who took the placeholder at face value typed it in,
// saw "0 results," and walked away thinking the OCR channel didn't
// work. Replaced with "Stanford" which matches 10 OCR rows from the
// Stanford CS183B / Y Combinator slides bundled in the demo
// (verified via SELECT COUNT(*) FROM ocr_segments WHERE text LIKE).
// Keeps the OCR-channel signal in the placeholder rotation — that's
// the whole reason the example exists.
const PLACEHOLDER_EXAMPLES = [
  "Find any moment…",
  "pricing strategy",
  "Series A funding",
  "orange cat",
  "mountain lake",
  "Stanford",
  "first hire",
  "person at a whiteboard",
  // Quoted-phrase placeholder. Tern's sanitize_fts_query (service_
  // pipeline/tern/search.py) preserves user-supplied double quotes
  // through to FTS5's phrase-match operator, so "customer success"
  // finds the exact two-word sequence (not the union of "customer"
  // and "success" within the same segment). The capability has
  // shipped since v1.0 but was silently buried in the search engine
  // — no UI surface mentioned it until the commit 8603702 FAQ
  // entry. Rotating it through the placeholder slot gives every
  // trial user the "wait, quotes work?" lightbulb before they
  // bounce assuming Tern is a naive bag-of-words tool. Matches
  // 4 transcript rows + 12 OCR rows in the bundled demo (verified
  // via the SELECT COUNT above), so the placeholder pin test
  // (test_every_placeholder_example_matches_demo_workspace) still
  // passes — its tokenizer strips the quotes and matches "customer"
  // alone against the demo content.
  "\"customer success\"",
];
const PLACEHOLDER_PERIOD_MS = 2800;

let _placeholderTimer = null;
let _placeholderIdx = 0;

function _startPlaceholderRotation(input) {
  if (!input) return;
  if (_placeholderTimer) return; // already running
  // First example is the prompt "Find any moment…" — keep it as the
  // initial state so the rotation effect feels intentional, not random.
  input.placeholder = PLACEHOLDER_EXAMPLES[_placeholderIdx];
  _placeholderTimer = setInterval(() => {
    _placeholderIdx = (_placeholderIdx + 1) % PLACEHOLDER_EXAMPLES.length;
    input.placeholder = PLACEHOLDER_EXAMPLES[_placeholderIdx];
  }, PLACEHOLDER_PERIOD_MS);
}

function _stopPlaceholderRotation() {
  if (_placeholderTimer) {
    clearInterval(_placeholderTimer);
    _placeholderTimer = null;
  }
}

export function initTopbar() {
  const input = document.getElementById("search-input");
  const count = document.getElementById("search-count");

  // Restore last query on launch — keeps the user where they left off.
  try {
    const last = localStorage.getItem(PERSIST_KEY);
    if (last && last.trim()) {
      input.value = last;
      // Fire after main.js wires the listener below; setTimeout(0) is enough.
      setTimeout(() => _runSearch(last), 50);
    }
  } catch (e) { console.error("topbar last-query restore failed", e); }

  input.addEventListener("input", (ev) => {
    clearTimeout(_searchTimer);
    _stopPlaceholderRotation();
    const v = ev.target.value;
    // Persist the in-flight query on every keystroke — fires the most
    // frequently of any localStorage writer in the codebase. Commit
    // 95103ca added console.error to every other writer's silent catch;
    // this one was missed because that commit's grep targeted
    // _persist-named helpers. Same diagnostic-trail rationale: WebKit
    // devtools needs the exception class name to distinguish a quota-
    // overflow QuotaExceededError from a SecurityError (private-
    // browsing WebView) or a transient I/O failure, so support can
    // ask "open Inspector → Console" and get an actionable answer.
    try { localStorage.setItem(PERSIST_KEY, v); }
    catch (e) { console.error("topbar last-query persist failed", e); }
    _searchTimer = setTimeout(() => _runSearch(v), 180);
  });
  // Pause rotation while focused (cursor in the box is its own visual
  // cue; rotating placeholder underneath flashes distractingly).
  // Resume when blurred AND empty.
  input.addEventListener("focus", _stopPlaceholderRotation);
  input.addEventListener("blur", () => {
    if (!input.value.trim()) _startPlaceholderRotation(input);
  });
  // Kick off the rotation now if the input is empty (i.e., no persisted
  // last query). The interval is 2.8s — slow enough to read each one.
  if (!input.value.trim()) _startPlaceholderRotation(input);

  // ⌘K / "/" — universal Spotlight/Raycast/Linear convention to focus
  // the search box from anywhere in the app. keyboard.js dispatched the
  // INTENT but no module subscribed, so the shortcut was silently a
  // no-op — and the in-app keyhelp listed it as supported (a classic
  // broken-promise UX gap). select() so the user can either keep
  // editing their current query OR overwrite with a single keystroke
  // (the more common case after ⌘K from somewhere else in the UI).
  on(INTENTS.FOCUS_SEARCH, () => {
    input.focus();
    input.select();
  });

  on(INTENTS.RESULT_CLEAR, () => {
    if (input.value) {
      input.value = "";
      _runSearch("");
    } else {
      input.focus();
    }
  });

  // React to result count
  const starBtn = document.getElementById("btn-save-search");
  const starIcon = document.getElementById("icon-star");
  function _refreshStar() {
    const q = (state.query || "").trim();
    starBtn.hidden = !q;
    if (!q) return;
    const saved = isSaved(q);
    starIcon.innerHTML = (saved ? icons.starFilled : icons.starOutline)({ w: 16, h: 16 });
    starBtn.title = saved ? `Remove from Saved (⌘D)` : `Save this search (⌘D)`;
    starBtn.style.color = saved ? "var(--accent)" : "";
  }
  const filterBtn = document.getElementById("btn-filters");
  function _refreshFilters() {
    const hasQuery = !!state.query.trim();
    filterBtn.hidden = !hasQuery;
    if (!hasQuery) return;
    const allOn = isAllSourcesActive();
    filterBtn.style.color = allOn ? "" : "var(--accent)";
    // Compose a more informative tooltip — name the specific restriction
    // instead of generic "filtered" so the user knows whether to dismiss.
    if (allOn) {
      filterBtn.title = "Search scope (all sources on)";
    } else {
      const parts = [];
      if (Array.isArray(state.sources) && state.sources.length < 3) {
        const labels = { transcript: "Speech", ocr: "On-screen", visual: "Visual" };
        parts.push("only " + state.sources.map(s => labels[s] || s).join(" + "));
      }
      if (state.folderFilter) {
        const f = state.folderFilter;
        parts.push("in " + (f.length > 30 ? "…" + f.slice(-28) : f));
      }
      filterBtn.title = "Search scope · " + (parts.join(" · ") || "filtered");
    }
    // Active-filter badge — small dot at the top-right corner when scope
    // is restricted. Easier to notice than the icon color shift alone.
    let badge = filterBtn.querySelector(".filter-badge");
    if (!allOn) {
      if (!badge) {
        badge = document.createElement("span");
        badge.className = "filter-badge";
        filterBtn.appendChild(badge);
      }
    } else if (badge) {
      badge.remove();
    }
  }
  filterBtn.addEventListener("click", (ev) => {
    ev.stopPropagation();
    toggleFilterPopover(filterBtn);
  });

  // Pulse the magnifying-glass icon while a search is in-flight. The
  // icon → spinner swap-out is just a CSS class; SVG stays the same.
  // Off by default; turned on when state.isSearching flips true.
  const searchIcon = document.getElementById("icon-search");
  function _setSearching(on) {
    if (!searchIcon) return;
    searchIcon.classList.toggle("searching", !!on);
  }

  subscribe((k) => {
    if (k === "isSearching") {
      _setSearching(state.isSearching);
      // While a new search is in flight, state.results still holds the
      // PREVIOUS query's hits — the count won't refresh until the new
      // results land. Hiding the count during the search avoids
      // showing a stale "14" while the user has already typed the
      // next query and is waiting for its results. The "results"
      // subscriber below restores visibility + populates the new
      // count the moment the fetch returns; the only visible window
      // is the in-flight ~50-200ms, where the pulsing search icon
      // already signals "working". Net effect: no more stale-count
      // mismatch during search-as-you-type.
      if (state.isSearching) count.hidden = true;
    }
    if (k === "results") {
      const n = state.results.length;
      count.hidden = !state.query;
      // Per-source breakdown — surfaces WHERE the hits are coming from
      // without leaving the topbar. Each hit's `sources` array can list
      // multiple kinds (multi-source matches), so summed counts can
      // exceed `n`; that's informative, not a bug. Show inline tag-line
      // when ≥2 sources contributed; otherwise just the number, like
      // before. Also set a `title` for the full breakdown on hover.
      if (state.query && n > 0) {
        const counts = { transcript: 0, ocr: 0, visual: 0 };
        for (const h of state.results) {
          const srcs = h.sources && h.sources.length ? h.sources : [h.source];
          for (const s of srcs) {
            if (s in counts) counts[s]++;
          }
        }
        // Colored dots match the filter popover's per-source colors
        // (filters.js: transcript=#0a84ff, ocr=#ff9500, visual=#34c759).
        // Wrapped in <span class="search-count-pill"> so the CSS can
        // dim them when only one source is active, and so they don't
        // bleed monospace styling from the parent span.
        const colors = { transcript: "#0a84ff", ocr: "#ff9500", visual: "#34c759" };
        const sourcesShown = Object.entries(counts).filter(([, v]) => v > 0);
        if (sourcesShown.length >= 2) {
          const dots = sourcesShown
            .map(([k, v]) => `<span class="search-count-pill" style="--c:${colors[k]}">${v}</span>`)
            .join("");
          count.innerHTML = `<span class="search-count-n">${n}</span>${dots}`;
        } else {
          count.textContent = `${n}`;
        }
        const labels = { transcript: "speech", ocr: "on-screen", visual: "visual" };
        const parts = sourcesShown.map(([k, v]) => `${v} ${labels[k]}`);
        count.title = parts.length >= 2
          ? `${n} results · ${parts.join(", ")}`
          : `${n} result${n === 1 ? "" : "s"}`;
      } else {
        count.textContent = "";
        count.removeAttribute("title");
      }
    }
    if (k === "query" || k === "saved") _refreshStar();
    if (k === "query") _refreshFilters();
    if (k === "sources" || k === "folderFilter") {
      _refreshFilters();
      // Re-run the current query immediately on scope change.
      if (state.query.trim()) {
        clearTimeout(_searchTimer);
        _searchTimer = setTimeout(() => _runSearch(state.query), 50);
      }
    }
  });
  _refreshFilters();
  // Build a scope-snapshot to pin into the saved entry — sources +
  // folderFilter at the moment of saving. Re-running this saved search
  // later restores the user's intent instead of silently flipping the
  // result set based on whatever scope is current.
  const _scopeSnapshot = () => ({
    sources: Array.isArray(state.sources) ? state.sources : null,
    folder: state.folderFilter || null,
  });
  starBtn.addEventListener("click", () => {
    const q = (state.query || "").trim();
    if (!q) return;
    toggleSaved(q, state.results?.length || 0, _scopeSnapshot());
    _refreshStar();
  });
  on(INTENTS.SAVE_SEARCH, () => {
    const q = (state.query || "").trim();
    if (!q) return;
    toggleSaved(q, state.results?.length || 0, _scopeSnapshot());
    _refreshStar();
  });
  _refreshStar();

  // Sidebar toggle
  document.getElementById("btn-toggle-sidebar").addEventListener("click", () => {
    state.sidebarOpen = !state.sidebarOpen;
  });
  on(INTENTS.TOGGLE_SIDEBAR, () => { state.sidebarOpen = !state.sidebarOpen; });

  // Gear → prefs
  document.getElementById("btn-prefs").addEventListener("click", () => {
    document.dispatchEvent(new CustomEvent("tern:toggle-prefs"));
  });
  on(INTENTS.PREFERENCES, () => document.dispatchEvent(new CustomEvent("tern:toggle-prefs")));

  // Focus input on launch
  input.focus();
}
