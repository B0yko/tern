// app/modules/toast.js — transient bottom-right confirmation toasts.
//
// Used to acknowledge user-initiated actions that otherwise have no
// visible feedback: clipboard writes, bookmark toggles, saved-search
// changes, anything where success means "nothing visibly happened".
//
// Usage:
//   import { flashToast } from "/modules/toast.js";
//   flashToast("Copied"); // 1.8 s fade-in / fade-out
//   flashToast("Saved", { kind: "ok" });        // optional kind
//   flashToast("Couldn't copy", { kind: "err" }); // error tint
//
// Stacks up to 3 toasts; older ones get pushed up. Each manages its
// own dismiss timer so consecutive calls don't reset earlier toasts.
//
// CSS comes from /toast.css (loaded in index.html).

let _stack = null;

function _ensureStack() {
  if (_stack) return _stack;
  _stack = document.createElement("div");
  _stack.id = "toast-stack";
  _stack.className = "toast-stack";
  document.body.appendChild(_stack);
  return _stack;
}

const MAX_TOASTS = 3;
const DEFAULT_TTL = 1800;  // ms

export function flashToast(message, opts = {}) {
  if (!message) return;
  const stack = _ensureStack();

  // Cap the stack — drop the oldest immediately if we're at the limit.
  while (stack.children.length >= MAX_TOASTS) {
    stack.firstChild?.remove();
  }

  const kind = opts.kind || "info";
  // Toasts with an Undo button get a longer default TTL — the user
  // needs time to read + decide.
  const ttl = typeof opts.ttl === "number" ? opts.ttl
            : (opts.undo ? 4200 : DEFAULT_TTL);

  const el = document.createElement("div");
  el.className = `toast toast-${kind}`;

  if (opts.undo) {
    const msgSpan = document.createElement("span");
    msgSpan.className = "toast-msg";
    msgSpan.textContent = message;
    const undoBtn = document.createElement("button");
    undoBtn.type = "button";
    undoBtn.className = "toast-undo";
    undoBtn.textContent = "Undo";
    el.appendChild(msgSpan);
    el.appendChild(undoBtn);
    undoBtn.addEventListener("click", (ev) => {
      ev.stopPropagation();
      try { opts.undo(); } catch (e) { console.error("undo failed", e); }
      // Dismiss this toast and confirm with a fresh one.
      el.remove();
      flashToast("Undone", { kind: "ok", ttl: 1400 });
    });
  } else {
    el.textContent = message;
  }
  stack.appendChild(el);

  // Force layout so the entrance animation runs (otherwise the element
  // is created already-styled and CSS transitions don't fire).
  // requestAnimationFrame is the cheapest way to flush.
  requestAnimationFrame(() => el.classList.add("toast-in"));

  // Fade out + remove. Two-step so the CSS transition actually plays.
  const fade = setTimeout(() => {
    el.classList.remove("toast-in");
    el.classList.add("toast-out");
    setTimeout(() => el.remove(), 250);
  }, ttl);

  // Click outside the Undo button dismisses early.
  el.addEventListener("click", (ev) => {
    if (ev.target.classList?.contains("toast-undo")) return;
    clearTimeout(fade);
    el.classList.remove("toast-in");
    el.classList.add("toast-out");
    setTimeout(() => el.remove(), 200);
  });
}
