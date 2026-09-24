// app/modules/keyboard.js
// Single global keyboard dispatcher. Handlers register by intent, not key
// code, so reassignment / docs stay sane.

const _handlers = new Map();  // intent → fn

export const INTENTS = {
  FOCUS_SEARCH:  "focus_search",     // ⌘K
  RESULT_UP:     "result_up",        // ↑
  RESULT_DOWN:   "result_down",      // ↓
  RESULT_OPEN:   "result_open",      // ⏎  (focuses player, starts playback)
  RESULT_CLEAR:  "result_clear",     // Esc
  TOGGLE_SIDEBAR:"toggle_sidebar",   // ⌘B
  ADD_FOLDER:    "add_folder",       // ⇧⌘O
  EXPORT_CLIP:   "export_clip",      // ⌘E
  COPY_LINK:     "copy_link",        // ⌘L
  REVEAL_FILE:   "reveal_file",      // ⇧⌘R — Reveal selected hit in Finder
  PREFERENCES:   "preferences",      // ⌘,
  QUICK_LOOK:    "quick_look",       // Space (when not typing)
  SAVE_SEARCH:   "save_search",      // ⌘D
  TOGGLE_SPEECH: "toggle_speech",    // ⌥1 — toggle Speech source kind
  TOGGLE_OCR:    "toggle_ocr",       // ⌥2 — toggle On-screen text source kind
  TOGGLE_VISUAL: "toggle_visual",    // ⌥3 — toggle Visual scene source kind
  COPY_QUOTE:    "copy_quote",       // ⌘C (when not in input field)
  TOGGLE_BOOKMARK: "toggle_bookmark", // ⌘⇧B — pin/unpin selected hit moment
};

export function on(intent, fn) {
  _handlers.set(intent, fn);
  return () => _handlers.delete(intent);
}

function _dispatch(intent, ev) {
  const fn = _handlers.get(intent);
  if (fn) {
    ev.preventDefault();
    fn(ev);
  }
}

function _isTyping(el) {
  return el && (el.tagName === "INPUT" || el.tagName === "TEXTAREA" || el.isContentEditable);
}

// Stricter check used for ↑/↓ navigation: these only need to pass through
// when the editor has VERTICAL caret movement to preserve. Single-line
// <input> elements have none (↑/↓ do nothing native), so we still hijack
// them for result navigation. Multi-line <textarea> and contentEditable
// regions DO move the caret between lines, so ↑/↓ must pass through —
// otherwise the folder-modal textarea (rows="3", users paste multiple
// folder paths) becomes uneditable line-by-line.
function _isMultilineEditor(el) {
  return el && (el.tagName === "TEXTAREA" || el.isContentEditable);
}

export function initKeyboard() {
  window.addEventListener("keydown", (ev) => {
    const meta = ev.metaKey || ev.ctrlKey;
    const shift = ev.shiftKey;
    const typing = _isTyping(document.activeElement);

    // Always-available shortcuts (work even while typing in search input)
    if (meta && ev.key.toLowerCase() === "k")    return _dispatch(INTENTS.FOCUS_SEARCH, ev);
    // TOGGLE_SIDEBAR is plain ⌘B — explicitly exclude shift so the
    // SHIFTED form falls through to TOGGLE_BOOKMARK below. Without
    // `!shift` here, ⌘⇧B matched line "meta && b" first and returned,
    // shadowing TOGGLE_BOOKMARK entirely — bookmark shortcut was
    // advertised in ⌘/ keyhelp but never actually fired since the
    // intent was added.
    if (meta && !shift && ev.key.toLowerCase() === "b") return _dispatch(INTENTS.TOGGLE_SIDEBAR, ev);
    if (meta && shift && ev.key.toLowerCase() === "o") return _dispatch(INTENTS.ADD_FOLDER, ev);
    if (meta && shift && ev.key.toLowerCase() === "b") return _dispatch(INTENTS.TOGGLE_BOOKMARK, ev);
    if (meta && ev.key === ",")                  return _dispatch(INTENTS.PREFERENCES, ev);
    if (meta && ev.key.toLowerCase() === "e")    return _dispatch(INTENTS.EXPORT_CLIP, ev);
    if (meta && ev.key.toLowerCase() === "l")    return _dispatch(INTENTS.COPY_LINK, ev);
    if (meta && ev.key.toLowerCase() === "d")    return _dispatch(INTENTS.SAVE_SEARCH, ev);
    if (meta && shift && ev.key.toLowerCase() === "r") return _dispatch(INTENTS.REVEAL_FILE, ev);

    // ⌘C — when NOT typing (no input/textarea/contenteditable focused),
    // copy the selected row's quote. Preserves native ⌘C inside inputs
    // so users can still copy from the search field / textareas. The
    // browser's default ⌘C also still works on selected text — we only
    // intercept the "no text selection, no input focus" case.
    if (meta && !shift && ev.key.toLowerCase() === "c" && !typing) {
      const sel = window.getSelection?.();
      // If the user has an actual text selection, let the browser handle it.
      if (!sel || sel.isCollapsed) {
        return _dispatch(INTENTS.COPY_QUOTE, ev);
      }
    }

    // ⌥1 / ⌥2 / ⌥3 — toggle source-kind scope filters from anywhere
    // (including while typing in the search input). On macOS Option+digit
    // produces a special character; we key off ev.code === "Digit1/2/3"
    // which is layout-independent. Excludes meta/shift so we don't
    // collide with system Cmd-1 shortcuts (which we never bind anyway).
    if (ev.altKey && !meta && !shift) {
      if (ev.code === "Digit1") return _dispatch(INTENTS.TOGGLE_SPEECH, ev);
      if (ev.code === "Digit2") return _dispatch(INTENTS.TOGGLE_OCR, ev);
      if (ev.code === "Digit3") return _dispatch(INTENTS.TOGGLE_VISUAL, ev);
    }

    // GitHub/Twitter/Notion-style "/" focus-search. Only fires when NOT
    // already typing — so `/` inside the search input passes through to
    // the actual character (e.g., for URL-like queries "https://x.com").
    if (ev.key === "/" && !typing && !meta && !shift) {
      return _dispatch(INTENTS.FOCUS_SEARCH, ev);
    }

    // Navigation: ↑↓ work even while typing in the single-line search
    // input (no vertical caret motion to step on), but pass through to
    // <textarea> / contentEditable so the user CAN navigate lines of
    // pasted folder paths in the Add-folder modal. Without this exception,
    // a user pasting three folder paths into folder-path and pressing ↑
    // to fix a typo on line 2 got their cursor stolen by the (hidden)
    // result list.
    const ae = document.activeElement;
    if (ev.key === "ArrowUp"   && !_isMultilineEditor(ae)) return _dispatch(INTENTS.RESULT_UP, ev);
    if (ev.key === "ArrowDown" && !_isMultilineEditor(ae)) return _dispatch(INTENTS.RESULT_DOWN, ev);
    if (ev.key === "Enter" && !typing) return _dispatch(INTENTS.RESULT_OPEN, ev);
    if (ev.key === "Enter" && typing && document.activeElement.id === "search-input")
      return _dispatch(INTENTS.RESULT_OPEN, ev);
    if (ev.key === "Escape")     return _dispatch(INTENTS.RESULT_CLEAR, ev);
    if (ev.key === " " && !typing) return _dispatch(INTENTS.QUICK_LOOK, ev);
  });
}
