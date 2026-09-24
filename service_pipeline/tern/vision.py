"""Visual indexing: scene detection, keyframe extraction, embedding, OCR.

Also: image EXIF extraction (date taken, camera, GPS) for photos. Treated as
metadata, not as searchable text — surfaced in the UI as small tags below a
photo result so users can recognize "that beach photo from August 2023".
"""
from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from pathlib import Path

import numpy as np
from PIL import Image

# Register HEIC/HEIF opener with PIL. iPhone photos are .heic by
# default since iOS 11; without this PIL raises "cannot identify image file"
# and extract_image_exif silently returns {} for every iPhone photo.
try:
    from pillow_heif import register_heif_opener  # type: ignore
    register_heif_opener()
except Exception:
    pass

from .models import Keyframe, OCRSegment


def extract_image_exif(image_path: Path) -> dict:
    """Read EXIF from a photo and return a small dict of just the human-useful
    fields. Returns {} when the image has no EXIF (most stock photos, screenshots,
    web downloads etc.).

    The fields we surface are intentionally conservative: too many tags creates
    noise. Date/camera/lens are the ones a human would care to see in a search
    result. GPS is extracted but rounded to ~100m precision to avoid leaking
    exact home addresses in the demo workspace.
    """
    try:
        from PIL import Image as PILImage, ExifTags
    except Exception:
        return {}

    try:
        img = PILImage.open(image_path)
        # Prefer the modern getexif() — works for HEIC/HEIF/JPEG/PNG.
        # The legacy _getexif() is JPEG-only and raises AttributeError on
        # pillow_heif's HeifImageFile, silently dropping every iPhone photo's
        # metadata. Fall back to _getexif() only if getexif() doesn't exist.
        if hasattr(img, "getexif"):
            raw_obj = img.getexif() or {}
            raw = dict(raw_obj) if raw_obj else {}
        else:
            raw = img._getexif() or {}
    except Exception:
        return {}
    if not raw:
        return {}

    tag_name = {v: k for k, v in ExifTags.TAGS.items()}  # name -> id
    by_name = {ExifTags.TAGS.get(k, k): v for k, v in raw.items()}

    out: dict = {}

    # Date taken (DateTimeOriginal preferred, then DateTime)
    for key in ("DateTimeOriginal", "DateTime"):
        v = by_name.get(key)
        if isinstance(v, str):
            # EXIF format "2023:08:14 12:34:56" → normalise to "2023-08-14 12:34:56"
            out["date_taken"] = v.replace(":", "-", 2)
            break

    for key in ("Make", "Model", "LensModel"):
        v = by_name.get(key)
        if v:
            out[key.lower()] = str(v).strip().strip("\x00")

    iso = by_name.get("ISOSpeedRatings")
    if iso:
        out["iso"] = int(iso) if isinstance(iso, (int, float)) else iso

    # GPS: deg/min/sec rationals → decimal, rounded to 3dp (~100m)
    gps = by_name.get("GPSInfo")
    if gps and isinstance(gps, dict):
        gps_named = {ExifTags.GPSTAGS.get(k, k): v for k, v in gps.items()}
        def _dms_to_decimal(coord, ref):
            try:
                d, m, s = (float(x) for x in coord)
                val = d + m / 60.0 + s / 3600.0
                if ref in ("S", "W"):
                    val = -val
                return round(val, 3)
            except Exception:
                return None
        lat = _dms_to_decimal(gps_named.get("GPSLatitude"), gps_named.get("GPSLatitudeRef", "N"))
        lon = _dms_to_decimal(gps_named.get("GPSLongitude"), gps_named.get("GPSLongitudeRef", "E"))
        if lat is not None and lon is not None:
            out["gps"] = {"lat": lat, "lon": lon}

    # Dimensions (useful for the UI to lay out)
    try:
        out["width"], out["height"] = img.size
    except Exception:
        pass

    return out


class KeyframeExtractor:
    """Extract scene-change keyframes from video via ffmpeg."""

    def __init__(
        self,
        scene_threshold: float = 0.3,
        max_keyframes: int = 300,
        ffmpeg_binary: str = "ffmpeg",
    ):
        self.scene_threshold = scene_threshold
        self.max_keyframes = max_keyframes
        self.ffmpeg = ffmpeg_binary

    def extract(self, video_path: Path, out_dir: Path) -> list[tuple[int, Path]]:
        """Return list of (ts_ms, thumbnail_path) for each keyframe.

        ⚠ ts_ms correctness is critical — the whole product relies on the
        timestamp returned for a keyframe being the actual moment in the source
        video where that frame was extracted. Two ways to get this wrong:

        1. `-loglevel error` SUPPRESSES showinfo output (showinfo logs at info
           level), so `_parse_showinfo()` returned `[]` and the index-based
           fallback `ts_ms = i * 1000` ran — every keyframe got tagged with
           sequential 1-second timestamps that have NOTHING to do with the
           actual source-video time. Fixed by raising `-loglevel` to `info`
           and routing showinfo through `-loglevel level+info` style explicit
           level so we still suppress noise but keep our diagnostic line.
        2. Even with showinfo enabled, the `pts_time:` lines must MATCH the
           output file order. ffmpeg writes scene-change frames in temporal
           order both to stderr (via showinfo) and to disk (via image2), so
           `zip(ts_list, sorted(files))` works as long as ffmpeg processed
           them in order — which it does for a single-input, single-output
           pipeline with `select` upstream of `showinfo`.
        """
        out_dir.mkdir(parents=True, exist_ok=True)

        # Use ffmpeg select filter with scene detection. NOTE: keep -loglevel
        # at 'info' (default-equivalent) so showinfo's pts_time lines reach
        # stderr; we filter them in _parse_showinfo. `-hide_banner` still
        # suppresses the version/build header.
        pattern = str(out_dir / "kf_%05d.jpg")
        cmd = [
            self.ffmpeg,
            "-y",
            "-hide_banner",
            "-loglevel", "info",   # <-- was "error", which hid the pts_time we need
            "-i", str(video_path),
            "-vf", f"select='gt(scene,{self.scene_threshold})',showinfo,scale=1280:-2",
            "-vsync", "vfr",
            "-frames:v", str(self.max_keyframes),
            "-f", "image2",
            pattern,
        ]
        # 300 s ceiling matches the audio.py extract_audio + whisper-cli
        # pattern (commit b180c42). A corrupt video / hung demuxer would
        # otherwise stall the WHOLE indexing pipeline on this file
        # indefinitely — TimeoutExpired propagates → file marked errored
        # in ingest.run_indexing_task's per-file try/except → loop
        # continues to next file. Scene-detection extraction is bounded
        # by `-frames:v {max_keyframes}` (default ~300) so a healthy
        # ffmpeg finishes in tens of seconds even on 6-hour video;
        # 300 s gives ~10× headroom.
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        except subprocess.TimeoutExpired:
            # Surface as a clean error so the caller's per-file
            # try/except marks the file errored; no kf jpegs in
            # out_dir means caller falls back to interval extraction.
            raise RuntimeError(
                f"ffmpeg keyframe extraction timed out after 300s on {video_path.name} "
                "(corrupt video or stuck demuxer); skipping"
            )
        # ffmpeg writes showinfo to stderr. Parse pts_time from stderr.
        result: list[tuple[int, Path]] = []
        ts_list = self._parse_showinfo(proc.stderr)
        files = sorted(out_dir.glob("kf_*.jpg"))
        if not ts_list:
            # Hard fail loudly — silent fallback to i*1000 was the bug that
            # gave every keyframe the wrong timestamp for months.
            import sys
            print(
                f"WARNING: ffmpeg showinfo produced no pts_time for {video_path}; "
                f"timestamps WILL BE WRONG. stderr sample: {proc.stderr[:400]}",
                file=sys.stderr,
            )
        for i, fpath in enumerate(files):
            if i < len(ts_list):
                ts_ms = int(ts_list[i] * 1000)
            else:
                # Last-resort fallback. Should never fire when showinfo is on.
                ts_ms = i * 1000
            result.append((ts_ms, fpath))
        # Ensure visual coverage scales with video length. Scene-detect
        # alone can leave multi-minute gaps on talking-head content (a 125-min
        # YC lecture + BBB concat produced only 29 scene-cut keyframes — one
        # every 4.3 min — which made "butterfly" queries miss every BBB scene).
        # Two complementary triggers:
        #   (a) "too few overall": classic small-video fallback (< 10 scene cuts)
        #   (b) "too sparse for length": density < ~1 keyframe per 60 s of video
        # When either fires, supplement with interval-spaced frames (60 s apart
        # for long videos, 10 s for short) and merge by timestamp-bucket dedupe.
        try:
            from .audio import probe_media
            video_dur_s = probe_media(video_path)["duration_ms"] / 1000.0
        except Exception:
            video_dur_s = 0.0
        too_few = len(result) < 10
        too_sparse = video_dur_s > 120 and len(result) > 0 and (video_dur_s / len(result)) > 60
        if too_few or too_sparse:
            # Cap interval frames so a 5-hour video doesn't crush SigLIP. Aim
            # for ~1 keyframe per 30 s of video, capped at max_keyframes / 2.
            interval_s = 10.0 if too_few else 30.0
            interval_result = self._extract_at_intervals(video_path, out_dir, interval_s=interval_s)
            # Bucket-dedupe so a scene-cut at 12.3 s and an interval frame at
            # 12.0 s don't BOTH get stored — keep the scene-cut (earlier in
            # `result`) and drop the interval near-duplicate.
            existing_buckets = {r[0] // 2000 for r in result}
            for ts, p in interval_result:
                if (ts // 2000) not in existing_buckets:
                    result.append((ts, p))
                    existing_buckets.add(ts // 2000)
        result.sort(key=lambda x: x[0])
        return result[: self.max_keyframes]

    def _parse_showinfo(self, stderr: str) -> list[float]:
        """Parse showinfo output for pts_time values."""
        timestamps = []
        for line in stderr.split("\n"):
            if "pts_time:" in line:
                try:
                    ts_part = line.split("pts_time:")[1].split()[0]
                    timestamps.append(float(ts_part))
                except (IndexError, ValueError):
                    continue
        return timestamps

    def snapshot_image(self, image_path: Path, out_dir: Path) -> Path | None:
        """For a still image input, produce a normalized 1280-wide JPEG in `out_dir`
        and return its path. This makes images interchangeable with video keyframes
        downstream (same SigLIP embedding + Apple Vision OCR path).

        Animated/multi-frame inputs (e.g. animated GIFs) are reduced to their first
        frame; ffmpeg handles HEIC, WebP, TIFF, BMP, etc. transparently on macOS.
        """
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / "image_00000.jpg"
        cmd = [
            self.ffmpeg,
            "-y",
            "-hide_banner",
            "-loglevel", "error",
            "-i", str(image_path),
            "-frames:v", "1",
            # Scale down to max 1280px wide; never upscale; keep aspect ratio
            "-vf", "scale=w=1280:h=-2:force_original_aspect_ratio=decrease",
            "-q:v", "3",
            str(out_path),
        ]
        try:
            # 60 s ceiling — image downscale should be <1s on any
            # reasonable image. 60 s gives margin for huge HEIC bursts
            # or a HEIC decoder that's chunking through a 50 MP
            # multi-image. TimeoutExpired returns None (no thumbnail)
            # rather than raise; caller falls back to full-res preview.
            subprocess.run(cmd, check=True, capture_output=True, timeout=60)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
            return None
        return out_path if out_path.exists() else None

    def _extract_at_intervals(
        self, video_path: Path, out_dir: Path, interval_s: float = 10.0
    ) -> list[tuple[int, Path]]:
        """Extract one frame every `interval_s` from the video.

        Bug history and final design:

        Pure input-seek (`-ss` BEFORE `-i`) is fast (uses the demuxer index
        to jump straight to the nearest keyframe) but lands on the wrong
        frame for files with discontinuous PTS — `ffmpeg -f concat -c copy`
        output is the obvious example. On a 125-min YC+BBB concat, input-
        seek returned the same byte-identical "frame 0" for every interval
        past ~10 min; visual search for BBB content returned 0 hits.

        Pure output-seek (`-ss` AFTER `-i`) decodes forward from the start
        and lands on the exact frame, even with broken PTS. But it scales
        O(ts) per frame: extracting 250 interval frames from a 2-hour video
        cost ~13 min (vs 50 s with the broken input-seek). Unacceptable.

        Hybrid: input-seek to `ts - 4s` (fast — lands on a keyframe before
        the target), then output-seek 4 s forward (decodes only those 4 s).
        Per-frame cost stays roughly constant regardless of where in the
        video we are, and accuracy survives broken PTS because the second
        seek is post-decode. This is the canonical ffmpeg seek pattern.
        """
        from .audio import probe_media

        info = probe_media(video_path)
        duration_s = info["duration_ms"] / 1000.0
        if duration_s <= 0:
            return []
        result: list[tuple[int, Path]] = []
        n = min(int(duration_s / interval_s), self.max_keyframes // 2)
        # Pre-roll window: how far before the target we input-seek. Larger
        # values are more tolerant of sparse keyframes; smaller is faster.
        # 4 s is a safe compromise for typical 1-4 s GOP videos.
        preroll = 4.0
        for i in range(n):
            ts = i * interval_s
            out_path = out_dir / f"interval_{i:05d}.jpg"
            input_seek = max(0.0, ts - preroll)
            output_seek = ts - input_seek  # 0 for very-early ts, ~preroll otherwise
            cmd = [
                self.ffmpeg,
                "-y",
                "-hide_banner",
                "-loglevel", "error",
                "-ss", str(input_seek),    # fast: jump near the target
                "-i", str(video_path),
                "-ss", str(output_seek),   # accurate: decode the last few seconds
                "-frames:v", "1",
                "-vf", "scale=1280:-1",
                str(out_path),
            ]
            try:
                # 60 s per-frame ceiling. Each interval extraction is
                # one input-seek + output-seek + decode of ~`preroll`
                # seconds of GOP context — typically <1s on local SSD,
                # several seconds for network-mounted media. 60s
                # generous margin. TimeoutExpired skips this frame
                # rather than aborting the whole interval loop.
                subprocess.run(cmd, check=True, capture_output=True, timeout=60)
                if out_path.exists():
                    result.append((int(ts * 1000), out_path))
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
                continue
        return result


class Embedder:
    """Embed images and text via a SigLIP-family model.

    Uses `transformers` directly (NOT sentence-transformers) because the latter's
    SentenceTransformer wrapper does not route inputs through SigLIP's separate
    image/text encoder heads correctly — empirically that produces ~0 cosine
    similarity for matching text↔image pairs, breaking visual semantic search.

    With `transformers` directly + `model.get_text_features` / `get_image_features`,
    a clean cat photo scores ~+0.16 against "a photograph of a ginger cat" vs ~-0.01
    for unrelated frames. The same cosine values are then stored in ChromaDB and
    ranked correctly.
    """

    def __init__(self, model_name: str = "google/siglip2-base-patch16-256"):
        from transformers import AutoModel, AutoProcessor
        import os
        import torch

        # Device selection:
        #  - TERN_DEVICE env var wins if set (e.g. "cpu" on CI / containerized
        #    runs where MPS allocation may fail with OOM on small hosts).
        #  - GitHub Actions sets CI=true; fall back to CPU there too because
        #    the macos-14 runner caps MPS shared pool at ~7.9 GiB and SigLIP-2
        #    base wants ~750 MiB which conflicts with other CI processes.
        #  - Otherwise prefer MPS when available, else CPU.
        forced = os.environ.get("TERN_DEVICE", "").strip().lower()
        if forced in ("cpu", "mps", "cuda"):
            device = forced
        elif os.environ.get("CI", "").lower() in ("true", "1"):
            device = "cpu"
        else:
            device = "mps" if torch.backends.mps.is_available() else "cpu"
        # When forced off MPS, set both defaults AND pass device_map to
        # from_pretrained. Just set_default_device isn't enough — transformers
        # internally probes MPS during model construction (we observed exactly
        # this on the macos-14 CI runner: even .to('cpu') failed with MPS OOM
        # because from_pretrained allocated 750 MiB on the MPS pool before
        # our .to() call could move it).
        self.device = device
        self.model_name = model_name
        from_pretrained_kwargs = {}
        if device == "cpu":
            try:
                torch.set_default_device("cpu")
            except Exception:
                pass
            # device_map='cpu' tells transformers to skip MPS device probing
            # entirely and load weights directly on CPU memory.
            from_pretrained_kwargs["device_map"] = "cpu"
            from_pretrained_kwargs["torch_dtype"] = torch.float32
        self.model = AutoModel.from_pretrained(model_name, **from_pretrained_kwargs).to(device).eval()
        self.processor = AutoProcessor.from_pretrained(model_name)
        # Probe embedding dim with a dummy text encode
        with torch.no_grad():
            tin = self.processor(
                text=["x"], padding="max_length", return_tensors="pt"
            ).to(device)
            self.embedding_dim = int(self._text_features(tin).shape[-1])
        # LRU cache for text embeddings — typical user behavior re-runs the
        # same query multiple times: clicking a Saved search, toggling scope
        # filters (each toggle re-runs the query), pressing Enter on a hit
        # then coming back to search. Each call costs ~30ms on MPS for the
        # base SigLIP-2 text encoder. Cache 256 most-recent — 3 KB per
        # 768-dim float32, total ~768 KB. Negligible RAM, big UX win.
        # `_text_cache` is a manual OrderedDict (instance-bound) instead of
        # @functools.lru_cache on a method — lru_cache on a bound method
        # shares state across all Embedder instances, which would leak in
        # tests and during get_engine() re-init after a failed warmup.
        from collections import OrderedDict
        self._text_cache: "OrderedDict[str, np.ndarray]" = OrderedDict()
        self._text_cache_max = 256

    # --- internal helpers: SigLIP-2 in this transformers version returns
    #     BaseModelOutputWithPooling from get_*_features; the actual vector
    #     is in .pooler_output. Wrap both modalities behind one method.
    def _text_features(self, inputs):
        out = self.model.get_text_features(**inputs)
        return out.pooler_output if hasattr(out, "pooler_output") else out

    def _image_features(self, inputs):
        out = self.model.get_image_features(**inputs)
        return out.pooler_output if hasattr(out, "pooler_output") else out

    def embed_images(self, paths: list[Path]) -> tuple[list[Path], np.ndarray]:
        """Embed a list of image paths via SigLIP-2.

        Returns (surviving_paths, stacked_embeddings) where surviving_paths
        is the subset of input that successfully opened + decoded, in the
        original input order. Caller aligns embeddings to its keyframe
        metadata via the surviving_paths list (build a dict, or zip).

        Per-path try/except: previously the entire batch built by an eager
        list comprehension threw if ANY one image failed to open (truncated
        keyframe artifact from a partial ffmpeg run, file moved between
        discover and embed, permission denied on a TCC-locked subdir). A
        single bad frame in an 80-keyframe video would propagate out of
        the comprehension, get caught by ingest.index_file's outer
        try/except, and mark the WHOLE source file as errored — losing
        Whisper transcript + OCR work that had already completed
        successfully for the same file. Now: the bad frame is logged and
        skipped; the other 79 keyframes' embeddings still land in
        ChromaDB and the file finishes as "done".
        """
        import logging
        if not paths:
            return [], np.zeros((0, self.embedding_dim), dtype=np.float32)
        import torch

        out_chunks: list[np.ndarray] = []
        surviving: list[Path] = []
        batch_size = 8
        logger = logging.getLogger("tern.vision")
        for i in range(0, len(paths), batch_size):
            batch_paths = paths[i : i + batch_size]
            batch_imgs: list = []
            batch_ok: list[Path] = []
            for p in batch_paths:
                try:
                    # `.convert("RGB")` is what actually reads the image
                    # bytes — Image.open alone is lazy and only validates
                    # the header. Both failure modes (header parse, body
                    # decode) land here.
                    batch_imgs.append(Image.open(p).convert("RGB"))
                    batch_ok.append(p)
                except Exception as e:
                    logger.warning("embed_images: skipping %s (%s: %s)", p, type(e).__name__, e)
            if not batch_imgs:
                continue
            inputs = self.processor(images=batch_imgs, return_tensors="pt").to(self.device)
            with torch.no_grad():
                feats = self._image_features(inputs)
            feats = feats / feats.norm(dim=-1, keepdim=True)
            out_chunks.append(feats.cpu().to(torch.float32).numpy())
            surviving.extend(batch_ok)
        if not out_chunks:
            return [], np.zeros((0, self.embedding_dim), dtype=np.float32)
        return surviving, np.concatenate(out_chunks, axis=0)

    def embed_text(self, text: str) -> np.ndarray:
        import torch

        # LRU cache hit — most queries are repeats (saved searches re-runs,
        # scope-toggle re-queries, debounced typeahead landing on the same
        # final string). Return a COPY so caller mutations don't corrupt
        # the cached vector.
        cached = self._text_cache.get(text)
        if cached is not None:
            # Move to end (LRU touch). Wrapped in try/except because the
            # cache is shared across threads when commit 3c8b91d's search-
            # time perf optimization runs embed_text on a background
            # thread concurrently with FTS5 channels: a different search
            # firing in parallel (rare but possible — concurrent CLI +
            # desktop search, or two close-together keystrokes that beat
            # the 180 ms debounce) could call popitem(last=False) between
            # our get() and the move_to_end(), evicting THIS key if it
            # happened to be the LRU oldest. The KeyError that would
            # otherwise propagate would crash the visual channel for the
            # caller search (caught by search.py's per-channel except,
            # but it'd log a noisy traceback for what is effectively just
            # a transient cache miss). Catching it here turns the rare
            # race into a clean recompute, no log noise.
            try:
                self._text_cache.move_to_end(text)
            except KeyError:
                # Another thread evicted this entry between our .get()
                # and the move_to_end. Recompute via the cold path below.
                cached = None
            else:
                return cached.copy()

        inputs = self.processor(
            text=[text], padding="max_length", return_tensors="pt"
        ).to(self.device)
        with torch.no_grad():
            feats = self._text_features(inputs)
        feats = feats / feats.norm(dim=-1, keepdim=True)
        emb = feats[0].cpu().to(torch.float32).numpy()

        # Store + evict oldest if over capacity.
        self._text_cache[text] = emb
        if len(self._text_cache) > self._text_cache_max:
            self._text_cache.popitem(last=False)
        return emb.copy()


class VisionOCR:
    """Wraps the Swift Apple Vision OCR binary."""

    def __init__(self, binary: str = "bin/vision-ocr"):
        self.binary = Path(binary).resolve()
        if not self.binary.exists():
            raise RuntimeError(
                f"Vision OCR binary not found at {self.binary}. "
                f"Run: swiftc -O -o bin/vision-ocr bin/vision-ocr.swift"
            )

    def ocr_batch(
        self, image_paths: list[Path], mode: str = "fast", confidence_threshold: float = 0.5
    ) -> dict[Path, list[OCRSegment]]:
        """Run OCR on a batch of images. Returns map of path -> OCR segments (without file_id set).

        Timeout + diagnostics: the previous subprocess.run had no timeout
        and no return-code check. A corrupted JPEG or a Vision framework
        driver glitch could hang the whole indexing run forever, with
        the user seeing nothing but a stuck "Reading text from N frames"
        toast. Worse, on a crash the binary's stderr was silently
        discarded — there was no way to tell whether the empty result
        dict meant "no text found" or "binary failed."

        Now: cap at max(60s, 5s/image). 5s/frame is ~10× headroom over
        the typical Apple Silicon Vision Framework throughput; if a
        single frame is taking that long, something is wrong. On
        TimeoutExpired we mark the batch as failed (empty dict) and let
        ingest's per-file try/except mark the source file as errored so
        the indexing loop continues with the next file instead of
        hanging. On non-zero returncode we log stderr to our own stderr
        so it lands in tern-debug.log.
        """
        if not image_paths:
            return {}
        # Build stdin: one path per line
        stdin_text = "\n".join(str(p.resolve()) for p in image_paths)
        cmd = [str(self.binary), "--batch", "--mode", mode]
        # 5 s per image is ~10× the observed Apple Silicon Vision throughput
        # for "fast" mode (~50–200ms / image at 1280px). For "accurate" mode
        # it's still ~3–5× headroom. A batch that exceeds this is hung.
        timeout_s = max(60.0, 5.0 * len(image_paths))
        try:
            proc = subprocess.run(
                cmd,
                input=stdin_text,
                capture_output=True,
                text=True,
                timeout=timeout_s,
            )
        except subprocess.TimeoutExpired as e:
            import sys
            print(
                f"WARNING: vision-ocr hung on {len(image_paths)}-image batch "
                f"(timeout={timeout_s:.0f}s) — skipping OCR for these frames. "
                f"Source file will still be indexed for transcript + visual.",
                file=sys.stderr,
            )
            return {p: [] for p in image_paths}
        if proc.returncode != 0:
            # Non-zero return = the Swift binary crashed mid-batch. Log
            # the stderr tail so users have something to put in a bug
            # report. Don't raise — the caller treats an empty result as
            # "no text on this frame", which degrades gracefully (visual +
            # transcript search still work).
            import sys
            print(
                f"WARNING: vision-ocr exited with rc={proc.returncode} on "
                f"{len(image_paths)}-image batch. stderr tail: "
                f"{(proc.stderr or '')[-400:]!r}",
                file=sys.stderr,
            )
        result: dict[Path, list[OCRSegment]] = {p: [] for p in image_paths}
        path_map = {str(Path(p).resolve()): p for p in image_paths}
        for line in proc.stdout.split("\n"):
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue
            if data.get("error"):
                continue
            path_str = data.get("path", "")
            original_path = path_map.get(path_str)
            if original_path is None:
                continue
            segments: list[OCRSegment] = []
            for block in data.get("blocks", []):
                if block.get("confidence", 0) < confidence_threshold:
                    continue
                text = block.get("text", "").strip()
                if not text or len(text) < 2:
                    continue
                segments.append(
                    OCRSegment(
                        file_id=0,  # filled in by caller
                        frame_ts_ms=0,
                        text=text,
                        confidence=block.get("confidence", 0),
                        bbox=block.get("bbox", [0, 0, 0, 0]),
                    )
                )
            result[original_path] = segments
        return result
