// app/modules/filters.js — search-scope popover (Speech / On-screen / Visual).
// All three are on by default. The popover is anchored to the small ▼ next
// to the result-count chip in the topbar; toggling any pill immediately
// re-runs the current query (via state.sources subscriber in topbar.js).
//
// Persistence: localStorage key tern.sources.v1 (so the user's scope
// preference survives reloads).
import { state, subscribe } from "/modules/state.js";
import { icons } from "/modules/icons.js";
import { on, INTENTS } from "/modules/keyboard.js";
import { api } from "/modules/api.js";

const KEY = "tern.sources.v1";
const ALL = ["transcript", "ocr", "visual"];
const META = {
  transcript: { label: "Speech",     hint: "Spoken words in audio + video",  color: "#0a84ff" },
  ocr:        { label: "On-screen",  hint: "Text in keyframes (OCR)",        color: "#ff9500" },
  visual:     { label: "Visual",     hint: "What's in the image / scene",    color: "#34c759" },
};

let _pop = null;
let _open = false;

// HTML escape (text + attribute safe). Previous code only escaped the
// attribute quote in attributes and stripped angle-brackets-and-ampersand
// in text — both insufficient: f.path is a filesystem path that can
// legitimately contain `<`, `>`, `&`, `'`, and the bare-text
// interpolation in <option>… was a real XSS sink if a user indexed a
// folder like `…/<img onerror=…>/`.
function _esc(s) {
  return String(s == null ? "" : s).replace(/[&<>"']/g, c =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function _load() {
  try {
    const arr = JSON.parse(localStorage.getItem(KEY) || "null");
    return Array.isArray(arr) && arr.length ? arr.filter(s => ALL.includes(s)) : [...ALL];
  } catch { return [...ALL]; }
}

function _save() {
  // Diagnostic-trail addition: surface the
  // QuotaExceededError / SecurityError class to WebKit devtools so
  // a user reporting "my Speech/On-screen/Visual scope keeps
  // resetting on restart" has SOMETHING to read in Inspector
  // instead of a silent disk-write failure.
  try { localStorage.setItem(KEY, JSON.stringify(state.sources)); }
  catch (e) { console.error("filters persist failed", e); }
}

export function loadSources() {
  state.sources = _load();
}

export function isAllActive() {
  return state.sources
    && state.sources.length === ALL.length
    && ALL.every(s => state.sources.includes(s))
    && !state.folderFilter;
}

function _toggle(name) {
  const set = new Set(state.sources || ALL);
  if (set.has(name)) {
    if (set.size === 1) return; // never zero — would be empty result by spec
    set.delete(name);
  } else {
    set.add(name);
  }
  // Preserve canonical order
  state.sources = ALL.filter(s => set.has(s));
  _save();
  _render();
}

// Public toggle for keyboard shortcuts (⌥1/2/3 in keyboard.js).
// Also dispatches a tiny toast so the user sees what flipped without
// needing to open the filter popover. Re-uses the popover toggle logic.
export function toggleSource(name) {
  if (!ALL.includes(name)) return;
  _toggle(name);
  const labels = { transcript: "Speech", ocr: "On-screen", visual: "Visual" };
  const on = (state.sources || []).includes(name);
  // Toast import is lazy so initFilters doesn't drag toast.js into
  // the cold-start chain when no one ever fires this.
  import("/modules/toast.js").then(m => {
    m.flashToast(`${labels[name]}: ${on ? "on" : "off"}`, { kind: on ? "ok" : "info" });
  }).catch(() => {});
}

function _renderItems() {
  const set = new Set(state.sources || ALL);
  return ALL.map(name => {
    const m = META[name];
    const on = set.has(name);
    return `
      <button class="filter-item ${on ? "on" : "off"}" data-name="${name}" role="switch" aria-checked="${on}">
        <span class="filter-dot" style="background:${on ? m.color : 'var(--text-4)'};"></span>
        <span class="filter-label">${m.label}</span>
        <span class="filter-hint">${m.hint}</span>
        <span class="filter-check">${on ? icons.starFilled({ w: 11, h: 11 }) : ""}</span>
      </button>
    `;
  }).join("");
}

function _render() {
  if (!_pop) return;
  const allOn = isAllActive();
  const folderActive = !!state.folderFilter;
  _pop.innerHTML = `
    <div class="filter-title">
      <span>Scope</span>
      ${(!allOn || folderActive) ? `<button class="filter-reset" id="filter-reset">Reset</button>` : ""}
    </div>
    <div class="filter-list">${_renderItems()}</div>
    <div class="filter-folder">
      <div class="filter-section-label">Limit to folder</div>
      <select class="filter-folder-select" id="filter-folder-select">
        <option value="">All folders</option>
        ${_folderOptions()}
      </select>
    </div>
  `;
  _pop.querySelectorAll(".filter-item").forEach(btn => {
    btn.addEventListener("click", () => _toggle(btn.dataset.name));
  });
  const reset = _pop.querySelector("#filter-reset");
  if (reset) reset.addEventListener("click", () => {
    state.sources = [...ALL];
    state.folderFilter = null;
    _save();
    _render();
  });
  const sel = _pop.querySelector("#filter-folder-select");
  sel.value = state.folderFilter || "";
  sel.addEventListener("change", (ev) => {
    state.folderFilter = ev.target.value || null;
  });
}

function _folderOptions() {
  // Group indexed files by parent dir (top 2-3 levels collapsed for sanity).
  // The dropdown shows unique folder prefixes that contain ≥ 1 file.
  if (!_folderCache) return "";
  return _folderCache.map(f => `
    <option value="${_esc(f.path)}">${_esc(f.label)} (${f.count})</option>
  `).join("");
}

let _folderCache = null;

async function _refreshFolders() {
  try {
    // Use api.files() instead of raw fetch — inherits the
    // default 60 s timeout. _refreshFolders is called every time the
    // filter dropdown opens AND on the state.files subscriber fire,
    // so a wedged backend used to hang all those promises (and
    // accumulate stuck timers via the subscriber chain). Same migration
    // pattern as sidebar.removeFolder, detail.exportSrt,
    // license.activate/clear and results.bulkExportFcpxml.
    const r = await api.files();
    const groups = new Map();
    (r.files || []).forEach(f => {
      const parent = f.path.split("/").slice(0, -1).join("/") || "/";
      groups.set(parent, (groups.get(parent) || 0) + 1);
    });
    _folderCache = [...groups.entries()]
      .map(([path, count]) => ({
        path,
        label: path.length > 36 ? "…" + path.slice(-34) : path,
        count,
      }))
      .sort((a, b) => b.count - a.count);
  } catch {
    _folderCache = [];
  }
}

export async function toggleFilterPopover(anchorEl) {
  if (!_pop) return;
  _open = !_open;
  if (!_open) { _pop.hidden = true; return; }
  await _refreshFolders();
  _render();
  const r = anchorEl.getBoundingClientRect();
  _pop.style.position = "fixed";
  _pop.style.top = (r.bottom + 6) + "px";
  _pop.style.right = Math.max(8, window.innerWidth - r.right) + "px";
  _pop.style.left = "auto";
  _pop.hidden = false;
}

function _hide() { _open = false; if (_pop) _pop.hidden = true; }

export function initFilters() {
  loadSources();
  _pop = document.getElementById("filter-popover");
  document.addEventListener("click", (ev) => {
    if (!_open) return;
    if (_pop.contains(ev.target)) return;
    if (ev.target.closest("#btn-filters")) return;
    _hide();
  });
  window.addEventListener("keydown", (ev) => {
    if (ev.key === "Escape" && _open) _hide();
  });
  // Wire the ⌥1/⌥2/⌥3 keyboard intents (registered in keyboard.js).
  on(INTENTS.TOGGLE_SPEECH, () => toggleSource("transcript"));
  on(INTENTS.TOGGLE_OCR,    () => toggleSource("ocr"));
  on(INTENTS.TOGGLE_VISUAL, () => toggleSource("visual"));
  // Persist state.sources whenever it changes via ANY path — not just
  // the popover toggle and Reset button that already explicitly call
  // _save(). The no-results "Search everything" button in results.js
  // assigns state.sources = ALL directly and previously skipped
  // persistence — so after a user clicked it once, their restored
  // scope on next app launch was STILL the old restricted set, and
  // the next search returned 0 results again. The subscriber here
  // makes any future writer of state.sources persist correctly
  // without having to know about the localStorage key. Existing
  // explicit _save() calls in _toggle() and the popover Reset stay
  // — they're now harmless (idempotent), and removing them would
  // be a broader refactor for no benefit.
  subscribe((k) => {
    if (k === "sources") _save();
  });
}
