# `app/modules/` — frontend architecture

Vanilla ES-modules, no build step, no framework. `app/main.js` is the entry point: it imports `state`, `api`, `icons`, `theme` and `keyboard` statically and the rest on demand through dynamic `import()`. Modules also import each other.

## Module map (28 modules)

### Plumbing
| Module | Responsibility |
|---|---|
| `state.js` | Tiny pub-sub via `Proxy(_state)`. Single source of truth: `query`, `results`, `selectedIndex`, `selectedHit`, `sidebarOpen`, `isSearching`, `isIndexing`, `stats`, `recents`, `saved`, `bookmarks`, `prefs`, `sources`, `folderFilter`, `lastSearchMs`, `license`. |
| `api.js` | Thin REST client. One function per backend endpoint. Throws on non-2xx. |
| `icons.js` | SF-Symbol-style inline SVGs. All stroke 1.8, geometric. Returns string HTML for `innerHTML`. |
| `theme.js` | Light/dark + accent + density + Power Mode. Persists to `localStorage.tern.prefs.v1`; applies via `data-*` attrs on `<html>`. |
| `keyboard.js` | Single window-level `keydown` listener. Maps key combos → `INTENTS.*`; modules register handlers via `on(intent, fn)`. |
| `contextmenu.js` | Shared native-Mac-style right-click menu. One overlay element, positioned to stay inside the viewport. |
| `toast.js` | Transient bottom-right confirmation toasts (`flashToast`) for actions with no other visible feedback. |

### Search input + result list
| Module | Responsibility |
|---|---|
| `topbar.js` | Search input + 180 ms debounced search call. Result-count chip. Star button (saved). Filter button. Gear button. ⌘B/⌘,/⌘D wiring. |
| `recents.js` | Most-recently-searched queries. `localStorage.tern.recents.v1`. |
| `saved.js` | User-pinned queries. `localStorage.tern.saved.v1`. |
| `suggest.js` | Autocomplete dropdown for the search input (matching saved + recents). Linear/Notion-style. |
| `filters.js` | Source-scope popover (Speech / On-screen / Visual) + "Limit to folder" dropdown. Persists to `localStorage.tern.sources.v1`. |
| `results.js` | Result list render. Keyboard ↑↓ + auto-preview. Bulk FCPXML + CSV export pill. Auto-advance on play-end. |
| `row.js` | Single result-row component (smart-thumb, source pill, snippet with `<mark>`). |
| `preview.js` | Live hover-preview (500 ms → 6 s of audio at 30 % volume). |

### Detail pane
| Module | Responsibility |
|---|---|
| `detail.js` | Right-pane orchestration — header (badge + tc + filename + ⭐ bookmark + nav hint) → player slot → transcript window → action bar (⌘E export, ⇧⌘R reveal, Space quicklook, ⌘L copy, ⋯ overflow). |
| `player.js` | Inline audio (waveform + scrub + ±10s skip + 0.75-2× speed + trim handles) / video with the dual-zoom trim editor (overview + working strip, keyframe filmstrip, snapping, frame stepping) / image. Real waveform via WebAudio decode → canvas. |
| `transcript.js` | Transcript window render (matched line + surrounding context). Click line → seek player. |

### Sidebar (⌘B)
| Module | Responsibility |
|---|---|
| `sidebar.js` | Header + Bookmarks + Saved + Recents + Folders + License + Privacy footer. ⌘B toggle. Slide-over at < 1100 px. |
| `bookmarks.js` | Per-moment pins (file_id + ts_ms). `localStorage.tern.bookmarks.v1`. |

### Modals + overlays
| Module | Responsibility |
|---|---|
| `empty.js` | Day-1 hero (headline + "Index your folder" CTA, no example queries) → day-N library view with recent pills. Live workspace stats line. |
| `prefs.js` | Preferences popover (Appearance / Accent / Density / Power Mode toggle) plus Advanced actions: License, Replay onboarding, Clear recents, Copy diagnostics. |
| `keyhelp.js` | ⌘/ keyboard-shortcuts overlay + About section. |
| `onboarding.js` | Single-shot first-launch sheet (3 screens). |
| `indexing.js` | Drag-drop folder overlay + Add Folder modal + indexing-status toast with ETA. |
| `lightbox.js` | Fullscreen photo viewer with ← / → cycling. |
| `license.js` | License-key activation modal. |
| `updater.js` | Tauri auto-update check (24 h throttle). |

## Conventions

- **No framework.** Just `import` between modules + DOM APIs. Keep it that way.
- **State changes go through `state.X = Y`.** The `Proxy` notifies subscribers (`subscribe(fn)`). Direct DOM mutations bypass the model and break re-renders.
- **CSS files mirror module names.** `row.js` ↔ `row.css`. `detail.js` ↔ `detail.css`. Component styles live next to the component.
- **Tokens in `tokens.css`.** Every color / font / spacing / motion value is a CSS custom property defined in `tokens.css`. Power Mode + Density + Accent override these via `:root[data-*]` selectors.
- **Hot-patchable.** Every JS / CSS file is served as a static file from FastAPI's StaticFiles mount; rewriting one and refreshing the WebView picks up the change without rebuilding Tauri.
- **No emoji icons.** All glyphs are SF-Symbol-style SVGs via `icons.js`. The one exception is the date, camera and location markers on photo EXIF chips in `detail.js`.
- **One concern per module.** The old guideline was to split past ~250 lines, and several modules have outgrown it. `player.js` (about 1,800 lines, both trim editors) is the obvious candidate to split next.

## Adding a new module

1. Write `app/modules/<name>.js`. Export an `init<Name>()` function (called once at boot) and any helpers other modules need.
2. Write `app/<name>.css` if it has visual presence.
3. Add `<link rel="stylesheet" href="/<name>.css">` to `index.html` after the existing component CSS.
4. Add the dynamic import + `init<Name>()` call to `app/main.js`'s `boot()`.
5. If the module persists state, add a field to `state.js`'s `_state` initial object.
6. If the module reacts to global state changes, `subscribe((k) => { if (k === "X") this.render(); })`.
7. If it dispatches cross-module events, use `CustomEvent` on `document` (e.g., `tern:open-license-modal`) — no direct imports between sibling modules.

## Adding a new shortcut

1. Add to `keyboard.js`'s `INTENTS` object.
2. Add the key combo detection in `initKeyboard()`'s `keydown` handler.
3. Register a handler from the responsible module: `on(INTENTS.X, fn)`.
4. Add a row to `keyhelp.js`'s `SHORTCUTS` so it appears in the ⌘/ overlay.

## Testing

Backend tests: `api/tests/` and `service_pipeline/tests/` (pytest). The top-level README has the current counts.

Frontend tests: no unit tests. `api/tests/test_frontend_template_safety.py` checks that every module here parses as an ES module under `node` and that no `innerHTML` template contains a nested backtick. Add Vitest + jsdom + `import-meta-resolve` shims if you want unit tests for individual modules.

Manual smoke: run the app (`./run.sh`, or a built `Tern.app`) and exercise every shortcut + every overflow menu item + every state transition. `scripts/qa_smoke.py` covers the backend endpoints the frontend depends on; run it against a live backend with `python3 scripts/qa_smoke.py --base http://127.0.0.1:18765`, or through `scripts/dev_check.sh`.

## Performance notes

- **State changes are synchronous.** A `state.X = Y` fan-out runs every subscriber inline. Keep handlers cheap.
- **Detail-pane render replaces `innerHTML` on every selection change.** OK for tens of selections per second; would need fragment-diffing if scaled higher.
- **Real waveform decodes the full audio file via WebAudio.** Capped at 60 MB / file (`MAX_DECODE_BYTES` in `player.js`); peaks cached per `file_id` so re-selecting is instant.
- **SigLIP warms up at backend startup**, so the first search doesn't pay for the model load (0.09 s instead of 5.6 s when the warmup was added).
