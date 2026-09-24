// app/modules/lightbox.js — fullscreen image viewer for photo hits.
// Opens via "tern:open-lightbox" CustomEvent (dispatched by player.js when
// the user clicks a photo). Esc closes; ← / → cycle through image-kind
// hits in the current result list.
import { state } from "/modules/state.js";
import { icons } from "/modules/icons.js";

let _el = null;

function _escape(s) {
  return (s || "").replace(/[&<>"']/g, c =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function _imageHits() {
  return (state.results || []).filter(h => h.media_kind === "image");
}

function open(hit) {
  if (!hit || hit.media_kind !== "image") return;
  const photos = _imageHits();
  const idx = photos.findIndex(h => h.file_id === hit.file_id && h.ts_ms === hit.ts_ms);
  const counterText = photos.length > 1 && idx >= 0 ? `${idx + 1} / ${photos.length}` : "";
  _el.innerHTML = `
    <button class="lb-close" id="lb-close" aria-label="Close (Esc)">${icons.close({ w: 18, h: 18 })}</button>
    ${photos.length > 1 ? `
      <button class="lb-nav lb-nav-prev" id="lb-prev" aria-label="Previous photo (←)"><span style="font-size:20px;font-weight:300;">‹</span></button>
      <button class="lb-nav lb-nav-next" id="lb-next" aria-label="Next photo (→)"><span style="font-size:20px;font-weight:300;">›</span></button>
    ` : ""}
    <div class="lb-image-wrap">
      <img src="${hit.preview_url}" alt="${_escape(hit.file_name)}">
    </div>
    <div class="lb-meta">
      <span class="lb-file">${_escape(hit.file_name)}</span>
      ${counterText ? `<span class="lb-counter">${counterText}</span>` : ""}
    </div>
  `;
  _el.hidden = false;
  _el.querySelector("#lb-close").addEventListener("click", close);
  const prev = _el.querySelector("#lb-prev");
  const next = _el.querySelector("#lb-next");
  if (prev) prev.addEventListener("click", () => navigate(-1));
  if (next) next.addEventListener("click", () => navigate(+1));
  _el.onclick = (ev) => { if (ev.target === _el) close(); };
}

function close() {
  _el.hidden = true;
  _el.innerHTML = "";
}

function navigate(direction) {
  if (_el.hidden) return;
  const photos = _imageHits();
  if (photos.length < 2) return;
  const cur = state.selectedHit;
  let idx = photos.findIndex(h => h.file_id === cur?.file_id && h.ts_ms === cur?.ts_ms);
  if (idx < 0) idx = 0;
  idx = (idx + direction + photos.length) % photos.length;
  const next = photos[idx];
  if (next) {
    state.selectedHit = next;
    state.selectedIndex = state.results.indexOf(next);
    open(next);
  }
}

export function initLightbox() {
  _el = document.getElementById("lightbox-overlay");
  document.addEventListener("tern:open-lightbox", (ev) => open(ev.detail?.hit));
  // `capture: true` so this listener runs BEFORE keyboard.js's bubble-
  // phase global dispatcher (which registers earlier in main.js init
  // order, so without capture it would fire first and RESULT_CLEAR
  // would wipe the search input behind the lightbox before we close
  // it). stopImmediatePropagation on the keys we consume so the
  // bubble-phase dispatcher never sees them.
  window.addEventListener("keydown", (ev) => {
    if (_el.hidden) return;
    if (ev.key === "Escape") {
      ev.preventDefault();
      ev.stopImmediatePropagation();   // don't let keyboard.js also fire RESULT_CLEAR
      close();
      return;
    }
    if (ev.key === "ArrowRight") {
      ev.preventDefault();
      ev.stopImmediatePropagation();
      navigate(+1);
      return;
    }
    if (ev.key === "ArrowLeft") {
      ev.preventDefault();
      ev.stopImmediatePropagation();
      navigate(-1);
      return;
    }
    // ↑ / ↓ in this mode are result-list navigation, not lightbox
    // navigation (← / → handle inter-photo stepping). The global
    // result-up / -down handlers SHOULD still fire so the detail pane
    // moves on to the next hit — we just close the lightbox so the
    // user actually sees that update.
    if (ev.key === "ArrowUp" || ev.key === "ArrowDown") close();
  }, { capture: true });
}
