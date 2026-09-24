# Tern demo — real photos

This directory holds **real third-party photos** to verify Tern's photo-search pipeline (1-frame SigLIP-2 visual embedding + Apple Vision OCR + ChromaDB cosine ranking) actually surfaces the right image when you query for what's in it.

Photo files are **not committed to git** (see `.gitignore`); fetch them via `download.sh`. Indexer output (keyframes, OCR, ChromaDB) lives in `../db/` alongside audio + video.

---

## Files

| File | Source | License | Why it's here |
|---|---|---|---|
| `cat_portrait.jpg` | Wikimedia Commons `Cat03.jpg` | CC-BY-SA 3.0, © Alvesgaspar | Crisp ginger tabby photograph — single dominant subject. Exercises the "photo of <object>" semantic match path. |
| `PNG_transparency_demonstration_1.png` | Wikimedia Commons | Public Domain | Translucent chess piece on a checkered floor. Unusual content, useful as an "is this even in my archive?" test. |
| `picsum_beach.jpg` | Lorem Picsum (seed=beach) | Unsplash License | Hazy city skyline + reflective water, despite the name — exercises "city at sunset" / "skyline" semantic queries. |
| `picsum_cafe.jpg` | Lorem Picsum (seed=cafe) | Unsplash License | Random street scene; matches against negative queries to confirm the index doesn't false-positive. |
| `picsum_mountain.jpg` | Lorem Picsum (seed=mountain) | Unsplash License | Alpine mountains + green lake + wooden dock. |
| `picsum_office.jpg` | Lorem Picsum (seed=office) | Unsplash License | Random photo (an aerial shot of mountains, despite the seed). Useful negative-control entry. |
| `picsum_whiteboard.jpg` | Lorem Picsum (seed=whiteboard) | Unsplash License | Random photo. Same use. |

Lorem Picsum serves photos from Unsplash. The seeds are fixed, so the same URL should return the same photo, but that is Picsum's behaviour, not something this repository controls.

Three of the Picsum files are named after content they don't contain. The filename channel matches on basenames, so those names can outrank the photo that actually shows what you asked for; [`../demo_reel/README.md`](../demo_reel/README.md) lists the queries where that happens.

The files are downloaded to the machine that runs the script, for local testing. This repository does not redistribute them.

---

## Reproduce

From the repository root:

```bash
demo/real_photos/download.sh
```

Then index them with the backend **stopped** (ChromaDB's `PersistentClient` allows one process per workspace, so don't run the CLI against a workspace `./run.sh` is serving):

```bash
cd service_pipeline
uv run python -m tern.cli -w ../demo index ../demo/real_photos -l en
```

Or run `scripts/init_demo.sh`, which indexes everything under `demo/` through the API.

Expected output, abridged (a few seconds for the 7 photos):

```
  → cat_portrait.jpg (image)
    ✓ embedded 1/1 keyframes in 0.0s
    ✓ OCR: 0 text blocks across 1 frames in 0.2s
… (repeats per file)
Done indexed=7 skipped=0 errors=0 time=2.5s
```

Each photo becomes one "keyframe" at `ts_ms=0` in the same `keyframes` table as video keyframes, so the search engine treats them uniformly.

---

## Verified queries

Run through `/api/search` on 24 Sep 2026, on the author's MacBook Pro, against the demo index (this folder plus `real_videos` and `demo_reel`, and eight synthetic podcast episodes kept outside the repository), restricted to the visual channel. From the CLI, in `service_pipeline/`:

```bash
uv run python -m tern.cli -w ../demo search "<query>" -s visual -n 4
```

| Query | Result | What it proves |
|---|---|---|
| `orange cat` | `cat_portrait.jpg`, the only hit | The filename also contains `cat`, so this one has help. `ginger tabby` finds the same photo on the embedding alone. |
| `mountain lake with wooden dock` | `picsum_mountain.jpg`, the only hit | Again the filename matches (`mountain`). `a lake with a wooden pier` finds it with no filename help. |
| `city skyline at sunset` | `picsum_beach.jpg` first of 2 | Despite the filename, SigLIP-2 finds the photo that **actually shows** a hazy city silhouette. |
| `transparent chess piece on checkered floor` | nothing | The match is too weak to clear the visual noise floor, so the ranker drops it rather than guess. |

An earlier version of the embedder wrapped SigLIP in `sentence-transformers`, which did not route text and images through the right encoder heads, and queries like these returned video keyframes only, never photos. The embedder now calls `transformers.AutoModel.get_text_features` / `get_image_features` and reads `.pooler_output` directly (`service_pipeline/tern/vision.py`).

---

## Licensing reminder

- **Wikimedia files** (`cat_portrait.jpg`, `PNG_transparency_demonstration_1.png`) — attribute the original photographers per the file's Wikimedia page if you redistribute them.
- **Lorem Picsum photos** — Unsplash photos under the Unsplash License (free to use, attribution appreciated). Keep this README alongside the files.
- The files only exist locally on the machine that downloaded them. Tern does not redistribute them.
