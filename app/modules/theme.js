// app/modules/theme.js
// Single source of truth for appearance / accent / density / power-mode.
// Persists to localStorage; pushes attrs onto <html data-...> and sets CSS
// custom properties for the chosen accent color.

const KEY = "tern.prefs.v1";

const DEFAULTS = {
  appearance: "system",  // "system" | "light" | "dark"
  accent: "blue",        // see ACCENT_HEX below
  density: "comfortable", // "comfortable" | "compact"
  powerMode: false,
};

const ACCENT_HEX = {
  blue:     { hex: "#007aff", dark: "#0a84ff" },
  purple:   { hex: "#af52de", dark: "#bf5af2" },
  pink:     { hex: "#ff2d55", dark: "#ff66bd" },
  red:      { hex: "#ff3b30", dark: "#ff453a" },
  orange:   { hex: "#ff9500", dark: "#ff9f0a" },
  yellow:   { hex: "#ffcc00", dark: "#ffd60a" },
  green:    { hex: "#34c759", dark: "#30d158" },
  graphite: { hex: "#8e8e93", dark: "#98989d" },
};

function _hexToRgba(hex, alpha) {
  const m = hex.replace("#", "");
  const r = parseInt(m.slice(0, 2), 16);
  const g = parseInt(m.slice(2, 4), 16);
  const b = parseInt(m.slice(4, 6), 16);
  return `rgba(${r}, ${g}, ${b}, ${alpha})`;
}

export function loadPrefs() {
  try {
    const stored = JSON.parse(localStorage.getItem(KEY) || "{}");
    return { ...DEFAULTS, ...stored };
  } catch {
    return { ...DEFAULTS };
  }
}

export function savePrefs(prefs) {
  // Wrapped because prefs.js's _persist() is sync — an unguarded throw
  // here (private-browsing WKWebView, OS-restricted localStorage, full
  // quota) would skip applyPrefs() right after, so the user clicks a
  // theme/accent and sees NOTHING change. Same pattern as recents.js
  // (eb22047), bookmarks.js, and sidebar.js. The setting still applies
  // for the current session; it just won't survive a restart.
  // Diagnostic-trail addition (commit 8cef452 sibling): name the
  // exception class so support diagnostics ("Inspector → Console")
  // can read the failure type out loud — without this, a user
  // reporting "my theme keeps reverting on restart" had no signal
  // distinguishing quota failures from a model bug.
  try { localStorage.setItem(KEY, JSON.stringify(prefs)); }
  catch (e) { console.error("theme prefs persist failed", e); }
}

export function applyPrefs(prefs) {
  const html = document.documentElement;
  html.dataset.appearance = prefs.appearance;
  html.dataset.density = prefs.density;
  html.dataset.powerMode = prefs.powerMode ? "on" : "off";
  html.dataset.accent = prefs.accent;

  // Resolve accent color for the current effective appearance
  const isDark = prefs.appearance === "dark"
    || (prefs.appearance === "system" && matchMedia("(prefers-color-scheme: dark)").matches);
  const accent = ACCENT_HEX[prefs.accent] || ACCENT_HEX.blue;
  const hex = isDark ? accent.dark : accent.hex;

  html.style.setProperty("--accent", hex);
  html.style.setProperty("--accent-tint-12", _hexToRgba(hex, 0.12));
  html.style.setProperty("--accent-tint-18", _hexToRgba(hex, 0.18));
  html.style.setProperty("--accent-tint-30", _hexToRgba(hex, 0.30));
}

export function initTheme() {
  const prefs = loadPrefs();
  applyPrefs(prefs);
  // React to system-appearance changes when in "system" mode
  matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => {
    const cur = loadPrefs();
    if (cur.appearance === "system") applyPrefs(cur);
  });
  return prefs;
}

export const ACCENTS = Object.keys(ACCENT_HEX);
