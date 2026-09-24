// app/modules/contextmenu.js — native-Mac-style right-click context menu.
//
// Single shared overlay element appended to <body>; menus are mounted and
// dismounted on demand. Auto-positions to stay inside the viewport (no
// overflow off the right/bottom edges).
//
// Usage:
//   import { showContextMenu } from "/modules/contextmenu.js";
//   element.addEventListener("contextmenu", (ev) => {
//     ev.preventDefault();
//     showContextMenu(ev, [
//       { label: "Copy", shortcut: "⌘C", onClick: () => navigator.clipboard.writeText(text) },
//       { divider: true },
//       { label: "Delete", danger: true, onClick: () => …, disabled: !canDelete },
//     ]);
//   });
//
// Why not use the platform <menu>/<menuitem>? Browser support is patchy
// and styling is impossible. A hand-rolled menu lets us match Tern's
// theming (Power Mode, accent colors) and supports keyboard navigation.

let _root = null;
let _open = false;

function _ensureRoot() {
  if (_root) return _root;
  _root = document.createElement("div");
  _root.id = "context-menu";
  _root.className = "ctxmenu";
  _root.hidden = true;
  _root.setAttribute("role", "menu");
  document.body.appendChild(_root);
  // Click outside → close
  document.addEventListener("mousedown", (ev) => {
    if (!_open) return;
    if (_root.contains(ev.target)) return;
    hideContextMenu();
  });
  // Keyboard handling. Use the CAPTURE phase + stopImmediatePropagation so
  // the global dispatcher in modules/keyboard.js doesn't ALSO fire when
  // the menu is open. Without capture, the menu's ArrowUp/Down would also
  // step the underlying results list (since both listeners are on window
  // at the bubble phase and run in registration order). With it, the menu
  // intercepts first and consumes the event.
  window.addEventListener("keydown", (ev) => {
    if (!_open) return;
    if (ev.key === "Escape") {
      ev.preventDefault();
      ev.stopImmediatePropagation();
      hideContextMenu();
      return;
    }
    if (ev.key === "ArrowDown" || ev.key === "ArrowUp") {
      ev.preventDefault();
      ev.stopImmediatePropagation();
      _moveSelection(ev.key === "ArrowDown" ? 1 : -1);
      return;
    }
    if (ev.key === "Enter") {
      ev.preventDefault();
      ev.stopImmediatePropagation();
      const sel = _root.querySelector(".ctxmenu-item.sel");
      if (sel && !sel.classList.contains("disabled")) sel.click();
    }
  }, true);  // ← capture phase
  // Scroll/resize → dismiss (avoid floating-menu-orphan UX)
  window.addEventListener("scroll", () => _open && hideContextMenu(), true);
  window.addEventListener("resize", () => _open && hideContextMenu());
  return _root;
}

function _moveSelection(delta) {
  const items = [..._root.querySelectorAll(".ctxmenu-item:not(.disabled)")];
  if (!items.length) return;
  const curIdx = items.findIndex(it => it.classList.contains("sel"));
  const nextIdx = curIdx < 0
    ? (delta > 0 ? 0 : items.length - 1)
    : (curIdx + delta + items.length) % items.length;
  items.forEach((it, i) => it.classList.toggle("sel", i === nextIdx));
}

function _esc(s) {
  return String(s || "").replace(/[&<>"']/g, c =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

export function showContextMenu(ev, items) {
  if (!items || !items.length) return;
  _ensureRoot();
  _root.innerHTML = items.map((it, i) => {
    if (it.divider) return `<div class="ctxmenu-divider" role="separator"></div>`;
    const cls = ["ctxmenu-item"];
    if (it.disabled) cls.push("disabled");
    if (it.danger)   cls.push("danger");
    return `
      <div class="${cls.join(" ")}" role="menuitem" data-idx="${i}" tabindex="-1"
           aria-disabled="${it.disabled ? "true" : "false"}">
        <span class="ctxmenu-label">${_esc(it.label)}</span>
        ${it.shortcut ? `<span class="ctxmenu-shortcut">${_esc(it.shortcut)}</span>` : ""}
      </div>
    `;
  }).join("");

  // Wire click handlers (use mousedown to fire before mousedown-outside listener)
  _root.querySelectorAll(".ctxmenu-item").forEach(node => {
    if (node.classList.contains("disabled")) return;
    const idx = Number(node.dataset.idx);
    const item = items[idx];
    node.addEventListener("click", () => {
      hideContextMenu();
      try { item.onClick?.(); } catch (e) { console.error("ctxmenu click failed", e); }
    });
    node.addEventListener("mouseenter", () => {
      _root.querySelectorAll(".ctxmenu-item.sel").forEach(s => s.classList.remove("sel"));
      node.classList.add("sel");
    });
  });

  // Position: anchor at click point, but flip if overflowing right/bottom edge.
  _root.hidden = false;
  _root.style.visibility = "hidden"; // measure offscreen, then show
  _root.style.left = "0px";
  _root.style.top  = "0px";
  const W = _root.offsetWidth;
  const H = _root.offsetHeight;
  const PAD = 6;
  const x = ev.clientX + W + PAD > window.innerWidth  ? Math.max(PAD, ev.clientX - W) : ev.clientX;
  const y = ev.clientY + H + PAD > window.innerHeight ? Math.max(PAD, ev.clientY - H) : ev.clientY;
  _root.style.left = x + "px";
  _root.style.top  = y + "px";
  _root.style.visibility = "visible";
  _open = true;
}

export function hideContextMenu() {
  if (!_root) return;
  _open = false;
  _root.hidden = true;
  _root.innerHTML = "";
}

export function isContextMenuOpen() { return _open; }
