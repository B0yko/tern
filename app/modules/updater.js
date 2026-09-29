// app/modules/updater.js — Tauri auto-update check, throttled to 24 h.
// Surfaces a small in-app banner when a newer version is available; click
// "Update now" → download + install + relaunch via tauri-plugin-updater.
//
// Silently no-ops outside Tauri (plain-browser dev mode) so this file is
// safe to load anywhere.

const LAST_CHECK_KEY  = "tern.updater.lastCheck";
const CHECK_INTERVAL  = 24 * 60 * 60 * 1000; // 24 h

function _tauriUpdater() {
  return window.__TAURI__ && (window.__TAURI__.updater || window.__TAURI_INTERNALS__?.plugin?.updater);
}

// Returns one of:
//   { ok: true, upd: <Update> }   — check completed, update available
//   { ok: true, upd: null }       — check completed, no update available
//   { ok: false, err: <error> }   — check did not complete (network / API blip)
// The caller (initUpdater) uses `ok` to decide whether to advance the
// 24h LAST_CHECK throttle. Previously a network blip during _check
// would silently return null, get bucketed as "checked, no update",
// and burn the throttle window — a user who launched Tern offline
// (WiFi off, captive portal, VPN reconnecting) could miss an
// important update for a full day even after connectivity returned.
async function _check() {
  const u = _tauriUpdater();
  if (!u) return { ok: false, err: new Error("no updater plugin") };

  // Tauri 2.x API: check() returns an Update | null.
  // Fall back to v1 shape (checkUpdate / installUpdate) if needed.
  try {
    if (typeof u.check === "function") {
      const upd = await u.check();
      if (upd && (upd.available !== false)) return { ok: true, upd };
      return { ok: true, upd: null };
    }
    if (typeof u.checkUpdate === "function") {
      const { shouldUpdate, manifest } = await u.checkUpdate();
      return { ok: true, upd: shouldUpdate ? manifest : null };
    }
  } catch (e) {
    console.warn("updater check failed", e);
    return { ok: false, err: e };
  }
  return { ok: false, err: new Error("updater plugin missing both check shapes") };
}

function _shouldCheck() {
  try {
    const last = parseInt(localStorage.getItem(LAST_CHECK_KEY) || "0", 10);
    return (Date.now() - last) > CHECK_INTERVAL;
  } catch { return true; }
}

function _markChecked() {
  // Diagnostic-trail addition. Silent UX is
  // correct here — we don't want to spam the user about an updater-
  // metadata write that they can't act on — but a persistent failure
  // would mean the update-check fires every launch (the gate at line
  // ~50 reads the timestamp and treats "never set" as overdue). A user
  // reporting "Tern keeps banner-popping the update check every
  // launch" needs a console line to point at.
  try { localStorage.setItem(LAST_CHECK_KEY, String(Date.now())); }
  catch (e) { console.error("updater mark-checked persist failed", e); }
}

function _showBanner(upd) {
  const version = upd?.version || upd?.manifest?.version || "newer";
  // Reuse the api-banner DOM with success styling
  const bar = document.getElementById("api-banner");
  if (!bar) return;
  bar.style.background = "var(--accent)";
  bar.innerHTML = `
    <span class="api-banner-dot" style="background:#fff"></span>
    <span>Tern <strong>${version}</strong> is available.</span>
    <button class="api-banner-retry" id="updater-install">Update now</button>
    <button class="api-banner-retry" id="updater-dismiss" style="background:transparent;">Later</button>
  `;
  bar.hidden = false;
  bar.querySelector("#updater-install").addEventListener("click", async () => {
    bar.innerHTML = `<span class="api-banner-dot" style="background:#fff"></span><span>Downloading update…</span>`;
    try {
      if (typeof upd.downloadAndInstall === "function") {
        await upd.downloadAndInstall();
        // Tauri may auto-relaunch; if not:
        if (window.__TAURI__?.process?.relaunch) await window.__TAURI__.process.relaunch();
      } else if (window.__TAURI__?.updater?.installUpdate) {
        await window.__TAURI__.updater.installUpdate();
        if (window.__TAURI__?.process?.relaunch) await window.__TAURI__.process.relaunch();
      }
    } catch (e) {
      console.error("update install failed", e);
      bar.innerHTML = `<span class="api-banner-dot"></span><span>Update failed. Download the latest build from github.com/B0yko/tern/releases.</span>`;
    }
  });
  bar.querySelector("#updater-dismiss").addEventListener("click", () => {
    bar.hidden = true;
    bar.style.background = ""; // restore default
  });
}

export async function initUpdater() {
  if (!_tauriUpdater()) return;            // dev browser — skip
  if (!_shouldCheck()) return;             // throttled
  // Brief delay so backend ping completes first (banner shouldn't overlap).
  setTimeout(async () => {
    const res = await _check();
    if (!res.ok) {
      // Do NOT _markChecked() — the throttle window is reserved for
      // successful checks. A failed check (offline, captive portal, VPN
      // mid-reconnect, Tauri updater plugin glitch) means we still don't
      // know whether an update is available; retrying on the next app
      // launch is the right behaviour. Without this guard, a user who
      // launches Tern with WiFi off would burn the entire 24h window
      // and miss security/feature updates until tomorrow.
      return;
    }
    _markChecked();
    if (res.upd) _showBanner(res.upd);
  }, 3000);
}
