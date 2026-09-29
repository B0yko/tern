// app/modules/results.js
import { state, subscribe } from "/modules/state.js";
import { on, INTENTS } from "/modules/keyboard.js";
import { renderRow } from "/modules/row.js";
import { attachPreview, cancelPreview } from "/modules/preview.js";
import { api } from "/modules/api.js";
import { showContextMenu } from "/modules/contextmenu.js";
import { isBookmarked, toggle as toggleBookmark } from "/modules/bookmarks.js";
import { isSaved, toggle as toggleSaved } from "/modules/saved.js";
import { flashToast } from "/modules/toast.js";

let _listEl = null;

function _renderSkeletons(n = 8) {
  _listEl.innerHTML = "";
  for (let i = 0; i < n; i++) {
    const sk = document.createElement("div");
    sk.className = "skeleton-row";
    _listEl.appendChild(sk);
  }
}

// Escape HTML for safe interpolation into innerHTML.
function _esc(s) {
  return String(s || "").replace(/[&<>"']/g, c =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

// Find recent queries that look "similar" to `q` so we can offer
// "Did you mean…?" rescue options when search returns nothing.
// Heuristic: case-insensitive substring match in either direction,
// or ≥50% token overlap. Cheap; runs only on the empty-state path.
function _similarRecents(q, recents, max = 3) {
  const ql = q.toLowerCase().trim();
  if (!ql || !Array.isArray(recents)) return [];
  const qTokens = new Set(ql.split(/\s+/).filter(Boolean));
  const scored = [];
  for (const r of recents) {
    const rq = (r.query || "").toLowerCase();
    if (!rq || rq === ql) continue;
    let score = 0;
    if (rq.includes(ql) || ql.includes(rq)) score += 3;
    const rTokens = rq.split(/\s+/).filter(Boolean);
    const shared = rTokens.filter(t => qTokens.has(t)).length;
    if (shared) score += shared * 2;
    if (score > 0) scored.push({ q: r.query, hits: r.hits || 0, score });
  }
  scored.sort((a, b) => b.score - a.score);
  return scored.slice(0, max);
}

// Other recent queries (excluding the current one) to offer as quick re-tries.
function _otherRecents(q, recents, max = 4) {
  const ql = q.toLowerCase().trim();
  return (recents || [])
    .filter(r => r.query && r.query.toLowerCase() !== ql)
    .slice(0, max);
}

function _renderEmpty() {
  const q = state.query || "";
  const qEsc = _esc(q);
  const recents = state.recents || [];
  const similar = _similarRecents(q, recents, 3);
  const others = _otherRecents(q, recents, 4);

  // Active-filter detection — most common reason for "0 results" is the
  // user accidentally restricted scope (e.g., toggled Visual off) or a
  // folder filter is still applied from a previous query.
  const restrictedSources = Array.isArray(state.sources) && state.sources.length < 3;
  const folderActive = !!state.folderFilter;
  const filtersOn = restrictedSources || folderActive;

  const filterParts = [];
  if (restrictedSources) {
    const onLabels = (state.sources || []).map(s =>
      ({ transcript: "Speech", ocr: "On-screen", visual: "Visual" }[s] || s)
    );
    filterParts.push(`Sources limited to <strong>${onLabels.join(" + ")}</strong>`);
  }
  if (folderActive) {
    const f = state.folderFilter;
    const shortFolder = f.length > 40 ? "…" + f.slice(-38) : f;
    filterParts.push(`Folder limited to <code>${_esc(shortFolder)}</code>`);
  }

  const similarChips = similar.length
    ? `<div class="empty-q-section">
         <div class="empty-q-label">Did you mean?</div>
         <div class="empty-q-pills">
           ${similar.map(s => `
             <button class="empty-q-pill primary" data-q="${_esc(s.q)}">
               ${_esc(s.q)}<span class="empty-q-count">${s.hits}</span>
             </button>
           `).join("")}
         </div>
       </div>`
    : "";

  const otherChips = others.length
    ? `<div class="empty-q-section">
         <div class="empty-q-label">Or try one of your recent searches</div>
         <div class="empty-q-pills">
           ${others.map(o => `
             <button class="empty-q-pill" data-q="${_esc(o.query)}">${_esc(o.query)}</button>
           `).join("")}
         </div>
       </div>`
    : "";

  const filterBanner = filtersOn
    ? `<div class="empty-q-filterbanner">
         <span class="empty-q-fbicon">⚠</span>
         <div class="empty-q-fbtext">${filterParts.join(" · ")}</div>
         <button class="empty-q-fbreset" id="empty-clear-filters">Search everything</button>
       </div>`
    : "";

  // Tip branches on three cases. Empty-workspace handling matters
  // most for the day-1 buyer who arrived here by clicking a pill
  // on the empty-state hero — the phonetic-keyword tip is useless
  // advice for a user who literally has 0 files indexed. Send them
  // to the actual fix (index a folder) instead. Re-uses state.stats
  // (same field the library subtitle reads) so the
  // detection is consistent across both surfaces.
  const stats = state.stats;
  const workspaceEmpty = stats && (stats.files_total || 0) === 0;
  let tip;
  if (filtersOn) {
    tip = `Clearing the active filter is usually the fastest fix.`;
  } else if (workspaceEmpty) {
    tip = `Your library is empty — index a folder first with ⇧⌘O or the + button in the sidebar.`;
  } else {
    tip = `Whisper transcribes phonetically — try a single distinctive keyword instead of a whole phrase, or a synonym.`;
  }

  // Library context subtitle. "0 results" alone reads as
  // "is search broken?" — adding the actual library size
  // ("you have 4h 32m of content across 19 files") reframes it
  // as "search works, this query just didn't match" and softens
  // the trust friction at the exact moment buyers conclude
  // an app doesn't work. Uses the same total_duration_ms +
  // files_total fields that the empty-state
  // breakdown reads, so the data is already in flight on every
  // workspace with any indexed content. Skips silently if
  // state.stats hasn't loaded yet (no point rendering "0 files")
  // or if the workspace is genuinely empty.
  //
  // Reuses the `stats` const already captured above for the
  // workspace-empty tip branch — previously a second
  // `const stats = state.stats;` here was a duplicate declaration
  // in the same function scope, which is a parse-time SyntaxError
  // in ES modules (verified: `node --input-type=module -e ...`
  // throws "Identifier 'stats' has already been declared"). The
  // bug came from adding the workspace-empty detection block above
  // without realizing the same name was already in use further
  // down. WKWebView
  // SHOULD have failed to load results.js on launch — the fact
  // that the app appeared to run means either the empty-state
  // codepath rarely runs in practice or the bundle copies were
  // out of sync.
  let librarySubtitle = "";
  if (stats && (stats.files_total || 0) > 0) {
    const fileCount = stats.files_total;
    const fileWord = fileCount === 1 ? "file" : "files";
    let durStr = "";
    const durMs = stats.total_duration_ms || 0;
    if (durMs > 0) {
      const totalMin = Math.round(durMs / 60_000);
      const h = Math.floor(totalMin / 60);
      const m = totalMin % 60;
      durStr = h > 0 ? `${h}h ${m}m of content across ` : `${m}m of content across `;
    }
    librarySubtitle = `<div class="empty-q-subtitle" style="font-size:12px;color:var(--text-3);margin-top:4px;">Your library has ${durStr}${fileCount} ${fileWord} indexed.</div>`;
  }

  _listEl.innerHTML = `
    <div class="results-empty">
      <div class="empty-q-title">
        <strong>No moments matched “${qEsc}” yet.</strong>
      </div>
      ${librarySubtitle}
      ${filterBanner}
      ${similarChips}
      ${otherChips}
      <div class="empty-q-tip"><span class="empty-q-tip-label">Tip</span>${tip}</div>
    </div>
  `;

  // Wire pills → re-run search with the chosen query.
  _listEl.querySelectorAll(".empty-q-pill").forEach(btn => {
    btn.addEventListener("click", () => {
      const input = document.getElementById("search-input");
      if (input) {
        input.value = btn.dataset.q;
        input.dispatchEvent(new Event("input", { bubbles: true }));
        input.focus();
      }
    });
  });

  // Filter banner reset → clear scope filters AND re-run current query.
  const clear = _listEl.querySelector("#empty-clear-filters");
  if (clear) clear.addEventListener("click", () => {
    state.sources = ["transcript", "ocr", "visual"];
    state.folderFilter = null;
    // The sources/folderFilter subscriber in topbar.js auto-re-runs the query.
  });
}

function _render() {
  if (!_listEl) return;

  // Empty query → results.js leaves rendering to empty.js (which mounts in
  // the same pane). Clear our own content.
  if (!state.query.trim()) {
    _listEl.innerHTML = "";
    _listEl.hidden = true;
    return;
  }
  _listEl.hidden = false;

  if (state.isSearching && state.results.length === 0) {
    _renderSkeletons(8);
    return;
  }
  if (state.results.length === 0) {
    _renderEmpty();
    return;
  }

  _listEl.innerHTML = "";
  // Bulk-export pill: only show when ≥ 2 results to keep low-N clean
  if (state.results.length >= 2) {
    const bulk = document.createElement("div");
    bulk.className = "results-bulk";
    // Coverage + latency subtitle. Two pieces of texture:
    //   - "across N files" — signals search depth (12 hits in 4 files
    //     is meaningfully different from 12 hits in one file). Tells
    //     a buyer "Tern actually pulled from across my archive,
    //     not just regex'd one transcript."
    //   - "0.04s" — matches the homepage's "find any moment in 3
    //     seconds" promise with PROOF at the moment of use. We measure
    //     the full client-observed round trip (fetch + JSON parse +
    //     async offload) so the number is what the buyer just felt,
    //     not whatever the engine reports internally.
    // Only render when state.lastSearchMs is set (skip on the very
    // first search if the timer somehow didn't fire).
    const uniqueFiles = new Set(state.results.map(h => h.file_id).filter(x => x != null)).size;
    const fileCountFrag = uniqueFiles >= 2
      ? ` <span class="results-bulk-meta">· across ${uniqueFiles} files</span>`
      : "";
    const latencyFrag = (typeof state.lastSearchMs === "number")
      ? ` <span class="results-bulk-meta">· ${(state.lastSearchMs / 1000).toFixed(2)}s</span>`
      : "";
    bulk.innerHTML = `
      <span class="results-bulk-count">${state.results.length}</span>
      <span>results</span>${fileCountFrag}${latencyFrag}
      <span class="results-bulk-actions">
        <button class="results-bulk-btn" id="bulk-export-csv" title="Dump all ${state.results.length} matched moments to a CSV — opens in Numbers / Excel / Sheets for tagging, sorting, citing">Export CSV</button>
        <button class="results-bulk-btn" id="bulk-export-fcpxml" title="Generate FCPXML containing all ${state.results.length} matched moments for Final Cut import">Export FCPXML</button>
      </span>
    `;
    bulk.querySelector("#bulk-export-csv").addEventListener("click", _bulkExportCsv);
    bulk.querySelector("#bulk-export-fcpxml").addEventListener("click", _bulkExportFcpxml);
    _listEl.appendChild(bulk);
  }
  state.results.forEach((hit, i) => {
    const node = renderRow(hit, i, i === state.selectedIndex);
    node.addEventListener("click", (ev) => {
      // Hover-action buttons get their own clicks routed below; the row
      // background click still selects.
      if (ev.target.closest(".row-hover-btn")) return;
      _select(i);
    });
    node.addEventListener("contextmenu", (ev) => _onRowContextMenu(ev, hit, i));
    // Hover-action wiring — three small icons that appear on row hover.
    // Avoids the user needing right-click for the most common actions.
    node.querySelectorAll(".row-hover-btn").forEach(btn => {
      btn.addEventListener("click", (ev) => {
        ev.stopPropagation();
        const action = btn.dataset.action;
        if (action === "reveal") {
          if (hit.file_path) {
            api.reveal(hit.file_path).then(() => flashToast("Revealed in Finder", { kind: "ok" }))
              .catch(e => flashToast("Reveal failed: " + (e.message || e), { kind: "err" }));
          }
        } else if (action === "bookmark") {
          if (!hit.file_id) return;
          const wasOn = isBookmarked(hit.file_id, hit.ts_ms);
          toggleBookmark({
            file_id: hit.file_id,
            file_name: hit.file_name,
            ts_ms: hit.ts_ms,
            timecode: hit.timecode || "",
            snippet: (hit.snippet || "").replace(/<\/?mark>/g, ""),
          });
          flashToast(wasOn ? "Bookmark removed" : "Bookmarked", { kind: "ok" });
        } else if (action === "more") {
          _onRowContextMenu(ev, hit, i);
        }
      });
    });
    attachPreview(node, hit);
    _listEl.appendChild(node);
  });
}

// Right-click on a result row — show a native-style context menu with
// the common actions. Mirrors what's available in detail.js but reachable
// without first making the row "selected". This is the muscle-memory
// affordance every Mac user expects.
function _onRowContextMenu(ev, hit, i) {
  ev.preventDefault();
  ev.stopPropagation();
  _select(i); // also select the row visually so the user sees what they're acting on

  // Build a clean plain-text snippet (no <mark> tags) for clipboard copy.
  const cleanSnippet = (hit.snippet || "").replace(/<\/?mark>/g, "");
  const tc = hit.timecode || "";
  const isImg = hit.media_kind === "image";

  const items = [
    {
      label: cleanSnippet ? "Copy quote" : "Copy file name",
      shortcut: "⌘C",
      onClick: () => {
        _writeClipboard(cleanSnippet || hit.file_name || "");
        flashToast("Copied", { kind: "ok" });
      },
      disabled: !cleanSnippet && !hit.file_name,
    },
    !isImg && {
      label: `Copy timestamp ${tc ? `(${tc})` : ""}`,
      onClick: () => {
        _writeClipboard(`${hit.file_name || "?"} @ ${tc || "0:00"}`);
        flashToast("Timestamp copied", { kind: "ok" });
      },
      disabled: !tc,
    },
    !isImg && cleanSnippet && tc && {
      label: "Copy quote with reference",
      onClick: () => {
        _writeClipboard(`"${cleanSnippet.trim()}" — ${hit.file_name || "?"} @ ${tc}`);
        flashToast("Quote with reference copied", { kind: "ok" });
      },
    },
    { divider: true },
    {
      label: "Reveal in Finder",
      // GLOBAL binding lives in keyboard.js as REVEAL_FILE = `meta + shift + r`,
      // dispatched by detail.js as api.reveal(state.selectedHit.file_path).
      // The label MUST match the binding — a user who reads "⌘R" and tries
      // it gets nothing (Safari-style reload is also unbound) and assumes
      // the menu lied. The backend timeout handling covers the reliability
      // surface; this covers the keyboard-shortcut promise surface.
      shortcut: "⇧⌘R",
      onClick: () => api.reveal(hit.file_path).catch(e =>
        flashToast(`Couldn't reveal: ${e?.message || e}`, { kind: "err", ttl: 3500 })
      ),
      disabled: !hit.file_path,
    },
    {
      label: "Open in default app",
      // No global ⌘O binding exists (ADD_FOLDER uses ⇧⌘O — distinct intent).
      // Dropping the wrong-shortcut hint instead of inventing a new global
      // binding: ⌘O would collide with the macOS standard "Open file"
      // dialog metaphor (system already shows ⌘O in EVERY File menu) so
      // adding a Tern-specific override would confuse muscle memory. The
      // context menu IS the discoverable surface for this action.
      onClick: () => api.open(hit.file_path).catch(e =>
        flashToast(`Couldn't open: ${e?.message || e}`, { kind: "err", ttl: 3500 })
      ),
      disabled: !hit.file_path,
    },
    !isImg && {
      label: "Quick Look",
      // Plain "Space" matches keyboard.js INTENTS.QUICK_LOOK binding (Space
      // when not typing). Correct as-is — kept for reference next to the
      // siblings whose labels HAD to be fixed.
      shortcut: "Space",
      onClick: () => api.quicklook(hit.file_path).catch(e =>
        flashToast(`Quick Look failed: ${e?.message || e}`, { kind: "err", ttl: 3500 })
      ),
      disabled: !hit.file_path,
    },
    { divider: true },
    !isImg && {
      label: isBookmarked(hit.file_id, hit.ts_ms) ? "Remove bookmark" : "Bookmark this moment",
      // GLOBAL binding lives in keyboard.js as TOGGLE_BOOKMARK = `meta +
      // shift + b`, dispatched by detail.js. Plain "B" alone is unbound —
      // it would fall through the global dispatcher (no plain-letter
      // shortcuts exist except `/` for focus-search) and the user got
      // nothing. ⌘⇧B is the actually-bound chord.
      shortcut: "⌘⇧B",
      onClick: () => {
        const wasOn = isBookmarked(hit.file_id, hit.ts_ms);
        toggleBookmark({
          file_id: hit.file_id,
          file_name: hit.file_name,
          ts_ms: hit.ts_ms,
          timecode: tc,
          snippet: cleanSnippet,
        });
        flashToast(wasOn ? "Bookmark removed" : "Bookmarked", { kind: "ok" });
      },
      disabled: !hit.file_id,
    },
    state.query && state.query.trim() && {
      label: isSaved(state.query.trim()) ? "Remove from Saved searches" : "Save this search",
      shortcut: "⌘D",
      onClick: () => {
        const q = state.query.trim();
        const wasOn = isSaved(q);
        // Snapshot scope (sources + folderFilter) into the saved entry
        // so re-running it restores the user's intent — see saved.js
        // toggle() docstring.
        const scope = {
          sources: Array.isArray(state.sources) ? state.sources : null,
          folder: state.folderFilter || null,
        };
        toggleSaved(q, state.results?.length || 0, scope);
        flashToast(wasOn ? "Removed from Saved" : "Saved", { kind: "ok" });
      },
    },
  ].filter(Boolean);

  showContextMenu(ev, items);
}

// Wrapper around navigator.clipboard.writeText with a graceful execCommand
// fallback (Safari WKWebView in older bundles can be picky about clipboard
// permission inside the loopback origin). Best-effort, swallow errors.
function _writeClipboard(text) {
  if (!text) return;
  try {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).catch(() => _fallbackCopy(text));
    } else {
      _fallbackCopy(text);
    }
  } catch {
    _fallbackCopy(text);
  }
}
function _fallbackCopy(text) {
  const ta = document.createElement("textarea");
  ta.value = text;
  ta.style.position = "fixed";
  ta.style.opacity = "0";
  document.body.appendChild(ta);
  ta.select();
  try { document.execCommand("copy"); } catch {}
  document.body.removeChild(ta);
}

// Disable + relabel a bulk-pill export button during the await so
// an impatient buyer can't double-click and fire a second backend
// pass. Same pattern as detail.js _exportClip and
// license.js _activate. Returns a restore function the caller MUST
// invoke in a finally block — the button stays disabled until the
// caller cleans up. Defensive: skips when the button isn't in the
// DOM (pill re-rendered mid-export, or a programmatic caller fired
// the handler without a click event).
function _bulkBtnLoading(btnId, busyLabel) {
  const btn = _listEl.querySelector("#" + btnId);
  if (!btn) return () => {};
  const orig = btn.innerHTML;
  btn.disabled = true;
  btn.textContent = busyLabel;
  return () => {
    const still = _listEl.querySelector("#" + btnId);
    if (still) {
      still.disabled = false;
      still.innerHTML = orig;
    }
  };
}

async function _bulkExportCsv() {
  if (!state.results.length) return;
  const projectName = state.query ? `Tern · ${state.query}` : "Tern Results";
  const restore = _bulkBtnLoading("bulk-export-csv", "Exporting…");
  try {
    // Same plumbing-via-api.js rationale as _bulkExportFcpxml: 60 s
    // default timeout, throw-on-non-2xx so a 422 (>200 hit cap) or
    // 500 surfaces as a friendly toast instead of a silent no-op.
    const r = await api.exportCsv(state.results, projectName);
    if (r && r.path) {
      flashToast(`CSV ready in Finder — ${state.results.length} hit${state.results.length === 1 ? "" : "s"}`,
                 { kind: "ok" });
      // Reveal-on-success mirrors the FCPXML path. Catch the reveal
      // failure separately so a missed Finder pop doesn't swallow
      // the export success.
      api.reveal(r.path).catch((e) =>
        flashToast(`CSV saved but reveal failed: ${e?.message || e}`,
                   { kind: "err", ttl: 3500 })
      );
    }
  } catch (e) {
    console.error("bulk CSV export failed", e);
    flashToast(`CSV export failed: ${e?.message || e}`,
               { kind: "err", ttl: 4500 });
  } finally {
    restore();
  }
}

async function _bulkExportFcpxml() {
  if (!state.results.length) return;
  const projectName = state.query ? `Tern · ${state.query}` : "Tern Results";
  // Disable + relabel during the await — same rationale
  // as _bulkExportCsv above. FCPXML is the worst-case multi-click
  // target because it shells out to ffprobe per UNIQUE source file
  // (up to 30 s ceiling each); a buyer impatient with a 50-hit
  // FCPXML spanning 10 source videos could fire two passes that
  // both run for ~25 s.
  const restore = _bulkBtnLoading("bulk-export-fcpxml", "Exporting…");
  try {
    // Use api.exportFcpxml instead of raw fetch — same migration rationale
    // as sidebar.removeFolder, detail.exportSrt and
    // license.activate/clear. Three things this inherits
    // from api.js _json that the raw fetch didn't have:
    //   - 60 s default timeout (matters: FCPXML generation shells out
    //     to ffprobe per UNIQUE source file in the result set, so a
    //     50-hit FCPXML spanning 10 source videos = 10 sync ffprobe
    //     calls; backend wraps in to_thread but a wedged ffprobe
    //     would still hang the response — 60 s timeout surfaces it)
    //   - throw on non-2xx (matters: 422 if hits > 200 cap; 500 if
    //     ffprobe failed; before this, `.then(r.json())` parsed the
    //     `{detail: "..."}` error body as success-shape, `r.path` was
    //     undefined, `if (r && r.path)` silently skipped, user
    //     clicked Export FCPXML and saw NOTHING happen)
    //   - AbortController support (no current caller, but the
    //     plumbing exists for a future "cancel bulk export" button)
    const r = await api.exportFcpxml(state.results, projectName);
    if (r && r.path) {
      // Surface success — without this toast the user clicks "Export
      // FCPXML", Finder pops to the saved file, but if they're on
      // multi-monitor / focused on the Tern window the Finder pop is
      // easy to miss. A success toast in the Tern window itself makes
      // the action's effect visible without forcing a context switch.
      flashToast(`FCPXML ready in Finder — ${state.results.length} hit${state.results.length === 1 ? "" : "s"}`,
                 { kind: "ok" });
      // Reveal in Finder — wire toast-on-fail just like every other
      // api.reveal call (same pattern). Previously the
      // reveal was fire-and-forget; if it failed (workspace exports/
      // dir vanished, perms changed, etc.), the user got the success
      // toast above but Finder didn't open and the rejection landed
      // as an unhandled-promise console.error nobody saw.
      api.reveal(r.path).catch((e) =>
        flashToast(`FCPXML saved but reveal failed: ${e?.message || e}`,
                   { kind: "err", ttl: 3500 })
      );
    }
  } catch (e) {
    console.error("bulk FCPXML export failed", e);
    // Surface the error so the user knows their click registered AND
    // why it didn't work, instead of staring at the unchanged results
    // pane (the raw-fetch pre-migration just console.error'd and
    // moved on — no UI feedback at all).
    flashToast(`FCPXML export failed: ${e?.message || e}`,
               { kind: "err", ttl: 4500 });
  } finally {
    restore();
  }
}

function _select(i) {
  cancelPreview(); // stop any hover preview when user makes a real selection
  state.selectedIndex = i;
  state.selectedHit = state.results[i] || null;
  // Scroll the selected row into view if off-screen
  const node = _listEl.querySelector(`.row[data-index="${i}"]`);
  if (node) node.scrollIntoView({ block: "nearest", behavior: "smooth" });
  _highlightOnly(i);
}

function _highlightOnly(i) {
  _listEl.querySelectorAll(".row").forEach(n => {
    n.classList.toggle("sel", Number(n.dataset.index) === i);
  });
}

export function initResults() {
  const pane = document.getElementById("results-pane");
  _listEl = document.createElement("div");
  _listEl.className = "results-list";
  _listEl.id = "results-list";
  // a11y: announce the results list as a listbox so screen readers
  // describe row navigation correctly (↓↑ moves selection; rows get
  // role="option" + aria-selected in row.js).
  _listEl.setAttribute("role", "listbox");
  _listEl.setAttribute("aria-label", "Search results");
  pane.appendChild(_listEl);

  subscribe((k) => {
    if (k === "results" || k === "isSearching" || k === "query") _render();
    if (k === "selectedIndex") _highlightOnly(state.selectedIndex);
    // Bookmarks: re-render when user toggles a bookmark (via ⌘⇧B,
    // context menu, or row hover-action star). Without this, the
    // row.js bookmarked-state visual indicator (the change that added
    // starFilled vs starOutline) would only update on the NEXT
    // search re-render — toggling a bookmark on a stable result
    // list wouldn't visually reflect until you typed a new query.
    if (k === "bookmarks") _render();
  });

  on(INTENTS.RESULT_UP, () => {
    if (!state.results.length) return;
    _select(Math.max(0, state.selectedIndex - 1));
  });
  on(INTENTS.RESULT_DOWN, () => {
    if (!state.results.length) return;
    _select(Math.min(state.results.length - 1, state.selectedIndex + 1));
  });

  // Auto-advance on clip end. player.js emits 'tern:play-next' when an
  // audio/video element fires 'ended'. We step to next result + autoplay.
  document.addEventListener("tern:play-next", () => {
    if (!state.results.length) return;
    if (state.selectedIndex >= state.results.length - 1) return; // at end
    _select(state.selectedIndex + 1);
    // Let detail.js render, then play. Two RAFs so render+player init complete.
    requestAnimationFrame(() => requestAnimationFrame(() => {
      const a = document.querySelector("#detail-pane audio, #detail-pane video");
      if (a) a.play().catch(() => {});
    }));
  });
}
