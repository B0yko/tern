"""Audio extraction and Whisper transcription."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import urllib.request
from pathlib import Path

from .models import TranscriptSegment


# Lazy-loaded singleton; loading the JIT model costs ~200 ms, fine to amortize
_silero_model = None
def _get_silero():
    global _silero_model
    if _silero_model is None:
        from silero_vad import load_silero_vad
        _silero_model = load_silero_vad()
    return _silero_model


def _load_wav_16k_mono(wav_path: Path):
    """Load a 16 kHz mono PCM_S16LE WAV as a 1-D float32 torch tensor in
    [-1, 1]. We use Python's `wave` module + manual PCM→float conversion to
    avoid pulling in torchaudio (the silero-vad PyPI dep has a torchaudio≥2.9
    requirement which then requires torchcodec — a heavyweight C++ build
    we don't need). Our extract_audio always produces PCM_S16LE @ 16kHz mono,
    so the assumption is safe."""
    import wave
    import torch
    import struct
    with wave.open(str(wav_path), "rb") as wf:
        n = wf.getnframes()
        sw = wf.getsampwidth()
        ch = wf.getnchannels()
        fr = wf.getframerate()
        raw = wf.readframes(n)
    if sw != 2 or ch != 1 or fr != 16000:
        raise RuntimeError(
            f"Unexpected WAV format: sample_width={sw} channels={ch} rate={fr}; "
            f"expected 16-bit mono 16kHz"
        )
    # int16 → float32 in [-1, 1]
    ints = struct.unpack(f"<{n}h", raw)
    t = torch.tensor(ints, dtype=torch.float32) / 32768.0
    return t


def _get_speech_regions_ms(wav_path: Path, sample_rate: int = 16000) -> list[tuple[int, int]]:
    """Return [(start_ms, end_ms), ...] of detected speech regions in a WAV.
    Used to filter out Whisper hallucinations that fall in silent regions
    (the well-known "Thank you" / "Music" / "you" artifacts on intros + outros).
    We DO NOT modify the WAV — timestamps stay aligned with the source video."""
    from silero_vad import get_speech_timestamps

    audio = _load_wav_16k_mono(wav_path)
    if audio.numel() == 0:
        return []
    model = _get_silero()
    ts_list = get_speech_timestamps(
        audio, model,
        sampling_rate=sample_rate,
        min_silence_duration_ms=500,
        min_speech_duration_ms=120,
        speech_pad_ms=300,
        return_seconds=False,
    )
    # Convert sample-indices to ms
    return [(int(ts["start"] * 1000 / sample_rate), int(ts["end"] * 1000 / sample_rate)) for ts in ts_list]


def _is_in_speech(midpoint_ms: int, regions: list[tuple[int, int]]) -> bool:
    """True if `midpoint_ms` falls inside any [start, end] region."""
    for s, e in regions:
        if s <= midpoint_ms <= e:
            return True
    return False


# Public mirror for whisper.cpp GGML models (Hugging Face). Stable URLs.
_WHISPER_MODEL_URLS = {
    "ggml-large-v3-turbo-q5_0.bin":
        "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-large-v3-turbo-q5_0.bin",
    "ggml-large-v3-turbo-q8_0.bin":
        "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-large-v3-turbo-q8_0.bin",
    "ggml-large-v3-turbo.bin":
        "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-large-v3-turbo.bin",
    "ggml-base.en.bin":
        "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-base.en.bin",
    "ggml-tiny.en.bin":
        "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-tiny.en.bin",
}


# Minimum acceptable Whisper model size — Q5 turbo is ~547 MB, tiny.en
# is ~75 MB, base.en is ~141 MB. 50 MB is well below the smallest while
# still safely catching the "HF returned an HTML maintenance page" or
# "connection dropped at 5 MB" failure modes. Same defence as the
# prepare_bundle.sh check added in commit 726ca0a.
_WHISPER_MODEL_MIN_BYTES = 50 * 1024 * 1024  # 50 MB


def _download_whisper_model(model_path: Path) -> None:
    """Fetch a known Whisper.cpp GGML model on demand. Writes atomically
    (tmp file → rename) so a half-download can't masquerade as complete on
    the next run. Logs MB progress to stderr every 50 MB.

    Validates THREE failure modes that the prior version missed and that
    have all been seen in the wild for similar HF-CDN downloads:
      1. HTTP non-200 (HF returns a transient 503/504 + HTML body during
         maintenance) — without `getstatus()`, urlopen happily handed us
         the HTML and we renamed it to .bin. whisper-cli then crashes
         with "magic number mismatch" and the broken file SURVIVES,
         skipping re-download on every subsequent indexing run.
      2. Truncated body — connection drop at 3 MB into a 547 MB stream
         returns successfully from urlopen. Without a min-size guard
         we ship the 3 MB blob as a "model".
      3. Hung connection — no timeout means a stalled TCP socket can
         block the indexing run forever.
    """
    fname = model_path.name
    url = _WHISPER_MODEL_URLS.get(fname)
    if not url:
        raise RuntimeError(
            f"Whisper model {fname!r} not known to the auto-downloader. "
            f"Either pick one of {sorted(_WHISPER_MODEL_URLS)} "
            f"or fetch manually from https://huggingface.co/ggerganov/whisper.cpp/tree/main"
        )
    model_path.parent.mkdir(parents=True, exist_ok=True)
    # Defensive: nuke any stale .part from a previous failed run so a
    # half-written carcass doesn't bleed into the new attempt's tmp.
    tmp = model_path.with_suffix(model_path.suffix + ".part")
    if tmp.exists():
        tmp.unlink()
    print(
        f"Tern: Whisper model {fname} not found locally — "
        f"downloading from Hugging Face (≈550 MB, one-time)…",
        file=sys.stderr,
    )
    try:
        # timeout=60 — covers TCP connect + every read() call. HF's CDN
        # streams at >5 MB/s on a normal connection, so a 60 s gap with
        # zero bytes is definitively a hung socket, not slow progress.
        with urllib.request.urlopen(url, timeout=60) as r, open(tmp, "wb") as f:
            status = getattr(r, "status", None) or r.getcode()
            if status != 200:
                raise RuntimeError(
                    f"Hugging Face returned HTTP {status} for {fname} — "
                    f"likely transient. Retry the indexing run."
                )
            total = int(r.headers.get("Content-Length") or 0)
            seen = 0
            last_log = 0
            while True:
                buf = r.read(1 << 20)  # 1 MiB
                if not buf:
                    break
                f.write(buf)
                seen += len(buf)
                if total and seen - last_log >= 50 << 20:
                    pct = seen * 100 // total
                    print(f"      {seen >> 20} / {total >> 20} MB  ({pct}%)", file=sys.stderr)
                    last_log = seen
        # Body-size sanity AFTER close. urlopen returns successfully even
        # if the connection drops mid-stream; the only reliable check is
        # what landed on disk.
        downloaded = tmp.stat().st_size
        if downloaded < _WHISPER_MODEL_MIN_BYTES:
            raise RuntimeError(
                f"Whisper model {fname} download truncated: got "
                f"{downloaded // (1024 * 1024)} MB, "
                f"expected ≥ {_WHISPER_MODEL_MIN_BYTES // (1024 * 1024)} MB. "
                f"Network blip — retry the indexing run."
            )
    except Exception:
        tmp.unlink(missing_ok=True)
        raise
    tmp.rename(model_path)
    print(f"Tern: model saved to {model_path}", file=sys.stderr)


_WHISPER_PROGRESS_RE = re.compile(r"progress\s*=\s*(\d+)\s*%")


class WhisperTranscriber:
    """Wraps the whisper.cpp CLI for transcription with word-level timestamps."""

    def __init__(self, model_path: str, binary: str = "whisper-cli"):
        self.model_path = model_path
        self.binary = binary
        # Optional sub-file progress callback. Signature: progress_cb(pct: int)
        # where pct is 0-100. Set by Indexer (which forwards it into
        # app.state.indexing.stage_progress) so the indexing toast can show
        # "Transcribing 47%" instead of just "Transcribing speech (23s)".
        # Whisper.cpp emits one progress line per decode chunk on stderr —
        # we Popen + tail stderr in a background thread, parse the regex,
        # and call this on every increment. No-op if unset.
        self.progress_cb = None
        # Optional cancel-check callback. Signature: cancel_cb() -> bool.
        # When True, transcribe() will kill the in-flight whisper-cli
        # child + raise RuntimeError so the indexer's outer try/except
        # treats the file as errored and continues with the next one.
        # Pre-this-attr: cancel-during-file was no-op until the file
        # finished naturally. With the commit b180c42 timeout, "finishes
        # naturally" can be HOURS for a multi-hour audio — Cancel meant
        # "wait up to wait_timeout seconds before the loop can react."
        # Polled at 1 Hz inside the wait loop so a cancel request shows
        # effect within ~1 s instead of waiting on the natural finish.
        self.cancel_cb = None
        if not shutil.which(binary):
            raise RuntimeError(
                f"{binary} not found in PATH. Install via `brew install whisper-cpp`."
            )
        if not Path(model_path).exists():
            # First-run friendliness: auto-fetch a known GGML model rather than
            # crashing with a generic "not found". Removes a major adoption
            # blocker — new users no longer have to know what whisper.cpp is.
            try:
                _download_whisper_model(Path(model_path))
            except Exception as e:
                raise RuntimeError(
                    f"Whisper model not found at {model_path} and auto-download failed: {e}. "
                    f"Fetch manually from https://huggingface.co/ggerganov/whisper.cpp/tree/main"
                ) from e

    def transcribe(
        self,
        media_path: Path,
        file_id: int,
        language: str | None = None,
    ) -> list[TranscriptSegment]:
        """Extract audio (if video) and transcribe via whisper-cli."""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            audio_path = tmp / "audio.wav"
            self._extract_audio(media_path, audio_path)

            # Compute speech regions via Silero VAD BEFORE Whisper so we
            # can filter Whisper's hallucinated segments in silent intros/outros.
            # We don't modify the audio (that would shift timestamps); we just
            # keep the region list and drop transcript segments whose midpoint
            # lands outside any speech region. TERN_VAD=0 disables.
            speech_regions: list[tuple[int, int]] = []
            if os.environ.get("TERN_VAD", "1") != "0":
                try:
                    speech_regions = _get_speech_regions_ms(audio_path)
                except Exception as e:
                    print(f"Tern: Silero VAD failed ({e}); skipping silence-filter", file=sys.stderr)

            # whisper-cli outputs JSON with --output-json
            out_prefix = tmp / "out"
            cmd = [
                self.binary,
                "-m", self.model_path,
                "-f", str(audio_path),
                "--output-json",
                "--output-file", str(out_prefix),
                "--no-prints",
                "--print-progress",
            ]
            if language:
                cmd.extend(["-l", language])
            # max length per segment (ms) - keep reasonable for searchability
            cmd.extend(["-ml", "1"])  # 1-word-ish for better timestamp granularity

            # Popen + background stderr reader so we can surface whisper.cpp's
            # per-chunk progress (`whisper_print_progress_callback: progress = N%`)
            # to the indexing toast in near-real-time. Falls back gracefully if
            # no progress_cb is set — we just drain stderr without parsing.
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
            )
            # Bounded ring buffer for stderr lines. Pre-fix this was a
            # plain list that grew until proc.wait returned — fine for
            # healthy runs (~500 lines on a 5-min audio) but with the
            # commit b180c42 timeout, the wait can now be HOURS for a
            # multi-hour audio file. A malformed whisper-cli stuck in
            # a tight loop printing 1 MB/s to stderr would balloon the
            # list to GBs and OOM-kill the python sidecar (taking down
            # the WHOLE indexing job, not just the bad file). deque
            # with maxlen=500 keeps only the most recent lines —
            # plenty for the error-reporting use case (we only show
            # the last ~50 in the CalledProcessError message).
            from collections import deque as _deque
            err_buf: "_deque[str]" = _deque(maxlen=500)

            def _drain_stderr() -> None:
                try:
                    for line in proc.stderr:
                        err_buf.append(line)
                        if self.progress_cb is None:
                            continue
                        m = _WHISPER_PROGRESS_RE.search(line)
                        if m:
                            try:
                                self.progress_cb(int(m.group(1)))
                            except Exception:
                                pass  # callbacks must never break the pipeline
                except Exception:
                    pass  # stderr pipe closed unexpectedly — ignore

            reader = threading.Thread(target=_drain_stderr, daemon=True, name="whisper-progress")
            reader.start()
            # Generous timeout proportional to audio length so a corrupt
            # input / hung GGML backend can't stall the whole indexing
            # job indefinitely on one file. Whisper Large v3 Turbo Q5
            # runs at 8-15× realtime on Apple Silicon; allow 1 s of wall
            # per 1 s of audio (well above the worst observed ratio), with
            # a 300 s floor so a 5-second voice memo still has runway for
            # GGML init. 16 kHz mono PCM = 32_000 bytes/s, so file size
            # converts directly to duration.
            try:
                wav_bytes = audio_path.stat().st_size
                wav_seconds = wav_bytes / 32_000.0
            except Exception:
                wav_seconds = 60.0  # fallback if stat() fails — pick a sane median
            wait_timeout = max(300.0, wav_seconds * 1.0)
            # Poll the subprocess in 1-second chunks so a cancel_cb can
            # take effect mid-file. Pre-this-loop the wait was a single
            # `proc.wait(timeout=wait_timeout)` — for a 6-hour audio
            # file under the commit b180c42 timeout that meant cancel
            # could be queued for up to 6 HOURS before the indexer's
            # between-files check noticed. The 1 Hz cancel poll means
            # Cancel takes effect within ~1 s instead.
            POLL_S = 1.0
            elapsed = 0.0
            rc = None
            while elapsed < wait_timeout:
                if self.cancel_cb is not None:
                    try:
                        if self.cancel_cb():
                            proc.kill()
                            try: proc.wait(timeout=5)
                            except Exception: pass
                            raise RuntimeError(
                                f"whisper-cli cancelled by user on {media_path}"
                            )
                    except RuntimeError:
                        raise
                    except Exception:
                        pass  # cancel_cb errors must never break the pipeline
                try:
                    rc = proc.wait(timeout=POLL_S)
                    break  # subprocess finished — exit poll loop
                except subprocess.TimeoutExpired:
                    elapsed += POLL_S
                    continue
            if rc is None:
                # Timeout fired before the subprocess finished naturally.
                # Kill the stuck child so the subprocess slot frees up,
                # collect zombie via a second wait, then surface a clear
                # error. ingest.index_file's outer try/except will mark
                # this file as errored and the loop continues.
                proc.kill()
                try: proc.wait(timeout=5)
                except Exception: pass
                raise RuntimeError(
                    f"whisper-cli hung after {wait_timeout:.0f}s on "
                    f"{media_path} ({wav_seconds:.0f}s of audio) — "
                    f"input file may be malformed; skipping. "
                    f"stderr tail: {''.join(list(err_buf)[-10:])!r}"
                )
            reader.join(timeout=2)
            if rc != 0:
                raise subprocess.CalledProcessError(
                    rc, cmd, output=None, stderr="".join(list(err_buf)[-50:])
                )

            json_path = out_prefix.with_suffix(out_prefix.suffix + ".json")
            if not json_path.exists():
                # whisper-cli writes <prefix>.json
                json_path = Path(str(out_prefix) + ".json")
            if not json_path.exists():
                # Try alternative naming
                candidates = list(tmp.glob("*.json"))
                if candidates:
                    json_path = candidates[0]
                else:
                    return []

            with open(json_path) as f:
                data = json.load(f)

            return self._parse_whisper_json(data, file_id, speech_regions=speech_regions)

    def _extract_audio(self, media_path: Path, out_path: Path) -> None:
        """Extract 16 kHz mono PCM audio for Whisper.

        Timeout: 300 s ceiling. ffmpeg's PCM remux of a 6-hour podcast
        takes 30-60 s on an M-series SSD; 5 minutes is ~5× headroom.
        Without a timeout, a malformed container / a stuck demuxer on
        a corrupt file could hang the whole indexing run on that file
        indefinitely. Pairs with the per-file try/except in ingest's
        run_indexing_task: TimeoutExpired propagates → file marked
        errored → loop continues with the next file.
        """
        cmd = [
            "ffmpeg",
            "-y",
            "-i", str(media_path),
            "-vn",
            "-ac", "1",
            "-ar", "16000",
            "-c:a", "pcm_s16le",
            str(out_path),
        ]
        subprocess.run(cmd, check=True, capture_output=True, timeout=300)

    def _parse_whisper_json(
        self,
        data: dict,
        file_id: int,
        speech_regions: list[tuple[int, int]] | None = None,
    ) -> list[TranscriptSegment]:
        segments = []
        dropped_outside_speech = 0
        # whisper.cpp JSON has "transcription" key with segments
        for seg in data.get("transcription", []):
            start_ms = self._parse_offset(seg.get("offsets", {}).get("from"))
            end_ms = self._parse_offset(seg.get("offsets", {}).get("to"))
            text = seg.get("text", "").strip()
            if not text or start_ms is None or end_ms is None:
                continue
            # Drop segments whose midpoint isn't inside any VAD region
            # (Whisper hallucinations on silent intros / outros). Only filter
            # when we have a non-empty region list — empty means VAD found
            # zero speech, in which case the audio is genuinely silent and
            # Whisper's output is already noise we don't care about.
            if speech_regions:
                mid = (start_ms + end_ms) // 2
                if not _is_in_speech(mid, speech_regions):
                    dropped_outside_speech += 1
                    continue
            segments.append(
                TranscriptSegment(
                    file_id=file_id,
                    start_ms=start_ms,
                    end_ms=end_ms,
                    text=text,
                    confidence=1.0,
                )
            )
        if dropped_outside_speech:
            print(
                f"Tern: VAD dropped {dropped_outside_speech} hallucinated segments outside speech regions",
                file=sys.stderr,
            )
        # Coalesce single-word segments into ~5-15s chunks for better search snippets
        return self._coalesce(segments)

    @staticmethod
    def _parse_offset(value) -> int | None:
        """Convert a Whisper JSON offset value to int milliseconds.

        Whisper-cli emits offsets as integers (ms) in its JSON output, but
        the parser is defensive: pre-this-comment-cleanup, the docstring
        claimed timestamp-string support ("00:01:23.456") which the code
        does NOT implement — `int("00:01:23.456")` raises ValueError and
        returns None. That mismatch was misleading (a maintainer reading
        the docstring would assume timestamp parsing existed), so the
        comment is now removed.

        Accepts:
          * None → None (Whisper JSON sometimes omits the field)
          * int / float → int truncation
          * numeric strings like "5000" → int parsed value
        Returns None for anything else (timestamp strings, malformed
        data, etc.) so the caller can drop the segment cleanly."""
        if value is None:
            return None
        if isinstance(value, (int, float)):
            return int(value)
        try:
            return int(value)
        except (ValueError, TypeError):
            return None

    @staticmethod
    def _coalesce(segments: list[TranscriptSegment], target_ms: int = 8000) -> list[TranscriptSegment]:
        """Merge tiny segments into searchable chunks."""
        if not segments:
            return []
        result: list[TranscriptSegment] = []
        cur_text: list[str] = []
        cur_start = segments[0].start_ms
        cur_end = segments[0].end_ms
        for seg in segments:
            if seg.end_ms - cur_start > target_ms and cur_text:
                result.append(
                    TranscriptSegment(
                        file_id=seg.file_id,
                        start_ms=cur_start,
                        end_ms=cur_end,
                        text=" ".join(cur_text).strip(),
                    )
                )
                cur_text = [seg.text]
                cur_start = seg.start_ms
                cur_end = seg.end_ms
            else:
                cur_text.append(seg.text)
                cur_end = seg.end_ms
        if cur_text:
            result.append(
                TranscriptSegment(
                    file_id=segments[-1].file_id,
                    start_ms=cur_start,
                    end_ms=cur_end,
                    text=" ".join(cur_text).strip(),
                )
            )
        return result


def _safe_num(v, fallback=0.0):
    """ffprobe sometimes emits "N/A" (raw AAC streams, m3u8 playlists,
    partial downloads, certain WebM/MKV without a top-level duration).
    `float("N/A")` raises ValueError, the ingest loop catches that
    generically and silently SKIPS the entire file — no transcript, no
    OCR, no embeddings, no row in /api/files. The user just sees their
    .aac never show up in search and assumes the indexer is broken.
    Treat missing / unparseable values as the fallback instead so the
    file proceeds through the rest of the pipeline (transcript still
    works on raw audio decode; duration is just a display field)."""
    if v is None:
        return fallback
    try:
        return float(v)
    except (ValueError, TypeError):
        return fallback


def probe_media(path: Path) -> dict:
    """Use ffprobe to get media duration and mime info.

    Robust to ffprobe outputting `"N/A"` for duration / bit_rate — those
    files now return duration_ms=0 and proceed through indexing rather
    than crashing the file's pipeline run.

    Timeout: 30s. ffprobe normally completes in <1s, but a malformed /
    corrupted source or a stalled network mount could hang it forever.
    Without a timeout, every caller (ingest, FCPXML export, /api/files
    metadata refresh) would hang too. Raise TimeoutExpired so callers
    can mark the file as unprobable and skip it rather than blocking
    the whole pipeline."""
    cmd = [
        "ffprobe",
        "-v", "error",
        "-show_entries", "format=duration,bit_rate,format_name",
        "-of", "json",
        str(path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, check=True, timeout=30)
    data = json.loads(result.stdout)
    fmt = data.get("format", {})
    duration_s = _safe_num(fmt.get("duration"), 0.0)
    bit_rate = int(_safe_num(fmt.get("bit_rate"), 0))
    return {
        "duration_ms": int(duration_s * 1000),
        "bit_rate": bit_rate,
        "format_name": fmt.get("format_name", ""),
    }
