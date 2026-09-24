# Tern demo — real video samples

This directory holds **real, third-party video files** used to verify that Tern's video pipeline (Whisper transcript + ffmpeg keyframes + SigLIP-2 visual embedding + Apple Vision OCR) works on non-synthetic content.

Files are **not committed to git** (see `.gitignore`). `download.sh` fetches them from YouTube with `yt-dlp`, for local testing only. Indexer output lives in `../db/` alongside the rest of the demo workspace.

---

## Files

### `big_buck_bunny.mp4`

- **Source:** https://www.youtube.com/watch?v=aqz-KE-bpKQ
- **License:** Creative Commons Attribution (CC-BY 3.0)
- **Attribution:** © copyright Blender Foundation | www.bigbuckbunny.org
- **Duration:** 10:34 (635 s)
- **Resolution:** 1280×720, h264 + aac
- **File size:** ~154 MB
- **Why this file:** cinematic animation with rich visual variety (forest scenes, a rabbit, butterflies, multiple environments), no dialogue and almost no on-screen text. Stress-tests the visual semantic channel (SigLIP-2) and shows that the OCR channel correctly returns nothing rather than hallucinating.

### `yc_lecture1_intro.mp4`

- **Source:** https://www.youtube.com/watch?v=CBYhVcO4WgI (first 10:00 only)
- **License:** YouTube marks the video as Creative Commons, and the uploader's description names CC BY-NC-ND 2.5. It is fetched to the local machine for testing and is not redistributed.
- **Attribution:** Y Combinator — "Lecture 1: How to Start a Startup" with Sam Altman and Dustin Moskovitz, Stanford CS183B
- **Duration:** 10:00 (clipped from a 44-min original)
- **Resolution:** 1280×720, h264 + aac
- **File size:** ~35 MB
- **Why this file:** a real talking-head lecture. Speech is dense and well-articulated, which tests Whisper; a title card and a speaker lower-third test OCR.

---

## Reproduce

From the repository root, download both files:

```bash
demo/real_videos/download.sh
```

Then index them with the backend **stopped** (ChromaDB's `PersistentClient` allows one process per workspace, so don't run the CLI against a workspace `./run.sh` is serving):

```bash
cd service_pipeline
uv run python -m tern.cli -w ../demo index ../demo/real_videos -l en
```

Or run `scripts/init_demo.sh`, which indexes everything under `demo/` through the API.

What the demo index built from these files on 15 Aug 2026 holds:

| File | Transcript segments | Keyframes | On-screen text |
|---|---|---|---|
| `big_buck_bunny.mp4` | 1 (a lone music marker; the film has no dialogue) | 125 (scene detection finds many cuts) | none |
| `yc_lecture1_intro.mp4` | 76 | 30 (mostly a static stage shot, so the interval fallback adds frames) | text on 29 keyframes (title card, slides, lower-thirds) |

---

## Verified queries

Run through `/api/search` on 24 Sep 2026, on the author's MacBook Pro, against the demo index (this folder plus `real_photos` and `demo_reel`, and eight synthetic podcast episodes kept outside the repository). From the CLI, in `service_pipeline/`: `uv run python -m tern.cli -w ../demo search "<q>"`.

| Query | Top hit | Channels |
|---|---|---|
| `Stanford` | `yc_lecture1_intro.mp4` @ 00:27, 4 hits | speech + on-screen + visual on the top hit |
| `Y Combinator` | `yc_lecture1_intro.mp4` @ 00:29, 3 hits | on-screen + visual |
| `How to Start a Startup` (`-s ocr`) | `yc_lecture1_intro.mp4` @ 00:29, 2 hits | on-screen (title card) |
| `Sam Altman` | `yc_lecture1_intro.mp4` @ 00:29, 2 hits | on-screen + visual |
| `rabbit in a forest` (`-s visual`) | `big_buck_bunny.mp4` @ 00:52, 4 keyframes | visual |
| `butterfly` | `big_buck_bunny.mp4` @ 03:10; 5 hits, at most 4 per file | visual |

`Y Combinator` and `Sam Altman` don't match on speech even though both are said. Whisper's word-level output splits some words (`Y Com bin ator`, `Sam Alt man`), and the transcript stores the pieces with spaces between them.

---

## Licensing reminder

- Big Buck Bunny is CC BY 3.0: credit Blender Foundation if you redistribute it.
- The YC lecture clip is for local testing only. Don't redistribute it, and leave this folder out of anything you share, including an app bundle built with `scripts/prepare_bundle.sh`, which copies all of `demo/`.

These files only exist locally on the machine that downloaded them.
