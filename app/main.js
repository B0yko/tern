// app/main.js — entry point. Wires all modules to the shell defined in index.html.
import { state, subscribe } from "/modules/state.js";
import { api } from "/modules/api.js";
import { icons } from "/modules/icons.js";
import { initTheme } from "/modules/theme.js";
import { initKeyboard, on, INTENTS } from "/modules/keyboard.js";

// Static icon mounts (run once on load)
function mountStaticIcons() {
  document.getElementById("icon-sidebar").innerHTML = icons.sidebar({ w: 16, h: 16 });
  document.getElementById("icon-search").innerHTML  = icons.search({ w: 14, h: 14 });
  document.getElementById("icon-gear").innerHTML    = icons.gear({ w: 16, h: 16 });
  document.getElementById("icon-star").innerHTML    = icons.starOutline({ w: 16, h: 16 });
  document.getElementById("icon-filters").innerHTML = icons.filter({ w: 14, h: 14 });
  document.getElementById("icon-keyhelp").innerHTML = icons.question({ w: 16, h: 16 });
  document.getElementById("btn-keyhelp").addEventListener("click", async () => {
    const m = await import("/modules/keyhelp.js");
    m.toggleKeyhelp();
  });
  document.getElementById("icon-close-1").innerHTML = icons.close({ w: 14, h: 14 });
  const dropIcon = document.getElementById("drop-icon");
  if (dropIcon) dropIcon.innerHTML = icons.folder({ w: 48, h: 48 });
}

// Backend health check + banner.
//
// Two trigger paths:
//   1. Boot-time `await pingBackend()` — surfaces a backend that never
//      came up at all (e.g., port collision with another Tern instance).
//   2. Recurring poll — if the sidecar dies mid-session (force-quit,
//      OOM-kill, manual `kill -9`), the banner appears within 15 s
//      and auto-hides as soon as health comes back. Polling cadence is
//      asymmetric: only 60 s when healthy (cheap), 15 s while down
//      (so recovery feels fast). This avoids hammering /api/health 4×/min
//      forever just to catch a once-a-month sidecar death.
async function pingBackend() {
  const banner = document.getElementById("api-banner");
  let alive = false;
  try {
    await api.health();
    banner.hidden = true;
    alive = true;
  } catch {
    // Reset inline style + innerHTML before showing. The same DOM is
    // reused by the updater (modules/updater.js sets accent background
    // + custom HTML when an update is available) and by the error
    // boundary (`var(--err)` background). Without the reset, a backend
    // that dies AFTER the updater banner has been dismissed shows the
    // accent-coloured updater HTML ("Tern 0.1.2 is available…") even
    // though the actual situation is "backend isn't responding."
    banner.style.background = "";  // → CSS default (var(--err))
    banner.innerHTML = `
      <span class="api-banner-dot"></span>
      <span>Tern backend isn't responding.</span>
      <button class="api-banner-retry" id="api-banner-retry">Retry</button>
    `;
    banner.hidden = false;
    // Re-wire the Retry click since we just blew away the old listener.
    banner.querySelector("#api-banner-retry").addEventListener("click", pingBackend);
  }
  return alive;
}

let _healthTimer = null;
function _schedulePing(alive) {
  clearTimeout(_healthTimer);
  // Slower when alive (don't burn the user's CPU); faster when down
  // (so the banner clears the moment the sidecar comes back).
  _healthTimer = setTimeout(async () => {
    const ok = await pingBackend();
    _schedulePing(ok);
  }, alive ? 60_000 : 15_000);
}

async function refreshStats() {
  try {
    state.stats = await api.stats();
  } catch (e) {
    console.warn("stats failed", e);
  }
}

// Global error boundary — uncaught JS / unhandled rejections would otherwise
// be invisible to the user. Show a small dismissable banner that explains
// + offers reload. Banner reuses the api-banner DOM with a different tint.
function _installErrorBoundary() {
  let banner = null;
  function _showCrash(label, source) {
    // Surfaces only the first crash per session — subsequent errors
    // would spam the user with stacked banners.
    if (banner) return;
    banner = document.getElementById("api-banner");
    if (!banner) return;
    banner.style.background = "var(--err)";
    banner.innerHTML = `
      <span class="api-banner-dot"></span>
      <span>${(label || "Something went wrong").replace(/[<>&]/g, "")} — try reloading.</span>
      <button class="api-banner-retry" id="crash-reload">Reload</button>
      <button class="api-banner-retry" id="crash-dismiss" style="background:transparent;">Dismiss</button>
    `;
    banner.hidden = false;
    banner.querySelector("#crash-reload").addEventListener("click", () => location.reload());
    banner.querySelector("#crash-dismiss").addEventListener("click", () => {
      banner.hidden = true;
      banner.style.background = "";
      banner = null;
    });
    console.error("[tern crash]", source);
  }
  window.addEventListener("error", (ev) => _showCrash(ev.message || "Script error", ev.error));
  window.addEventListener("unhandledrejection", (ev) => _showCrash(
    ev.reason?.message || "Unhandled async error",
    ev.reason
  ));
}

async function boot() {
  _installErrorBoundary();
  initTheme();
  mountStaticIcons();
  initKeyboard();

  const { loadRecents } = await import("/modules/recents.js");
  loadRecents();

  // Search input focus shortcut
  on(INTENTS.FOCUS_SEARCH, () => {
    const el = document.getElementById("search-input");
    el.focus();
    el.select();
  });

  // Backend banner retry
  document.getElementById("api-banner-retry").addEventListener("click", pingBackend);

  const _alive = await pingBackend();
  _schedulePing(_alive);  // recurring health poller — see _schedulePing above
  await refreshStats();

  // Other modules wire themselves in their own bootstraps (called below
  // as they're built — empty for now). The order matters: sidebar before
  // results before detail before empty.
  const { initTopbar }    = await import("/modules/topbar.js");
  const { initSidebar }   = await import("/modules/sidebar.js");
  const { initResults }   = await import("/modules/results.js");
  const { initDetail }    = await import("/modules/detail.js");
  const { initEmpty }     = await import("/modules/empty.js");
  const { initPrefs }     = await import("/modules/prefs.js");
  const { initIndexing }  = await import("/modules/indexing.js");
  const { initOnboarding }= await import("/modules/onboarding.js");
  const { initLightbox }  = await import("/modules/lightbox.js");
  const { loadSaved }     = await import("/modules/saved.js");
  const { initFilters }   = await import("/modules/filters.js");
  const { initKeyhelp }   = await import("/modules/keyhelp.js");
  const { initSuggest }   = await import("/modules/suggest.js");
  const { initUpdater }   = await import("/modules/updater.js");
  const { initLicense }   = await import("/modules/license.js");
  const { loadBookmarks } = await import("/modules/bookmarks.js");
  loadSaved();
  loadBookmarks();
  initFilters();
  initKeyhelp();
  initSuggest();
  initUpdater();
  initLicense();

  initTopbar();
  initSidebar();
  initResults();
  initDetail();
  initEmpty();
  initLightbox();
  initPrefs();
  initIndexing();
  initOnboarding();
}

boot();
