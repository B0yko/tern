// app/modules/transcript.js
// Renders the matched line + N surrounding lines from /api/transcript/window.
// Highlights all instances of the query terms via marks. Click any word to
// seek the visible player to its segment timestamp (segment granularity in
// v1; word granularity would need word-offset payload).
import { state } from "/modules/state.js";
import { snippetHtml } from "/modules/row.js";

function _escape(s) {
  return (s || "").replace(/[&<>"']/g, c => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
  }[c]));
}

function _markupLine(text, query, isMatch) {
  // Split into word spans for click-seek. Inline-mark query terms (case-insensitive whole-word).
  const terms = (query || "").trim().split(/\s+/).filter(t => t.length >= 2);
  const reBuilder = terms.length
    ? new RegExp("(" + terms.map(_escapeRe).join("|") + ")", "gi")
    : null;

  // Tokenize on whitespace boundaries
  const html = _escape(text).split(/(\s+)/).map(token => {
    if (/^\s+$/.test(token)) return token;
    const wrapped = reBuilder
      ? token.replace(reBuilder, (m) => `<mark>${m}</mark>`)
      : token;
    return `<span class="word">${wrapped}</span>`;
  }).join("");
  return html;
}

function _escapeRe(s) {
  return s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

export function renderTranscript(container, data, hit) {
  if (!data || !data.lines || data.lines.length === 0) {
    // snippetHtml, not _escape: the backend hands us a snippet that already
    // carries <mark>…</mark> around the matched terms. Escaping it wholesale
    // printed the tags as visible text. snippetHtml protects the marks,
    // escapes everything else, then restores them — so OCR'd text out of an
    // untrusted media file still can't inject markup.
    container.innerHTML = `<div style="color:var(--text-3);font-size:12px;">${snippetHtml(hit.snippet || "")}</div>`;
    return;
  }
  const matchedIdx = data.matched_index;
  const q = state.query;
  // Track whether the user has already expanded this window — if true,
  // don't re-show the "Show more" button after a re-render.
  const isExpanded = container.dataset.transcriptExpanded === "1";

  const el = document.createElement("div");
  el.className = "transcript";
  data.lines.forEach((line, i) => {
    const isMatch = i === matchedIdx;
    const div = document.createElement("div");
    div.className = "transcript-line " + (isMatch ? "match" : "context");
    div.dataset.tsMs = String(line.ts_ms);
    div.innerHTML = _markupLine(line.text, q, isMatch);
    el.appendChild(div);
  });

  // "Show more context" affordance — extends the radius to 12 (≈25 lines
  // total) on click. Hidden once expanded so it can't double-click into
  // an infinite request loop. Hidden when isExpanded was already true
  // from a prior render.
  if (!isExpanded && data.lines.length >= 5) {
    const more = document.createElement("button");
    more.className = "transcript-more";
    more.type = "button";
    more.textContent = "Show more context ↓";
    more.addEventListener("click", async (ev) => {
      ev.stopPropagation();
      more.disabled = true;
      more.textContent = "Loading…";
      try {
        const { api } = await import("/modules/api.js");
        const expanded = await api.transcriptWindow(hit.file_id, hit.ts_ms, 12);
        container.dataset.transcriptExpanded = "1";
        renderTranscript(container, expanded, hit);
      } catch (e) {
        console.warn("expand transcript failed", e);
        more.textContent = "Couldn't load more — try again";
        more.disabled = false;
      }
    });
    el.appendChild(more);
  }

  // Wire click-seek
  el.addEventListener("click", (ev) => {
    const lineEl = ev.target.closest(".transcript-line");
    if (!lineEl) return;
    const tsMs = parseInt(lineEl.dataset.tsMs || "0", 10);
    const audio = document.querySelector("#detail-pane audio, #detail-pane video");
    if (audio) {
      audio.currentTime = tsMs / 1000;
      audio.play().catch(() => {});
    }
  });

  container.innerHTML = "";
  container.appendChild(el);
}
