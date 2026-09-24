// app/modules/icons.js
// SF-Symbol-style inline SVGs. All stroke 1.8, geometric, monoline.
// Returns string HTML for direct insertion via innerHTML / template literals.

const _wrap = (path, attrs = {}) => {
  const { fill = "none", stroke = "currentColor", w = 16, h = 16, sw = 1.8, vb = "0 0 24 24" } = attrs;
  return `<svg viewBox="${vb}" width="${w}" height="${h}" fill="${fill}" stroke="${stroke}" stroke-width="${sw}" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${path}</svg>`;
};

export const icons = {
  search:   (a) => _wrap('<circle cx="11" cy="11" r="7"/><path d="m20 20-3-3"/>', a),
  play:     (a) => _wrap('<path d="M8 5v14l11-7z" stroke="none"/>', { ...a, fill: "currentColor" }),
  pause:    (a) => _wrap('<rect x="6" y="5" width="4" height="14" rx="1" stroke="none"/><rect x="14" y="5" width="4" height="14" rx="1" stroke="none"/>', { ...a, fill: "currentColor" }),
  folder:   (a) => _wrap('<path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z" stroke="none"/>', { ...a, fill: "currentColor" }),
  gear:     (a) => _wrap('<circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z"/>', a),
  reveal:   (a) => _wrap('<path d="M3 7h18v13H3z"/><path d="M3 7l3-4h12l3 4"/>', a),
  open:     (a) => _wrap('<path d="M15 3h6v6"/><path d="M10 14 21 3"/><path d="M21 14v7H3V3h7"/>', a),
  download: (a) => _wrap('<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><path d="m7 10 5 5 5-5"/><path d="M12 15V3"/>', a),
  copy:     (a) => _wrap('<rect x="9" y="9" width="11" height="11" rx="2"/><path d="M5 15V6a2 2 0 0 1 2-2h9"/>', a),
  more:     (a) => _wrap('<circle cx="5"  cy="12" r="1.4" stroke="none"/><circle cx="12" cy="12" r="1.4" stroke="none"/><circle cx="19" cy="12" r="1.4" stroke="none"/>', { ...a, fill: "currentColor" }),
  sidebar:  (a) => _wrap('<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M9 4v16"/>', a),
  close:    (a) => _wrap('<path d="M18 6 6 18"/><path d="m6 6 12 12"/>', a),
  plus:     (a) => _wrap('<path d="M12 5v14"/><path d="M5 12h14"/>', a),
  shield:   (a) => _wrap('<path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/>', a),
  waveform: (a) => _wrap(
    '<rect x="3"  y="13" width="2.4" height="6"  rx="1.2" stroke="none"/>' +
    '<rect x="8"  y="9"  width="2.4" height="14" rx="1.2" stroke="none"/>' +
    '<rect x="13" y="4"  width="2.4" height="24" rx="1.2" stroke="none"/>' +
    '<rect x="18" y="9"  width="2.4" height="14" rx="1.2" stroke="none"/>' +
    '<rect x="23" y="11" width="2.4" height="10" rx="1.2" stroke="none"/>' +
    '<rect x="28" y="13" width="2.4" height="6"  rx="1.2" stroke="none"/>',
    { ...a, fill: "currentColor", vb: "0 0 32 32" }
  ),
  ternBird: (a) => _wrap('<path d="M2 14 L11 8 L13 11 L11 13 L19 11 L22 14 L13 16 L11 19 L8 16 Z" stroke="none"/>', { ...a, fill: "currentColor" }),
  starOutline: (a) => _wrap('<path d="M12 2.7l2.8 6.2 6.5.6-4.9 4.4 1.5 6.5L12 17l-5.9 3.4 1.5-6.5L2.7 9.5l6.5-.6z"/>', a),
  starFilled:  (a) => _wrap('<path d="M12 2.7l2.8 6.2 6.5.6-4.9 4.4 1.5 6.5L12 17l-5.9 3.4 1.5-6.5L2.7 9.5l6.5-.6z" stroke="none"/>', { ...a, fill: "currentColor" }),
  filter:      (a) => _wrap('<path d="M3 6h18M6 12h12M10 18h4"/>', a),
  question:    (a) => _wrap('<circle cx="12" cy="12" r="10"/><path d="M9.5 9a2.5 2.5 0 1 1 4.5 1.5c-.6.5-1.5 1-2 2"/><circle cx="12" cy="17" r="1" fill="currentColor"/>', a),
};
