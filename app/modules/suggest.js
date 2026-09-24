// app/modules/suggest.js — search-input suggestions dropdown.
// Shows up to 6 matching saved + recent queries while the user is typing,
// before any results have come back. Click → run that query. ↓ on input
// focuses the first suggestion; Enter selects it.

import { state, subscribe } from "/modules/state.js";
import { icons } from "/modules/icons.js";

const MAX = 6;

let _input = null;
let _dropdown = null;
let _items = [];
let _highlightIdx = -1;

function _matches(q) {
  const needle = q.toLowerCase().trim();
  if (!needle) return [];
  const seen = new Set();
  const out = [];
  // Saved first (higher signal) then recents
  for (const list of [(state.saved || []).map(s => ({ ...s, kind: "saved" })),
                      (state.recents || []).map(r => ({ ...r, kind: "recent" }))]) {
    for (const item of list) {
      const q = item.query.toLowerCase();
      if (q === needle) continue; // already what user typed
      if (q.includes(needle) && !seen.has(item.query)) {
        seen.add(item.query);
        out.push(item);
        if (out.length >= MAX) return out;
      }
    }
  }
  return out;
}

function _escapeHtml(s) {
  // String(s == null ? "" : s) so number / boolean inputs (`it.hits` is
  // a count) coerce safely. Original `(s || "")` returned 5 unchanged
  // for the int 5 → `5.replace(...)` → TypeError. Also: 0 used to
  // collapse to "" (truthy check), which would have HIDDEN a legitimate
  // zero-count hit. Matches the canonical pattern in sidebar.js / detail.js.
  return String(s == null ? "" : s).replace(/[&<>"']/g, c =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
function _escapeRe(s) { return s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"); }

// Bold the matched substring inside the suggestion text — Spotlight/
// Raycast convention. Falls back to plain escape if there's no needle.
function _highlightMatch(text, needle) {
  const esc = _escapeHtml(text);
  if (!needle || !needle.trim()) return esc;
  const re = new RegExp("(" + _escapeRe(needle.trim()) + ")", "gi");
  return esc.replace(re, "<strong>$1</strong>");
}

function _render() {
  if (!_items.length) {
    _dropdown.hidden = true;
    return;
  }
  const needle = (_input?.value || "").trim();
  _dropdown.innerHTML = _items.map((it, i) => `
    <button class="suggest-item ${i === _highlightIdx ? "on" : ""}" data-idx="${i}">
      <span class="suggest-glyph">${it.kind === "saved" ? icons.starFilled({ w: 11, h: 11 }) : icons.search({ w: 11, h: 11 })}</span>
      <span class="suggest-text">${_highlightMatch(it.query, needle)}</span>
      ${it.hits != null ? `<span class="suggest-hits">${_escapeHtml(it.hits)}</span>` : ""}
    </button>
  `).join("");
  _dropdown.querySelectorAll(".suggest-item").forEach(btn => {
    btn.addEventListener("mousedown", (ev) => {
      // mousedown not click — input loses focus on click which would hide
      // the dropdown before this fires.
      ev.preventDefault();
      _select(parseInt(btn.dataset.idx, 10));
    });
    btn.addEventListener("mouseenter", () => {
      _highlightIdx = parseInt(btn.dataset.idx, 10);
      _refreshHighlight();
    });
  });
  _dropdown.hidden = false;
}

function _refreshHighlight() {
  _dropdown.querySelectorAll(".suggest-item").forEach((b, i) => {
    b.classList.toggle("on", i === _highlightIdx);
  });
}

function _select(idx) {
  const item = _items[idx];
  if (!item) return;
  _input.value = item.query;
  _dropdown.hidden = true;
  _items = [];
  _input.dispatchEvent(new Event("input", { bubbles: true }));
}

export function initSuggest() {
  _input = document.getElementById("search-input");
  // Mount dropdown right below the search wrap
  _dropdown = document.createElement("div");
  _dropdown.className = "suggest-dropdown";
  _dropdown.id = "suggest-dropdown";
  _dropdown.hidden = true;
  // Attach to body (overlay above everything)
  document.body.appendChild(_dropdown);

  function _reposition() {
    const wrap = document.querySelector(".search-wrap");
    if (!wrap) return;
    const r = wrap.getBoundingClientRect();
    _dropdown.style.position = "fixed";
    _dropdown.style.top = (r.bottom + 4) + "px";
    _dropdown.style.left = r.left + "px";
    _dropdown.style.width = r.width + "px";
  }
  window.addEventListener("resize", _reposition);
  _reposition();

  _input.addEventListener("input", () => {
    _reposition();
    _items = _matches(_input.value);
    _highlightIdx = -1;
    _render();
  });
  _input.addEventListener("focus", () => {
    _reposition();
    if (_input.value) {
      _items = _matches(_input.value);
      _render();
    }
  });
  _input.addEventListener("blur", () => {
    // Tiny delay so click on suggestion fires before hide
    setTimeout(() => { _dropdown.hidden = true; }, 150);
  });
  _input.addEventListener("keydown", (ev) => {
    if (_dropdown.hidden || !_items.length) return;
    // Each branch must stopPropagation in addition to preventDefault.
    // keyboard.js attaches its global dispatcher to `window`, so any
    // event bubbling up from the input gets a second handler that
    // dispatches RESULT_UP / RESULT_DOWN / RESULT_OPEN / RESULT_CLEAR.
    // Without stopping propagation, pressing ↓ to navigate suggestions
    // ALSO moves the result-list selection down by one (and Enter on a
    // highlighted suggestion fires RESULT_OPEN, focusing the player
    // for whatever is selected in the stale result list, instead of
    // running the suggestion's query). The user sees both jumps and
    // the wrong thing plays.
    if (ev.key === "ArrowDown") {
      ev.preventDefault();
      ev.stopPropagation();
      _highlightIdx = Math.min(_items.length - 1, _highlightIdx + 1);
      _refreshHighlight();
    } else if (ev.key === "ArrowUp") {
      ev.preventDefault();
      ev.stopPropagation();
      _highlightIdx = Math.max(-1, _highlightIdx - 1);
      _refreshHighlight();
    } else if (ev.key === "Enter" && _highlightIdx >= 0) {
      ev.preventDefault();
      ev.stopPropagation();
      _select(_highlightIdx);
    } else if (ev.key === "Escape") {
      ev.preventDefault();
      ev.stopPropagation();
      _dropdown.hidden = true;
      _items = [];
      _highlightIdx = -1;
    }
  });
  subscribe((k) => {
    if (k === "recents" || k === "saved") {
      if (!_dropdown.hidden) {
        _items = _matches(_input.value);
        _render();
      }
    }
  });
}
