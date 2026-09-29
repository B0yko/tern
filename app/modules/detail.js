// app/modules/detail.js — detail pane orchestration.
import { state, subscribe } from "/modules/state.js";
import { on, INTENTS } from "/modules/keyboard.js";
import { api } from "/modules/api.js";
import { icons } from "/modules/icons.js";
import { renderPlayer } from "/modules/player.js";
import { renderTranscript } from "/modules/transcript.js";
import { snippetHtml } from "/modules/row.js";
import { isBookmarked, toggle as toggleBookmark } from "/modules/bookmarks.js";
import { flashToast } from "/modules/toast.js";
import { showContextMenu } from "/modules/contextmenu.js";

let _paneEl = null;

function _kindLabel(hit) {
  if (!hit) return "";
  const map = { transcript: "Speech", ocr: "On-screen", visual: "Visual" };
  if (hit.sources && hit.sources.length > 1) {
    return hit.sources.slice(0, 2).map(s => map[s] || s).join(" + ");
  }
  return map[hit.source] || hit.source;
}

// Monotonic id used to discard stale transcript-window responses. Rapid ↓
// arrow presses fire _render() per selected hit; the first invocation's
// awaited fetch can resolve AFTER the next render has replaced the DOM,
// flashing the wrong transcript into the new hit's pane.
let _renderReqId = 0;
// Per-render AbortController so superseded transcript-window fetches get
// CANCELLED mid-flight, not just discarded after they complete on the
// backend. Rapid ↓ through 20 hits used to fire 20 SQL queries; the
// _renderReqId guard discarded 19 responses but the backend still ran
// each (20× /api/transcript/window = ~30 ms each,
// ~600 ms wasted in aggregate). With AbortController + the
// api.js signal plumbing, FastAPI's client-disconnect handler cancels
// the in-flight request within milliseconds. Same end-to-end abort
// pattern as topbar.js's search-as-you-type cancel.
let _renderAbort = null;

async function _render() {
  if (!_paneEl) return;
  const hit = state.selectedHit;
  if (!hit) {
    _paneEl.innerHTML = `
      <div class="detail">
        <div class="detail-empty">Pick a result to see the moment.</div>
      </div>
    `;
    return;
  }

  const myReq = ++_renderReqId;
  // Abort the previous render's transcript fetch before kicking off a
  // new one. The _renderReqId race-guard is KEPT — there's a tiny
  // window where the response headers landed before the abort
  // propagated, so the catch path might not see AbortError.
  if (_renderAbort) _renderAbort.abort();
  _renderAbort = new AbortController();
  const mySignal = _renderAbort.signal;

  // Source kind for the detail badge color — same data-source attribute
  // pattern as row.js, so CSS can tint the badge to match the result
  // row that produced it. Multi-source hits use the accent color via
  // the existing default styling.
  const _sources = hit.sources && hit.sources.length ? hit.sources : [hit.source];
  const _primarySource = (_sources.length > 1) ? "multi" : (_sources[0] || "transcript");

  // Skeleton scaffold
  _paneEl.innerHTML = `
    <div class="detail">
      <header class="detail-head">
        <span class="detail-badge" data-source="${_primarySource}">${_kindLabel(hit)}</span>
        <span class="detail-tc">${hit.timecode || ""}${hit.duration_ms ? " / " + _fmtDuration(hit.duration_ms) : ""}</span>
        <span class="detail-file">${_escape(hit.file_name)}</span>
        <button class="detail-bookmark ${isBookmarked(hit.file_id, hit.ts_ms) ? "on" : ""}" id="detail-bookmark" title="Bookmark this moment (⇧⌘B)" aria-label="Toggle bookmark (Shift-Command-B)">
          ${icons.starOutline({ w: 14, h: 14 })}
        </button>
        <span class="detail-nav-hint">↑↓ step · ⏎ play · ⌘E export</span>
      </header>
      <div class="detail-player-slot"></div>
      <div class="detail-body" id="detail-transcript-slot"></div>
      <footer class="detail-actions">
        <button class="detail-btn primary" id="btn-export" aria-label="Crop and save clip (Command-E)"><span class="ico">${icons.download({ w: 13, h: 13 })}</span>Crop &amp; Save<span class="kbd">⌘E</span></button>
        <button class="detail-btn" id="btn-reveal" aria-label="Reveal in Finder (Command-Shift-R)"><span class="ico">${icons.reveal({ w: 13, h: 13 })}</span>Reveal<span class="kbd">⇧⌘R</span></button>
        <button class="detail-btn" id="btn-quicklook" aria-label="Quick Look (Space)"><span class="ico">${icons.open({ w: 13, h: 13 })}</span>Quick Look<span class="kbd">␣</span></button>
        <button class="detail-btn" id="btn-copy" aria-label="Copy link to this moment (Command-L)"><span class="ico">${icons.copy({ w: 13, h: 13 })}</span>Copy link<span class="kbd">⌘L</span></button>
        <button class="detail-btn ghost detail-overflow" id="btn-more" aria-label="More actions" aria-haspopup="menu"><span class="ico">${icons.more({ w: 16, h: 16 })}</span></button>
      </footer>
    </div>
  `;

  // Player
  renderPlayer(_paneEl.querySelector(".detail-player-slot"), hit);

  // Transcript window
  if (hit.media_kind === "audio" || hit.media_kind === "video") {
    // Show skeleton while the window fetches — avoids a flash of empty pane.
    // CSS in motion.css renders the five shimmer lines.
    const slot = _paneEl.querySelector("#detail-transcript-slot");
    slot.innerHTML = `
      <div class="transcript-skeleton" aria-label="Loading transcript">
        <div class="ts-line"></div>
        <div class="ts-line"></div>
        <div class="ts-line"></div>
        <div class="ts-line"></div>
        <div class="ts-line"></div>
      </div>`;
    try {
      const data = await api.transcriptWindow(hit.file_id, hit.ts_ms, 3, { signal: mySignal });
      if (myReq !== _renderReqId) return; // a newer selection superseded this render
      renderTranscript(slot, data, hit);
    } catch (e) {
      if (myReq !== _renderReqId) return;
      // AbortError is the expected outcome when the next ↓ keystroke
      // cancelled this fetch — don't log it as a warning. Same pattern
      // as topbar.js _runSearch.
      if (e?.name === "AbortError" || /aborted/i.test(e?.message || "")) {
        return;
      }
      console.warn("transcript fetch failed", e);
      slot.innerHTML =
        `<div style="color:var(--text-3);font-size:12px;">${snippetHtml(hit.snippet || "")}</div>`;
    }
  } else {
    // Image hit — show visual description + EXIF chips (date, camera, GPS, dims).
    const meta = hit.metadata || {};
    const chips = [];
    if (meta.date_taken) {
      try {
        const d = new Date(meta.date_taken);
        chips.push(`<span class="exif-chip">📅 ${d.toLocaleDateString()} · ${d.toLocaleTimeString([], {hour:"2-digit",minute:"2-digit"})}</span>`);
      } catch { chips.push(`<span class="exif-chip">📅 ${_escape(String(meta.date_taken))}</span>`); }
    }
    if (meta.make || meta.model) {
      chips.push(`<span class="exif-chip">📷 ${_escape([meta.make, meta.model].filter(Boolean).join(" "))}</span>`);
    }
    if (meta.width && meta.height) {
      chips.push(`<span class="exif-chip">${meta.width} × ${meta.height}</span>`);
    }
    if (meta.gps_lat != null && meta.gps_lon != null) {
      const lat = Number(meta.gps_lat).toFixed(3);
      const lon = Number(meta.gps_lon).toFixed(3);
      chips.push(`<span class="exif-chip"><a href="https://maps.apple.com/?ll=${lat},${lon}" target="_blank" rel="noopener" style="color:inherit;text-decoration:none">📍 ${lat}, ${lon}</a></span>`);
    }
    // snippetHtml, not _escape. A photo that matched on OCR arrives with the
    // matched words already wrapped in <mark> by the backend; _escape printed
    // those tags as literal text right under the image — the most-looked-at
    // spot in the pane. snippetHtml keeps the marks and escapes the rest, so
    // text lifted off an untrusted image still can't inject markup.
    _paneEl.querySelector("#detail-transcript-slot").innerHTML = `
      <div style="color:var(--text-3);font-size:12.5px;font-style:italic;line-height:1.5;">${hit.snippet ? snippetHtml(hit.snippet) : "no description"}</div>
      ${chips.length ? `<div class="exif-chips">${chips.join("")}</div>` : ""}
    `;
  }

  if (myReq !== _renderReqId) return;
  _bindActions(hit);
}

function _bindActions(hit) {
  _paneEl.querySelector("#btn-export").addEventListener("click", () => _exportClip(hit));
  // Reveal + Quick Look both 4xx if the source file was deleted/moved
  // externally between indexing and now. Without a catch, the failed
  // promise just logs an unhandled-rejection — user clicks the button,
  // Finder doesn't open, no feedback. Surface via toast so the user
  // knows WHY nothing happened.
  _paneEl.querySelector("#btn-reveal").addEventListener("click", () =>
    api.reveal(hit.file_path).catch((e) =>
      flashToast(`Couldn't reveal: ${e?.message || e}`, { kind: "err", ttl: 3500 })
    )
  );
  _paneEl.querySelector("#btn-quicklook").addEventListener("click", () =>
    api.quicklook(hit.file_path).catch((e) =>
      flashToast(`Quick Look failed: ${e?.message || e}`, { kind: "err", ttl: 3500 })
    )
  );
  _paneEl.querySelector("#btn-copy").addEventListener("click", () => _copyLink(hit));
  _paneEl.querySelector("#btn-more").addEventListener("click", (ev) => {
    ev.stopPropagation();
    _showMoreMenu(ev.currentTarget, hit);
  });
  const bk = _paneEl.querySelector("#detail-bookmark");
  if (bk) bk.addEventListener("click", () => {
    const nowOn = toggleBookmark(hit);
    bk.classList.toggle("on", nowOn);
    bk.innerHTML = (nowOn ? icons.starFilled : icons.starOutline)({ w: 14, h: 14 });
    // Pop animation — class is removed after the keyframes finish so
    // a rapid re-click re-runs it (CSS animations don't restart
    // automatically when toggled to the same value).
    bk.classList.remove("just-toggled");
    void bk.offsetWidth; // force reflow so the next class-add restarts the animation
    bk.classList.add("just-toggled");
    setTimeout(() => bk.classList.remove("just-toggled"), 320);
    flashToast(nowOn ? "Bookmarked" : "Bookmark removed", { kind: "ok" });
  });
}

// Detail-pane overflow menu (⋯ button on the action bar). Previously had
// its own custom menu element (.detail-more-menu) + dismiss handlers + Esc
// wiring. Now reuses the shared contextmenu.js system:
// same styling as right-click menus everywhere else, ↑↓ Enter Esc keyboard
// nav included, auto-flip near viewport edges, no separate CSS.
function _showMoreMenu(btn, hit) {
  const isMedia = hit.media_kind === "audio" || hit.media_kind === "video";
  const items = [
    // Open in default app — needs the same toast-on-fail wire as the
    // sibling btn-reveal / btn-quicklook click handlers above
    // so a 404 from /api/open (file moved/deleted externally)
    // surfaces as an actionable toast instead of an unhandled promise
    // rejection that just logs to console. Without this catch, the
    // _showMoreMenu's "Open in default app" was the only file-action
    // item in the entire UI that still failed silently.
    {
      label: "Open in default app",
      onClick: () => api.open(hit.file_path).catch((e) =>
        flashToast(`Couldn't open: ${e?.message || e}`, { kind: "err", ttl: 3500 })
      ),
      shown: true,
    },
    { divider: true, shown: isMedia || !!hit.snippet },
    { label: "Export SRT subtitle", onClick: () => _exportSrt(hit), shown: isMedia },
    { label: "Export audio only (MP3)", onClick: () => _exportClip(hit, true), shown: isMedia },
    { divider: true, shown: !!hit.snippet },
    { label: "Copy snippet text", onClick: () => _copySnippet(hit), shown: !!hit.snippet },
    { label: "Copy quote with reference", onClick: () => _copyCitation(hit), shown: !!hit.snippet && isMedia },
  ].filter(i => i.shown !== false).map(({ shown, ...rest }) => rest);
  if (!items.some(i => i.label)) return;

  // Synthesize a click-event with coordinates anchored to the button so
  // showContextMenu's auto-flip places the menu above (button is near the
  // bottom edge of the detail pane). Use the button's RIGHT edge so the
  // menu's natural left-of-cursor positioning flows out toward the inside
  // of the pane.
  const rect = btn.getBoundingClientRect();
  const fakeEv = {
    clientX: rect.right,
    clientY: rect.top,
    preventDefault: () => {},
    stopPropagation: () => {},
  };
  showContextMenu(fakeEv, items);
}

async function _exportSrt(hit) {
  try {
    // Use api.exportSrt instead of raw fetch — inherits the
    // default 60 s timeout AND throws on non-2xx instead of
    // silently parsing the error body as if it were a success
    // response. Pre-fix: a 404 (file has no transcript) or 5xx
    // returned `{detail: "..."}` → `r.path` was undefined → the
    // `if (r && r.path)` branch silently skipped → user clicked
    // Export SRT, saw nothing happen, no error toast. Now: api.js
    // _json throws "{status} {statusText}: {detail}" which the
    // outer catch surfaces as a clear toast.
    const r = await api.exportSrt(hit.file_id, hit.ts_ms, 8);
    if (r && r.path) {
      api.reveal(r.path).catch((e) =>
        flashToast(`SRT saved but reveal failed: ${e?.message || e}`,
                   { kind: "err", ttl: 3500 })
      );
      flashToast(`${r.cues || 0} SRT cues ready in Finder`, { kind: "ok" });
    }
  } catch (e) {
    console.error("SRT export failed", e);
    flashToast(`SRT export failed: ${e?.message || e}`,
               { kind: "err", ttl: 4500 });
  }
}

function _copySnippet(hit) {
  const text = (hit.snippet || "").replace(/<\/?mark>/g, "");
  if (!text) return;
  navigator.clipboard.writeText(text)
    .then(() => flashToast("Snippet copied", { kind: "ok" }))
    .catch(() => flashToast("Couldn't copy", { kind: "err" }));
}

// Combined "quote + reference" copy — for citing in articles, Notion
// docs, scripts, etc. Format:
//   "…the matched line…" — file_name @ 00:14:32
// Skipped for image hits (no snippet, no meaningful timestamp).
function _copyCitation(hit) {
  const quote = (hit.snippet || "").replace(/<\/?mark>/g, "").trim();
  const ref = `${hit.file_name || "?"}${hit.timecode ? ` @ ${hit.timecode}` : ""}`;
  const text = quote
    ? `"${quote}" — ${ref}`
    : ref;
  navigator.clipboard.writeText(text)
    .then(() => flashToast("Quote with reference copied", { kind: "ok" }))
    .catch(() => flashToast("Couldn't copy", { kind: "err" }));
}

async function _exportClip(hit, forceAudioOnly = null) {
  // Prefer the trim handles' window if the player published one. Falls back
  // to a sensible default (1.5 s before match + 8 s total).
  const wave = _paneEl.querySelector("#player-wave");
  let startMs, endMs;
  if (wave && wave.dataset.clipStartMs && wave.dataset.clipEndMs) {
    startMs = parseInt(wave.dataset.clipStartMs, 10);
    endMs   = parseInt(wave.dataset.clipEndMs, 10);
  } else {
    const PADDING = 1500;
    startMs = Math.max(0, hit.ts_ms - PADDING);
    endMs   = hit.ts_ms + 8000;
  }
  const audioOnly = forceAudioOnly !== null ? forceAudioOnly : (hit.media_kind === "audio");
  // Disable + relabel the visible Crop & Save button during the
  // export so a user clicking it doesn't fire a second ffmpeg pass
  // while the first is still running (real-world clips take 1-5 s;
  // max-size clips take 20-30 s — long enough that an impatient
  // double-click was kicking off duplicate encodes that both
  // landed in workspace/exports/, confusing the user about which
  // file Finder revealed). Defensive: the button might not be in
  // the DOM if _exportClip was invoked via ⌘E on a hit that's not
  // currently rendered (rare race) or via the right-click context
  // menu's "Export audio only (MP3)" item on an image hit (the
  // primary Crop & Save button is hidden for images). Pattern
  // mirrors license.js's Activate-button discipline.
  const exportBtn = _paneEl.querySelector("#btn-export");
  const origLabel = exportBtn ? exportBtn.innerHTML : null;
  if (exportBtn) {
    exportBtn.disabled = true;
    exportBtn.textContent = "Saving…";
  }
  try {
    const r = await api.exportClip(hit.file_path, startMs, endMs, audioOnly);
    if (r && r.path) {
      // Reveal may fail (e.g., user just emptied the workspace exports
      // dir manually, or LaunchServices misbehaves). Don't let that
      // swallow the success toast — surface a separate err toast if
      // the reveal can't open Finder, but still tell the user the
      // clip itself succeeded.
      api.reveal(r.path).catch((e) =>
        flashToast(`Clip saved but reveal failed: ${e?.message || e}`,
                   { kind: "err", ttl: 3500 })
      );
      // Post-export hint: the clip is now in Finder, pre-selected. The
      // editor's next step is drag → NLE timeline. Spelling it out
      // saves the "what do I do with this?" beat for first-time users.
      const dur = ((endMs - startMs) / 1000).toFixed(1);
      const kind = audioOnly ? "MP3" : "MP4";
      flashToast(
        `${dur}s ${kind} ready in Finder — drag into CapCut, Final Cut, or DaVinci`,
        { kind: "ok", ttl: 4500 }
      );
    }
  } catch (e) {
    console.error("export failed", e);
    flashToast("Export failed: " + (e?.message || e), { kind: "err", ttl: 3500 });
  } finally {
    // Restore the button no matter what happened. innerHTML restore
    // preserves the original icon + label + ⌘E kbd hint exactly
    // (textContent assignment above blew them away on the disable
    // path; restoring innerHTML brings them back). Re-query because
    // _paneEl may have re-rendered during the await (state.selectedHit
    // changes trigger a fresh renderDetail → new button element).
    const stillThere = _paneEl.querySelector("#btn-export");
    if (stillThere && origLabel != null) {
      stillThere.disabled = false;
      stillThere.innerHTML = origLabel;
    }
  }
}

function _copyLink(hit) {
  // ⌘L is documented in the keyhelp overlay as "Copy a link to the
  // selected moment." Previously it copied a plain "filename @
  // timecode" string — useful as a label, useless as a "link." A
  // researcher / journalist / podcaster pasting the result into
  // Notion, Slack, Obsidian, or any markdown editor saw a plain
  // text fragment they couldn't click on.
  //
  // Three lines now, each useful in a different paste context:
  //   1. Markdown link: [filename @ timecode](file://abs/path)
  //      Pastes as a clickable hyperlink in any markdown-aware
  //      editor (Notion, Slack rich-text, Obsidian, GitHub, Hugo,
  //      Dropbox Paper, Discord). The "file://" URL opens the
  //      source file in the default app on macOS when clicked.
  //   2. Snippet line — the actual matched quote, with FTS5
  //      <mark>…</mark> tags stripped. Lets the paste-destination
  //      reader see WHY this moment was bookmarked without
  //      switching back to Tern. Skipped entirely when the hit
  //      has no snippet (visual-only hits).
  //   3. The bare absolute path — fallback for plain-text editors
  //      that don't render markdown. Same path that's inside the
  //      markdown link above.
  //
  // Multi-line format pastes cleanly into Notion/Slack/Obsidian
  // as a quote block. Plain-text destinations get the same content
  // with a visible structure.
  const fileUrl = "file://" + encodeURI(hit.file_path || "");
  const label = `${hit.file_name} @ ${hit.timecode || "00:00"}`;
  const snippet = (hit.snippet || "").replace(/<\/?mark>/g, "").trim();
  const parts = [`[${label}](${fileUrl})`];
  if (snippet) parts.push(`"${snippet}"`);
  parts.push(fileUrl);
  const text = parts.join("\n");
  navigator.clipboard.writeText(text)
    .then(() => flashToast("Link copied", { kind: "ok" }))
    .catch(() => flashToast("Couldn't copy", { kind: "err" }));
}

function _fmtDuration(ms) {
  const s = Math.floor(ms / 1000);
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = s % 60;
  const pad = (n) => String(n).padStart(2, "0");
  return h > 0 ? `${pad(h)}:${pad(m)}:${pad(sec)}` : `${pad(m)}:${pad(sec)}`;
}

function _escape(s) {
  return (s || "").replace(/[&<>"']/g, c => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
  }[c]));
}

export function initDetail() {
  _paneEl = document.getElementById("detail-pane");
  _render();

  subscribe((k) => { if (k === "selectedHit") _render(); });

  on(INTENTS.RESULT_OPEN, () => {
    // Focus + start the player if present
    const audio = _paneEl.querySelector("audio, video");
    if (audio) {
      audio.play().catch(() => {});
      audio.focus();
    }
  });
  on(INTENTS.EXPORT_CLIP, () => state.selectedHit && _exportClip(state.selectedHit));
  on(INTENTS.COPY_LINK, () => state.selectedHit && _copyLink(state.selectedHit));
  on(INTENTS.REVEAL_FILE, () => {
    const h = state.selectedHit;
    if (!h?.file_path) return;
    // Symmetric with the visible #btn-reveal click handler above: surface
    // the rejection via toast. The ⇧⌘R keyboard handler must not swallow
    // failures to console.error — same user, same failure (file
    // moved/deleted between indexing and shortcut press → 404), and
    // pressing the SHORTCUT must give the same feedback as CLICKING the
    // button, so a user can't be confused about whether the action fired.
    api.reveal(h.file_path).catch(e =>
      flashToast(`Couldn't reveal: ${e?.message || e}`, { kind: "err", ttl: 3500 })
    );
  });
  on(INTENTS.COPY_QUOTE, () => {
    // Match the row-context-menu "Copy quote" path — plain snippet text,
    // <mark> tags stripped, falls back to filename if there's no snippet.
    const h = state.selectedHit;
    if (!h) return;
    const quote = (h.snippet || "").replace(/<\/?mark>/g, "").trim();
    const text = quote || h.file_name || "";
    if (!text) return;
    navigator.clipboard.writeText(text)
      .then(() => flashToast(quote ? "Quote copied" : "Filename copied", { kind: "ok" }))
      .catch(() => flashToast("Couldn't copy", { kind: "err" }));
  });
  on(INTENTS.QUICK_LOOK, () => {
    // Mirror the REVEAL_FILE handler above + the #btn-quicklook click
    // handler — was previously an unhandled-rejection (Promise returned
    // but no .catch), so a Quick Look failure (file moved/deleted)
    // silently logged an "Uncaught (in promise) Error" with NO user
    // feedback. Space key now surfaces the same toast as the visible
    // button.
    const h = state.selectedHit;
    if (!h?.file_path) return;
    api.quicklook(h.file_path).catch(e =>
      flashToast(`Quick Look failed: ${e?.message || e}`, { kind: "err", ttl: 3500 })
    );
  });
  on(INTENTS.TOGGLE_BOOKMARK, () => {
    // Drive the SAME click path as the visible star — so the pop
    // animation + toast + icon swap all fire as if the user clicked.
    // Falls back to a no-op if no detail row is currently rendered.
    const bk = _paneEl?.querySelector("#detail-bookmark");
    if (bk) bk.click();
  });
}
