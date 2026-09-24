// app/modules/indexing.js — drag-drop + folder modal + status polling toast.
import { state } from "/modules/state.js";
import { api } from "/modules/api.js";
import { on, INTENTS } from "/modules/keyboard.js";

let _toast = null;
let _modal = null;
let _pollTimer = null;
let _drop = null;

// Module-level "user pressed Cancel" flag, checked by the multi-folder
// queue IIFE between submissions. Without this, the background
// queue kept POSTing remaining folders to /api/index AFTER the user
// clicked Cancel on the in-flight one — user thought they stopped,
// folder 2/N kicked off ~1.5 s later. The flag is set by the toast's
// Cancel button (sibling to api.indexCancel()) and reset at the start
// of every new _startIndexing call.
let _queueCancelled = false;
// Monotonic queue generation — see _startIndexing's myGen capture.
let _queueGen = 0;

// HTML escape (text + attribute safe). The toast renders user-controlled
// filenames (current_file) and log lines, both of which can carry `<`
// and friends if the user indexed an oddly-named file. Previous code
// stripped only `[<>&]` in some sites and left filename interpolation
// raw — `data-stage="${s.stage}"` and the current_file render were
// real XSS sinks for a folder containing e.g. `<img onerror=…>.mp4`.
function _esc(s) {
  return String(s == null ? "" : s).replace(/[&<>"']/g, c =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function _openFolderModal() {
  _modal.hidden = false;
  document.getElementById("folder-path").focus();
}
function _closeFolderModal() {
  _modal.hidden = true;
  document.getElementById("folder-error").hidden = true;
  document.getElementById("folder-path").value = "";
}

// Surface an inline message inside the folder modal — reuses the
// existing #folder-error slot. Used for non-fatal hints (Browse fallback)
// as well as backend errors when indexing fails.
function _showFolderHint(msg) {
  const errEl = document.getElementById("folder-error");
  if (!errEl) return;
  errEl.textContent = msg;
  errEl.hidden = false;
}

async function _startIndexing(paths) {
  const list = paths.split("\n").map(s => s.trim()).filter(Boolean);
  if (!list.length) return;
  // Reset the cancel flag for this new queue. A previous queue's
  // leftover cancellation must not pre-empt the new one.
  _queueCancelled = false;
  // Per-queue generation token. _queueCancelled is
  // a single module-level boolean shared by every queue IIFE ever
  // started — resetting it above REVIVED an older cancelled queue
  // that was still inside its 1.5-3 s status-wait sleep (its next
  // flag check saw false and resumed submitting its leftover
  // folders; two live IIFEs then submit-raced into 409 toasts).
  // Each queue captures its generation; any check that fails the
  // generation match means a NEWER queue exists and this one must
  // die regardless of the shared flag's current value.
  const myGen = ++_queueGen;
  const cancelled = () => _queueCancelled || myGen !== _queueGen;

  // POST the FIRST folder synchronously so any user-input errors
  // (path doesn't exist, refused by the /api/index folder-safety
  // gate, missing required binaries → 424) appear immediately in the
  // modal's error slot — exactly as before this multi-folder loop
  // existed. Subsequent folders queue in the background; errors
  // there land in the toast log instead.
  const errEl = document.getElementById("folder-error");
  errEl.hidden = true;
  let resp;
  try {
    resp = await api.indexFolder(list[0]);
  } catch (e) {
    errEl.textContent = String(e.message || e);
    errEl.hidden = false;
    return;
  }
  // Backend returns 200 with `{ok: false, message: "No supported media
  // files found in folder"}` when discover_files returns empty — NOT a
  // throw, so the old code happily closed the modal, started polling,
  // saw running=false, hid the toast — user saw NOTHING for what was
  // clearly a deliberate click. Surface the message in the modal's
  // existing error slot so the user knows the click registered and
  // why no indexing started. Keep them in the modal so they can either
  // pick a different folder OR cancel cleanly.
  if (resp && resp.ok === false) {
    errEl.textContent = resp.message || "Couldn't start indexing (no message returned).";
    errEl.hidden = false;
    return;
  }

  // First POST succeeded — close the modal and start the toast NOW so
  // the user sees indexing kick off, then drain the remaining queue
  // in the background. Without this early close, the modal stayed
  // open for the entire indexing run (potentially hours for many
  // folders) since the multi-folder fix made the loop
  // long-running.
  _closeFolderModal();
  _pollStatus();

  // Queue any additional folders. Each waits for the prior run to
  // complete (single-slot /api/index gate from main.py). Errors here
  // can't surface in the closed modal, so we use the toast. Respects
  // _queueCancelled — the cancel button (toast.js) sets that flag and
  // we bail before each remaining submission.
  (async () => {
    for (const p of list.slice(1)) {
      if (cancelled()) break;
      // Wait for `running: false` between submissions. /api/index/status
      // is cheap (already polled at 1.2 s by the toast); 1.5 s here
      // matches that cadence so we don't double-tax the backend.
      //
      // Transient status-fetch failures (brief loopback blip, sidecar
      // restart after a crash, dev-hot-reload pause) USED TO `break`
      // out of this wait loop, which then submitted the next folder
      // INTO the still-running indexer — backend returned 409, the
      // outer catch surfaced "Queue failed: 409 Indexing already in
      // progress" for THIS folder, then immediately did the same for
      // every remaining folder. A 2-second blip dropped the entire
      // queue with a flurry of red toasts when the first job was
      // actually fine. Slow-retry pattern: keep polling at 1.5 s on
      // healthy, back off to 3 s on consecutive failures, give up
      // only after ~30 s of consecutive failures (= backend really
      // is down, not a blip) so a permanently-dead sidecar doesn't
      // loop forever either.
      let consecutiveFailures = 0;
      const MAX_FAILURES = 10;   // ~30 s of failed polls before giving up
      while (true) {
        if (cancelled()) return;
        try {
          const s = await api.indexStatus();
          consecutiveFailures = 0;
          if (!s || !s.running) break;
        } catch {
          consecutiveFailures += 1;
          if (consecutiveFailures >= MAX_FAILURES) {
            const toast = await import("/modules/toast.js");
            toast.flashToast(
              `Queue paused — backend not responding (gave up after ${MAX_FAILURES} retries). Restart Tern to resume.`,
              { kind: "err", ttl: 6000 });
            return;
          }
        }
        // 1.5 s healthy cadence; 3 s once we've started accumulating
        // failures so we're not hammering a flailing backend.
        await new Promise(r =>
          setTimeout(r, consecutiveFailures > 0 ? 3000 : 1500));
      }
      if (cancelled()) break;
      try {
        await api.indexFolder(p);
        // Restart the toast poll for THIS folder. The done-toast branch of _pollStatus does not
        // reschedule itself, and the toast's 1.2 s cadence vs this
        // queue's 1.5 s wait means the toast almost always observed
        // the running=false gap BETWEEN folders and died — folders
        // 2..N indexed invisibly: no progress bar, no stage label,
        // and no Stop button (the only Cancel control lives in the
        // toast). _pollStatus is idempotent-safe to call here: it
        // clears any pending timer before scheduling its own.
        _pollStatus();
      } catch (e) {
        const toast = await import("/modules/toast.js");
        toast.flashToast(`Queue failed for ${p}: ${e.message || e}`,
                         { kind: "err", ttl: 4500 });
      }
    }
  })();
}

// Has the user seen at least one "running" poll? If yes, finishing earns
// a 3-second "done" pause + fade-out instead of a snap-hide.
let _seenRunning = false;

async function _pollStatus() {
  clearTimeout(_pollTimer);
  try {
    const s = await api.indexStatus();
    state.isIndexing = !!s.running;
    if (s.running) {
      _seenRunning = true;
      _toast.classList.remove("done");
      _renderToast(s);
      _pollTimer = setTimeout(_pollStatus, 1200);
    } else if (_seenRunning) {
      // Show a brief "done" state, then fade out via the .done CSS class.
      _renderDoneToast(s);
      _toast.classList.remove("done");
      _toast.hidden = false;
      // Two RAFs so the just-revealed state paints before adding .done,
      // letting the opacity/transform transition kick in.
      requestAnimationFrame(() => requestAnimationFrame(() => {
        setTimeout(() => {
          _toast.classList.add("done");
          setTimeout(() => { _toast.hidden = true; _toast.classList.remove("done"); }, 400);
        }, 2200);
      }));
      _seenRunning = false;
      // Refresh state.stats so the empty-state and any other
      // stats-driven UI re-renders with the new file count. Without this,
      // a day-1 user who just indexed their first folder still sees the
      // marketing hero because files_total in the cached stats is 0 —
      // the "Indexed 47 files" toast fires, the hero stays, looks broken.
      // empty.js's subscriber on "stats" handles the re-render.
      try { state.stats = await api.stats(); } catch {}
    } else {
      _toast.hidden = true;
    }
  } catch {
    // Status fetch failed — usually a transient sidecar hiccup (force-
    // quit + restart, crash + auto-respawn, brief network blip on the
    // loopback). If we'd already started showing the indexing toast,
    // hiding it now reads as "Tern silently cancelled my indexing
    // job" — contradicting the pingBackend banner that says "backend
    // isn't responding" and burning user trust. Keep the toast frozen
    // at its last-known state and retry at a slower cadence (3 s vs
    // the normal 1.2 s) until the backend comes back, at which point
    // the next poll either resumes normal updates (still running) or
    // transitions to the done state (the restart cleared running=false).
    if (_seenRunning) {
      _pollTimer = setTimeout(_pollStatus, 3000);
    } else {
      // Previously, the "no toast ever shown" branch hid the toast
      // and STOPPED the poll loop entirely. Real failure mode that hit
      // this branch:
      //   1. Tern launches before its sidecar has bound the port.
      //   2. First _pollStatus() from initIndexing() hits 503 / connect-
      //      refused, lands here.
      //   3. _seenRunning=false → poll loop dies.
      //   4. User opens Terminal, runs `tern index ~/Podcasts` via CLI.
      //   5. Backend is now indexing, but the desktop app's indexing
      //      toast doesn't fire — frontend has no live poller.
      // Schedule a slow-cadence retry (15 s) so we eventually catch
      // either: (a) the sidecar finishing its startup + reporting
      // running=true, or (b) the user-triggered indexing pass.
      // 15 s picked to be MUCH slower than the 1.2 s healthy / 3 s
      // recovering cadences (this path runs forever if nothing ever
      // indexes) but fast enough that a CLI-started index gets a
      // matching desktop toast within ~15 s of starting. Toast stays
      // hidden until we actually see running=true, so this is purely
      // background polling — no UI surface change.
      _toast.hidden = true;
      _pollTimer = setTimeout(_pollStatus, 15000);
    }
  }
}

function _renderDoneToast(s) {
  const done = s.files_done || 0;
  const errs = s.files_errored || 0;
  const skipped = s.files_skipped || 0;

  // Wall-clock elapsed — start_time may be missing on very fast batches.
  let elapsedStr = "";
  if (s.start_time) {
    const elapsed_s = (Date.now() / 1000) - Number(s.start_time);
    if (elapsed_s > 0 && isFinite(elapsed_s)) {
      elapsedStr = elapsed_s < 60
        ? ` in ${Math.round(elapsed_s)}s`
        : ` in ${Math.floor(elapsed_s / 60)}m ${Math.round(elapsed_s % 60)}s`;
    }
  }

  // Stitch the per-category counts. Skipped + errored only surface when
  // they're non-zero; the common case ("everything indexed cleanly")
  // stays a single short line. Previously, re-indexing a folder
  // where most files were already done showed "Indexed 5 files in 12s"
  // — the user was looking at a folder of 100 and wondered where the
  // other 95 went. Now: "Indexed 5 files in 12s · 95 already up-to-date".
  const parts = [`Indexed ${done} file${done === 1 ? "" : "s"}${elapsedStr}`];
  if (skipped > 0) parts.push(`${skipped} already up-to-date`);
  if (errs > 0)    parts.push(`${errs} errored`);
  const summary = parts.join(" · ");
  // Sub-text branches on outcome quality. "Search updated with the
  // new files." is the right message when SOME files actually
  // indexed; it reads as cheerful gaslighting when every file in
  // the batch failed (done === 0 && errs > 0). In that case the
  // user needs the path to the log, not a "we're all good!" pat
  // on the head. Mostly-failed batches (errs > done) get a softer
  // log-pointer too so the user knows where to look for the
  // per-file traceback that log_event("index_file_failed", …)
  // wrote (commit 8377671).
  let subText = "Search updated with the new files.";
  if (done === 0 && errs > 0) {
    subText = `All ${errs} file${errs === 1 ? "" : "s"} failed to index — check <code>~/Library/Logs/tern-crash.log</code> for the codec / path / permission issue.`;
  } else if (errs > done && errs >= 3) {
    subText = `Many files failed (${errs} of ${done + errs}) — the per-file errors are in <code>~/Library/Logs/tern-crash.log</code>.`;
  }
  _toast.innerHTML = `
    <div class="indexing-toast-head">
      <div class="indexing-toast-spinner" style="border-color:transparent;border-top-color:var(--ok);animation:none;background:var(--ok);border-radius:50%;width:14px;height:14px;"></div>
      <div class="indexing-toast-title">${summary}</div>
    </div>
    <div class="indexing-toast-log">${subText}</div>
  `;
}

function _fmtETA(seconds) {
  if (!isFinite(seconds) || seconds <= 0) return "";
  if (seconds < 60) return `~${Math.round(seconds)}s`;
  if (seconds < 3600) return `~${Math.round(seconds / 60)}m`;
  const h = Math.floor(seconds / 3600);
  const m = Math.round((seconds % 3600) / 60);
  return `~${h}h ${m}m`;
}

function _renderToast(s) {
  const done = s.files_done || 0;
  // Backend semantic: `files_pending` is set ONCE at start to the TOTAL
  // file count (see api/main.py:1183) and never decremented. Field name
  // is a long-standing misnomer — it's the discovered-total, not what
  // remains. The previous toast code computed `total = done + pending`
  // which double-counted the done files: at 3/5 progress, toast said
  // "3 / 8" and ETA was pessimistic by 2× near the end. Treat the field
  // as TOTAL (since that's what it actually is) and derive remaining.
  const total = s.files_pending || 0;
  const skipped = s.files_skipped || 0;
  const errs = s.files_errored || 0;
  // Progress bar counts skipped + errored toward "processed" so the bar
  // actually reaches 100% on completion. Previously, only files_done
  // counted — re-indexing a folder where most files were already up-to-date
  // (force=False default, mtime matches stored row) left the bar stalled
  // under 100% even after the loop finished, because skipped files counted
  // in total but not in done. The brief flash of stuck-at-70% before the
  // toast flipped to its done variant felt broken; counting all
  // outcome-categories matches the user's mental model of "this folder is
  // done being processed".
  const processed = done + skipped + errs;
  const remaining = Math.max(0, total - processed);
  const pct = total ? Math.round((processed / total) * 100) : 0;
  // ETA: avg seconds per PROCESSED file (done + skipped + errored) ×
  // remaining files. Using processed rather than just done matters when
  // re-indexing a partially-done folder: skipped files are near-free
  // (mtime check, no Whisper/SigLIP work) so a pass through 50 already-
  // indexed + 50 new files takes much less than "50 done × 30s each".
  // The old elapsed/done formula over-estimated wildly on those mixed
  // batches. Only render after 1 file is processed (avoids the
  // wildly wrong "~0s" sample-of-zero estimate).
  const elapsed = s.elapsed_s || 0;
  const etaText = (processed >= 1 && remaining > 0)
    ? _fmtETA((elapsed / processed) * remaining)
    : "";
  // Sub-file stage label + elapsed + Whisper sub-progress percent.
  // For a 60-min podcast, Whisper alone takes 3-5 min; without this row,
  // the toast just said "ep03.mp3" the whole time. With this row:
  //   "Transcribing speech 47% (23s)"
  // stage_progress is null for stages that don't emit percent (probe,
  // embed, ocr) — only Whisper currently does.
  let stageText = "";
  if (s.stage_label) {
    const stageElapsed = s.stage_started_at
      ? Math.max(0, (Date.now() / 1000) - Number(s.stage_started_at))
      : 0;
    const stageElapsedStr = stageElapsed >= 5
      ? ` <span style="color:var(--text-3);font-family:var(--font-mono);font-size:10.5px;">(${Math.round(stageElapsed)}s)</span>`
      : "";
    const pct = (typeof s.stage_progress === "number" && s.stage_progress >= 0 && s.stage_progress <= 100)
      ? s.stage_progress : null;
    const pctStr = pct !== null
      ? ` <span style="font-family:var(--font-mono);font-size:11px;font-weight:600;">${pct}%</span>`
      : "";
    stageText = `<div class="indexing-toast-stage" data-stage="${_esc(s.stage)}">${_esc(s.stage_label)}${pctStr}${stageElapsedStr}</div>`;
  }

  _toast.innerHTML = `
    <div class="indexing-toast-head">
      <div class="indexing-toast-spinner"></div>
      <div class="indexing-toast-title">Indexing… ${processed} / ${total}${etaText ? ` <span style="color:var(--text-3);font-weight:500;font-family:var(--font-mono);font-size:11px;">· ${etaText} left</span>` : ""}</div>
    </div>
    <div class="indexing-toast-bar"><div class="indexing-toast-bar-fill" style="width:${pct}%;"></div></div>
    <div class="indexing-toast-current">${_esc((s.current_file || "preparing").split("/").pop())}</div>
    ${stageText}
    <div class="indexing-toast-log">${(s.log || []).slice(-3).map(l => _esc(l)).join("<br/>")}</div>
    <button class="indexing-toast-cancel" id="toast-cancel">Stop indexing</button>
  `;
  _toast.hidden = false;
  _toast.querySelector("#toast-cancel").addEventListener("click", async (ev) => {
    // Capture the button BEFORE any await:
    // ev.currentTarget is nulled by the browser once event dispatch
    // completes, so reading it after `await api.indexCancel()` threw
    // TypeError — the "re-enable so the user CAN retry" recovery in
    // the catch never ran, and the button stayed stuck at a disabled
    // "Stopping…" precisely when the backend was unreachable (the
    // exact scenario the recovery exists for).
    const btn = ev.currentTarget;
    // Set the queue-cancelled flag FIRST so the background IIFE bails
    // before its next submission, even if api.indexCancel takes a
    // moment to round-trip. Cancel-after-cancel is idempotent.
    _queueCancelled = true;
    // Disable + relabel inline so the user gets a sub-second confirmation
    // the click registered, instead of staring at a button that looks
    // dead until the next 1.2 s status poll repaints the toast. Commit
    // 8ca4d91 made the backend kill whisper-cli within ~1 s (1 Hz
    // cancel_cb poll) so "Stopping…" is the truthful interim state.
    btn.disabled = true;
    btn.textContent = "Stopping…";
    try {
      await api.indexCancel();
    } catch (e) {
      // Cancel POST failed — most likely a transient loopback blip
      // (the kind that motivated commit d5c9cfd). The queue-cancel
      // flag we set above already bails the in-frontend submission
      // loop, but the backend's currently-running pass continues.
      // Surface the failure so the user knows to retry rather than
      // staring at a frozen "Stopping…" button forever.
      const toast = await import("/modules/toast.js");
      toast.flashToast(`Cancel failed: ${e.message || e}. Retry?`,
                       { kind: "err", ttl: 5000 });
      // Re-enable so the user CAN retry. btn may have been replaced
      // by a toast re-render since; isConnected guards the stale ref.
      if (btn.isConnected) {
        btn.disabled = false;
        btn.textContent = "Stop indexing";
      }
    }
  });
}

function _initDragDrop() {
  _drop = document.getElementById("drop-overlay");

  // Drag counter: dragenter/dragleave fire for every child element, not only
  // the page-level transition. Without counting, the overlay flickers and —
  // worse — can get stuck visible if a dragleave fires for a nested element
  // but the matching dragenter was on a sibling. A stuck overlay (z-index 80,
  // inset 0) captures every click in the app, looking like a total freeze.
  let depth = 0;
  const _hide = () => { depth = 0; _drop.hidden = true; };

  const _hasFiles = (ev) => ev.dataTransfer && (
    ev.dataTransfer.types.includes("Files") ||
    ev.dataTransfer.types.includes("text/uri-list")
  );

  window.addEventListener("dragenter", (ev) => {
    if (!_hasFiles(ev)) return;
    depth++;
    _drop.hidden = false;
  });
  window.addEventListener("dragover", (ev) => {
    if (_hasFiles(ev)) ev.preventDefault(); // required to allow drop
  });
  window.addEventListener("dragleave", () => {
    depth = Math.max(0, depth - 1);
    if (depth === 0) _drop.hidden = true;
  });
  window.addEventListener("drop", (ev) => {
    ev.preventDefault();
    _hide();
    const paths = [];
    if (ev.dataTransfer.files) {
      for (const f of ev.dataTransfer.files) {
        // For folder drops in Tauri/Webkit, file.path is the absolute
        // POSIX path of the dragged item. Standard browsers don't
        // expose this (security), so dev-mode drag-drop silently
        // returns nothing.
        if (f.path) paths.push(f.path);
      }
    }
    if (!paths.length) {
      // No usable paths means we're either in a browser dev environment
      // (file.path is gated behind a Tauri permission) or the user
      // dragged something pathless like a clipboard payload. Open the
      // modal so they can paste a path instead of leaving them with
      // nothing happening at all.
      _openFolderModal();
      _showFolderHint("Drag-drop only works inside the Tern app (browser sandboxing hides folder paths). Paste an absolute path here instead.");
      return;
    }
    _startIndexing(paths.join("\n"));
  });
  // Esc as defensive fallback — always dismisses the overlay regardless of
  // what the drag-counter thinks.
  window.addEventListener("keydown", (ev) => {
    if (ev.key === "Escape" && !_drop.hidden) _hide();
  });
  // Belt-and-suspenders: a click on the overlay itself dismisses it. If the
  // drag-counter ever desyncs and the user finds the overlay stuck, a single
  // click anywhere clears it instead of needing to force-quit the app.
  _drop.addEventListener("click", _hide);
}

export function initIndexing() {
  _toast = document.getElementById("indexing-toast");
  _modal = document.getElementById("folder-modal");

  document.addEventListener("tern:open-folder-modal", _openFolderModal);
  on(INTENTS.ADD_FOLDER, _openFolderModal);

  document.getElementById("btn-close-folder-modal").addEventListener("click", _closeFolderModal);
  document.getElementById("btn-cancel-folder").addEventListener("click", _closeFolderModal);
  document.getElementById("btn-start-indexing").addEventListener("click", () => {
    const v = document.getElementById("folder-path").value;
    _startIndexing(v);
  });

  // Tauri native browse. The plugin may be absent in:
  //   - the bare `run.sh` dev mode (browser-served app, no Tauri shell)
  //   - older bundle versions where the dialog plugin wasn't enabled
  // In either case we fall back to the existing textarea + paste-path
  // flow and surface a quick inline hint so the user knows why nothing
  // happened. Previously this listener silently no-op'd, leaving people
  // staring at an unresponsive "Browse…" button.
  const browseBtn = document.getElementById("btn-browse-folder");
  browseBtn.addEventListener("click", async () => {
    const dialog = window.__TAURI__?.dialog;
    if (!dialog?.open) {
      _showFolderHint("Native picker is only available inside the Tern app. Paste an absolute path below — or in Finder, right-click your folder → Copy as Pathname.");
      return;
    }
    try {
      const sel = await dialog.open({ directory: true, multiple: true });
      if (sel) {
        const arr = Array.isArray(sel) ? sel : [sel];
        document.getElementById("folder-path").value = arr.join("\n");
      }
    } catch (e) {
      _showFolderHint("Couldn't open the native folder picker: " + (e?.message || e));
    }
  });

  _initDragDrop();
  _pollStatus();   // in case indexing is already running on load
}
