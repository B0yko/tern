// app/modules/prefs.js — preferences popover anchored to the gear button.
import { loadPrefs, savePrefs, applyPrefs, ACCENTS } from "/modules/theme.js";
import { state } from "/modules/state.js";

const SWATCH_COLOR = {
  blue: "#007aff", purple: "#af52de", pink: "#ff2d55", red: "#ff3b30",
  orange: "#ff9500", yellow: "#ffcc00", green: "#34c759", graphite: "#8e8e93",
};

let _pop = null;
let _open = false;
let _prefs = null;

function _toggle() {
  _open = !_open;
  if (_open) _show(); else _hide();
}

function _show() {
  if (!_prefs) _prefs = loadPrefs();
  const gear = document.getElementById("btn-prefs");
  const rect = gear.getBoundingClientRect();
  _pop.style.top = (rect.bottom + 6) + "px";
  _pop.style.right = "12px";
  _pop.style.left = "auto";
  _pop.hidden = false;
  _render();
}

function _hide() {
  _pop.hidden = true;
  _open = false;
}

function _render() {
  _pop.innerHTML = `
    <div class="prefs">
      <div class="prefs-title">Preferences</div>

      <div class="prefs-section">
        <div class="prefs-label">Appearance</div>
        <div class="prefs-seg" data-key="appearance">
          ${["system", "light", "dark"].map(v =>
            `<button data-v="${v}" class="${_prefs.appearance === v ? "on" : ""}">${v[0].toUpperCase() + v.slice(1)}</button>`
          ).join("")}
        </div>
      </div>

      <div class="prefs-section">
        <div class="prefs-label">Accent</div>
        <div class="prefs-swatches">
          ${ACCENTS.map(a =>
            `<button class="prefs-swatch ${_prefs.accent === a ? "on" : ""}"
                     style="background:${SWATCH_COLOR[a]}"
                     data-accent="${a}" title="${a}"></button>`
          ).join("")}
        </div>
      </div>

      <div class="prefs-section">
        <div class="prefs-label">Density</div>
        <div class="prefs-seg" data-key="density">
          ${["comfortable", "compact"].map(v =>
            `<button data-v="${v}" class="${_prefs.density === v ? "on" : ""}">${v[0].toUpperCase() + v.slice(1)}</button>`
          ).join("")}
        </div>
      </div>

      <div class="prefs-section">
        <div class="prefs-toggle">
          <div class="prefs-toggle-track ${_prefs.powerMode ? "on" : ""}" id="prefs-power"><div class="prefs-toggle-knob"></div></div>
          <div style="flex:1;">
            <div class="prefs-toggle-text">Power Mode</div>
            <div class="prefs-toggle-sub">Editor-grade dark theme with three-color source coding (speech / on-screen / visual). Denser rows, mono meta.</div>
          </div>
        </div>
      </div>

      <div class="prefs-section">
        <div class="prefs-label">Advanced</div>
        <button class="prefs-link" id="prefs-license" type="button" title="Enter your license key, view activation status, or remove the current license">
          License…
        </button>
        <button class="prefs-link" id="prefs-replay-onboarding" type="button" title="Show the 3-screen capability tour again (useful for demos)" style="margin-top:4px;">
          Replay onboarding tour…
        </button>
        <button class="prefs-link" id="prefs-clear-recents" type="button" title="Remove all queries from Recent searches (Saved + Bookmarks untouched)" style="margin-top:4px;">
          Clear recent searches…
        </button>
        <button class="prefs-link" id="prefs-copy-diagnostics" type="button" title="Copy Tern version, workspace stats, recent log entries, and current indexing state — paste into a support email" style="margin-top:4px;">
          Copy diagnostics for support…
        </button>
      </div>
    </div>
  `;

  // Appearance + density segs
  _pop.querySelectorAll(".prefs-seg").forEach(seg => {
    const key = seg.dataset.key;
    seg.querySelectorAll("button").forEach(btn => {
      btn.addEventListener("click", () => {
        _prefs[key] = btn.dataset.v;
        _persist();
      });
    });
  });
  _pop.querySelectorAll(".prefs-swatch").forEach(btn => {
    btn.addEventListener("click", () => { _prefs.accent = btn.dataset.accent; _persist(); });
  });
  _pop.querySelector("#prefs-power").addEventListener("click", () => {
    _prefs.powerMode = !_prefs.powerMode;
    _persist();
  });
  // "License…" — second discoverable path to the license modal,
  // mirroring how most macOS apps surface activation in their
  // Preferences > Advanced section. The primary path stays the
  // sidebar "Trial mode · Activate" badge (always visible, no
  // hunt) but a buyer with macOS muscle memory expects gear-menu
  // > License too. Dispatches the same tern:open-license-modal
  // CustomEvent the sidebar badge + keyhelp overlay link both
  // fire — single modal, three entry points. _hide() the prefs
  // popover first so it doesn't overlap the license modal.
  _pop.querySelector("#prefs-license")?.addEventListener("click", () => {
    _hide();
    document.dispatchEvent(new CustomEvent("tern:open-license-modal"));
  });
  // "Replay onboarding tour…" — useful for users showing Tern to a teammate
  // or client. Clears the onboarded flag and re-mounts the sheet, then
  // closes the prefs popover so nothing covers it.
  _pop.querySelector("#prefs-replay-onboarding")?.addEventListener("click", async () => {
    // Wrapped because every OTHER localStorage call in the codebase
    // is — and removeItem CAN throw under macOS WKWebView private-
    // browsing or OS-restricted storage. Previously a throw here
    // would propagate out of the click handler and the user clicking
    // "Replay onboarding tour…" would see absolutely nothing happen
    // (the _hide() + onboarding mount would never run). Console-log
    // the exception class so devtools captures the actual failure
    // class. Even on the failure path we continue with _hide() +
    // re-mount because the onboarding mount is independent of the
    // flag — the user can re-watch the tour even if the flag couldn't
    // be cleared.
    try { localStorage.removeItem("tern.onboarded.v1"); }
    catch (e) { console.error("prefs replay-onboarding clear failed", e); }
    _hide();
    const mod = await import("/modules/onboarding.js");
    mod.initOnboarding();
  });
  // "Clear recent searches…" — confirms then wipes tern.recents.v1.
  // Saved + Bookmarks intentionally untouched (those are user-curated;
  // recents are passive history).
  _pop.querySelector("#prefs-clear-recents")?.addEventListener("click", async () => {
    if (!confirm("Clear all recent searches?\n\nThis only affects the Recent searches list in the sidebar. Saved searches and Bookmarks stay.")) return;
    const recents = await import("/modules/recents.js");
    recents.clearRecents();
    const toast = await import("/modules/toast.js");
    toast.flashToast("Recents cleared", { kind: "ok" });
  });
  // "Copy diagnostics for support…" — bridges the gap between "this
  // doesn't work" and a useful bug report. Previously the only
  // way to attach the diagnostics payload to a report was to know
  // that /api/diagnostics exists AND open Terminal AND curl it. A
  // typical user hits an issue, reports "it doesn't work," and there
  // is nothing to debug with. One click here grabs the redacted JSON
  // (/Users/<name>/ → ~ via /api/diagnostics' _redact_home, so no
  // username leak), formats it readably, copies to clipboard, and
  // toast-confirms with where to paste it (the project's issue
  // tracker).
  _pop.querySelector("#prefs-copy-diagnostics")?.addEventListener("click", async () => {
    const toast = await import("/modules/toast.js");
    try {
      const r = await fetch("/api/diagnostics");
      if (!r.ok) throw new Error(r.status + " " + r.statusText);
      const data = await r.json();
      const text = JSON.stringify(data, null, 2);
      await navigator.clipboard.writeText(text);
      toast.flashToast("Diagnostics copied — paste them into an issue at github.com/B0yko/tern/issues",
                       { kind: "ok", ttl: 4500 });
    } catch (e) {
      console.error("copy diagnostics failed", e);
      toast.flashToast(`Couldn't copy diagnostics: ${e?.message || e}`,
                       { kind: "err", ttl: 4500 });
    }
  });
}

function _persist() {
  savePrefs(_prefs);
  applyPrefs(_prefs);
  state.prefs = { ..._prefs };
  _render();
}

export function initPrefs() {
  _pop = document.getElementById("prefs-popover");
  document.addEventListener("tern:toggle-prefs", _toggle);
  document.addEventListener("click", (ev) => {
    if (!_open) return;
    if (_pop.contains(ev.target)) return;
    if (ev.target.closest("#btn-prefs")) return;
    _hide();
  });
  window.addEventListener("keydown", (ev) => {
    if (ev.key === "Escape" && _open) _hide();
  });
  _prefs = loadPrefs();
}
