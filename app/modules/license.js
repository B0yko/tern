// app/modules/license.js — license-key activation modal.
// Opened via 'tern:open-license-modal' CustomEvent (sidebar badge, keyhelp
// About link). Backend endpoints: GET /api/license/status, POST
// /api/license/activate, POST /api/license/clear.

import { state } from "/modules/state.js";
import { icons } from "/modules/icons.js";
import { api } from "/modules/api.js";
import { flashToast } from "/modules/toast.js";

let _el = null;

// HTML escape (text + attribute safe). email/license_key come from the
// license server response — trusted in normal operation but defense in
// depth: a tampered local license.json could carry `<script>` strings
// that would otherwise render straight into the WKWebView. The prior
// `[<>&]` strip silently corrupted email addresses containing `&`.
function _esc(s) {
  return String(s == null ? "" : s).replace(/[&<>"']/g, c =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function _renderState(status) {
  if (!status) return `<div style="color:var(--text-3);font-size:12.5px;">Loading…</div>`;
  if (status.status === "active") {
    return `
      <div class="license-state license-state-active">
        <div class="license-state-badge"><span class="license-dot-on"></span> Licensed</div>
        <div class="license-state-meta">
          ${status.email ? `<div><strong>${_esc(status.email)}</strong></div>` : ""}
          ${status.license_key ? `<div style="font-family:var(--font-mono);font-size:11px;color:var(--text-3);">${_esc(status.license_key)}</div>` : ""}
          ${status.validated_at ? `<div style="font-size:11px;color:var(--text-3);">Validated ${new Date(status.validated_at).toLocaleDateString()}</div>` : ""}
        </div>
      </div>
    `;
  }
  const t = status.trial || null;
  const spent = t && t.exhausted;
  return `
    <div class="license-state license-state-trial">
      <div class="license-state-badge"><span class="license-dot-off"></span> ${spent ? "Trial used up" : "Unlicensed (trial)"}</div>
      <div class="license-state-meta" style="color:var(--text-3);font-size:12.5px;">
        ${_trialLine(t)}
      </div>
    </div>
  `;
}

// The trial meters media duration, not days. Say so in the same units the
// user thinks in, and say what happens next — a bare "trial" badge with no
// number is how someone discovers the limit by hitting it mid-job.
function _trialLine(trial) {
  if (!trial) return "Trial mode.";
  if (trial.licensed) return "Licensed.";
  const min = ms => Math.max(0, Math.floor((ms || 0) / 60000));
  const limit = min(trial.limit_ms);
  if (trial.exhausted) {
    return `You have indexed all ${limit} trial minutes. Enter a licence key to index more &mdash; everything already indexed stays searchable.`;
  }
  const left = min(trial.remaining_ms);
  return `${left} of ${limit} trial minutes left. Photos don't count &mdash; only audio and video length does.`;
}

async function _open() {
  let status = null;
  try { status = await api.licenseStatus(); state.license = status; } catch {}

  _el.innerHTML = `
    <div class="modal">
      <header class="modal-header">
        <h2>License</h2>
        <button class="topbar-btn" id="license-close" aria-label="Close">${icons.close({ w: 14, h: 14 })}</button>
      </header>
      <div class="modal-body">
        <div id="license-state-block">${_renderState(status)}</div>
        <label for="license-key-input" style="margin-top:14px;">License key</label>
        <input type="text" id="license-key-input" placeholder="TERN-XXXX-XXXX-XXXX" autocorrect="off" autocapitalize="off" spellcheck="false" />
        <p style="font-size:11px;color:var(--text-3);margin-top:6px;">
          Without a key Tern runs as a trial and stops indexing once the trial
          minutes are used. Search, playback and export of anything already
          indexed keep working either way.
        </p>
        ${status && status.status === "active" ? "" : `
          <!-- Licence-request CTA. Only shown when the user is NOT
               already licensed (i.e. trial or status-load-failed), so a
               trial user has a path to a key right next to the input
               instead of having to go looking for one. Tern has no store:
               keys are issued on request through the project's issue
               tracker. target="_blank" + rel="noopener" matches the
               keyhelp.js pattern for external links so it opens in the
               system browser, not inside the WKWebView. -->
          <div style="margin-top:12px;padding:10px 12px;background:var(--surface-2);border:1px solid var(--border);border-radius:8px;display:flex;align-items:center;gap:10px;">
            <div style="flex:1;font-size:12px;color:var(--text-2);line-height:1.4;">
              No key yet? Tern is not sold through a store. Licence keys are issued on request.
            </div>
            <a href="https://github.com/B0yko/tern/issues" target="_blank" rel="noopener"
               class="btn primary"
               style="white-space:nowrap;text-decoration:none;font-size:12px;">
              Request a licence
            </a>
          </div>
        `}
        <div id="license-error" class="folder-error" hidden></div>
      </div>
      <footer class="modal-footer">
        ${status && status.status === "active"
          ? `<button class="btn" id="license-clear">Remove license</button>`
          : ""}
        <span style="flex:1"></span>
        <button class="btn" id="license-cancel">Close</button>
        <button class="btn primary" id="license-activate">Activate</button>
      </footer>
    </div>
  `;
  _el.hidden = false;

  _el.querySelector("#license-close").addEventListener("click", _close);
  _el.querySelector("#license-cancel").addEventListener("click", _close);
  _el.addEventListener("click", (ev) => { if (ev.target === _el) _close(); });
  _el.querySelector("#license-activate").addEventListener("click", _activate);
  const clearBtn = _el.querySelector("#license-clear");
  if (clearBtn) clearBtn.addEventListener("click", _clear);
  const input = _el.querySelector("#license-key-input");
  input.focus();
  input.addEventListener("keydown", (ev) => {
    if (ev.key === "Enter") { ev.preventDefault(); _activate(); }
    if (ev.key === "Escape") { ev.preventDefault(); _close(); }
  });
}

function _close() {
  _el.hidden = true;
  _el.innerHTML = "";
}

async function _activate() {
  const input = _el.querySelector("#license-key-input");
  const err = _el.querySelector("#license-error");
  const btn = _el.querySelector("#license-activate");
  const key = (input.value || "").trim();
  if (!key) {
    err.textContent = "Please enter a license key.";
    err.hidden = false;
    return;
  }
  err.hidden = true;
  btn.textContent = "Activating…";
  btn.disabled = true;
  try {
    // Use api.licenseActivate instead of raw fetch — inherits the
    // default 60 s timeout (matters: backend's own
    // license-server call has an 8 s ceiling so the full /api/license/
    // activate call should always return in <10 s; if it doesn't, the
    // SIDECAR is wedged, not the license server). Pre-migration the
    // raw fetch hung the spinner indefinitely on a wedged sidecar —
    // the Activate button stayed disabled, the user had to restart
    // the app. Now: 60 s timeout fires, friendly toast surfaces via
    // the existing catch path, button re-enables for retry.
    const r = await api.licenseActivate(key);
    // ok=true means "the activation roundtrip succeeded" (server
    // responded, no network failure). is_valid=true means "the key
    // is recognized by the license server." Pre-fix, the success
    // branch fired on ok=true regardless of is_valid — so entering
    // an INVALID key took the success branch, silently dropped the
    // server's "key not found" message, and the user saw only the
    // refreshed status (status=invalid) with no clear explanation
    // of WHY the key failed. Now check both flags before celebrating;
    // a roundtripped-but-invalid key shows the server's actual reason
    // in the existing error-message slot, same as a 4xx/5xx would.
    if (r && r.ok && r.is_valid) {
      // Refresh status, then re-render so the user sees the "Licensed" state
      try { state.license = await api.licenseStatus(); } catch {}
      _open();  // re-render modal with new state
      // Success toast. Without it, a buyer who clicks Activate and
      // then immediately switches focus (Cmd-Tab to copy a follow-up
      // license-key email confirmation, or just looks away while the
      // request flies) sees zero confirmation that their click did
      // anything — the modal-re-render is local feedback that only
      // lands if they're STILL looking at the modal. A toast is non-
      // local and survives a focus switch.
      flashToast("Tern activated — thanks!", { kind: "ok", ttl: 3500 });
    } else {
      err.textContent = (r && r.message) || "Activation failed. Check the key and try again.";
      err.hidden = false;
    }
  } catch (e) {
    err.textContent = "License server unreachable. Try again in a minute.";
    err.hidden = false;
  } finally {
    btn.textContent = "Activate";
    btn.disabled = false;
  }
}

async function _clear() {
  // Pre-fix this body was wrapped in a bare `try { … } catch {}` that
  // silently swallowed every failure path. Two distinct failure shapes
  // existed and BOTH produced zero user-visible feedback:
  //
  //   (a) network/timeout: api.licenseClear throws — the bare catch
  //       ate it, modal stayed on the active-license state, user has
  //       no clue whether the click registered.
  //   (b) backend returns 200 + `{ok: false, message: "Failed to
  //       clear license: PermissionError"}` — this is the documented
  //       shape from /api/license/clear when the unlink raises
  //       (see api/main.py license_clear + test_license_clear_
  //       reports_failure_when_unlink_raises in test_security_and_
  //       polish.py). The HTTP 200 means _json doesn't throw, so the
  //       OLD code happily ran the `_open()` re-render — but
  //       _open() refetches /api/license/status which still reports
  //       active, so the modal kept the active-license badge with NO
  //       error explaining why. The user was led to believe sign-out
  //       failed for unknown reasons, would spam-click, get the same
  //       silent non-result every time.
  //
  // Mirror the _activate() lifecycle: pending label "Removing…",
  // disabled button so spam-clicks are ignored, distinguish (a)
  // network failure (catch branch) from (b) backend explicit-failure
  // (`r.ok === false` branch), surface both in the same #license-
  // error slot _activate uses. Only restore the button on the failure
  // path; the success path calls _open() which rebuilds the modal
  // innerHTML so the original button is gone (and the new modal
  // won't have a clear button at all — the state-block flipped to
  // unlicensed).
  const clearBtn = _el.querySelector("#license-clear");
  const err = _el.querySelector("#license-error");
  if (err) err.hidden = true;
  if (clearBtn) {
    clearBtn.textContent = "Removing…";
    clearBtn.disabled = true;
  }
  try {
    const r = await api.licenseClear();
    if (!r || r.ok !== true) {
      // Backend-reported failure (PermissionError, OSError on unlink).
      // Surface the server's message verbatim — it names the actual
      // exception class so support diagnostics dumps and the user-
      // visible toast carry the same vocabulary.
      if (err) {
        err.textContent = (r && r.message) ||
          "Couldn't remove license. The license file may be locked or read-only.";
        err.hidden = false;
      }
    } else {
      // licenseStatus failure here is non-fatal — the clear succeeded
      // server-side, so the user IS signed out. Re-render to trial
      // state even if the follow-up status fetch transiently fails;
      // _open()'s own internal try/catch on licenseStatus will retry
      // and fall back to a "Loading…" render if even that fails. The
      // important invariant: the modal MUST re-render so the user
      // sees the state change they just triggered.
      try { state.license = await api.licenseStatus(); } catch {}
      _open();
      // Symmetric with the activate success toast — non-local
      // confirmation that the click landed, in case the user
      // switched focus mid-request.
      flashToast("License removed", { kind: "ok", ttl: 3000 });
      return; // success — _open rebuilt the modal, no button restore needed
    }
  } catch (e) {
    if (err) {
      err.textContent = "Couldn't remove license. The license server may be unreachable — try again in a minute.";
      err.hidden = false;
    }
  }
  if (clearBtn) {
    clearBtn.textContent = "Remove license";
    clearBtn.disabled = false;
  }
}

export function initLicense() {
  _el = document.getElementById("license-modal");
  if (!_el) return;
  document.addEventListener("tern:open-license-modal", _open);
}
