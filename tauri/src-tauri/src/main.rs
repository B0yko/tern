// Tern — Tauri desktop wrapper
// Spawns the FastAPI backend as a sidecar, opens window pointing at it.

#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::os::unix::process::CommandExt;
use std::process::{Command, Stdio, Child};
use std::sync::Mutex;
use std::time::Duration;
use tauri::Manager;

struct ApiProcess(Mutex<Option<Child>>);

/// Find a free TCP port starting from `preferred`, falling back to the next
/// 49 ports if it's taken. Returns the preferred port if we couldn't bind
/// anywhere (best-effort — the subsequent uvicorn spawn will error out
/// clearly if no port is actually free).
///
/// We bind, close, then return the port (TIME_WAIT can theoretically race,
/// but on macOS loopback the kernel reuses it within a few hundred ms and
/// uvicorn binds within ~1.5 s after this, so it's a non-issue in practice).
fn find_free_port(preferred: u16) -> u16 {
    use std::net::{SocketAddr, TcpListener};
    for offset in 0..50u16 {
        let port = preferred.saturating_add(offset);
        let addr: SocketAddr = ([127, 0, 0, 1], port).into();
        if TcpListener::bind(addr).is_ok() {
            return port;
        }
    }
    preferred
}

/// Resolve `uv` binary path. When the app is launched from Finder the inherited
/// PATH is just `/usr/bin:/bin:/usr/sbin:/sbin`, so a plain `Command::new("uv")`
/// fails with ENOENT. We probe the common Homebrew install locations and fall
/// back to PATH lookup if none of them exist.
fn resolve_uv() -> String {
    for candidate in &["/opt/homebrew/bin/uv", "/usr/local/bin/uv", "/opt/local/bin/uv"] {
        if std::path::Path::new(candidate).exists() {
            return (*candidate).to_string();
        }
    }
    "uv".to_string()
}

/// Build a PATH value that includes common Homebrew + system locations so that
/// any subprocesses `uv` itself spawns (python, ffmpeg, vision-ocr, etc.) are
/// also resolvable when launched from Finder.
fn finder_safe_path() -> String {
    let extras = "/opt/homebrew/bin:/opt/homebrew/sbin:/usr/local/bin:/opt/local/bin";
    match std::env::var("PATH") {
        Ok(p) if !p.is_empty() => format!("{extras}:{p}"),
        _ => format!("{extras}:/usr/bin:/bin:/usr/sbin:/sbin"),
    }
}

/// PID file used by the orphan-killer at startup. Lives next to the
/// workspace under ~/Library/Application Support/Tern/. Written after
/// every successful sidecar spawn; read + acted on at the start of
/// every launch; deleted on clean shutdown.
fn sidecar_pid_file() -> Option<std::path::PathBuf> {
    std::env::var_os("HOME").map(|h| {
        std::path::PathBuf::from(h)
            .join("Library/Application Support/Tern/sidecar.pid")
    })
}

/// On startup, kill any sidecar process group left over from a previous
/// run that didnt clean up. Real-world failure mode (reported by a user
/// with 15 GB RAM tied up by THREE 5 GB orphan python processes that
/// hung for days): the macOS Tauri CloseRequested handler is the only
/// path that SIGTERMs the sidecar process group — but it doesnt fire
/// when:
///   - The OS kills Tern via OOM / SIGKILL (no userspace cleanup runs)
///   - Tauri itself crashes mid-shutdown (the on_window_event closure
///     never executes)
///   - The user force-quits via Activity Monitor or `kill -9 Tern`
/// In those cases the python sidecar + its multiprocessing
/// resource_tracker + any in-flight whisper-cli / ffmpeg / vision-ocr
/// subprocesses are reparented to launchd PID 1 and live forever,
/// each holding ~5 GB resident (the loaded Whisper Q5 + SigLIP-2
/// weights).
///
/// Fix is a PID file + startup orphan killer:
///   1. After every successful spawn (below), write the child PID to
///      ~/Library/Application Support/Tern/sidecar.pid.
///   2. On the NEXT launch (here), read the PID file. If it points to
///      a live process, SIGTERM the whole process group (matches the
///      CloseRequested handlers convention; `cmd.process_group(0)` at
///      spawn means pgid == pid). Wait briefly, SIGKILL if still alive,
///      then delete the file.
/// Belt + suspenders: the CloseRequested handler also deletes the PID
/// file after killing, so a clean shutdown leaves no stale file to
/// trip the orphan-killer on next launch.
fn kill_orphan_sidecar() {
    let Some(path) = sidecar_pid_file() else { return };
    let Ok(raw) = std::fs::read_to_string(&path) else { return };
    let Ok(pid) = raw.trim().parse::<i32>() else {
        let _ = std::fs::remove_file(&path);
        return;
    };
    // `kill(pid, 0)` returns 0 if the process exists AND we have
    // permission to signal it. Errno ESRCH (3) means "no such
    // process" — the cleanup was already done elsewhere. Anything
    // else (EPERM, etc.) means the PID was reused by an unrelated
    // process; safer to NOT kill in that case.
    let alive = unsafe { libc::kill(pid, 0) } == 0;
    if !alive {
        let _ = std::fs::remove_file(&path);
        return;
    }
    // Verify the PID actually points at a Tern sidecar before killing —
    // PIDs get recycled by the OS, and unconditionally SIGTERMing a
    // recycled PID would kill an unrelated user process. `ps -p <pid>
    // -o command=` returns the command line; we look for uvicorn +
    // main:app, the load-bearing tokens of the sidecar invocation.
    let looks_like_tern = std::process::Command::new("ps")
        .args(["-p", &pid.to_string(), "-o", "command="])
        .output()
        .ok()
        .and_then(|o| String::from_utf8(o.stdout).ok())
        .map(|s| s.contains("uvicorn") && s.contains("main:app"))
        .unwrap_or(false);
    if !looks_like_tern {
        dlog(&format!("orphan-killer: PID {pid} no longer points at a Tern sidecar; skipping"));
        let _ = std::fs::remove_file(&path);
        return;
    }
    dlog(&format!("orphan-killer: SIGTERM pgid {pid} (leftover from previous run)"));
    unsafe { libc::kill(-pid, libc::SIGTERM); }
    // Wait up to 2s for graceful exit (python + multiprocessing
    // resource_tracker need a moment to release torch / chroma /
    // sqlite handles cleanly).
    for _ in 0..20 {
        std::thread::sleep(std::time::Duration::from_millis(100));
        if unsafe { libc::kill(pid, 0) } != 0 { break; }
    }
    // If still alive, SIGKILL the group. Belt-and-suspenders for
    // python processes that trap SIGTERM and refuse to exit
    // (multiprocessing.spawn() does this on some shutdown paths).
    if unsafe { libc::kill(pid, 0) } == 0 {
        dlog(&format!("orphan-killer: SIGTERM didnt take; SIGKILL pgid {pid}"));
        unsafe { libc::kill(-pid, libc::SIGKILL); }
    }
    let _ = std::fs::remove_file(&path);
}

/// Write the just-spawned sidecar PID to the orphan-killer file.
/// Best-effort — a write failure (read-only home, disk full) is logged
/// but doesnt block startup; the worst case is the next launch cant
/// auto-clean the orphan if this one crashes.
fn write_sidecar_pid(pid: u32) {
    if let Some(path) = sidecar_pid_file() {
        if let Some(parent) = path.parent() {
            let _ = std::fs::create_dir_all(parent);
        }
        match std::fs::write(&path, pid.to_string()) {
            Ok(_)  => dlog(&format!("sidecar PID {pid} written to {}", path.display())),
            Err(e) => dlog(&format!("WARN: could not write sidecar PID to {}: {e}", path.display())),
        }
    }
}

/// When launched via Finder, stdout/stderr are swallowed by launchservicesd.
/// Append a line to ~/Library/Logs/tern-debug.log so we can diagnose startup
/// problems in the wild without needing a terminal.
fn dlog(msg: &str) {
    use std::io::Write;
    if let Some(home) = std::env::var_os("HOME") {
        let path = std::path::PathBuf::from(home).join("Library/Logs/tern-debug.log");
        if let Some(parent) = path.parent() {
            let _ = std::fs::create_dir_all(parent);
        }
        if let Ok(mut f) = std::fs::OpenOptions::new().create(true).append(true).open(&path) {
            let now = std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .map(|d| d.as_secs())
                .unwrap_or(0);
            let _ = writeln!(f, "[{}] {}", now, msg);
        }
    }
    eprintln!("{msg}");
}

fn main() {
    dlog("--- Tern launch ---");
    // Kill any sidecar process group left over from a previous unclean
    // shutdown BEFORE we try to spawn our own (the new one would fight
    // for the loopback port + 5 GB of resident weights with the
    // orphan). See kill_orphan_sidecar() docstring for the real-world
    // 3 × 5 GB RAM-leak report this fixes.
    kill_orphan_sidecar();
    tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
        // Native folder picker so the user doesn't have to
        // Cmd-Click → "Copy as Pathname" → paste their library path.
        .plugin(tauri_plugin_dialog::init())
        // Auto-updater scaffold. The plugin is wired but the endpoint
        // URL + pubkey in tauri.conf.json must be set before this actually
        // does anything. scripts/release.sh refuses to build a signed
        // release while the pubkey is still the placeholder.
        .plugin(tauri_plugin_updater::Builder::new().build())
        .manage(ApiProcess(Mutex::new(None)))
        .setup(|app| {
            let resource_dir = app.path().resource_dir().unwrap_or_else(|_| std::env::current_dir().unwrap());
            dlog(&format!("resource_dir = {}", resource_dir.display()));
            dlog(&format!("cwd = {}", std::env::current_dir().map(|p| p.display().to_string()).unwrap_or_else(|_| "?".into())));
            let mut candidates = vec![
                // 1. Bundled: <Tern.app>/Contents/Resources/resources/api/
                //    (populated by scripts/prepare_bundle.sh, copied by Tauri)
                resource_dir.join("resources").join("api"),
                // 2-3. Legacy resource_dir-relative paths (kept for back-compat)
                resource_dir.join("../api"),
                resource_dir.join("api"),
                // 4-5. cwd-relative (launched from the repo root, or from a
                //      directory next to api/)
                std::env::current_dir().unwrap_or_default().join("../api"),
                std::env::current_dir().unwrap_or_default().join("api"),
            ];
            // 6. Dev fallbacks for running from a source checkout. They come
            //    after the bundled candidate, so an installed .app (which has
            //    resources/api/) never falls through to them.
            //    a) TERN_DEV_API_DIR names the api/ directory explicitly, for
            //       a binary launched from anywhere else.
            //    b) The repo's api/ relative to a cargo-built binary at
            //       <repo>/tauri/src-tauri/target/<profile>/tern, which is
            //       what `cargo tauri dev` builds and runs (unless
            //       CARGO_TARGET_DIR moves it; then use TERN_DEV_API_DIR).
            if let Some(dir) = std::env::var_os("TERN_DEV_API_DIR") {
                candidates.push(std::path::PathBuf::from(dir));
            }
            if let Some(exe_dir) = std::env::current_exe().ok().and_then(|e| e.parent().map(|p| p.to_path_buf())) {
                candidates.push(exe_dir.join("../../../../api"));
            }
            for c in &candidates {
                dlog(&format!("  candidate: {} -> main.py exists: {}", c.display(), c.join("main.py").exists()));
            }
            let api_dir = match candidates.into_iter().find(|p| p.join("main.py").exists()) {
                Some(d) => d,
                None => {
                    dlog("ERROR: could not locate api/ directory; backend not started.");
                    return Ok(());
                }
            };

            // Prefer bundled Python interpreter + site-packages over
            // host-installed uv. Falls back to uv if bundled Python is missing
            // (dev mode where resources/python/ doesn't exist yet).
            let bundled_python = {
                let nested = resource_dir.join("resources").join("python").join("bin").join("python3.11");
                if nested.exists() { Some(nested) } else { None }
            };
            let uv = resolve_uv();
            let path = finder_safe_path();
            dlog(&format!("api_dir = {}", api_dir.display()));
            dlog(&format!("bundled_python = {:?}", bundled_python));
            dlog(&format!("uv = {} (exists: {})", uv, std::path::Path::new(&uv).exists()));

            // Bundled binaries. Tauri copies our `resources/` dir
            // verbatim into <App>/Contents/Resources/resources/, so the
            // bundled binaries live at Contents/Resources/resources/bin/.
            // (Outside the bundle — dev mode — we fall back to checking the
            // src-tauri/resources/ source directly. cargo tauri dev sets
            // resource_dir to the cwd, so this works for both modes.)
            let bin_dir = {
                let nested = resource_dir.join("resources").join("bin");
                if nested.exists() { nested } else { resource_dir.join("bin") }
            };
            let libs_dir = {
                let nested = resource_dir.join("resources").join("libs");
                if nested.exists() { nested } else { resource_dir.join("libs") }
            };
            let ggml_backend_dir = {
                let nested = resource_dir.join("resources").join("ggml-backend");
                if nested.exists() { nested } else { resource_dir.join("ggml-backend") }
            };
            // Bundled Whisper Q5 model. Saves 547 MB first-run
            // download.
            let models_dir = {
                let nested = resource_dir.join("resources").join("models");
                if nested.exists() { nested } else { resource_dir.join("models") }
            };
            dlog(&format!("models_dir = {} (exists: {})", models_dir.display(), models_dir.exists()));
            // Writable workspace at ~/Library/Application Support/Tern/workspace/.
            // The bundled demo at resources/demo/ is read-only (inside .app);
            // first launch copies it here so indexing can write back. Subsequent
            // launches reuse the existing writable copy.
            let user_workspace = std::env::var_os("HOME")
                .map(|h| std::path::PathBuf::from(h).join("Library/Application Support/Tern/workspace"))
                .unwrap_or_else(|| std::path::PathBuf::from("/tmp/tern-workspace"));
            let _ = std::fs::create_dir_all(&user_workspace);
            // First-launch demo seed.
            // Seed only when BOTH the .tern-seeded marker is absent AND the
            // workspace is empty. The previous gate was marker-only — if a
            // user deleted the marker while their workspace held real indexed
            // data, the cp + rename flow would have either silently failed
            // (POSIX rename refuses a non-empty target) or, in a fully empty
            // parent, clobbered the existing workspace. Surface every error
            // into dlog so the user's ~/Library/Logs/tern-debug.log captures
            // what actually happened.
            let bundled_demo = {
                let nested = resource_dir.join("resources").join("demo");
                if nested.exists() { Some(nested) } else { None }
            };
            if let Some(ref demo) = bundled_demo {
                let marker = user_workspace.join(".tern-seeded");
                let ws_empty = std::fs::read_dir(&user_workspace)
                    .map(|mut it| it.next().is_none())
                    .unwrap_or(false);

                if !marker.exists() && ws_empty {
                    dlog(&format!("seeding demo workspace from {} → {}",
                                  demo.display(), user_workspace.display()));
                    let parent = user_workspace.parent().unwrap_or(&user_workspace);
                    let cp_status = std::process::Command::new("cp")
                        .args(["-R"])
                        .arg(demo)
                        .arg(parent)
                        .status();
                    let mut ok = matches!(cp_status, Ok(s) if s.success());
                    if !ok {
                        dlog(&format!("seed cp failed: {:?}", cp_status));
                    }
                    let copied = parent.join("demo");
                    if ok && copied.exists() && copied != user_workspace {
                        if let Err(e) = std::fs::rename(&copied, &user_workspace) {
                            dlog(&format!("seed rename failed: {} (orphan at {})",
                                          e, copied.display()));
                            ok = false;
                        }
                    }
                    if ok {
                        if let Err(e) = std::fs::write(&marker, "1") {
                            dlog(&format!("seed marker write failed: {}", e));
                        }
                    } else {
                        dlog("seed incomplete — marker NOT written, will retry next launch");
                    }
                } else if !marker.exists() && !ws_empty {
                    // Populated workspace without a marker — the user may
                    // have restored a backup or manually seeded. Write the
                    // marker so subsequent launches don't attempt a seed
                    // that would clobber their data.
                    let _ = std::fs::write(&marker, "1");
                    dlog("found populated workspace without marker — wrote marker without seeding");
                }
            }
            dlog(&format!("user_workspace = {}", user_workspace.display()));
            dlog(&format!("bin_dir = {} (exists: {})", bin_dir.display(), bin_dir.exists()));
            // Prepend bin_dir to PATH so any subprocess spawned by uv/Python
            // also picks up the bundled tools (subprocess.run("ffmpeg") works).
            let path = format!("{}:{}", bin_dir.display(), path);
            dlog(&format!("PATH = {}", path));

            // Pick a free port. Preferred = 18765, matching run.sh
            // (commit 9b6ae4a), scripts/dev_check.sh, and
            // qa_smoke.py's DEFAULT_BASE — one canonical default
            // across every entry point. The original 8765 collided
            // with other local dev tooling that also defaults to it;
            // 18765 is high-range + memorable + almost
            // never contested. Falls back to next 49 ports if
            // anything else owns it. The chosen port is passed to
            // uvicorn AND used to navigate the webview AND exposed
            // to the frontend via the TERN_PORT env var →
            // window.__TERN_PORT__ injection.
            let port = find_free_port(18765);
            dlog(&format!("port = {} (preferred 18765)", port));
            let port_str = port.to_string();

            // Spawn bundled python -m uvicorn directly when
            // available; otherwise fall back to `uv run uvicorn`.
            let mut cmd = if let Some(ref py) = bundled_python {
                let mut c = Command::new(py);
                c.args(["-m", "uvicorn", "main:app",
                        "--host", "127.0.0.1", "--port", &port_str]);
                c
            } else {
                let mut c = Command::new(&uv);
                c.args(["run", "uvicorn", "main:app",
                        "--host", "127.0.0.1", "--port", &port_str]);
                c
            };
            cmd.current_dir(&api_dir)
                .env("PATH", &path)
                .env("TERN_BIN_DIR", &bin_dir)
                .env("TERN_MODEL_DIR", &models_dir)
                .env("TERN_WORKSPACE", &user_workspace)
                .env("GGML_BACKEND_DL_PATH", &ggml_backend_dir)
                // DYLD fallback so any ad-hoc dylib lookups can resolve too
                .env("DYLD_FALLBACK_LIBRARY_PATH", &libs_dir)
                .stdout(Stdio::null())
                .stderr(Stdio::null());
            // Put the python sidecar in its own process group so we can
            // SIGTERM the WHOLE group on Tauri close. Without this, the
            // CloseRequested handler's child.kill() only takes down the
            // direct python child — multiprocessing's resource_tracker
            // helper (separate child of python, *not* of tauri) is
            // orphaned, plus any in-flight ffmpeg / whisper / vision-ocr
            // subprocesses python spawned. Confirmed orphans observed in
            // `ps aux` after Tern UI close: python PID + a separate
            // resource_tracker python PID. process_group(0) makes the
            // child its own group leader (pgid == pid).
            cmd.process_group(0);
            // Make bundled python find tern-service in the vendored
            // service_pipeline (PYTHONPATH supplements site-packages but
            // doesn't override it).
            cmd.env("PYTHONPATH", api_dir.parent().unwrap().join("service_pipeline"));

            match cmd.spawn() {
                Ok(child) => {
                    let child_pid = child.id();
                    dlog(&format!("backend spawned, child pid = {child_pid}"));
                    // Write the PID file so the next launchs orphan
                    // killer can clean up if THIS process gets killed
                    // before the CloseRequested handler runs (OOM,
                    // crash, `kill -9 Tern`, force-quit via Activity
                    // Monitor — all paths that skip userspace cleanup).
                    write_sidecar_pid(child_pid);
                    *app.state::<ApiProcess>().0.lock().unwrap() = Some(child);
                    // Poll /api/stats up to 8 s instead of sleeping a fixed 3 s.
                    // First boot of the FastAPI sidecar usually takes ~1.5-2 s; on cold
                    // disk it can stretch to 5 s while Python imports + Whisper init.
                    // A fixed sleep is the worst of both worlds: too long on fast boots,
                    // too short on slow ones. Polling cuts the common case in half.
                    let started = std::time::Instant::now();
                    let max_wait = Duration::from_millis(8000);
                    let interval = Duration::from_millis(100);
                    let addr_str = format!("127.0.0.1:{}", port);
                    loop {
                        if std::net::TcpStream::connect_timeout(
                            &addr_str.parse().unwrap(),
                            Duration::from_millis(120),
                        ).is_ok() {
                            dlog(&format!("backend ready after {} ms", started.elapsed().as_millis()));
                            break;
                        }
                        if started.elapsed() >= max_wait {
                            dlog(&format!("backend not responsive after {} ms; opening window anyway", started.elapsed().as_millis()));
                            break;
                        }
                        std::thread::sleep(interval);
                    }

                    // Navigate the auto-created main window to the chosen
                    // port. The declarative URL in tauri.conf.json points
                    // at the preferred port (18765); when find_free_port
                    // returned the fallback, we override here. (If port
                    // == 18765, the declarative URL already matches and
                    // no navigate is needed.)
                    if port != 18765 {
                        if let Some(window) = app.get_webview_window("main") {
                            let url_str = format!("http://127.0.0.1:{}/", port);
                            match url_str.parse() {
                                Ok(url) => {
                                    if let Err(e) = window.navigate(url) {
                                        dlog(&format!("ERROR: navigate to {} failed: {}", url_str, e));
                                    } else {
                                        dlog(&format!("window navigated to {}", url_str));
                                    }
                                }
                                Err(e) => dlog(&format!("ERROR: failed to parse url {}: {}", url_str, e)),
                            }
                        } else {
                            dlog("WARN: main window not found for navigate — UI may show wrong port");
                        }
                    }
                }
                Err(e) => {
                    dlog(&format!("ERROR: failed to spawn backend ({uv}): {e}"));
                }
            }

            Ok(())
        })
        .on_window_event(|window, event| {
            if let tauri::WindowEvent::CloseRequested { .. } = event {
                kill_sidecar(window.app_handle());
            }
        })
        .build(tauri::generate_context!())
        .expect("error while building Tern")
        .run(|app_handle, event| {
            // App-level exit hook. Verified live: Cmd-Q /
            // AppleScript `quit app "Tern"` / Dock right-click → Quit do
            // NOT fire WindowEvent::CloseRequested — the app tears down
            // through the run loop's exit path instead, and pre-this-
            // commit the sidecar survived every normal quit. That — not
            // force-quit — is the most common way users close Mac apps,
            // so this was the PRIMARY 5 GB-per-quit leak path behind the
            // 3-orphans-for-days report. RunEvent::Exit is the last
            // userspace callback on ALL graceful exit paths (Cmd-Q,
            // window close, menu quit); kill_sidecar's Mutex take() is
            // idempotent so double-fire with CloseRequested is harmless.
            if let tauri::RunEvent::Exit = event {
                kill_sidecar(app_handle);
            }
        });
}

/// SIGTERM the sidecar process group + remove the PID file. Idempotent:
/// the Mutex<Option<Child>> take() means whichever of the two shutdown
/// hooks (window CloseRequested, app RunEvent::Exit) fires first does
/// the work and the second no-ops.
fn kill_sidecar(app_handle: &tauri::AppHandle) {
    let state = app_handle.state::<ApiProcess>();
    let mut guard = match state.0.lock() {
        Ok(g) => g,
        Err(poisoned) => poisoned.into_inner(),
    };
    if let Some(mut child) = guard.take() {
        // SIGTERM the WHOLE process group (pgid == child.pid() because
        // we set process_group(0) at spawn). Takes down the python
        // sidecar AND its multiprocessing resource_tracker AND any
        // in-flight ffmpeg/whisper/vision-ocr subprocesses in one
        // syscall. Negative PID = process-group target, per kill(2).
        let pid = child.id() as i32;
        unsafe { libc::kill(-pid, libc::SIGTERM); }
        // Belt + suspenders: direct-kill the immediate child in case
        // process_group(0) silently failed. No-op if already dead.
        let _ = child.kill();
        dlog(&format!("sidecar terminated (pgid={})", pid));
        // Clean shutdown — remove the PID file so the next launch's
        // orphan-killer doesn't waste a SIGTERM on a dead process.
        if let Some(pf) = sidecar_pid_file() {
            let _ = std::fs::remove_file(pf);
        }
    }
}
