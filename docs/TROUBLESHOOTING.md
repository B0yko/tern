# Tern · Troubleshooting

Written for builds made from this source. No build has been distributed,
and the [licence](../LICENSE) does not grant permission to build or run Tern;
see the README's Status section.

Common issues and fixes. If yours isn't here, open an issue at
<https://github.com/B0yko/tern/issues>.

**Before you open an issue:** open Tern, gear icon → **Preferences** → **Advanced** → **Copy diagnostics for support…**, then paste the result into the issue. The dump is redacted JSON (your username shows as `~`) with Tern's version, workspace stats, the last 100 lines of `tern-crash.log`, and the current indexing state. With `./run.sh`, `curl -s http://127.0.0.1:18765/api/diagnostics` returns the same JSON.

---

## "Tern.app can't be opened — it is damaged"

macOS Gatekeeper doesn't recognise the signature.

Every build of Tern is ad-hoc signed. It was never signed with a Developer ID or notarized, because no Apple Developer identity was ever obtained, so Gatekeeper blocks the app on any Mac other than the one that built it. To open it anyway, clear the quarantine flag:

```bash
xattr -dr com.apple.quarantine /Applications/Tern.app
```

Or right-click → Open → Open on first launch. On macOS 15 and later that route is gone: launch once, then use System Settings → Privacy & Security → Open Anyway.

---

## "Tern backend isn't responding" (red banner)

The window is up, but the FastAPI sidecar isn't answering on its port. The app takes the first free port from **18765** upwards (it tries 18765–18814) and writes its choice to `~/Library/Logs/tern-debug.log`:

```bash
grep 'port =' ~/Library/Logs/tern-debug.log | tail -n 1
# → port = 18765 (preferred 18765)
```

Substitute that port number anywhere `18765` appears below. (`./run.sh` uses its own shorter list: 18765–18769, then 8765–8767.)

The launcher waits up to 8 seconds for the sidecar before it opens the window anyway, and logs either `backend ready after N ms` or `backend not responsive after N ms; opening window anyway`.

Causes:

1. **The sidecar is still importing torch / transformers / SigLIP.** On a cold disk this can take longer than the 8-second wait. `curl -s http://127.0.0.1:18765/api/health` answers as soon as it is up; click **Retry** on the banner then.
2. **The sidecar never started.** `tern-debug.log` shows `ERROR: failed to spawn backend` or `could not locate api/ directory`. The bundled app discards the sidecar's stdout and stderr, so any Python-side error is in `~/Library/Logs/tern-crash.log` instead.
3. **A previous Tern was force-killed mid-startup** and left orphan `*.pyc.NNNNN` files in the bundled Python tree. Fix:
   ```bash
   find /Applications/Tern.app/Contents/Resources/resources/python/lib -name '*.pyc.[0-9]*' -delete
   ```
   Then relaunch.

A sidecar left running by a crashed session is not a cause: the launcher records the sidecar's PID and kills a leftover one (after checking it really is a Tern sidecar) before it starts a new one.

---

## The drag-drop overlay won't go away

If a transparent panel is blocking all clicks, **press Esc** or click anywhere on the overlay. Both dismiss it.

---

## Search returns 0 results even for known queries

Check that indexing actually finished:

```bash
curl -s http://127.0.0.1:18765/api/stats | python3 -m json.tool
```

Look at `files_done` vs `files_total`. If they don't match, indexing is still running (or got stuck on a file). The bottom-right indexing toast should reflect this; if the toast is hidden, click the gear → Preferences → Advanced → "Copy diagnostics for support…" and look at the `indexing.log` field in the JSON for the last 30 log lines.

If `files_total: 0`, nothing has been indexed yet. Either:
- The demo workspace was never seeded. The app copies its bundled demo into `~/Library/Application Support/Tern/workspace/` on first launch, but only into an empty workspace with no `.tern-seeded` marker, and only if the bundle has a demo in it. To re-run the seed, quit Tern, move the `workspace` folder aside, and relaunch. The demo media is not part of this repository: a bundle carries a demo only if `demo/` was populated (`demo/*/download.sh`, then `scripts/init_demo.sh`) when `scripts/prepare_bundle.sh` ran.
- Or you opened the Add Folder modal but didn't click Start.

If `files_done > 0` but you still get 0 hits, your query might not match what's actually indexed. On the demo media, try `range rover`, `hurricane`, `Stanford` or `butterfly`. The visual channel also drops weak matches on purpose (see the noise floor in [ARCHITECTURE.md](../ARCHITECTURE.md)), so a vague description can legitimately come back empty.

---

## "Folder path is too broad" when removing a folder

By design. To prevent accidental wipes of your whole library, you must specify a folder at least **two levels deep** (e.g., `/Users/you/Documents/Podcasts`, not `/Users/you` or `/Users`). If you really want to wipe everything indexed under a top-level dir, do it one sub-folder at a time, or delete the workspace database manually:
```bash
rm -rf "$HOME/Library/Application Support/Tern/workspace/db"
```

(That removes the index but not your source files.)

---

## SigLIP first download

On first launch Tern downloads the SigLIP-2 base patch16-256 weights (about 1.5 GB) from `huggingface.co`. The sidecar starts the download from a background warmup thread at startup, not at the first search, but on a slow connection the first searches still wait for it. It is a one-time download; the model is cached in the Hugging Face cache (`~/.cache/huggingface/hub/` unless `HF_HOME` points elsewhere).

If you're behind a proxy that blocks `huggingface.co`, point `HF_ENDPOINT` at a mirror your network allows:
```bash
launchctl setenv HF_ENDPOINT https://<your-mirror>
```

then relaunch Tern.

---

## Whisper is slow

Whisper Large v3 Turbo Q5 runs at 8-15× realtime on Apple Silicon (M1+). For a 1-hour podcast: ~4-8 minutes.

If you're significantly slower:
- Confirm you're on Apple Silicon (`uname -m` → `arm64`). Intel Macs are not supported.
- Check `~/Library/Logs/tern-crash.log` for `"category": "index_file_failed"` entries mentioning `whisper-cli`.
- Every file starts a fresh `whisper-cli` process that loads the model first, so a folder of short files carries a larger fixed overhead per minute of audio than one long file.

---

## Search is hanging on the first query (warmup)

The sidecar warms SigLIP-2 up in a background thread at startup, so by the time you type your first query the embedder is usually ready. When the warmup was added, it took the first search from ~5.6 s to ~0.09 s.

If first-search latency is still slow (>1 s), check `~/Library/Logs/tern-crash.log` for `"category": "warmup_failed"`. The fallback is lazy-loading on the first request, which is the old slow behaviour.

---

## "Search failed: timed out after 60s — backend may be wedged"

Every frontend → sidecar API call has a 60-second timeout (clip and FCPXML exports get 360 s). The longest legitimate call shape is `/api/search` on cold start (SigLIP-2 model load + first text embed: ~5–10 s on Apple Silicon), so 60 s gives ~6× margin. Past that, the sidecar is genuinely wedged (ChromaDB lock contention after a hard crash, a stuck subprocess, a crash-recovery loop).

What to do:

1. **Try once more.** Some rare cold-start sequences (downloading a missing model, recovering after sleep / wake) genuinely take longer than 60 s. The second request usually succeeds because the warmup completed during the first.
2. **Restart Tern.** Quit (⌘Q) and relaunch. The sidecar comes back up clean, in-flight subprocesses get SIGTERM'd, the next search runs normally.
3. **Check `~/Library/Logs/tern-crash.log`** for what was running when the timeout hit. A Whisper run that takes longer than max(300 s, the audio's length) is killed and shows up as an `index_file_failed` entry whose message starts with `whisper-cli hung after`.

---

## "Export timed out after 120s / 300s — source may be corrupted or on a stalled mount"

This is a toast on **Crop & Save** (audio or video clip export). The sidecar's ffmpeg call exceeded its built-in ceiling (120 s for audio MP3 extract, 300 s for video re-encode) and got killed. ffmpeg normally finishes a 30-second video clip in ~1–5 s on Apple Silicon, so this means something went genuinely wrong:

What to do:

1. **Check the source file.** Open the original audio/video in QuickTime first. If QuickTime won't play it either, the file is corrupted (truncated download, killed transcode, bit-rot on an old disk). Re-acquire a clean copy and re-index.
2. **Check the mount.** If the source is on a network drive (SMB / NFS / iCloud Drive that hasn't finished downloading), the mount may be stalled. `ls -la` the source path from Terminal — if THAT hangs, the drive is the problem, not Tern.
3. **Try a smaller clip.** A pathological case for re-encode is asking for the full 30-minute cap on a high-bitrate 4K source over a slow disk. Trim to a 30-second clip first to confirm the source itself encodes cleanly, then expand.
4. **Check `~/Library/Logs/tern-crash.log`** for `"category": "export_clip_timeout"`. The entry records the source path and which ceiling tripped. A timeout means ffmpeg never returned; an ffmpeg that ran and failed comes back as a different error, "ffmpeg failed: …", with ffmpeg's own message.

If you hit this repeatedly on different sources, suspect the ffmpeg binary itself. The bundled app uses the LGPL build from `scripts/build_ffmpeg_lgpl.sh`, copied in by `scripts/prepare_bundle.sh`; rebuild the bundle to replace it. With `./run.sh`, Tern uses whatever `ffmpeg` is first on your `PATH`.

---

## "FCPXML export timed out after 30s probing a source — one of the asset files may be on a stalled mount or corrupted"

This is a toast on **Export FCPXML** (the bulk export pill at the top of the results list). FCPXML generation shells out to `ffprobe` once per unique source file in the result set to discover fps / width / height / duration for the timeline format. If even ONE source file is on a stalled mount or unreadable, ffprobe hangs on it and the 30 s ceiling trips.

What to do:

1. **Narrow the search first.** The bulk export tries every result; if your search returned 50 hits across 10 source videos, all 10 ffprobe calls run. Trim your search down (add a filter or a more specific term) so the export only touches a smaller set of source files, then re-export. Whichever video you DROP between attempts is likely the culprit.
2. **Run `ls -la` on each source folder** from Terminal. The ones that hang are stalled mounts (iCloud Drive that hasn't finished downloading, SMB to a sleeping NAS, network drives that disconnected). Re-mount or wait for the sync to finish.
3. **Check `~/Library/Logs/tern-crash.log`** for `"category": "fcpxml_export_timeout"`. The entry records the hit count so you can confirm which export attempt hit the timeout. Single-hit exports rarely fail here (the source's been read at least once during indexing); bulk exports are where the per-source probe latency adds up.

If you only need the matched text and timecodes, export CSV from the same results pill, or SRT from a hit's ⋯ overflow menu. Neither probes the source files.

---

## "Indexing skipped N files" — the toast shows ✗ markers next to specific files

The indexing toast marks each file:

- **✓** = transcribed + OCR'd + embedded successfully.
- **⊝** = already indexed (mtime hasn't changed).
- **⊘** = refused by the trial limit (see "Trial limit reached" below).
- **✗** = failed. The toast prints the file name and a one-line reason next to it.

One bad file doesn't stop the pass. But the toast only shows the last 30 entries and clears on app close, so for the full error look in `~/Library/Logs/tern-crash.log`: every failure writes a structured `"category": "index_file_failed"` line with the Python traceback, the failing file path, and the exception class.

What to do:

1. **Grep the log:** `grep '"category": "index_file_failed"' ~/Library/Logs/tern-crash.log`. Each line carries `file_path` and `exception_type`. `subprocess.TimeoutExpired` usually means a stalled mount. `FileNotFoundError` means the file vanished between discovery and indexing. A `whisper.cpp` `RuntimeError` usually means an unsupported container.
2. **Try the file in QuickTime.** If QuickTime can't open it either, it's corrupted at the source. Get a clean copy and re-index that folder.
3. **If it plays in QuickTime but Tern's indexer still fails**, the bundled `ffmpeg` is probably missing the codec. Exotic formats like Sorenson Spark or older proprietary DV are the usual culprits. Re-encode with `ffmpeg -i source.mov -c:v libx264 -c:a aac out.mp4` and re-index.

If indexing never started at all (toast shows "FATAL: ..." instead of per-file ✗ marks), the log carries `"category": "index_run_fatal"` instead. That is the outer-loop failure case: ChromaDB collection corruption, DB lock, or OOM. Include the traceback when you open an issue.

---

## "Trial limit reached"

Without a licence key, Tern indexes up to 120 minutes of audio and video in total; photos don't count. Indexing refuses to start once the quota is spent, and a file longer than what is left is skipped whole (the ⊘ mark) rather than half-indexed. Everything already indexed stays searchable.

The quota is kept in `~/Library/Application Support/Tern/trial.json`. A damaged or hand-edited file counts as fully spent.

---

## "No such file or directory: '_editable_impl_tern_service.pth'" (dev only)

When the checkout sits in a folder that iCloud Drive syncs (such as `~/Documents` with Desktop & Documents sync on), macOS can set the hidden flag on files inside `api/.venv`. Python skips hidden `.pth` files, so the editable install of `service_pipeline` stops loading. This doesn't affect the bundled `.app`, only `uv run` in dev. Move the venv out of iCloud:

```bash
cd api
uv venv ~/.local/venvs/tern-api --python 3.11
rm -rf .venv
ln -s ~/.local/venvs/tern-api .venv
uv sync
```

`scripts/dev_check.sh` and `scripts/prepare_bundle.sh` do the same through `UV_PROJECT_ENVIRONMENT=$HOME/.local/venvs/tern-api`.

---

## `./run.sh` exits with "uv not found" or "No free port"

- **`uv not found. Install it: brew install uv`** → install it:
  ```bash
  brew install uv
  ```
  or `curl -LsSf https://astral.sh/uv/install.sh | sh`. Then re-run `./run.sh`.
- **`No free port in 18765-18769 / 8765-8767`** → free one of those ports, or pin another with `TERN_PORT=<port> ./run.sh`. The port probe runs `python3`, so if `python3` is missing (the Command Line Tools aren't installed) every port looks taken and you get this message too:
  ```bash
  xcode-select --install
  ```

`run.sh` is for the dev/source path only. The bundled `.app` ships its own Python and has no `uv` or `python3` host dependency.

---

## "ModuleNotFoundError: No module named 'tern'" (dev only)

Same root cause as the `.pth` entry above. If you don't want to move the venv, `api/conftest.py` puts `service_pipeline/` on `sys.path` itself, which makes `pytest` work, and `api/main.py` does the same at import time, which makes `uv run uvicorn main:app` work.

---

## I deleted the wrong folder from my index

The index is rebuildable. Click **+** next to Folders in the sidebar (or ⇧⌘O) and re-add the path. Whisper + OCR + SigLIP run again. Source files on disk are untouched.

---

## "Path not in allowlist" when trying to play/download a file

Security fix. The `/api/file` endpoint refuses paths outside (a) the writable workspace, or (b) files that match a row in the index. If you're seeing this for a file you indexed:

- The file may have been moved or renamed since indexing. Re-add the folder.
- The workspace path may have changed (TERN_WORKSPACE env). Confirm `curl -s http://127.0.0.1:18765/api/health` reports the workspace you expect. (If 18765 was busy at launch, see the port-discovery snippet under "Tern backend isn't responding" above.)

---

## "Couldn't load Audio / Video: file.mp3 — source may have been moved or deleted"

The player's `<audio>` or `<video>` element couldn't load the source file. Most common cause: the file was moved, renamed, or deleted **outside** Tern between when it was indexed and when you clicked the result. Tern still has the file in its index, but the bytes aren't where the index says they are.

What to do:

1. **Restore the file**, or move it back to the original folder, then re-index.
2. **If the file was intentionally deleted**, remove the containing folder from Tern's index via sidebar → folder row → context menu → Remove folder from index. The stale result row will stop appearing.
3. **If you're sure the file is still there**: open Finder at the path shown in the toast, confirm the file exists AND isn't on a stalled network mount (`ls -la` from Terminal — if THAT hangs, the mount is the problem).

The toast also names two less-common variants:

- **"... decode failed: source may be corrupted or use an unsupported codec"** — bytes arrived but WebKit's decoder rejected them. Open in QuickTime first; if QT also fails, the file is corrupted. If QT plays it fine, the codec might be one WebKit doesn't support. Re-encode with ffmpeg (`ffmpeg -i source.mkv -c:v libx264 -c:a aac out.mp4`) and re-index.
- **"... network error — sidecar may have restarted"** — rare on loopback; usually means the Python sidecar crashed mid-stream. Check `~/Library/Logs/tern-crash.log` for the crash that triggered it.

---

## Licence activation fails

Tern was never sold, so no licence keys were ever issued to users. For completeness, these are the messages the licence server in `license_server/` returns; the Activate dialog shows them verbatim:

- **"Licence key not recognised. …"** — the key doesn't exist on the server. Usually a typo; paste the key rather than retyping it.
- **"This licence has been revoked. …"** / **"This licence has expired. …"** — the key exists but is no longer active.
- **"All N seats on this licence are in use. …"** — the key is already bound to as many Macs as it has seats. A Mac that already holds a seat can always re-activate, even when the licence is full.
- **"License server unreachable: <error class>"** — the activation request didn't complete (no network, no answer within 8 s, or an error status from the server). `~/Library/Logs/tern-crash.log` has a `license_activate_failed` entry naming the exception class.

A failed activation keeps whatever licence was cached before. Tern doesn't re-validate the cached licence on launch, so a licence-server outage can't lock out an install that is already licensed.

---

## Where do my files live?

- **Workspace** (index + thumbnails + exports): `~/Library/Application Support/Tern/workspace/`
- **Logs**: `~/Library/Logs/tern-debug.log` captures the Rust launcher's own diagnostic notes — sidecar boot, workspace seeding, port selection. `~/Library/Logs/tern-crash.log` captures every structured Python event Tern emits via `log_event` — API errors, indexing failures, license activation outcomes, demo-path-rewrite results — plus Python warnings and errors from the pipeline's own loggers. For most "what broke?" questions the second file is the one to grep first; the bundled Tauri launcher discards Python stdout/stderr, so the sidecar's own `print()` calls only appear when running from `./run.sh` in dev.
- **License**: `~/Library/Application Support/Tern/license.json`
- **Trial quota**: `~/Library/Application Support/Tern/trial.json`
- **Preferences** (Light/Dark/Accent/Density/Power Mode): localStorage key `tern.prefs.v1`
- **Recent searches**: localStorage key `tern.recents.v1`
- **Last query** (restored on launch): localStorage key `tern.lastQuery.v1`
- **Saved searches**: localStorage key `tern.saved.v1`
- **Bookmarks** (pinned moments): localStorage key `tern.bookmarks.v1`
- **Source filters** (Speech/On-screen/Visual toggle state): localStorage key `tern.sources.v1`
- **Sidebar open/closed**: localStorage key `tern.sidebarOpen.v1`
- **Playback speed**: localStorage key `tern.playbackRate.v1`
- **Update-check throttle**: localStorage key `tern.updater.lastCheck`
- **Onboarding-shown flag**: localStorage key `tern.onboarded.v1` (delete to replay the 3-screen tour — or click Preferences → Advanced → "Replay onboarding tour…")

To fully reset: quit Tern, delete the workspace dir and all of the above localStorage keys (Developer Tools → Application → Local Storage in the WebView). Next launch boots like a fresh install.

---

## Still stuck?

Open an issue at <https://github.com/B0yko/tern/issues> with:
- The last 50 lines of `~/Library/Logs/tern-debug.log`
- The last 20 lines of `~/Library/Logs/tern-crash.log`
- A one-sentence description of what you were trying to do
- macOS version (`sw_vers`) and Tern version (sidebar → bottom).

Check the logs for paths or file names you'd rather not post before you paste them.
