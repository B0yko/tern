// app/modules/keyhelp.js — ⌘/ keyboard shortcut overlay.
// Lists every shortcut Tern responds to. Press ⌘/ to show, Esc to hide.

const SHORTCUTS = [
  { group: "Search", items: [
    ["⌘ K", "Focus the search field"],
    ["/",   "Focus search (when nothing else has focus — GitHub-style)"],
    ["⌘ D", "Save / unsave the current query"],
    ["Esc",  "Clear the search field (or refocus it if empty)"],
    ["⌥ 1 / ⌥ 2 / ⌥ 3", "Toggle Speech / On-screen / Visual scope"],
  ]},
  { group: "Navigation", items: [
    ["↓ / ↑", "Move selection; auto-previews the next/previous hit"],
    ["⏎",     "Play the selected hit"],
    ["Space", "Quick Look the selected file (Finder-style preview)"],
    ["← / →", "When the photo lightbox is open: previous / next photo"],
  ]},
  { group: "Playback (when audio/video is loaded)", items: [
    ["J", "Skip back 10 seconds"],
    ["K", "Play / pause"],
    ["L", "Skip forward 10 seconds"],
    ["I", "Set clip in-point at playhead (frame-accurate trim)"],
    ["O", "Set clip out-point at playhead (frame-accurate trim)"],
    [", / .", "Step playhead back / forward by ~1 frame (Shift = 1 second) — video trim only"],
    ["Home / End", "Jump playhead to clip start / clip end"],
  ]},
  { group: "Actions", items: [
    ["⌘ E",   "Export the selected hit as a clip (uses trim handles if set)"],
    ["⌘ L",   "Copy a link to the selected moment"],
    ["⌘ C",   "Copy the matched quote (or the file name when there's no snippet) — only fires when nothing else is selected, so it doesn't fight the browser's normal copy"],
    ["⇧ ⌘ R", "Reveal the source file in Finder"],
    ["⇧ ⌘ B", "Bookmark / un-bookmark the selected moment"],
    ["⌃ click", "Context menu on any row or sidebar entry — copy / reveal / bookmark / remove"],
  ]},
  { group: "Window", items: [
    ["⌘ B", "Toggle the sidebar (recent searches, folders, license)"],
    ["⌘ ,", "Open Preferences (Appearance / Accent / Density / Power Mode)"],
    ["⌘ /", "Show this shortcut overlay"],
  ]},
  { group: "Library", items: [
    ["⇧ ⌘ O", "Index a folder"],
  ]},
];

let _el = null;
let _open = false;
// Version is set once at backend boot from pyproject.toml (the only
// canonical source — see api/main.py _resolve_app_version). Cache the
// first successful fetch in module scope so subsequent ⌘/ toggles
// reuse it without re-hitting the backend at all. We now ask the
// tiny /api/version endpoint instead of /api/diagnostics — same
// version field, none of the diagnostics-payload overhead (log
// read, store.stats(), recursive path redaction). The in-module
// cache stays useful for avoiding even the constant-return round
// trip on subsequent overlay opens.
let _cachedVersion = null;

function _render() {
  const html = SHORTCUTS.map(g => `
    <div class="keyhelp-group">
      <div class="keyhelp-group-label">${g.group}</div>
      ${g.items.map(([keys, label]) => `
        <div class="keyhelp-row">
          <span class="keyhelp-keys">${keys.split(" ").map(k => `<kbd>${k}</kbd>`).join("")}</span>
          <span class="keyhelp-label">${label}</span>
        </div>
      `).join("")}
    </div>
  `).join("");
  _el.innerHTML = `
    <div class="keyhelp-card">
      <div class="keyhelp-head">
        <h2>Keyboard shortcuts</h2>
        <button class="keyhelp-close" id="keyhelp-close" aria-label="Close (Esc)">✕</button>
      </div>
      <div class="keyhelp-body">${html}</div>
      <div class="keyhelp-about" id="keyhelp-about">
        <div class="keyhelp-about-row">
          <span>Tern<span id="keyhelp-version-wrap" hidden> · <span id="keyhelp-version"></span></span></span>
          <span class="keyhelp-about-sep">·</span>
          <a href="#" id="keyhelp-license">License…</a>
          <span class="keyhelp-about-sep">·</span>
          <a href="https://github.com/B0yko/tern/issues" target="_blank" rel="noopener">Support</a>
          <span class="keyhelp-about-sep">·</span>
          <a href="https://github.com/B0yko/tern" target="_blank" rel="noopener">GitHub</a>
        </div>
        <div class="keyhelp-about-row keyhelp-about-sub">100 % local · Your audio never leaves this Mac</div>
      </div>
    </div>
  `;
  _el.querySelector("#keyhelp-close").addEventListener("click", _hide);
  _el.addEventListener("click", (ev) => { if (ev.target === _el) _hide(); });

  // Resolve the version once and cache. Backend version is set at boot
  // from pyproject.toml (api/main.py _resolve_app_version) and never
  // changes during a process lifetime; re-fetching on every overlay open
  // used to burn a /api/diagnostics round-trip (full log read + redact)
  // for no semantic gain. An earlier fix replaced the hardcoded "v0.1.1"
  // placeholder by hiding the chunk until the fetch returned. The cache
  // here builds on that. The fetch target is now /api/version — a tiny
  // endpoint that returns the version constant directly with no thread
  // offload or recursion, so even the first ⌘/ press doesn't pay the
  // diagnostics-payload cost.
  const _applyVersion = (v) => {
    const span = _el.querySelector("#keyhelp-version");
    const wrap = _el.querySelector("#keyhelp-version-wrap");
    if (span) span.textContent = "v" + v;
    if (wrap) wrap.hidden = false;
  };
  if (_cachedVersion) {
    _applyVersion(_cachedVersion);
  } else {
    fetch("/api/version").then(r => r.ok ? r.json() : null).then(d => {
      if (d && d.version && _el && !_el.hidden) {
        _cachedVersion = d.version;
        _applyVersion(d.version);
      }
    }).catch(() => {});
  }

  // Wire License… to open the existing license modal
  _el.querySelector("#keyhelp-license").addEventListener("click", (ev) => {
    ev.preventDefault();
    _hide();
    document.dispatchEvent(new CustomEvent("tern:open-license-modal"));
  });
}

function _show() {
  _open = true;
  _render();
  _el.hidden = false;
}

function _hide() {
  _open = false;
  _el.hidden = true;
}

export function toggleKeyhelp() {
  if (_open) _hide(); else _show();
}

export function initKeyhelp() {
  _el = document.getElementById("keyhelp-overlay");
  // `capture: true` + stopImmediatePropagation on the Escape branch so
  // dismissing the keyhelp overlay doesn't ALSO fire keyboard.js's
  // RESULT_CLEAR (which clears the search input behind the overlay).
  // Same fix as lightbox.js — keyboard.js's global dispatcher is a
  // window listener registered earlier in main.js init order, so
  // without capture it runs first in the bubble phase and the user
  // pressing Esc to close the keyhelp also wipes their results.
  // The ⌘/ branch doesn't need stopImmediatePropagation because no
  // other module binds ⌘/.
  window.addEventListener("keydown", (ev) => {
    if ((ev.metaKey || ev.ctrlKey) && ev.key === "/") {
      ev.preventDefault();
      toggleKeyhelp();
    } else if (ev.key === "Escape" && _open) {
      ev.preventDefault();
      ev.stopImmediatePropagation();
      _hide();
    }
  }, { capture: true });
}
