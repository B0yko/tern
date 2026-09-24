// app/modules/preview.js — quiet auto-preview on row hover (Mail-style).
// Hover a result row for 500 ms → 6-second audio preview from the matched
// timestamp at 30 % volume. Mouseleave / click / next-hover cancels.
// Image hits are skipped (nothing to preview audibly).

const HOVER_DELAY_MS    = 500;
const PREVIEW_LIMIT_MS  = 6000;
const VOLUME            = 0.30;

let _audio   = null;
let _timer   = null;
let _stopper = null;
let _row     = null;

function _cancel() {
  clearTimeout(_timer); _timer = null;
  clearTimeout(_stopper); _stopper = null;
  if (_audio) {
    _audio.pause();
    _audio.src = ""; // free decoder
    _audio = null;
  }
  if (_row) { _row.classList.remove("row-previewing"); _row = null; }
}

function _start(hit, row) {
  _cancel();
  if (!hit || hit.media_kind === "image" || !hit.preview_url) return;
  const seek = ((hit.ts_ms || 0) / 1000) | 0;
  const audio = new Audio(`${hit.preview_url}#t=${seek}`);
  audio.preload = "auto";
  audio.volume = VOLUME;
  _audio = audio;
  _row = row;
  row.classList.add("row-previewing");
  audio.addEventListener("loadedmetadata", () => {
    audio.currentTime = (hit.ts_ms || 0) / 1000;
  }, { once: true });
  audio.play().catch(() => {}); // ignore autoplay restrictions in dev browsers
  _stopper = setTimeout(() => { if (_audio === audio) _cancel(); }, PREVIEW_LIMIT_MS);
}

export function attachPreview(rowEl, hit) {
  if (hit.media_kind === "image") return; // no audio preview for photos
  rowEl.addEventListener("mouseenter", () => {
    clearTimeout(_timer);
    _timer = setTimeout(() => _start(hit, rowEl), HOVER_DELAY_MS);
  });
  rowEl.addEventListener("mouseleave", () => {
    clearTimeout(_timer); _timer = null;
    if (_row === rowEl) _cancel();
  });
  rowEl.addEventListener("click", _cancel); // user took control
}

export function cancelPreview() { _cancel(); }
