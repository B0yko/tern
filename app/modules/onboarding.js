// app/modules/onboarding.js — single-shot first-launch sheet (3 screens).
import { icons } from "/modules/icons.js";

const KEY = "tern.onboarded.v1";

const STEPS = [
  {
    title: "Search by what was said.",
    sub: "Tern transcribes every audio and video file locally with Whisper. Search any spoken phrase — \"pricing strategy\", \"Series A\", whatever.",
    icon: "waveform",
  },
  {
    title: "Search by what's on screen.",
    // Original copy quoted "$29/month" and "GitHub URL on a screenshare"
    // as illustrative examples. Both phrases matched ZERO rows in the
    // bundled demo workspace's ocr_segments — so a trial user who took
    // the tour, copied a quoted phrase into the search box, got "0
    // results" and walked away believing the OCR channel didn't work.
    // The topbar placeholder rotation avoids the same trap.
    // Replaced with two abstract use-cases (no scare-quoted
    // queries to copy verbatim) — the user understands what's possible
    // without being misled into typing a zero-match phrase.
    sub: "Apple Vision runs OCR on every keyframe on your Mac. The URL someone screenshared, a name in a lower-third — if it ever appeared on screen, you can search it later.",
    icon: "reveal",
  },
  {
    title: "Search by what's in the picture.",
    // Original copy quoted "orange cat", "mountain lake", "person at a
    // whiteboard". The demo's photos cover cat / mountain / whiteboard,
    // so single-word queries land — but the qualifiers ("orange",
    // "lake", "person at") don't, and a buyer typing the literal
    // phrase wonders why the marquee SigLIP-2 channel returned a
    // confidence-only-OK match instead of the perfect hit the tour
    // implied existed. Same trial-trust principle as screen 2: describe
    // the capability, don't seed a specific query that might let them
    // down.
    sub: "SigLIP-2 embeds every photo and video frame locally. You describe what you remember seeing; the model finds it. No tagging, no folder hunts.",
    icon: "open",
  },
];

let _root = null;
let _step = 0;

function _render() {
  const s = STEPS[_step];
  _root.innerHTML = `
    <div class="ob-card">
      <div class="ob-illustration">${icons[s.icon]({ w: 48, h: 48 })}</div>
      <h1 class="ob-title">${s.title}</h1>
      <p class="ob-sub">${s.sub}</p>
      <div class="ob-step-dots">
        ${STEPS.map((_, i) => `<span class="ob-step-dot ${i === _step ? "on" : ""}"></span>`).join("")}
      </div>
      <div class="ob-actions">
        ${_step === 0
          ? `<button class="btn" id="ob-skip">Skip</button>`
          : `<button class="btn" id="ob-back">Back</button>`}
        ${_step < STEPS.length - 1
          ? `<button class="btn primary" id="ob-next">Next</button>`
          : `<button class="btn" id="ob-add">Add my own folder</button>
             <button class="btn primary" id="ob-demo">Try the demo</button>`}
      </div>
    </div>
  `;
  const back = _root.querySelector("#ob-back");
  const skip = _root.querySelector("#ob-skip");
  const next = _root.querySelector("#ob-next");
  const add  = _root.querySelector("#ob-add");
  const demo = _root.querySelector("#ob-demo");
  if (back) back.addEventListener("click", () => { _step--; _render(); });
  if (skip) skip.addEventListener("click", _finish);
  if (next) next.addEventListener("click", () => { _step++; _render(); });
  if (add)  add.addEventListener("click", () => { _finish(); document.dispatchEvent(new CustomEvent("tern:open-folder-modal")); });
  if (demo) demo.addEventListener("click", _finish);
}

function _finish() {
  // Wrapped because an unguarded throw here (private-browsing WKWebView,
  // OS-restricted localStorage, full quota) would leave the sheet visible
  // — clicking the dismiss button would do nothing visible, and the user
  // would be stuck staring at the welcome screen. The "remember-dismiss"
  // state won't survive a restart in the broken-localStorage case, but
  // at least the user can actually USE the app now.
  try { localStorage.setItem(KEY, "1"); } catch {}
  _root.hidden = true;
}

export function initOnboarding() {
  _root = document.getElementById("onboarding-sheet");
  // Wrapped because an unguarded throw here on first paint (private
  // browsing, OS-restricted) would abort module init — every other
  // initX() in main.js scheduled AFTER initOnboarding never runs, and
  // the user gets a blank empty-state pane with no working search.
  let _alreadyOnboarded = false;
  try { _alreadyOnboarded = !!localStorage.getItem(KEY); } catch {}
  if (_alreadyOnboarded) {
    _root.hidden = true;
    return;
  }
  _root.hidden = false;
  _step = 0;
  _render();
}
