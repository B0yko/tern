// app/modules/row.js — render one search hit row.
import { icons } from "/modules/icons.js";
import { state } from "/modules/state.js";
import { isBookmarked } from "/modules/bookmarks.js";

function _sourceLabel(hit) {
  const s = hit.sources && hit.sources.length > 1 ? hit.sources : [hit.source];
  const map = { transcript: "Speech", ocr: "On-screen", visual: "Visual" };
  if (s.length === 1) return { text: map[s[0]] || s[0], multi: false };
  // Multi-source: "Speech + On-screen" (first two)
  const labels = s.slice(0, 2).map(x => map[x] || x);
  return { text: labels.join(" + "), multi: true };
}

function _escape(s) {
  return (s || "").replace(/[&<>"']/g, c => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
  }[c]));
}

// Highlight matching query terms in a filename. Returns HTML-safe string
// with <mark> wraps. Tokens shorter than 2 chars are skipped to avoid
// spurious noise (e.g., "a" matching every other letter). Falls back to
// plain escape if there's no query, no tokens, or the filename is empty.
function _highlightFilename(name, query) {
  if (!name) return "";
  const escaped = _escape(name);
  const terms = (query || "").trim().split(/\s+/).filter(t => t.length >= 2);
  if (!terms.length) return escaped;
  // Escape the terms so regex metachars in the query don't break the regex.
  const reTerms = terms.map(t => t.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"));
  const re = new RegExp("(" + reTerms.join("|") + ")", "gi");
  return escaped.replace(re, "<mark>$1</mark>");
}

// Exported because the detail pane needs the identical treatment: the
// transcript renderer's no-transcript fallback (photos, and any hit whose
// /api/transcript/window comes back empty) used to plain-_escape() the
// snippet, so a buyer searching a photo saw the literal text
// "<mark>RANGE</mark> <mark>ROVER</mark>" under the image.
export function snippetHtml(snippet) {
  return _snippetHtml(snippet);
}

function _snippetHtml(snippet) {
  if (!snippet) return '<span style="font-style:italic;color:var(--text-3);">no snippet</span>';
  // Backend already wraps matches in <mark>…</mark>. Trust it but escape stray HTML.
  // Strategy: temporarily replace marks, escape, then restore.
  const MARK_OPEN = "\x00MARK_OPEN\x00";
  const MARK_CLOSE = "\x00MARK_CLOSE\x00";
  const protected_ = snippet
    .replace(/<mark>/g, MARK_OPEN)
    .replace(/<\/mark>/g, MARK_CLOSE);
  const escaped = _escape(protected_);
  return escaped
    .replace(new RegExp(MARK_OPEN, "g"), "<mark>")
    .replace(new RegExp(MARK_CLOSE, "g"), "</mark>");
}

function _thumb(hit) {
  if (hit.media_kind === "audio") {
    return `<span class="row-thumb audio">${icons.waveform({ w: 30, h: 30 })}</span>`;
  }
  if (hit.media_kind === "video") {
    const bg = hit.thumbnail_url
      ? `background-image:url('${hit.thumbnail_url}');background-size:cover;background-position:center;`
      : "";
    return `<span class="row-thumb video" style="${bg}">
      <span class="row-thumb-overlay">
        <span class="row-thumb-play">${icons.play({ w: 7, h: 7 })}</span>
        <span class="row-thumb-tc">${hit.timecode || ""}</span>
      </span>
    </span>`;
  }
  if (hit.media_kind === "image") {
    if (hit.thumbnail_url || hit.preview_url) {
      const src = hit.thumbnail_url || hit.preview_url;
      return `<span class="row-thumb image-real" style="background-image:url('${src}')"></span>`;
    }
    return `<span class="row-thumb image-fallback">${icons.open({ w: 22, h: 22 })}</span>`;
  }
  return `<span class="row-thumb audio">${icons.waveform({ w: 26, h: 26 })}</span>`;
}

export function renderRow(hit, index, selected) {
  const src = _sourceLabel(hit);
  const ts = hit.media_kind === "image" ? "" : (hit.timecode || "");
  // Per-source color hint on the pill — speech blue / on-screen orange /
  // visual green / multi accent. CSS uses [data-source] attribute, so
  // a new source kind only needs a row.css entry, no JS change.
  const sources = hit.sources && hit.sources.length ? hit.sources : [hit.source];
  const primarySource = src.multi ? "multi" : (sources[0] || "transcript");
  const el = document.createElement("div");
  // className is set below after the bookmark check, so we know whether
  // to include the row-bookmarked class.
  el.dataset.index = String(index);
  el.dataset.fileId = String(hit.file_id);
  el.dataset.source = primarySource;
  // a11y: rows are interactive (click selects, contextmenu opens menu).
  // Announce as a listbox option so screen readers describe them
  // consistently. aria-label gives the full row context in one go since
  // the visible content is split across thumbnail + title + snippet.
  el.setAttribute("role", "option");
  el.setAttribute("aria-selected", selected ? "true" : "false");
  el.setAttribute("tabindex", "-1");
  // Bookmark state: reflect on the row's hover-action star icon AND on
  // an `aria-label` suffix so screen readers announce "Bookmarked" up
  // front. Previously, the user had no visual cue in the results
  // list that a hit was already bookmarked — they had to look in the
  // sidebar's Bookmarks section to know. Now the star icon flips to
  // filled when isBookmarked() returns true, the button title also
  // changes ("Remove bookmark" vs "Bookmark this moment"), and the
  // row gets a `row-bookmarked` class for any future CSS treatment
  // (e.g., a subtle gold dot or left-edge accent — defaulting to no
  // visual change yet so the change stays scoped to the icon).
  const bookmarked = isBookmarked(hit.file_id, hit.ts_ms);
  el.className = "row" + (selected ? " sel" : "") + (bookmarked ? " row-bookmarked" : "");
  const bookmarkTitle = bookmarked ? "Remove bookmark (⌘⇧B)" : "Bookmark this moment (⌘⇧B)";
  const bookmarkIcon = bookmarked
    ? icons.starFilled({ w: 13, h: 13 })
    : icons.starOutline({ w: 13, h: 13 });
  el.setAttribute("aria-label", `${bookmarked ? "Bookmarked. " : ""}${src.text} match in ${hit.file_name || "file"}${ts ? ` at ${ts}` : ""}${hit.snippet ? `: ${(hit.snippet || "").replace(/<\/?mark>/g, "")}` : ""}`);
  el.innerHTML = `
    ${_thumb(hit)}
    <div class="row-body">
      <div class="row-title">
        <span class="row-name">${_highlightFilename(hit.file_name, state.query)}</span>
        <span class="row-pill${src.multi ? " multi" : ""}" data-source="${primarySource}">${_escape(src.text)}</span>
        ${ts ? `<span class="row-ts">${_escape(ts)}</span>` : ""}
      </div>
      <div class="row-snip">${_snippetHtml(hit.snippet)}</div>
    </div>
    <div class="row-hover-actions" aria-hidden="true">
      <button class="row-hover-btn" data-action="reveal" title="Reveal in Finder (⇧⌘R)" aria-label="Reveal in Finder">${icons.reveal({ w: 13, h: 13 })}</button>
      <button class="row-hover-btn${bookmarked ? " is-on" : ""}" data-action="bookmark" title="${bookmarkTitle}" aria-label="Toggle bookmark">${bookmarkIcon}</button>
      <button class="row-hover-btn" data-action="more" title="More actions (right-click)" aria-label="More actions">⋯</button>
    </div>
  `;
  return el;
}
