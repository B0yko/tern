// app/modules/player.js
import { icons } from "/modules/icons.js";
import { api } from "/modules/api.js";
import { flashToast } from "/modules/toast.js";
import { showContextMenu } from "/modules/contextmenu.js";

// Unified media-error handler for <audio> + <video>. The native HTML5
// media element fires `error` when src is 404 / forbidden / malformed /
// fails to decode. Previously, the audio/video tag was created with
// no error listener — if the source file got deleted externally between
// indexing and click (common for podcast workflows where the user trims
// down their library while Tern's index lags), the player just rendered
// the WKWebView native broken-media icon with no toast, no log line,
// no actionable feedback. The user couldn't tell apart "Tern broke" from
// "I deleted the source last week and forgot."
//
// We map error.code → a useful message:
//   * MEDIA_ERR_SRC_NOT_SUPPORTED (4): usually means /api/file 404'd —
//     the bytes never arrived so WebKit can't even sniff the codec.
//     Most common cause: source file moved/deleted externally.
//   * MEDIA_ERR_DECODE (3): bytes arrived but decoder rejected them.
//     Usually a corrupted source or a codec the bundled WebKit doesn't
//     know — point the user at re-encoding via ffmpeg.
//   * MEDIA_ERR_NETWORK (2): network read failed mid-stream. On a
//     loopback connection to our own sidecar this is rare; usually
//     means the sidecar crashed/restarted mid-playback.
//   * MEDIA_ERR_ABORTED (1): user navigated away or our own code
//     reassigned src. Not user-visible noise — suppress.
function _bindMediaErrorToast(mediaEl, hit, kind) {
  mediaEl.addEventListener("error", () => {
    const err = mediaEl.error;
    if (!err) return;
    // Suppress benign aborts — these fire when our own code reassigns
    // src (next-hit navigation), not from a real load failure.
    if (err.code === 1) return;
    let msg;
    if (err.code === 4) {
      // Source not supported — almost always a 404 on /api/file (file
      // moved/deleted externally). Phrase the message that way so the
      // user knows where to look first.
      msg = `Couldn't load ${kind}: ${hit.file_name || "file"} — source may have been moved or deleted`;
    } else if (err.code === 3) {
      msg = `${kind} decode failed: ${hit.file_name || "file"} — source may be corrupted or use an unsupported codec`;
    } else if (err.code === 2) {
      msg = `${kind} network error — sidecar may have restarted`;
    } else {
      msg = `${kind} load failed (code ${err.code})`;
    }
    flashToast(msg, { kind: "err", ttl: 4500 });
  });
}

// Module-level AbortController for the live player's keyboard listener.
// Aborted at the top of every renderPlayer() call so a new hit's listener
// doesn't stack on the previous hit's (which would otherwise scrub a
// detached audio element on every J/L press — silent leak, hard to debug).
let _playerKeysAbort = null;

function _fmtTime(s) {
  if (!isFinite(s)) return "00:00";
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = Math.floor(s % 60);
  const pad = (n) => String(n).padStart(2, "0");
  return h > 0 ? `${pad(h)}:${pad(m)}:${pad(sec)}` : `${pad(m)}:${pad(sec)}`;
}

// J / K / L — YouTube / Final Cut / Avid convention. Used by both the
// audio and video player branches. Previously inlined inside
// _renderAudio only; pressing J/K/L on a VIDEO hit silently did nothing
// even though keyhelp.js advertised the shortcuts globally — classic
// broken-promise gap (same class as the earlier FOCUS_SEARCH no-op
// fix). The handler works on any HTMLMediaElement so both
// branches share one implementation. Listener installs via an
// AbortController stored at module scope; renderPlayer() aborts the
// previous instance before mounting the next so we never stack
// listeners across hits (which would scrub detached media elements
// on every keypress — silent memory leak + double-seek bug).
function _bindMediaShortcuts(mediaEl) {
  _playerKeysAbort = new AbortController();
  window.addEventListener("keydown", (ev) => {
    const t = document.activeElement;
    if (t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA" || t.isContentEditable)) return;
    if (ev.metaKey || ev.ctrlKey || ev.altKey || ev.shiftKey) return;
    const k = ev.key.toLowerCase();
    if (k === "j") {
      ev.preventDefault();
      mediaEl.currentTime = Math.max(0, mediaEl.currentTime - 10);
    } else if (k === "l") {
      ev.preventDefault();
      mediaEl.currentTime = Math.min(mediaEl.duration || 0, mediaEl.currentTime + 10);
    } else if (k === "k") {
      ev.preventDefault();
      if (mediaEl.paused) mediaEl.play().catch(() => {}); else mediaEl.pause();
    } else if (k === "i") {
      // I = set clip In-point at the current playhead. NLE-standard
      // (Final Cut, Premiere, DaVinci, Avid). Especially important for
      // long-form videos (3-hour podcasts) where dragging the visual
      // trim handle gives only ~13 s of precision per pixel on a 800 px
      // strip — keyboard at the playhead is millisecond-exact.
      // Dispatches a CustomEvent so whichever trim widget is currently
      // mounted (waveform for audio, timeline strip for video) hears
      // it and updates both the dataset (clipStartMs) and the visual
      // handles. Pattern keeps _bindMediaShortcuts agnostic of which
      // player branch is up.
      ev.preventDefault();
      document.dispatchEvent(new CustomEvent("tern:set-clip-in",
        { detail: { timeS: mediaEl.currentTime } }));
    } else if (k === "o") {
      ev.preventDefault();
      document.dispatchEvent(new CustomEvent("tern:set-clip-out",
        { detail: { timeS: mediaEl.currentTime } }));
    }
  }, { signal: _playerKeysAbort.signal });
}

export function renderPlayer(container, hit) {
  // Abort any keyboard listener from the previous hit's player BEFORE
  // mounting the new one. Prevents stacked listeners that would all
  // respond to J/K/L and scrub detached audio elements.
  if (_playerKeysAbort) {
    _playerKeysAbort.abort();
    _playerKeysAbort = null;
  }
  if (hit.media_kind === "audio") {
    return _renderAudio(container, hit);
  }
  if (hit.media_kind === "video") {
    return _renderVideo(container, hit);
  }
  if (hit.media_kind === "image") {
    return _renderImage(container, hit);
  }
  container.innerHTML = "";
}

function _renderAudio(container, hit) {
  container.innerHTML = `
    <div class="player">
      <div class="player-audio">
        <button class="player-skip" id="player-back" title="Skip −10 s (J)" aria-label="Skip back 10 seconds (J)">
          <svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M3 12a9 9 0 1 0 3-6.7"/><path d="M3 4v5h5"/><text x="11" y="15.5" font-size="8" stroke="none" fill="currentColor" font-weight="600">10</text></svg>
        </button>
        <button class="player-play" id="player-play" title="Play / pause (K)" aria-label="Play or pause (K)">${icons.play({ w: 11, h: 11 })}</button>
        <button class="player-skip" id="player-fwd" title="Skip +10 s (L)" aria-label="Skip forward 10 seconds (L)">
          <svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12a9 9 0 1 1-3-6.7"/><path d="M21 4v5h-5"/><text x="6" y="15.5" font-size="8" stroke="none" fill="currentColor" font-weight="600">10</text></svg>
        </button>
        <div class="player-wave-wrap">
          <div class="player-wave" id="player-wave">
            <span class="player-playhead" id="player-playhead" style="left: 50%;"></span>
          </div>
          <div class="player-times">
            <span id="player-time-cur">${hit.timecode || "00:00"}</span>
            <span id="player-time-end">--:--</span>
          </div>
        </div>
        <button class="player-speed" id="player-speed" title="Playback speed">1×</button>
        <audio id="player-audio" preload="metadata" src="${hit.preview_url}#t=${(hit.ts_ms / 1000) | 0}"></audio>
      </div>
    </div>
  `;
  const audio = container.querySelector("#player-audio");
  const btn   = container.querySelector("#player-play");
  const wave  = container.querySelector("#player-wave");
  const head  = container.querySelector("#player-playhead");
  const cur   = container.querySelector("#player-time-cur");
  const end   = container.querySelector("#player-time-end");
  // Surface load failures as toasts — see _bindMediaErrorToast docstring
  // for the err.code mapping. Catches the deleted-source-file case which
  // pre-fix manifested as a silent broken-media icon.
  _bindMediaErrorToast(audio, hit, "Audio");

  audio.addEventListener("loadedmetadata", () => {
    // Staleness guard — same race as the video
    // branch: a detached audio element from a superseded hit still
    // fires loadedmetadata and would run _setupTrimHandles against
    // the NEXT hit's DOM (duplicate "Clip" info row on audio→audio
    // switches; uncaught TypeError on audio→video/image where the
    // expected wrap element is gone).
    if (!audio.isConnected) return;
    end.textContent = _fmtTime(audio.duration);
    audio.currentTime = (hit.ts_ms || 0) / 1000;
    // Render real waveform overlay on top of the gradient placeholder.
    // Async + non-blocking; gradient stays as a fallback if decode fails.
    _renderRealWaveform(wave, hit, audio.duration).catch(() => {});
    // Add draggable clip-trim handles to the wave so user can adjust
    // export start/end without leaving the detail pane.
    _setupTrimHandles(container, wave, audio, hit);
  });
  audio.addEventListener("timeupdate", () => {
    cur.textContent = _fmtTime(audio.currentTime);
    if (audio.duration > 0) head.style.left = ((audio.currentTime / audio.duration) * 100) + "%";
  });
  audio.addEventListener("play",  () => btn.innerHTML = icons.pause({ w: 11, h: 11 }));
  audio.addEventListener("pause", () => btn.innerHTML = icons.play({ w: 11, h: 11 }));
  // .catch on play() — pause() during the pending play promise (K key
  // right after click) raises AbortError; uncaught it's console noise
  // on every fast toggle.
  btn.addEventListener("click", () => audio.paused ? audio.play().catch(() => {}) : audio.pause());

  // Auto-advance: when this hit's clip ends, signal results.js to step
  // selection by +1. results.js decides whether to honor it (e.g. skips
  // when user is already navigating, or at end of list).
  audio.addEventListener("ended", () => {
    document.dispatchEvent(new CustomEvent("tern:play-next", { detail: { from: hit.file_id, ts: hit.ts_ms } }));
  });

  // ─── Playback controls (speed, skip ±10s) ───
  const back = container.querySelector("#player-back");
  const fwd  = container.querySelector("#player-fwd");
  const spd  = container.querySelector("#player-speed");
  back.addEventListener("click", () => { audio.currentTime = Math.max(0, audio.currentTime - 10); });
  fwd.addEventListener("click",  () => { audio.currentTime = Math.min(audio.duration || 0, audio.currentTime + 10); });

  // J / K / L keyboard shortcuts — YouTube / Final Cut Pro / Avid convention.
  // Shared with the video branch via _bindMediaShortcuts so both media kinds
  // honour the keyhelp's global advertisement; before this share, video
  // hits silently did nothing on J/K/L.
  _bindMediaShortcuts(audio);

  // Speed cycle: 1× → 1.25× → 1.5× → 1.75× → 2× → 0.75× → 1× …
  // Persist between hits so the user's chosen speed sticks (saved as
  // tern.playbackRate.v1 in localStorage).
  const SPEEDS = [1, 1.25, 1.5, 1.75, 2, 0.75];
  let speedIdx = 0;
  try {
    const stored = parseFloat(localStorage.getItem("tern.playbackRate.v1") || "1");
    speedIdx = Math.max(0, SPEEDS.indexOf(stored));
    if (speedIdx === -1) speedIdx = 0;
  } catch { speedIdx = 0; }
  audio.playbackRate = SPEEDS[speedIdx];
  spd.textContent = `${SPEEDS[speedIdx]}×`;
  spd.addEventListener("click", () => {
    speedIdx = (speedIdx + 1) % SPEEDS.length;
    audio.playbackRate = SPEEDS[speedIdx];
    spd.textContent = `${SPEEDS[speedIdx]}×`;
    try { localStorage.setItem("tern.playbackRate.v1", String(SPEEDS[speedIdx])); } catch {}
  });

  wave.addEventListener("click", (ev) => {
    if (!audio.duration) return;
    const rect = wave.getBoundingClientRect();
    const ratio = (ev.clientX - rect.left) / rect.width;
    audio.currentTime = ratio * audio.duration;
  });
}

function _renderVideo(container, hit) {
  container.innerHTML = `
    <div class="player">
      <div class="player-video">
        <video controls preload="auto" src="${hit.preview_url}#t=${(hit.ts_ms / 1000) | 0}"></video>
      </div>
      <div class="vtrim" id="vtrim">
        <!-- Overview minimap: full video duration in 28 px. Drag the
             yellow viewport rectangle to pan the working strip below;
             the dark band shows the clip range at full-video scale. -->
        <div class="vtrim-overview" id="vtrim-overview" aria-label="Overview minimap — drag to pan the working strip">
          <div class="vtrim-overview-thumbs" id="vtrim-overview-thumbs"></div>
          <div class="vtrim-overview-clip" id="vtrim-overview-clip" aria-hidden="true"></div>
          <div class="vtrim-overview-match" id="vtrim-overview-match" aria-hidden="true"></div>
          <div class="vtrim-overview-playhead" id="vtrim-overview-playhead" aria-hidden="true"></div>
          <div class="vtrim-overview-viewport" id="vtrim-overview-viewport" tabindex="0" aria-label="Visible range — drag to pan"></div>
        </div>

        <!-- Ruler: ticks + timecode labels above the working strip. -->
        <div class="vtrim-ruler" id="vtrim-ruler" aria-hidden="true"></div>

        <!-- Working strip: zoomed window with big thumbnails, big handles,
             playhead, match marker. Most editing happens here. -->
        <div class="vtrim-work" id="vtrim-work" aria-label="Clip trim — drag handles, click-drag to select, I/O at playhead">
          <div class="vtrim-work-thumbs" id="vtrim-work-thumbs"></div>
          <div class="vtrim-work-band" id="vtrim-work-band" aria-hidden="true">
            <span class="vtrim-work-band-dur" id="vtrim-work-band-dur"></span>
          </div>
          <div class="vtrim-work-match" id="vtrim-work-match" aria-hidden="true"></div>
          <div class="vtrim-work-playhead" id="vtrim-work-playhead" aria-hidden="true"></div>
          <div class="vtrim-handle vtrim-handle-start" id="vtrim-handle-start" tabindex="0"
               title="Clip start — drag to set, or type the time below">
            <span class="vtrim-handle-tab"></span>
          </div>
          <div class="vtrim-handle vtrim-handle-end" id="vtrim-handle-end" tabindex="0"
               title="Clip end — drag to set, or type the time below">
            <span class="vtrim-handle-tab"></span>
          </div>
          <div class="vtrim-snap-flash" id="vtrim-snap-flash" aria-hidden="true"></div>
        </div>

        <!-- Magnified-frame popover removed: it was
             obscuring the keyframe filmstrip during the very drag the
             strip exists to support. Main video above already scrubs
             live to the handle position so the popover was redundant.
             NOTE: this comment intentionally avoids ALL backtick
             characters because the surrounding container.innerHTML
             template literal would parse them as string terminators
             and crash WKWebView with Unexpected-identifier — original
             wording with backticks around a code snippet actually
             would break the app. -->

        <!-- Numeric input row. Replaces the prior static "00:34 → 00:50"
             label with editable inputs; users type "0:14" or "1:23:45"
             for frame-accurate setting without dragging. -->
        <div class="vtrim-info">
          <label class="vtrim-info-label" for="vtrim-start-input">IN</label>
          <input type="text" id="vtrim-start-input" class="vtrim-input"
                 inputmode="text" autocomplete="off" autocorrect="off"
                 autocapitalize="off" spellcheck="false" size="9" maxlength="11"
                 title="Clip start — type mm:ss or hh:mm:ss, Enter to apply, Esc to cancel">
          <span class="vtrim-info-arrow">→</span>
          <label class="vtrim-info-label" for="vtrim-end-input">OUT</label>
          <input type="text" id="vtrim-end-input" class="vtrim-input"
                 inputmode="text" autocomplete="off" autocorrect="off"
                 autocapitalize="off" spellcheck="false" size="9" maxlength="11"
                 title="Clip end — type mm:ss or hh:mm:ss, Enter to apply, Esc to cancel">
          <span class="vtrim-info-dur" id="vtrim-info-dur"></span>
          <span class="vtrim-info-spacer"></span>
          <span class="vtrim-zoom-level" id="vtrim-zoom-level"
                title="Visible window of the working strip — scroll on the strip to zoom in/out, or use ⤢/⤡"></span>
          <button type="button" id="vtrim-step-back" class="vtrim-step-btn"
                  title="Step ⟨ 1 frame (,)  ·  Shift+⟨ steps 1 s"
                  aria-label="Step back one frame (comma) — Shift for one second">⟨</button>
          <button type="button" id="vtrim-step-fwd" class="vtrim-step-btn"
                  title="Step ⟩ 1 frame (.)  ·  Shift+⟩ steps 1 s"
                  aria-label="Step forward one frame (period) — Shift for one second">⟩</button>
          <button type="button" id="vtrim-zoom-fit" class="vtrim-icon-btn"
                  title="Zoom to fit the clip range"
                  aria-label="Zoom timeline to fit the clip range">⤢</button>
          <button type="button" id="vtrim-zoom-out" class="vtrim-icon-btn"
                  title="Show the whole video"
                  aria-label="Zoom timeline to show the whole video">⤡</button>
          <button type="button" id="vtrim-reset" class="vtrim-icon-btn"
                  title="Reset to the default window around the matched moment"
                  aria-label="Reset clip to the default window around the matched moment">↺</button>
        </div>

        <div class="vtrim-hint">
          Drag handles · click-drag on the strip to select · scroll on the strip to zoom · hold <kbd>⇧</kbd> while dragging to bypass snap · <kbd>I</kbd>/<kbd>O</kbd> set in/out at playhead · <kbd>,</kbd>/<kbd>.</kbd> step ±1 frame · <kbd>⌘E</kbd> exports
        </div>
      </div>
    </div>
  `;
  const video = container.querySelector("video");
  // Same error-toast wire as the audio path — without this the entire
  // dual-zoom trim widget tries to render against a never-loaded video
  // (loadedmetadata never fires → dur stays 0 → /api/file/thumbnails
  // still works but the strip computes a 0-second view and falls over
  // silently). User would just see a broken-media icon in the player
  // area with no toast or explanation.
  _bindMediaErrorToast(video, hit, "Video");
  video.addEventListener("loadedmetadata", () => {
    // Staleness guard. When the user arrows through
    // hits fast (↓ key-repeat auto-preview), container.innerHTML is
    // replaced before THIS video finished loading — but the detached
    // element still fires loadedmetadata. Without the guard, the
    // callback ran _setupVideoTrim against the NEXT hit's fresh DOM:
    // double-bound handle/wheel/contextmenu listeners, plus a second
    // ResizeObserver whose stale closure rewrote #player-wave's
    // dataset.clipStartMs/EndMs with the PREVIOUS hit's range — and
    // detail.js reads exactly that dataset for ⌘E export, so the
    // user exported the WRONG clip range. isConnected is false for
    // any element no longer in the live DOM — exactly the stale case.
    if (!video.isConnected) return;
    video.currentTime = (hit.ts_ms || 0) / 1000;
    _setupVideoTrim(container, hit, video);
  });
  _bindMediaShortcuts(video);
}


// ─────────────────────────────────────────────────────────────────────
// Video trim-range picker — CapCut / Final Cut-grade dual-zoom timeline.
// (Below this banner is the FULL rewrite that replaces _setupVideoTrim.)
//
// Architecture: TWO strips stacked vertically.
//   1. Overview minimap (28 px, full duration) — at-a-glance pan
//      target. Dark band shows clip range, yellow viewport rectangle
//      shows what the working strip is currently displaying.
//   2. Working strip (96 px, zoomed) — where editing happens. Big
//      thumbnails, big handles, playhead, match marker, snap targets.
//
// Thumbnails come from the BACKEND (/api/file/thumbnails — reads the
// already-extracted ffmpeg keyframes the indexer produced). Replaces
// the prior client-side seek-and-canvas extraction, which was 600-1500
// ms per video, sometimes hung on Safari, and got re-extracted every
// time the user re-opened a hit.
//
// Behaviors:
//   - Drag handles with scrubbing preview (main video jumps to handle
//     time) + magnified-frame popover near the cursor.
//   - Click-drag empty strip area = make a fresh selection.
//   - Hover empty strip area = ghost-scrub the main video without
//     committing currentTime (revert on mouseleave).
//   - Scroll wheel on the working strip = zoom in/out, anchored at
//     cursor position.
//   - Drag viewport on the overview to pan the working strip.
//   - Snap-to-playhead and snap-to-match within 8 px during handle
//     drag, with a flash animation.
//   - I/O: set in/out at the playhead (existing).
//   - , / . step ±1 frame (assume 30fps fallback); Shift+,/. step ±1 s.
//   - Numeric timecode inputs in the info row: type "0:14" or
//     "1:23:45", Enter commits, Esc reverts.
//   - ↺ resets to default ±1.5 s window around match.
//   - ⤢ zooms to fit the current clip range; ⤡ zooms to full video.
//
// Output contract: writes clipStartMs / clipEndMs on the working strip
// element's dataset (id "player-wave" — alias preserved so detail.js
// _exportClip lookup keeps working unchanged across audio + video).
// ─────────────────────────────────────────────────────────────────────

// (Old single-strip rewrite history kept below for reference until the
// new path replaces it. Going forward the only entry point is the new
// _setupVideoTrim defined further down.)
// (Original CapCut-style single-strip docstring follows; kept as
// historical context for the rationale of the BIG visual jump.)
//
//   - tall (64 px) so there's room for a thumbnail filmstrip
//   - 12-frame thumbnails extracted via canvas from a hidden duplicate
//     <video>, cached by file_id so flipping between hits doesn't
//     re-seek the source every time
//   - playhead that tracks video.currentTime (parity with audio player)
//   - chunky 14-px handles with grippy vertical lines
//   - drag-to-select on empty area = make a brand-new range (text-style)
//   - scrubbing preview: dragging a handle live-seeks the main <video>
//     so you SEE the frame at the cut, exactly like an NLE
//   - numeric timecode inputs that accept mm:ss or hh:mm:ss for frame-
//     accurate setting (snap to nearest seekable time)
//   - one-click reset button to restore the default ±1.5 s window
//
// Output contract unchanged: writes clipStartMs / clipEndMs on the strip
// element's dataset; detail.js's _exportClip reads them as before.
// Parse "ss", "mm:ss", "mm:ss.SSS", or "hh:mm:ss[.SSS]" → seconds (float).
// Returns null on anything we can't make sense of so the caller can
// fall back to the current value instead of clobbering it with 0.
function _parseTimecodeToSeconds(input) {
  if (input == null) return null;
  const s = String(input).trim();
  if (!s) return null;
  const parts = s.split(":");
  if (parts.length > 3) return null;
  let total = 0;
  for (let i = 0; i < parts.length; i++) {
    const p = parts[i];
    if (!/^\d+(\.\d+)?$/.test(p)) return null;
    total = total * 60 + parseFloat(p);
  }
  return isFinite(total) && total >= 0 ? total : null;
}

// Cache backend keyframe lists by file_id. The endpoint returns
// {ts_ms, url}[] sorted; we keep the parsed array. Re-opening the same
// hit (very common via ↓ auto-preview) is then instant.
const _vtrimThumbsCache = new Map();

async function _fetchVtrimThumbs(fileId) {
  if (_vtrimThumbsCache.has(fileId)) return _vtrimThumbsCache.get(fileId);
  try {
    // Use api.fileThumbnails instead of raw fetch — inherits the
    // default 60 s timeout. Lower-priority than the
    // other api.js migrations (this fn has its own
    // per-file negative cache, and the trim widget renders with a
    // gradient fallback if thumbs don't arrive — a wedged fetch
    // only blocks the FILMSTRIP, not the player itself) but the
    // consistency matters: every api.js _json caller now goes
    // through one timeout + error-translation path.
    const data = await api.fileThumbnails(fileId);
    const arr = Array.isArray(data?.thumbnails) ? data.thumbnails : [];
    _vtrimThumbsCache.set(fileId, arr);
    return arr;
  } catch {
    _vtrimThumbsCache.set(fileId, []);   // negative cache — don't re-spam
    return [];
  }
}

// Format a ruler tick label. Same rule as img.ly: short clips show
// seconds with one decimal; longer clips collapse to m:ss / h:mm:ss.
function _fmtTick(s) {
  if (s < 10) return s.toFixed(s % 1 ? 1 : 0) + "s";
  if (s < 60) return Math.round(s) + "s";
  return _fmtTime(s);
}

// NEW dual-zoom video trim picker. Replaces the prior single-strip
// 18-px design and the later single-strip 64-px design. See the
// banner docstring above the next-older helpers for the full
// architecture / behavior spec.
function _setupVideoTrim(container, hit, video) {
  const root      = container.querySelector("#vtrim");
  const overview  = container.querySelector("#vtrim-overview");
  const overThumbs= container.querySelector("#vtrim-overview-thumbs");
  const overClip  = container.querySelector("#vtrim-overview-clip");
  const overMatch = container.querySelector("#vtrim-overview-match");
  const overPlay  = container.querySelector("#vtrim-overview-playhead");
  const viewport  = container.querySelector("#vtrim-overview-viewport");
  const ruler     = container.querySelector("#vtrim-ruler");
  const work      = container.querySelector("#vtrim-work");
  const workThumbs= container.querySelector("#vtrim-work-thumbs");
  const workBand  = container.querySelector("#vtrim-work-band");
  const workMatch = container.querySelector("#vtrim-work-match");
  const workPlay  = container.querySelector("#vtrim-work-playhead");
  const sh        = container.querySelector("#vtrim-handle-start");
  const eh        = container.querySelector("#vtrim-handle-end");
  const snapFlash = container.querySelector("#vtrim-snap-flash");
  const startIn   = container.querySelector("#vtrim-start-input");
  const endIn     = container.querySelector("#vtrim-end-input");
  const durLabel  = container.querySelector("#vtrim-info-dur");
  const stepBack  = container.querySelector("#vtrim-step-back");
  const stepFwd   = container.querySelector("#vtrim-step-fwd");
  const zoomFit   = container.querySelector("#vtrim-zoom-fit");
  const zoomOut   = container.querySelector("#vtrim-zoom-out");
  const resetBtn  = container.querySelector("#vtrim-reset");
  // Hoisted out of _layoutAndPublish: that function
  // runs on every drag mousemove + every auto-pan rAF tick, and
  // per-call container.querySelector lookups there contradicted the
  // file's own memoization rationale for the thumbnail strip.
  const bandDur   = container.querySelector("#vtrim-work-band-dur");
  const zoomLevel = container.querySelector("#vtrim-zoom-level");

  if (!root || !work || !video.duration || !isFinite(video.duration)) return;
  // Keep the `#player-wave` alias on the WORKING strip so detail.js's
  // _exportClip dataset lookup works unchanged across audio + video.
  work.id = "player-wave";

  const dur = video.duration;
  const matchS = (hit.ts_ms || 0) / 1000;
  // Library-opened files (empty.js _fileToFakeHit) pass ts_ms=0 because
  // there's no search match — they're just "the user clicked this file."
  // Without this gate the trim widget rendered a "Matched moment @ 0:00"
  // marker over the very first frame AND the snap-to-match logic pulled
  // the start handle to 0:00 every time the user tried to set a clean
  // clip start near the file beginning ("why won't this stop snapping
  // to zero"). Real search hits AT exactly 0:00 are vanishingly rare in
  // practice — the worst case for them is a missing reference marker.
  const hasMatch = (hit.ts_ms || 0) > 0;
  const defaultStartS = Math.max(0, matchS - CLIP_DEFAULT_PADDING_S);
  const defaultEndS   = Math.min(dur, defaultStartS + CLIP_DEFAULT_DURATION_S);
  let startS = defaultStartS;
  let endS   = defaultEndS;

  // Working-strip visible window. Defaults to 2× the clip duration
  // centred on the clip, so the user has equal headroom on both sides
  // to extend a handle. Clamped to [5s, dur].
  let viewStartS = 0;
  let viewEndS   = dur;
  function _setView(s, e) {
    const minSpan = 4.0;            // floor: 4s visible (≈ frame-step territory)
    const maxSpan = dur;
    let span = Math.max(minSpan, Math.min(maxSpan, e - s));
    let cs = Math.max(0, Math.min(dur - span, s));
    let ce = cs + span;
    viewStartS = cs;
    viewEndS   = ce;
  }
  function _fitView() {
    const span = Math.max(4, (endS - startS) * 2.4);
    const center = (startS + endS) / 2;
    _setView(center - span / 2, center + span / 2);
  }
  function _zoomViewToAll() { _setView(0, dur); }
  _fitView();   // initial zoom centred on the clip

  // Helpers: time ↔ x within the WORKING strip
  function _workRect() { return work.getBoundingClientRect(); }
  function _tToPctInView(t) {
    return ((t - viewStartS) / (viewEndS - viewStartS)) * 100;
  }
  function _xToTimeInView(clientX) {
    const r = _workRect();
    return viewStartS + ((clientX - r.left) / r.width) * (viewEndS - viewStartS);
  }

  // Suppress _layoutAndPublish overwriting an in-progress typed value.
  let _typingInInput = false;

  // Memoise the last-rendered view window so we don't rebuild the
  // thumbnail strip + ruler on every handle-drag mousemove (which only
  // changes clipStartS/clipEndS, not the view). Without this, dragging
  // a handle thrashes `workThumbs.innerHTML = ...` ~60×/sec — measurable
  // jank on a 100-keyframe video. Tracks the view, not the clip, so
  // pan/zoom paths still re-render normally.
  let _lastViewStart = NaN;
  let _lastViewEnd   = NaN;

  function _layoutAndPublish() {
    // Overview: full-duration coordinates (0 .. dur)
    const ovStartPct = (startS / dur) * 100;
    const ovEndPct   = (endS   / dur) * 100;
    overClip.style.left  = ovStartPct + "%";
    overClip.style.width = (ovEndPct - ovStartPct) + "%";
    overMatch.style.left = ((matchS / dur) * 100) + "%";
    overMatch.style.opacity = hasMatch ? "" : "0";

    // Overview viewport indicator
    const vpStartPct = (viewStartS / dur) * 100;
    const vpEndPct   = (viewEndS   / dur) * 100;
    viewport.style.left  = vpStartPct + "%";
    viewport.style.width = (vpEndPct - vpStartPct) + "%";

    // Working strip: view-relative coordinates
    const wStartPct = _tToPctInView(startS);
    const wEndPct   = _tToPctInView(endS);
    const wMatchPct = _tToPctInView(matchS);
    // Visually clamp the band to the viewport edges so when the clip
    // extends past the current view (manual zoom-in past the clip span,
    // typed IN/OUT outside the visible window, restored save with a
    // wider range than the default view), the band renders a clean
    // rectangle inside the strip instead of bleeding off via the parent
    // overflow:hidden — previously the right edge of a 1s-25s clip
    // viewed at 0-16s zoom just disappeared into the right margin,
    // looking like the trim widget was broken. Toggling the data-clipped-
    // left/right attributes lets the CSS draw a chevron at the clamped
    // edge so the user knows the selection continues past the visible
    // window instead of stopping at the strip border.
    const bandLeftPct  = Math.max(0, wStartPct);
    const bandRightPct = Math.min(100, wEndPct);
    workBand.style.left  = bandLeftPct + "%";
    workBand.style.width = Math.max(0, bandRightPct - bandLeftPct) + "%";
    workBand.dataset.clippedLeft  = wStartPct < 0    ? "1" : "";
    workBand.dataset.clippedRight = wEndPct   > 100  ? "1" : "";
    // Floating duration badge on the band — Final Cut / Premiere /
    // CapCut convention: the clip rectangle on the timeline shows its
    // own length as a small pill in the middle, so the editors eye
    // doesnt have to flick down to the IN/OUT row below the strip to
    // read the duration mid-trim. CSS auto-hides the badge when the
    // visible band is < 60px wide (the pill wouldnt fit + would
    // overlap the handle hit-areas). Mirrors the IN/OUT input row
    // duration label format for consistency.
    if (bandDur) {
      const dSec = endS - startS;
      bandDur.textContent = dSec < 60 ? `${dSec.toFixed(1)}s` : _fmtTime(dSec);
    }
    // Native tooltip on the working strip — surfaces the full
    // clip-range AND (when clamped) the off-screen extent so a
    // hover explains the chevron stripe: the user
    // sees the stripe but otherwise has to read the IN/OUT input
    // row to learn HOW FAR the selection continues past the visible
    // window. Tooltip goes on `work` (not `workBand`) because the
    // band has pointer-events:none so click-drag on the strip can
    // start a fresh selection AT any point; strip-level title fires
    // on any hover with the tooltip rendering after the macOS
    // native ~1s delay so it doesnt fight quick scrub interactions.
    {
      const dSec = endS - startS;
      const durStr = dSec < 60 ? `${dSec.toFixed(1)}s` : _fmtTime(dSec);
      const rangeStr = `${_fmtTime(startS)} → ${_fmtTime(endS)} (${durStr})`;
      const leftOver  = Math.max(0, viewStartS - startS);
      const rightOver = Math.max(0, endS - viewEndS);
      let overflowStr = "";
      if (leftOver > 0 || rightOver > 0) {
        const parts = [];
        if (leftOver  > 0) parts.push(`${leftOver.toFixed(1)}s before view`);
        if (rightOver > 0) parts.push(`${rightOver.toFixed(1)}s past view`);
        overflowStr = ` — ${parts.join(", ")}`;
      }
      work.title = `Clip: ${rangeStr}${overflowStr}`;
    }
    // Zoom-level indicator. Shows the visible
    // window of the working strip so the user knows how zoomed in
    // they are — without it, a scroll-zoom can quietly land them at
    // a 4s view (the floor) with no on-screen cue and they think the
    // widget is broken when the band fills the whole strip. Pro NLEs
    // (Premiere, DaVinci, Final Cut) all surface the zoom span in a
    // status row for the same reason. Format mirrors the rest of
    // the trim widget: < 60s → "12s", else mm:ss / hh:mm:ss.
    if (zoomLevel) {
      const visibleSec = viewEndS - viewStartS;
      const fmt = visibleSec < 60 ? `${Math.round(visibleSec)}s` : _fmtTime(visibleSec);
      zoomLevel.textContent = `view ${fmt}`;
    }
    // Only show handles + match marker inside the visible window;
    // pseudo-hide via opacity when offscreen so we don't draw
    // confusingly-clipped widgets on the edge.
    function _setVisibleX(el, pct, opacity) {
      el.style.left = pct + "%";
      el.style.opacity = String(opacity);
    }
    _setVisibleX(sh, wStartPct, wStartPct >= -1 && wStartPct <= 101 ? 1 : 0);
    _setVisibleX(eh, wEndPct,   wEndPct   >= -1 && wEndPct   <= 101 ? 1 : 0);
    _setVisibleX(workMatch, wMatchPct, (hasMatch && wMatchPct >= 0 && wMatchPct <= 100) ? 1 : 0);
    workMatch.title = `Matched moment @ ${_fmtTime(matchS)}`;

    // Dataset for the existing export pipeline
    work.dataset.clipStartMs = String(Math.round(startS * 1000));
    work.dataset.clipEndMs   = String(Math.round(endS   * 1000));

    // Numeric inputs (unless user is typing)
    if (!_typingInInput) {
      if (startIn) startIn.value = _fmtTime(startS);
      if (endIn)   endIn.value   = _fmtTime(endS);
    }
    const d = endS - startS;
    if (durLabel) durLabel.textContent = d < 60
      ? `${d.toFixed(1)}s`
      : _fmtTime(d);

    // Re-render the working-strip thumbnails + ruler ONLY when the view
    // window changed. Drag-handle paths keep the view fixed but call
    // _layoutAndPublish on every mousemove — re-painting the filmstrip
    // + ruler there is ~60 DOM-heavy paints per second on a 100-keyframe
    // video, measurable as scrub jank on lower-end Macs. Pan/zoom paths
    // (scroll, viewport drag, zoom buttons, reset) DO change the view,
    // so they correctly trigger re-render.
    if (viewStartS !== _lastViewStart || viewEndS !== _lastViewEnd) {
      _lastViewStart = viewStartS;
      _lastViewEnd   = viewEndS;
      _renderWorkThumbs();
      _drawRuler();
    }
  }

  // ── Thumbnails (backend keyframes) ───────────────────────────────
  let _thumbsAll = [];  // [{ts_ms, url}, ...]
  _fetchVtrimThumbs(hit.file_id).then(arr => {
    _thumbsAll = arr;
    _renderOverviewThumbs();
    _renderWorkThumbs();
  });

  function _renderOverviewThumbs() {
    if (!_thumbsAll.length) {
      // Soft gradient fallback (matches CSS default)
      overThumbs.innerHTML = "";
      return;
    }
    // Tile thumbs uniformly across the overview width (max 40 to cap DOM)
    const n = Math.min(40, _thumbsAll.length);
    const stride = Math.max(1, Math.floor(_thumbsAll.length / n));
    const picked = [];
    for (let i = 0; i < _thumbsAll.length; i += stride) picked.push(_thumbsAll[i]);
    const tilePct = 100 / picked.length;
    overThumbs.innerHTML = picked.map(t => `
      <span class="vtrim-thumb-tile" style="background-image:url('${t.url}');width:${tilePct}%;"></span>
    `).join("");
  }

  function _renderWorkThumbs() {
    if (!_thumbsAll.length) {
      workThumbs.innerHTML = "";
      return;
    }
    // Keyframes are sparse on the time axis (scene cuts + interval
    // supplement). For the WORKING strip we lay them out by their
    // actual ts_ms — each tile spans from one keyframe's time to the
    // next keyframe's time, in view-relative %. Tiles outside the
    // view are skipped; partial-overlap tiles are clipped.
    const span = viewEndS - viewStartS;
    if (span <= 0) { workThumbs.innerHTML = ""; return; }
    const items = [];
    for (let i = 0; i < _thumbsAll.length; i++) {
      const t = _thumbsAll[i];
      const nextT = (i + 1 < _thumbsAll.length) ? _thumbsAll[i + 1].ts_ms : dur * 1000;
      const a = Math.max(viewStartS, t.ts_ms / 1000);
      const b = Math.min(viewEndS,   nextT / 1000);
      if (b <= a) continue;
      const leftPct  = ((a - viewStartS) / span) * 100;
      const widthPct = ((b - a)          / span) * 100;
      items.push(`<span class="vtrim-thumb-tile" style="background-image:url('${t.url}');left:${leftPct}%;width:${widthPct}%;"></span>`);
    }
    workThumbs.innerHTML = items.join("");
  }

  // ── Ruler ─────────────────────────────────────────────────────────
  function _drawRuler() {
    const span = viewEndS - viewStartS;
    if (span <= 0) { ruler.innerHTML = ""; return; }
    // Choose a tick interval that lands ~6-10 majors in the visible span
    const targetTicks = 8;
    const rawInterval = span / targetTicks;
    const niceSteps = [0.1, 0.2, 0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300, 600];
    let interval = niceSteps[0];
    for (const step of niceSteps) {
      if (step >= rawInterval) { interval = step; break; }
    }
    const first = Math.ceil(viewStartS / interval) * interval;
    const items = [];
    for (let t = first; t <= viewEndS; t += interval) {
      const pct = ((t - viewStartS) / span) * 100;
      items.push(`<span class="vtrim-tick" style="left:${pct}%;"><span class="vtrim-tick-label">${_fmtTick(t)}</span></span>`);
    }
    ruler.innerHTML = items.join("");
  }

  // ── Playhead tracking on BOTH strips ─────────────────────────────
  function _movePlayhead() {
    if (!isFinite(video.currentTime) || !dur) return;
    const ovPct = (video.currentTime / dur) * 100;
    overPlay.style.left = ovPct + "%";
    const wPct = _tToPctInView(video.currentTime);
    workPlay.style.left = wPct + "%";
    workPlay.style.opacity = (wPct >= 0 && wPct <= 100) ? "1" : "0";
  }
  video.addEventListener("timeupdate", _movePlayhead);
  video.addEventListener("seeked",     _movePlayhead);

  // ── Snap helpers ─────────────────────────────────────────────────
  // Snap target time → x px in viewport; returns target time if within
  // SNAP_PX of the current cursor, else null. Only snaps to points
  // VISIBLE in the current view (img.ly rule).
  //
  // `opts.playheadAt` lets callers freeze the playhead snap target to a
  // captured-at-drag-start value. Critical for the handle-drag path:
  // the drag's own scrub-preview keeps writing `video.currentTime = nextT`
  // on every mousemove, so reading `video.currentTime` here would snap
  // the handle to its OWN previous position (a constant ~0 px delta)
  // and effectively pin it after the first sub-snap movement. Frozen-
  // value snap behaves like every NLE: "snap to where the playhead
  // WAS when I started dragging," not "snap to wherever I'm currently
  // scrubbing to."
  const SNAP_PX = 8;
  function _maybeSnap(currentT, opts = {}) {
    const span = viewEndS - viewStartS;
    // Accept a caller-provided rect: _applyMove
    // already measured _workRect() this same frame — re-measuring
    // here doubled the getBoundingClientRect cost per mousemove.
    const w = (opts.workRect || _workRect()).width;
    if (w <= 0 || span <= 0) return null;
    const pxPerSec = w / span;
    const playheadT = (opts.playheadAt != null) ? opts.playheadAt : video.currentTime;
    const candidates = [
      { label: "playhead", t: playheadT },
    ];
    // Only snap to the match marker when there's a real search match —
    // library-opened files have matchS=0 (no semantic meaning) and
    // snapping there pulls the start handle to 0:00 on every drag near
    // the file beginning.
    if (hasMatch) candidates.push({ label: "match", t: matchS });
    // Keyframe-boundary snap. The indexer already extracted keyframes
    // at semantically meaningful timestamps, and the filmstrip below
    // the handles is rendered FROM those exact ts_ms values. Snapping
    // there feels precise (handle lines up with the visible thumb
    // edge) AND tends to land the cut on a real I-frame — avoiding a
    // fractional-GOP re-encode on export.
    //
    // Saturation gate: when the view is zoomed OUT
    // far enough that adjacent keyframes are closer together on screen
    // than the snap radius, EVERY cursor position is within SNAP_PX of
    // some keyframe — handle motion becomes permanently keyframe-
    // quantized (jumps thumb-to-thumb, fine positioning impossible
    // without Shift). Only register keyframe candidates when the
    // average on-screen spacing comfortably exceeds the snap diameter,
    // i.e. snapping is a deliberate act near a visible thumb edge, not
    // an ambient grid. avgGap uses count-over-duration (cheap, no
    // sort); 3× SNAP_PX (~24 px) keeps clear free space between zones.
    if (_thumbsAll.length) {
      const avgGapPx = (dur / _thumbsAll.length) * pxPerSec;
      if (avgGapPx > 3 * SNAP_PX) {
        for (const t of _thumbsAll) {
          const tSec = (t.ts_ms || 0) / 1000;
          candidates.push({ label: "keyframe", t: tSec });
        }
      }
    }
    let best = null;
    for (const c of candidates) {
      if (c.t < viewStartS || c.t > viewEndS) continue;
      const dPx = Math.abs((c.t - currentT) * pxPerSec);
      if (dPx <= SNAP_PX && (!best || dPx < best.dPx)) best = { ...c, dPx };
    }
    return best;
  }
  function _flashSnap(t, label) {
    const pct = _tToPctInView(t);
    snapFlash.style.left = pct + "%";
    // Color the flash by snap type so the user
    // gets a glanceable hint about WHAT their handle just snapped
    // to — playhead (blue, matches the playhead bar), match marker
    // (yellow, matches the match line + filled-star bookmark
    // convention), or keyframe (green, fresh + neutral, doesnt
    // collide with the other two). Previously every snap
    // flashed the same accent color so a power user dragging fast
    // couldnt tell whether they snapped to the right target. The
    // data-snap attribute drives the CSS variant — clears prior
    // values so the right styling applies even if the previous
    // flash was a different kind.
    snapFlash.dataset.snap = label || "playhead";
    snapFlash.classList.remove("show");
    // Re-trigger animation
    void snapFlash.offsetWidth;
    snapFlash.classList.add("show");
  }

  // ── Scrub-preview popover removed ─────────────────────────────────
  // The drag-anchored 260×160 magnified-frame popover was obscuring
  // the keyframe filmstrip during the very interaction the strip
  // exists to support (the point is to SEE the keyframes around the
  // in/out point being set; the popover covered them). The main
  // <video> element directly above the timeline already live-scrubs
  // to the current handle position via `video.currentTime = nextT`
  // in the drag handler, so the in/out frame is visible at full size
  // in the player — the popover was redundant + harmful.
  //
  // No-op stubs preserved so the call sites in the drag handlers
  // below stay readable and don't need a conditional branch. If we
  // ever want to re-add a *smaller* preview (e.g., 80px thumb
  // anchored ABOVE the strip with `top: -90px`), this is where to
  // wire it back in. The original implementation lived in git
  // history before this change if needed.
  function _showPreview(/* timeS, anchorClientX */) { /* no-op */ }
  function _hidePreview() { /* no-op */ }

  // ── Handle drag (with snap + live main-video scrub + edge auto-pan) ──
  // Edge auto-pan: when the cursor enters the leftmost / rightmost
  // EDGE_PAN_PX-wide strip during a drag, the view scrolls in that
  // direction so the user can keep dragging past the currently-visible
  // window without having to release, scroll, and re-grab the handle.
  // Speed scales with how deep into the edge zone the cursor is (0
  // at the zone boundary → max at the very edge), and the handle's
  // time keeps following the cursor as the view pans under it. Same
  // convention as Premiere / Final Cut / DaVinci timeline panels.
  const EDGE_PAN_PX = 32;     // width of the edge auto-pan trigger zone
  const EDGE_PAN_MAX_FRAC = 0.06;  // up to 6% of view-span per frame at the very edge
  function _bindHandle(handle, which) {
    function _onDown(ev) {
      ev.preventDefault();
      ev.stopPropagation();
      if (!video.paused) video.pause();
      handle.classList.add("dragging");
      const origStart = startS, origEnd = endS;
      // Capture the offset between the cursor's time and the handle's
      // time at mousedown so the handle doesn't "jump" to the cursor
      // (the cursor is somewhere inside the 28px handle hit-area, not
      // exactly at the logical handle position). Switched from the
      // earlier delta-math to absolute-cursor-time math because the
      // delta math breaks when the view auto-pans mid-drag — the
      // `(mv.clientX - startX) / width * span` formula assumes a
      // stationary view, which is no longer true with edge auto-pan.
      const downCursorT = _xToTimeInView(ev.clientX);
      const initialOffsetT = (which === "start" ? origStart : origEnd) - downCursorT;
      // Capture the playhead's position at drag-start so snap-to-
      // playhead has a stable target — without this, the live scrub
      // moves the playhead with the handle, making the playhead snap
      // target = "wherever I just dragged to" (always within snap
      // distance → pinned handle bug).
      const origPlayhead = video.currentTime;

      // Current cursor coords — updated on mousemove, also read by the
      // auto-pan rAF loop so the pan continues even when the cursor is
      // held still past the edge (mousemove stops firing in that case).
      let lastClientX = ev.clientX, lastClientY = ev.clientY;
      // Track shiftKey for snap-bypass. Standard
      // NLE convention (Premiere, DaVinci, Final Cut): hold Shift while
      // dragging a clip edge to temporarily disable snap. Without this
      // a user trying to set IN exactly 0.5s past a keyframe boundary
      // had no way to escape the 8px snap zone — Tern would yank the
      // handle to the keyframe edge every time. Tracking shift in the
      // closure (instead of reading mv.shiftKey only on mousemove)
      // means the edge-auto-pan rAF loop also respects the current
      // shift state when it re-applies the move with a stale cursor.
      let lastShift = !!ev.shiftKey;
      let _panRAF = 0;

      function _applyMove() {
        const r = _workRect();
        // Edge auto-pan: if cursor is in the left/right edge zone AND
        // the view can pan in that direction, shift the view by a
        // fraction proportional to how deep into the zone the cursor is.
        const leftOver  = (r.left  + EDGE_PAN_PX) - lastClientX;
        const rightOver = lastClientX - (r.right - EDGE_PAN_PX);
        const span = viewEndS - viewStartS;
        if (leftOver > 0 && viewStartS > 0) {
          const depth = Math.min(1, leftOver / EDGE_PAN_PX);
          const shift = depth * EDGE_PAN_MAX_FRAC * span;
          _setView(viewStartS - shift, viewEndS - shift);
        } else if (rightOver > 0 && viewEndS < dur) {
          const depth = Math.min(1, rightOver / EDGE_PAN_PX);
          const shift = depth * EDGE_PAN_MAX_FRAC * span;
          _setView(viewStartS + shift, viewEndS + shift);
        }
        // Compute handle's target time from CURRENT cursor position +
        // the initial cursor-to-handle offset. _xToTimeInView accepts
        // cursor X outside the strip (returns < viewStartS or >
        // viewEndS) — we clamp below to file bounds + min-duration.
        const cursorT = _xToTimeInView(lastClientX);
        let nextT = cursorT + initialOffsetT;
        if (which === "start") {
          nextT = Math.max(0, Math.min(origEnd - CLIP_MIN_DURATION_S, nextT));
        } else {
          nextT = Math.min(dur, Math.max(origStart + CLIP_MIN_DURATION_S, nextT));
        }
        const snap = lastShift ? null : _maybeSnap(nextT, { playheadAt: origPlayhead, workRect: r });
        if (snap) {
          if (which === "start") nextT = Math.min(origEnd - CLIP_MIN_DURATION_S, Math.max(0, snap.t));
          else                   nextT = Math.max(origStart + CLIP_MIN_DURATION_S, Math.min(dur, snap.t));
          _flashSnap(snap.t, snap.label);
        }
        if (which === "start") startS = nextT; else endS = nextT;
        // Live scrub the MAIN video so the user sees the frame at the cut
        if (Math.abs(video.currentTime - nextT) > 0.05) video.currentTime = nextT;
        _layoutAndPublish();
      }
      function _tick() {
        _applyMove();
        // Keep ticking ONLY if cursor is still in an edge zone with room
        // to pan — otherwise the loop stops and resumes on next mousemove.
        const r = _workRect();
        const leftOver  = (r.left  + EDGE_PAN_PX) - lastClientX;
        const rightOver = lastClientX - (r.right - EDGE_PAN_PX);
        const stillPanning =
          (leftOver  > 0 && viewStartS > 0) ||
          (rightOver > 0 && viewEndS   < dur);
        if (stillPanning) _panRAF = requestAnimationFrame(_tick);
        else _panRAF = 0;
      }
      function _move(mv) {
        lastClientX = mv.clientX;
        lastClientY = mv.clientY;
        lastShift   = !!mv.shiftKey;
        _applyMove();
        // If cursor entered the edge zone AND no rAF loop is active,
        // start one so panning continues when cursor is held still.
        if (!_panRAF) {
          const r = _workRect();
          const leftOver  = (r.left  + EDGE_PAN_PX) - lastClientX;
          const rightOver = lastClientX - (r.right - EDGE_PAN_PX);
          const inEdgeZone =
            (leftOver  > 0 && viewStartS > 0) ||
            (rightOver > 0 && viewEndS   < dur);
          if (inEdgeZone) _panRAF = requestAnimationFrame(_tick);
        }
      }
      function _up() {
        window.removeEventListener("mousemove", _move);
        window.removeEventListener("mouseup", _up);
        handle.classList.remove("dragging");
        if (_panRAF) { cancelAnimationFrame(_panRAF); _panRAF = 0; }
      }
      window.addEventListener("mousemove", _move);
      window.addEventListener("mouseup", _up);
    }
    handle.addEventListener("mousedown", _onDown);
    // Keyboard nudge while handle has focus (Tab into it, then arrows)
    handle.addEventListener("keydown", (ev) => {
      if (ev.key !== "ArrowLeft" && ev.key !== "ArrowRight") return;
      ev.preventDefault();
      const step = (ev.shiftKey ? 1 : 1 / 30) * (ev.key === "ArrowLeft" ? -1 : 1);
      if (which === "start") {
        startS = Math.max(0, Math.min(endS - CLIP_MIN_DURATION_S, startS + step));
      } else {
        endS = Math.min(dur, Math.max(startS + CLIP_MIN_DURATION_S, endS + step));
      }
      _layoutAndPublish();
    });
  }
  _bindHandle(sh, "start");
  _bindHandle(eh, "end");

  // ── Working-strip click-drag = make a fresh selection (with edge auto-pan) ─
  // Same auto-pan UX as the handle-drag handler above so click-drag
  // selection feels identical to dragging an existing handle past the
  // edge — pro NLE convention. Without this, a user click-dragging from
  // the middle of the strip toward the right edge to select a clip
  // would have their selection silently clamp at viewEndS and they'd
  // think the strip is broken (cursor moves past edge, selection
  // doesn't follow). With it: view auto-scrolls to follow the cursor
  // and the initialT anchor stays at its original absolute time even
  // as the view pans under it, so the selection extends correctly.
  work.addEventListener("mousedown", (ev) => {
    if (ev.target === sh || ev.target === eh
        || sh.contains(ev.target) || eh.contains(ev.target)) return;
    const initialX = ev.clientX;
    // initialT must be captured as an ABSOLUTE time (it's already that
    // — _xToTimeInView returns viewStart+offset which is absolute) and
    // NOT recomputed each frame. If the view pans, initialT stays put
    // and `curT` follows the cursor; together they define the live
    // selection [min,max].
    const initialT = _xToTimeInView(initialX);
    let didDrag = false;
    let lastClientX = ev.clientX, lastClientY = ev.clientY;
    let _panRAF = 0;

    function _applyMove() {
      // Edge auto-pan first (same EDGE_PAN_PX / EDGE_PAN_MAX_FRAC as
      // _bindHandle above so the feel is consistent across both drag
      // surfaces).
      const r = _workRect();
      const leftOver  = (r.left  + EDGE_PAN_PX) - lastClientX;
      const rightOver = lastClientX - (r.right - EDGE_PAN_PX);
      const span = viewEndS - viewStartS;
      if (leftOver > 0 && viewStartS > 0) {
        const depth = Math.min(1, leftOver / EDGE_PAN_PX);
        const shift = depth * EDGE_PAN_MAX_FRAC * span;
        _setView(viewStartS - shift, viewEndS - shift);
      } else if (rightOver > 0 && viewEndS < dur) {
        const depth = Math.min(1, rightOver / EDGE_PAN_PX);
        const shift = depth * EDGE_PAN_MAX_FRAC * span;
        _setView(viewStartS + shift, viewEndS + shift);
      }
      // _xToTimeInView returns absolute time and is safe to call with
      // a cursor X outside the strip rect (returns < viewStartS or
      // > viewEndS). Clamp ONLY to [0, dur] (file bounds) — NOT to
      // [viewStartS, viewEndS] (view bounds) because we WANT the
      // selection to extend past the visible window during a pan.
      const curT = Math.max(0, Math.min(dur, _xToTimeInView(lastClientX)));
      startS = Math.max(0, Math.min(initialT, curT));
      endS   = Math.min(dur, Math.max(initialT, curT, startS + CLIP_MIN_DURATION_S));
      // Live scrub the main video so the user sees the frame at the
      // moving edge of the click-drag selection (parity with the
      // handle-drag scrub at line ~948). Previously the handle
      // drag scrubbed but a fresh click-drag selection didnt — you
      // dragged out a 24s range and the player kept showing the
      // OLD frame from before the drag started. Pause first so the
      // main element doesnt fight the scrub by autoplaying past
      // every set frame. 50ms epsilon dedupes redundant writes
      // (matches the handle-drag scrub gate).
      if (!video.paused) video.pause();
      if (Math.abs(video.currentTime - curT) > 0.05) video.currentTime = curT;
      _layoutAndPublish();
    }
    function _tick() {
      _applyMove();
      const r = _workRect();
      const leftOver  = (r.left  + EDGE_PAN_PX) - lastClientX;
      const rightOver = lastClientX - (r.right - EDGE_PAN_PX);
      const stillPanning =
        (leftOver  > 0 && viewStartS > 0) ||
        (rightOver > 0 && viewEndS   < dur);
      if (stillPanning) _panRAF = requestAnimationFrame(_tick);
      else _panRAF = 0;
    }
    function _move(mv) {
      if (!didDrag && Math.abs(mv.clientX - initialX) >= 4) didDrag = true;
      if (!didDrag) return;
      // preventDefault on the LIVE mousemove (the
      // previous `ev.preventDefault()` targeted the long-dispatched
      // mousedown event, a guaranteed no-op). Suppresses native
      // text/image drag-selection during the rope-select.
      mv.preventDefault();
      lastClientX = mv.clientX;
      lastClientY = mv.clientY;
      _applyMove();
      if (!_panRAF) {
        const r = _workRect();
        const leftOver  = (r.left  + EDGE_PAN_PX) - lastClientX;
        const rightOver = lastClientX - (r.right - EDGE_PAN_PX);
        const inEdgeZone =
          (leftOver  > 0 && viewStartS > 0) ||
          (rightOver > 0 && viewEndS   < dur);
        if (inEdgeZone) _panRAF = requestAnimationFrame(_tick);
      }
    }
    function _up() {
      window.removeEventListener("mousemove", _move);
      window.removeEventListener("mouseup", _up);
      if (_panRAF) { cancelAnimationFrame(_panRAF); _panRAF = 0; }
      if (!didDrag) {
        // Click → seek main video
        const t = Math.max(0, Math.min(dur, initialT));
        video.currentTime = t;
      }
    }
    window.addEventListener("mousemove", _move);
    window.addEventListener("mouseup", _up);
  });

  // No hover-scrub preview on the working strip — the 260×140 popover
  // obscured most of the filmstrip and made it impossible to see WHAT
  // you were cropping while moving the cursor (the whole reason we
  // built the strip in the first place). The popover now appears ONLY
  // during an active handle drag or click-drag selection, when the
  // user has explicitly committed to editing a range — those paths
  // call _showPreview themselves and call _hidePreview on mouseup.

  // ── Wheel handler: trackpad-X pans the view, mouse-wheel-Y zooms ──
  // Discriminates by which axis dominates the gesture:
  //
  //   * |deltaX| > |deltaY|  → horizontal trackpad swipe = pan view
  //     (same direction as Premiere / Final Cut / DaVinci timelines —
  //     two-finger swipe right scrolls timeline right). Pan amount is
  //     proportional to deltaX, scaled by the current visible span so
  //     the gesture feels consistent at any zoom level (a fixed
  //     pixels-per-second mapping would make panning feel sluggish
  //     when zoomed out and frantic when zoomed in).
  //
  //   * |deltaY| ≥ |deltaX|  → mouse wheel or vertical trackpad
  //     gesture = cursor-anchored zoom. Existing behavior unchanged
  //     so muscle memory survives. Cursor's time stays under the
  //     cursor across the zoom (solve newStart + cursorPct * newSpan
  //     = cursorT).
  //
  // Both branches `preventDefault` so the WebView doesn't also try
  // to scroll the surrounding pane.
  // Shared pan helper — used by the working-strip wheel handler AND
  // the overview-strip wheel handler. Both call
  // it with the deltaX from the wheel event; the only difference is
  // the rect width used to translate pixels-to-time.
  function _panViewByPx(deltaXpx, refWidthPx) {
    const span = viewEndS - viewStartS;
    const dT = (deltaXpx / refWidthPx) * span;
    let newStart = viewStartS + dT;
    if (newStart < 0) newStart = 0;
    if (newStart + span > dur) newStart = dur - span;
    _setView(newStart, newStart + span);
    _layoutAndPublish();
  }
  work.addEventListener("wheel", (ev) => {
    if (!ev.deltaY && !ev.deltaX) return;
    ev.preventDefault();
    const r = _workRect();
    const span = viewEndS - viewStartS;
    // Horizontal-dominant gesture → pan. Threshold loosened from a
    // strict `>` to a horizontal-bias check: any swipe with deltaX
    // ≥ 4px AND |deltaX| ≥ 0.6 × |deltaY| counts as horizontal. The
    // strict `>` missed real two-finger swipes that the Mac trackpad
    // driver emitted with tiny deltaY noise (gestures along the actual
    // pad surface aren't perfectly horizontal — fingers wobble — and
    // a horizontal swipe with deltaY == deltaX + 1 would otherwise
    // get routed to the zoom branch and surprise the user with an
    // unexpected zoom-out). 4px floor avoids accidental ratio-trips
    // on near-stationary wheel events.
    if (Math.abs(ev.deltaX) >= 4 && Math.abs(ev.deltaX) >= 0.6 * Math.abs(ev.deltaY)) {
      // Translate deltaX pixels into time via the shared helper.
      // ev.deltaX is in WebKit "device pixels" so dividing by rect
      // width gives a fraction of visible span. 1.0 multiplier
      // matches Mac's expected trackpad feel.
      _panViewByPx(ev.deltaX, r.width);
      return;
    }
    // Vertical-dominant → cursor-anchored zoom.
    // The factor is deltaY-magnitude-aware instead of
    // a fixed 1.25 per event. The fixed factor was tuned for mouse-
    // wheel events (one click = one ~100px deltaY chunk = 25% zoom)
    // but the same code path also fires on Mac trackpad two-finger
    // scrolls, which emit MANY small events (deltaY 1-5px each). A
    // slow trackpad swipe was firing ~10 events × 1.25 = 9× zoom in
    // a moment, throwing the view into the 4s floor immediately.
    // Now factor = exp(|deltaY| * 0.008) per event, so:
    //   deltaY  =   1 (trackpad nudge)     → 1.008  (1% per event)
    //   deltaY  =   5 (trackpad slow swipe)→ 1.041
    //   deltaY  =  30 (one mouse wheel click)→ 1.271 (matches the
    //                                                 old per-click feel)
    //   deltaY  = 100 (big mouse wheel scroll) → 2.226
    // A slow trackpad burst of ~10 events at deltaY=3 cumulates to
    // ~1.27× — gentle and steerable instead of slamming into the
    // span floor. Mouse-wheel users keep their per-click responsiveness.
    // Clamp the per-event factor to [0.5, 2.0] so a single freak
    // big-deltaY event doesnt teleport the view across orders of
    // magnitude.
    const cursorT = viewStartS + ((ev.clientX - r.left) / r.width) * span;
    const rawFactor = Math.exp(ev.deltaY * 0.008);
    const factor = Math.max(0.5, Math.min(2.0, rawFactor));   // out (>1) / in (<1)
    const newSpan = Math.max(4, Math.min(dur, span * factor));
    const cursorPct = (cursorT - viewStartS) / span;
    const newStart = cursorT - cursorPct * newSpan;
    _setView(newStart, newStart + newSpan);
    _layoutAndPublish();
  }, { passive: false });

  // ── Right-click context menu on the working strip ────────────────
  // Universal NLE expectation: right-click on the timeline opens
  // Reset / Fit / Zoom / Copy actions. Previously users had
  // to find the small ↺/⤢/⤡ icon buttons in the header row OR type
  // timecodes manually for any of these — right-click is the muscle
  // memory in Final Cut, Premiere, DaVinci, CapCut. Reuses
  // showContextMenu from contextmenu.js (same helper sidebar / results
  // / detail use), so the visual + keyboard-handling matches the
  // rest of the app. Skips when the click landed on a handle or on
  // a numeric input — those have their own contextmenu semantics
  // and a strip-level menu would shadow them.
  work.addEventListener("contextmenu", (ev) => {
    // Don't override the native menu on form controls — the IN/OUT
    // inputs need their normal paste/select-all/look-up menu.
    if (ev.target.tagName === "INPUT" || ev.target.tagName === "TEXTAREA") return;
    // Skip when the click landed on a handle — keyboard nudge is the
    // primary handle-level interaction, no strip menu needed there.
    if (ev.target.closest(".vtrim-handle")) return;
    ev.preventDefault();
    ev.stopPropagation();

    const dSec = endS - startS;
    const fmtRange = `${_fmtTime(startS)} → ${_fmtTime(endS)}  (${dSec < 60 ? dSec.toFixed(1) + "s" : _fmtTime(dSec)})`;

    showContextMenu(ev, [
      {
        label: "Reset IN/OUT to default window",
        shortcut: "↺",
        onClick: () => {
          startS = defaultStartS;
          endS = defaultEndS;
          _fitView();
          _layoutAndPublish();
        },
      },
      { divider: true },
      {
        label: "Fit view to selection",
        shortcut: "⤢",
        onClick: () => { _fitView(); _layoutAndPublish(); },
      },
      {
        label: "Zoom to entire video",
        shortcut: "⤡",
        onClick: () => { _zoomViewToAll(); _layoutAndPublish(); },
      },
      { divider: true },
      {
        label: `Copy clip range — ${fmtRange}`,
        onClick: () => {
          // Plain-text copy of the range so the user can paste it
          // into a Slack/email/ticket as a citation. Uses the same
          // mm:ss / hh:mm:ss format the IN/OUT inputs use so it's
          // round-trippable back into Tern. Falls through to a toast
          // on clipboard failure (private-browsing WKWebView denies
          // navigator.clipboard.write to some origins) so the user
          // knows the click registered but the copy didn't take.
          const text = `${_fmtTime(startS)} → ${_fmtTime(endS)} (${dSec < 60 ? dSec.toFixed(1) + "s" : _fmtTime(dSec)})`;
          navigator.clipboard.writeText(text).then(
            () => flashToast("Clip range copied", { kind: "ok", ttl: 2500 }),
            (e) => flashToast(`Copy failed: ${e?.message || e}`, { kind: "err", ttl: 3500 })
          );
        },
      },
    ]);
  });

  // ── Overview wheel pan ─────────────────────────────────────────────
  // Two-finger trackpad horizontal swipe over the overview minimap
  // pans the working strip's view. Same horizontal-bias check as the
  // working-strip wheel handler (≥4px and ≥0.6× |deltaY|) so a
  // vertical scroll over the overview falls through to the page
  // (which has no scroll, so it's a no-op). Without this handler,
  // the overview was a drag-only surface — the user had to grab the
  // viewport rectangle to pan, which feels stiff when a quick
  // trackpad swipe would be the natural gesture.
  //
  // NOTE: deliberately does NOT reuse _panViewByPx
  // — the helper scales
  // deltaX by the visible SPAN (right for the working strip where
  // the gesture maps to what you see), while the overview maps the
  // gesture to total DURATION: a swipe across half the overview pans
  // the view by half the video, regardless of zoom level.
  overview.addEventListener("wheel", (ev) => {
    if (!ev.deltaX && !ev.deltaY) return;
    if (!(Math.abs(ev.deltaX) >= 4 && Math.abs(ev.deltaX) >= 0.6 * Math.abs(ev.deltaY))) return;
    ev.preventDefault();
    const r = overview.getBoundingClientRect();
    // Overview pan: deltaX over overview-width maps to fraction of
    // dur (not span) — a swipe across half the overview pans the
    // view by half the total duration.
    const span = viewEndS - viewStartS;
    const dT = (ev.deltaX / r.width) * dur;
    let ns = viewStartS + dT;
    if (ns < 0) ns = 0;
    if (ns + span > dur) ns = dur - span;
    _setView(ns, ns + span);
    _layoutAndPublish();
  }, { passive: false });

  // ── Overview viewport drag (pan) ─────────────────────────────────
  viewport.addEventListener("mousedown", (ev) => {
    ev.preventDefault();
    ev.stopPropagation();
    const r = overview.getBoundingClientRect();
    const span = viewEndS - viewStartS;
    const startX = ev.clientX;
    const origStart = viewStartS;
    function _move(mv) {
      const dT = ((mv.clientX - startX) / r.width) * dur;
      const ns = Math.max(0, Math.min(dur - span, origStart + dT));
      _setView(ns, ns + span);
      _layoutAndPublish();
    }
    function _up() {
      window.removeEventListener("mousemove", _move);
      window.removeEventListener("mouseup", _up);
    }
    window.addEventListener("mousemove", _move);
    window.addEventListener("mouseup", _up);
  });

  // ── Overview click (outside viewport) = jump view to that center ──
  overview.addEventListener("mousedown", (ev) => {
    if (ev.target === viewport || viewport.contains(ev.target)) return;
    const r = overview.getBoundingClientRect();
    const t = ((ev.clientX - r.left) / r.width) * dur;
    const span = viewEndS - viewStartS;
    _setView(t - span / 2, t + span / 2);
    _layoutAndPublish();
  });

  // ── Numeric inputs ───────────────────────────────────────────────
  // Escape-cancel flag. The Escape branch in
  // _wireInput reverts the field and blurs — but .blur() fires the
  // blur-commit handler, which pre-fix re-parsed the just-reverted
  // text. Because _fmtTime renders floor-seconds, the re-parse
  // QUANTIZED a sub-second startS/endS (set by careful handle
  // dragging) to whole seconds, then paused playback and seeked —
  // so "Esc to cancel" (the input's own tooltip promise) actually
  // committed, destroyed precision, paused, and jumped. The flag is
  // set by the Escape branch and consumed (one-shot) by the blur
  // handler, turning the post-Escape blur into a true no-op.
  let _skipNextCommit = false;
  function _commitInput(which) {
    return (ev) => {
      if (_skipNextCommit) { _skipNextCommit = false; return; }
      const raw = (ev.target.value || "").trim();
      // No-edit guard: clicking into
      // the field and clicking away without typing should not pause
      // playback or move the playhead. The rendered text is
      // _fmtTime(current) — if the field still holds exactly that,
      // nothing was edited; skip the commit AND the scrub. (A typed
      // value that happens to equal the formatted text is the same
      // no-op by definition.)
      const currentFmt = _fmtTime(which === "start" ? startS : endS);
      if (raw === currentFmt) { _typingInInput = false; return; }
      const parsed = _parseTimecodeToSeconds(raw);
      if (parsed == null) {
        // Two failure modes share this branch: (a) user cleared the
        // field (raw empty) and tabbed away — silently reverting is
        // correct UX, they explicitly want to discard. (b) user
        // typed something that isn't a parseable timecode (e.g.
        // "1:99", "abc", "1:2:3:4") — previously this also
        // silently reverted, leaving the user staring at the old
        // value with no clue why their typed input was rejected.
        // Toast on case (b) with the actual offending text + the
        // expected formats so they can fix it instead of re-typing
        // and re-failing the same way.
        if (raw !== "") {
          flashToast(
            `Couldn't parse "${raw}" — use ss, mm:ss, or hh:mm:ss (e.g. 12, 1:30, 0:01:30)`,
            { kind: "err", ttl: 4500 }
          );
          // Brief visual cue on the input itself — class fades the
          // border to var(--err) for 1.2s, mirrors the kind of
          // inline-validation pulse common in macOS form controls.
          ev.target.classList.add("vtrim-input-invalid");
          setTimeout(() => ev.target.classList.remove("vtrim-input-invalid"), 1200);
        }
        _typingInInput = false;
        _layoutAndPublish();
        return;
      }
      // Detect out-of-range typed values BEFORE the clamp eats them.
      // The user typed e.g. OUT=200 on an 80-second video — clamping
      // to dur (80) is the right BEHAVIOR but the user should know
      // their value got truncated; otherwise they retype the same
      // 200 and wonder why the clip didn't lengthen.
      const wasClampedHigh =
        (which === "start" && parsed > endS - CLIP_MIN_DURATION_S) ||
        (which === "end"   && parsed > dur);
      const wasClampedLow =
        (which === "start" && parsed < 0) ||
        (which === "end"   && parsed < startS + CLIP_MIN_DURATION_S);
      if (which === "start") {
        startS = Math.max(0, Math.min(endS - CLIP_MIN_DURATION_S, parsed));
      } else {
        endS = Math.min(dur, Math.max(startS + CLIP_MIN_DURATION_S, parsed));
      }
      if (wasClampedHigh || wasClampedLow) {
        const limit = which === "start"
          ? (wasClampedHigh ? `OUT − ${CLIP_MIN_DURATION_S}s = ${_fmtTime(endS - CLIP_MIN_DURATION_S)}` : "0:00")
          : (wasClampedHigh ? `video end = ${_fmtTime(dur)}` : `IN + ${CLIP_MIN_DURATION_S}s = ${_fmtTime(startS + CLIP_MIN_DURATION_S)}`);
        flashToast(
          `Clamped to ${limit}`,
          { kind: "info", ttl: 3500 }
        );
      }
      // Scrub the main video to whichever endpoint the user just
      // committed. Same UX principle as the
      // handle-drag + click-drag live scrub: the user typed a new
      // IN/OUT because they want to SEE whats at that timecode,
      // so jump the player there. Pause first so the player doesnt
      // autoplay past the typed point. The 50ms epsilon dedupes
      // redundant writes (the typed value often already equals
      // currentTime within float drift).
      const targetT = which === "start" ? startS : endS;
      if (!video.paused) video.pause();
      if (isFinite(targetT) && Math.abs(video.currentTime - targetT) > 0.05) {
        video.currentTime = targetT;
      }
      _typingInInput = false;
      _layoutAndPublish();
    };
  }
  function _wireInput(el, which) {
    if (!el) return;
    el.addEventListener("focus", () => { _typingInInput = true; });
    el.addEventListener("blur",  _commitInput(which));
    el.addEventListener("keydown", (ev) => {
      if (ev.key === "Enter") { ev.preventDefault(); ev.target.blur(); }
      if (ev.key === "Escape") {
        ev.preventDefault();
        // Arm the one-shot skip BEFORE blur() so the blur-commit
        // handler (which fires synchronously inside .blur()) sees it
        // and no-ops — see the _skipNextCommit rationale above.
        _skipNextCommit = true;
        _typingInInput = false;
        _layoutAndPublish();
        ev.target.blur();
      }
    });
  }
  _wireInput(startIn, "start");
  _wireInput(endIn,   "end");

  // ── Frame stepping buttons + comma / period keys ─────────────────
  // Assumes 30 fps if we don't know the real frame rate. Shift = 1 s.
  function _stepFrame(dir, oneSec) {
    const delta = (oneSec ? 1 : 1 / 30) * dir;
    video.pause();
    video.currentTime = Math.max(0, Math.min(dur - 0.05, video.currentTime + delta));
  }
  if (stepBack) stepBack.addEventListener("click", (ev) => _stepFrame(-1, ev.shiftKey));
  if (stepFwd)  stepFwd .addEventListener("click", (ev) => _stepFrame(+1, ev.shiftKey));

  // Global , / . handlers, scoped to the player's AbortController so
  // they teardown with the next renderPlayer call.
  document.addEventListener("keydown", (ev) => {
    const ae = document.activeElement;
    if (ae && (ae.tagName === "INPUT" || ae.tagName === "TEXTAREA" || ae.isContentEditable)) return;
    if (ev.metaKey || ev.ctrlKey || ev.altKey) return;
    if (ev.key === ",") { ev.preventDefault(); _stepFrame(-1, ev.shiftKey); }
    else if (ev.key === ".") { ev.preventDefault(); _stepFrame(+1, ev.shiftKey); }
    else if (ev.key === "Home") {
      ev.preventDefault();
      video.currentTime = startS;
    }
    else if (ev.key === "End") {
      ev.preventDefault();
      video.currentTime = Math.max(startS, endS - 0.05);
    }
  }, { signal: _playerKeysAbort?.signal });

  // ── Zoom buttons + reset ─────────────────────────────────────────
  if (zoomFit) zoomFit.addEventListener("click", () => { _fitView(); _layoutAndPublish(); });
  if (zoomOut) zoomOut.addEventListener("click", () => { _zoomViewToAll(); _layoutAndPublish(); });
  if (resetBtn) resetBtn.addEventListener("click", () => {
    startS = defaultStartS; endS = defaultEndS;
    _fitView();
    _layoutAndPublish();
  });

  // ── I / O — set in/out at current playhead ───────────────────────
  document.addEventListener("tern:set-clip-in", (ev) => {
    const t = Number(ev.detail?.timeS);
    if (!isFinite(t) || t < 0) return;
    startS = Math.max(0, Math.min(endS - CLIP_MIN_DURATION_S, t));
    _layoutAndPublish();
  }, { signal: _playerKeysAbort?.signal });
  document.addEventListener("tern:set-clip-out", (ev) => {
    const t = Number(ev.detail?.timeS);
    if (!isFinite(t) || t < 0) return;
    endS = Math.min(dur, Math.max(startS + CLIP_MIN_DURATION_S, t));
    _layoutAndPublish();
  }, { signal: _playerKeysAbort?.signal });

  // First paint + initial playhead positioning
  _movePlayhead();
  _layoutAndPublish();

  // Resize observer — re-paint thumbs if the pane changes width (e.g.,
  // sidebar toggle). Lightweight: just re-runs the % math.
  const ro = new ResizeObserver(() => _layoutAndPublish());
  ro.observe(work);
  // Cleanup the observer when the player tears down
  if (_playerKeysAbort) {
    _playerKeysAbort.signal.addEventListener("abort", () => ro.disconnect(), { once: true });
  }
}

// ─── Clip-trim handles ────────────────────────────────────────────────
// Adds draggable start/end handles over the waveform so the user can
// adjust the export-clip window without leaving the detail pane. The
// resulting range is published on `wave.dataset.clipStartMs` /
// `clipEndMs`; detail.js reads them when ⌘E / Export clip is invoked.
const CLIP_DEFAULT_PADDING_S = 1.5;
const CLIP_DEFAULT_DURATION_S = 8;
const CLIP_MIN_DURATION_S = 0.5;

function _setupTrimHandles(container, wave, audio, hit) {
  if (!audio.duration || !isFinite(audio.duration)) return;

  // Initial range: matchTs - padding → matchTs + (DEFAULT − padding)
  const matchS = (hit.ts_ms || 0) / 1000;
  let startS = Math.max(0, matchS - CLIP_DEFAULT_PADDING_S);
  let endS   = Math.min(audio.duration, startS + CLIP_DEFAULT_DURATION_S);

  // DOM
  const band = document.createElement("div");
  band.className = "player-clip-band";
  const sh = document.createElement("div");
  sh.className = "player-clip-handle player-clip-handle-start";
  sh.title = "Drag to set clip start";
  const eh = document.createElement("div");
  eh.className = "player-clip-handle player-clip-handle-end";
  eh.title = "Drag to set clip end";
  wave.appendChild(band);
  wave.appendChild(sh);
  wave.appendChild(eh);

  // Info row below times
  const wrap = container.querySelector(".player-wave-wrap");
  const info = document.createElement("div");
  info.className = "player-clip-info";
  info.innerHTML = `
    <span class="player-clip-info-label">Clip</span>
    <span class="player-clip-info-range">
      <span id="clip-start-label"></span> →
      <span id="clip-end-label"></span>
    </span>
    <span id="clip-duration-label" style="color:var(--text-4);"></span>
  `;
  wrap.appendChild(info);
  const startLabel = info.querySelector("#clip-start-label");
  const endLabel   = info.querySelector("#clip-end-label");
  const durLabel   = info.querySelector("#clip-duration-label");

  function _layoutAndPublish() {
    const startPct = (startS / audio.duration) * 100;
    const endPct   = (endS   / audio.duration) * 100;
    band.style.left  = startPct + "%";
    band.style.width = (endPct - startPct) + "%";
    sh.style.left = startPct + "%";
    eh.style.left = endPct + "%";
    wave.dataset.clipStartMs = String(Math.round(startS * 1000));
    wave.dataset.clipEndMs   = String(Math.round(endS   * 1000));
    startLabel.textContent = _fmtTime(startS);
    endLabel.textContent   = _fmtTime(endS);
    const dur = endS - startS;
    durLabel.textContent = dur < 60
      ? `· ${dur.toFixed(1)}s`
      : `· ${_fmtTime(dur)}`;
  }
  _layoutAndPublish();

  function _bindDrag(handle, which) {
    handle.addEventListener("mousedown", (ev) => {
      ev.preventDefault();
      ev.stopPropagation(); // don't let the wave's click-to-seek fire
      const rect = wave.getBoundingClientRect();
      const origStart = startS, origEnd = endS;
      const startX = ev.clientX;
      function _move(mv) {
        const dRatio = (mv.clientX - startX) / rect.width;
        const dT = dRatio * audio.duration;
        if (which === "start") {
          startS = Math.max(0, Math.min(origEnd - CLIP_MIN_DURATION_S, origStart + dT));
        } else {
          endS = Math.max(origStart + CLIP_MIN_DURATION_S, Math.min(audio.duration, origEnd + dT));
        }
        _layoutAndPublish();
      }
      function _up() {
        window.removeEventListener("mousemove", _move);
        window.removeEventListener("mouseup", _up);
        document.body.style.userSelect = "";
      }
      document.body.style.userSelect = "none";
      window.addEventListener("mousemove", _move);
      window.addEventListener("mouseup", _up);
    });
  }
  _bindDrag(sh, "start");
  _bindDrag(eh, "end");

  // I / O — NLE in-point / out-point at the current playhead, mirroring
  // the video branch (_setupVideoTrim). Frame-accurate alternative to
  // dragging the visual handle. Tied to _playerKeysAbort so renderPlayer
  // tears these listeners down on hit switch alongside the J/K/L ones.
  document.addEventListener("tern:set-clip-in", (ev) => {
    const t = Number(ev.detail?.timeS);
    if (!isFinite(t) || t < 0) return;
    startS = Math.max(0, Math.min(endS - CLIP_MIN_DURATION_S, t));
    _layoutAndPublish();
  }, { signal: _playerKeysAbort?.signal });
  document.addEventListener("tern:set-clip-out", (ev) => {
    const t = Number(ev.detail?.timeS);
    if (!isFinite(t) || t < 0) return;
    endS = Math.min(audio.duration, Math.max(startS + CLIP_MIN_DURATION_S, t));
    _layoutAndPublish();
  }, { signal: _playerKeysAbort?.signal });

  // Home / End — jump playhead to clip start / clip end. The video
  // trim widget has this same pair at the bottom of _initVideoTrim
  // (lines around 1130 — direct keypress handler with access to its
  // local startS / endS). Previously the audio trim was missing
  // both, so the keyhelp claim "Home / End — Jump playhead to clip
  // start / clip end" was qualified "video trim only" — a documented
  // limitation rather than parity. For audio there's no reason it
  // shouldn't work (no "frame" concept like the comma/period frame-
  // step keys, but Home/End is just two seek operations against the
  // local clip range).
  //
  // Scoped to document and gated on _playerKeysAbort so a hit switch
  // tears it down alongside J/K/L/I/O. Same modifier-key guard as
  // _bindMediaShortcuts above so ⌘End / ⇧End in a future shortcut
  // doesn't conflict.
  document.addEventListener("keydown", (ev) => {
    const t = document.activeElement;
    if (t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA" || t.isContentEditable)) return;
    if (ev.metaKey || ev.ctrlKey || ev.altKey || ev.shiftKey) return;
    if (ev.key === "Home") {
      ev.preventDefault();
      audio.currentTime = startS;
    } else if (ev.key === "End") {
      // Subtract 50 ms so the playhead lands JUST inside the clip end
      // — same nudge the video version uses, matters because seeking
      // to the exact end can trigger the media element's "ended" event
      // and the user has to scrub back. 0.05 s is well below human
      // perception of position for audio scrubbing.
      ev.preventDefault();
      audio.currentTime = Math.max(startS, endS - 0.05);
    }
  }, { signal: _playerKeysAbort?.signal });
}

// Cache decoded peaks per file_id so re-selecting a hit in the same file
// doesn't re-fetch + re-decode the audio. Bounded by browser's GC.
const _waveformCache = new Map();
// Skip waveform for very large files — fetch + decode would chew memory
// without a perceptible quality win at typical wave widths (~500px → 150
// peaks total).
const MAX_DECODE_BYTES = 60 * 1024 * 1024;

async function _renderRealWaveform(waveEl, hit, durationSec) {
  if (!hit.preview_url || !waveEl) return;

  // Compute the match position regardless of decode success — even a
  // gradient wave benefits from a sharp matched-span overlay.
  const matchRatio = durationSec > 0 ? (hit.ts_ms / 1000) / durationSec : 0;

  let peaks = _waveformCache.get(hit.file_id);
  if (!peaks) {
    try {
      const resp = await fetch(hit.preview_url);
      const len = Number(resp.headers.get("content-length") || 0);
      if (len > MAX_DECODE_BYTES) return;
      const ab = await resp.arrayBuffer();
      if (ab.byteLength > MAX_DECODE_BYTES) return;
      // Use OfflineAudioContext for decode-only work — the dead `const
      // Ctx = window.OfflineAudioContext || ...` line that lived here
      // for a long time hinted at the original intent but the code below
      // still allocated a live AudioContext. The difference matters:
      //
      //   - Live AudioContext binds to the system audio output device.
      //     On WKWebView, autoplay-policy unlock can require a user
      //     gesture before decodeAudioData on a live context resolves
      //     (the click on a hit is the gesture, so this MOSTLY worked,
      //     but a programmatic auto-preview via the ↓ key has had
      //     intermittent reports of waveform render failures).
      //   - OfflineAudioContext is a pure-decode context. It never
      //     touches the output device, never requires a user gesture,
      //     and the OAC instance can be garbage-collected immediately
      //     after decode (no need to call .close() because there's no
      //     output binding to release).
      //
      // Arguments to OfflineAudioContext are (numChannels, length,
      // sampleRate). For decodeAudioData they're nominal — the decoded
      // buffer comes back at the SOURCE's native sample rate, not the
      // context's. (1, 1, 44100) is a no-op-shaped factory.
      //
      // Fallback to AudioContext on the (probably-extinct) browser
      // where OfflineAudioContext isn't available — same path as
      // before, just guarded behind feature detection.
      const OACtor = window.OfflineAudioContext || window.webkitOfflineAudioContext;
      const buf = OACtor
        ? await new OACtor(1, 1, 44100).decodeAudioData(ab)
        : await (async () => {
            const ctx = new (window.AudioContext || window.webkitAudioContext)();
            try { return await ctx.decodeAudioData(ab); }
            finally { try { await ctx.close(); } catch {} }
          })();

      const N = 180;
      const data = buf.getChannelData(0);
      const bucketSize = Math.floor(data.length / N);
      const out = new Float32Array(N);
      let maxPeak = 0;
      for (let i = 0; i < N; i++) {
        let m = 0;
        const s = i * bucketSize;
        const e = s + bucketSize;
        for (let j = s; j < e; j++) {
          const v = Math.abs(data[j]);
          if (v > m) m = v;
        }
        out[i] = m;
        if (m > maxPeak) maxPeak = m;
      }
      // Normalize so the loudest peak hits the top
      if (maxPeak > 0) for (let i = 0; i < N; i++) out[i] /= maxPeak;
      peaks = out;
      _waveformCache.set(hit.file_id, peaks);
    } catch {
      return; // unsupported codec, CORS, etc. — keep gradient
    }
  }

  // Render via canvas (sharper than 180 SVG rects on hi-DPI)
  const dpr = window.devicePixelRatio || 1;
  const cssW = waveEl.clientWidth || 320;
  const cssH = waveEl.clientHeight || 36;
  const canvas = document.createElement("canvas");
  canvas.className = "player-wave-canvas";
  canvas.style.cssText = "position:absolute;inset:0;width:100%;height:100%;opacity:0;transition:opacity 240ms ease-out;";
  canvas.width  = Math.floor(cssW * dpr);
  canvas.height = Math.floor(cssH * dpr);
  const ctx2 = canvas.getContext("2d");
  ctx2.scale(dpr, dpr);

  const N = peaks.length;
  const barW = cssW / N;
  const accent = getComputedStyle(document.documentElement).getPropertyValue("--accent").trim() || "#0a84ff";
  const mark   = getComputedStyle(document.documentElement).getPropertyValue("--mark").trim()   || "#ffd166";
  const matchBucket = Math.floor(matchRatio * N);

  for (let i = 0; i < N; i++) {
    const barH = Math.max(1, peaks[i] * cssH * 0.85);
    const inMatch = Math.abs(i - matchBucket) <= 2; // ~5-bucket span
    ctx2.fillStyle = inMatch ? mark : accent;
    ctx2.globalAlpha = inMatch ? 1 : 0.40;
    const x = i * barW + 0.5;
    const w = Math.max(1, barW - 1);
    ctx2.fillRect(x, (cssH - barH) / 2, w, barH);
  }

  // Replace gradient backdrop, fade canvas in
  waveEl.style.background = "transparent";
  // Remove any older canvas (e.g., from a previous hit on the same wave element)
  waveEl.querySelectorAll(".player-wave-canvas").forEach(n => n.remove());
  waveEl.appendChild(canvas);
  requestAnimationFrame(() => { canvas.style.opacity = "1"; });
}

function _renderImage(container, hit) {
  // file_name lands inside an HTML attribute — escape to prevent breaking
  // attribute parsing if the filename contains a quote or angle bracket.
  // preview_url is URL-encoded server-side (api/main.py hit_to_json).
  const alt = (hit.file_name || "").replace(/[&<>"']/g, c =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  container.innerHTML = `
    <div class="player">
      <div class="player-image" title="Click to view full size">
        <img src="${hit.preview_url}" alt="${alt}" style="cursor: zoom-in;">
      </div>
    </div>
  `;
  // Click → fullscreen lightbox. Module decoupled via CustomEvent so the
  // player doesn't import lightbox directly.
  container.querySelector(".player-image").addEventListener("click", () => {
    document.dispatchEvent(new CustomEvent("tern:open-lightbox", { detail: { hit } }));
  });
}
