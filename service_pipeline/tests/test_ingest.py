"""Tests for tern.ingest module — discover_files robustness."""
from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

import pytest

from tern.ingest import discover_files, _SKIP_DIRS


def test_discover_files_finds_supported_extensions(tmp_path):
    """Sanity: a folder with mixed file types returns only supported ones."""
    (tmp_path / "podcast.mp3").write_bytes(b"")
    (tmp_path / "video.mp4").write_bytes(b"")
    (tmp_path / "photo.jpg").write_bytes(b"")
    (tmp_path / "notes.txt").write_bytes(b"")       # unsupported
    (tmp_path / "spreadsheet.xlsx").write_bytes(b"") # unsupported

    found = discover_files(tmp_path)
    names = sorted(f.name for f in found)
    assert names == ["photo.jpg", "podcast.mp3", "video.mp4"]


def test_discover_files_skips_skipped_dirs(tmp_path):
    """_SKIP_DIRS (db, __pycache__, .git, .venv, etc.) must be pruned even
    when they contain valid extensions — those are derived artifacts."""
    (tmp_path / "real.mp3").write_bytes(b"")
    db = tmp_path / "db"
    db.mkdir()
    (db / "thumbnail.jpg").write_bytes(b"")  # would match by extension
    cache = tmp_path / "__pycache__"
    cache.mkdir()
    (cache / "fake.mp4").write_bytes(b"")    # would match by extension

    found = discover_files(tmp_path)
    names = [f.name for f in found]
    assert names == ["real.mp3"]
    # Sanity: confirm we actually have both _SKIP_DIRS members covered
    assert "db" in _SKIP_DIRS
    assert "__pycache__" in _SKIP_DIRS


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits")
def test_discover_files_survives_permission_denied_subdir(tmp_path, caplog):
    """Real-world: macOS TCC-protected subdirs (~/Library/Mail, etc.)
    raise PermissionError on readdir. Pathlib's rglob would abort the
    WHOLE walk — partial results from sibling folders lost. os.walk +
    onerror handler must skip + continue.

    Setup: create a 0-permission subdir alongside a normal one. The
    normal one's files should still be discovered."""
    (tmp_path / "ok.mp3").write_bytes(b"")
    nested_ok = tmp_path / "subdir"
    nested_ok.mkdir()
    (nested_ok / "also_ok.mp4").write_bytes(b"")

    locked = tmp_path / "locked"
    locked.mkdir()
    (locked / "would_match.jpg").write_bytes(b"")
    # Strip ALL permissions — readdir fails, this is the TCC scenario.
    os.chmod(locked, 0o000)

    try:
        found = discover_files(tmp_path)
        names = sorted(f.name for f in found)
        # We must get BOTH siblings — old rglob behavior aborted at locked/
        assert "ok.mp3" in names, f"top-level file missing: {names}"
        assert "also_ok.mp4" in names, f"readable subdir missing: {names}"
        # locked/ contents must NOT appear (permission denied)
        assert "would_match.jpg" not in names, f"locked dir leaked: {names}"
    finally:
        # Restore so pytest can clean up tmp_path
        os.chmod(locked, stat.S_IRWXU)


def test_discover_files_doesnt_follow_symlink_loops(tmp_path):
    """Defensive: a symlink loop (subdir → parent) must NOT cause
    discover_files to loop infinitely. os.walk(followlinks=False) is
    the explicit guard."""
    (tmp_path / "real.mp3").write_bytes(b"")
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "nested.mp4").write_bytes(b"")
    # Make sub/loop point back to tmp_path — would loop forever under
    # followlinks=True.
    (sub / "loop").symlink_to(tmp_path)

    found = discover_files(tmp_path)
    names = sorted(f.name for f in found)
    # Each file appears exactly once (no symlink-loop duplicates)
    assert names == ["nested.mp4", "real.mp3"]


def test_discover_files_returns_sorted(tmp_path):
    """Result must be sorted — indexing UI relies on stable ordering for
    progress estimation and resume-after-cancel UX."""
    (tmp_path / "z.mp3").write_bytes(b"")
    (tmp_path / "a.mp3").write_bytes(b"")
    (tmp_path / "m.mp3").write_bytes(b"")

    found = discover_files(tmp_path)
    names = [f.name for f in found]
    assert names == sorted(names)


# ─── tern.audio.probe_media — N/A field robustness ─────────────────────
# Real-world ffprobe emits `"duration":"N/A"` (and sometimes the same for
# bit_rate) on raw AAC streams, m3u8 playlists, partial downloads, and
# certain WebM/MKV containers that lack a top-level duration. Before the
# audio.py _safe_num fix, `float("N/A")` raised ValueError, the
# ingest-loop's generic `except Exception` swallowed it, and the file
# silently never reached transcription. Pin both _safe_num and the full
# probe_media flow so the regression is impossible to reintroduce.

def test_probe_media_safe_num_handles_na_and_none():
    """The numeric parser at the heart of the fix must coerce ffprobe's
    junk values into a fallback instead of raising."""
    from tern.audio import _safe_num
    assert _safe_num("123.45")    == 123.45
    assert _safe_num(42)          == 42.0
    assert _safe_num("N/A")       == 0.0          # the actual ffprobe value
    assert _safe_num("")          == 0.0
    assert _safe_num(None)        == 0.0
    assert _safe_num("garbage")   == 0.0
    assert _safe_num("N/A", 7.5)  == 7.5          # caller-chosen fallback
    # Negative duration shouldn't be coerced away — ffprobe never emits it
    # but if it does, the caller (probe_media) decides what to do.
    assert _safe_num("-1.0")      == -1.0


def test_probe_media_survives_na_duration(monkeypatch, tmp_path):
    """End-to-end: when ffprobe outputs `"duration":"N/A"`, probe_media
    must return duration_ms=0 instead of raising. Without this the file
    is silently dropped from indexing (worst kind of UX failure: zero
    error surface, file just never appears in /api/files)."""
    from tern import audio as _audio_mod

    class _FakeResult:
        stdout = '{"format": {"duration": "N/A", "bit_rate": "N/A", "format_name": "aac"}}'

    monkeypatch.setattr(_audio_mod.subprocess, "run",
                        lambda *a, **kw: _FakeResult())
    info = _audio_mod.probe_media(tmp_path / "fake.aac")
    assert info["duration_ms"] == 0
    assert info["bit_rate"]    == 0
    assert info["format_name"] == "aac"


def test_probe_media_happy_path(monkeypatch, tmp_path):
    """Sanity: normal ffprobe output still parses correctly. Catches an
    over-zealous fallback that would zero-out real durations too."""
    from tern import audio as _audio_mod

    class _FakeResult:
        stdout = '{"format": {"duration": "126.789", "bit_rate": "128000", "format_name": "mp3"}}'

    monkeypatch.setattr(_audio_mod.subprocess, "run",
                        lambda *a, **kw: _FakeResult())
    info = _audio_mod.probe_media(tmp_path / "real.mp3")
    assert info["duration_ms"] == 126789
    assert info["bit_rate"]    == 128000
    assert info["format_name"] == "mp3"


# ─── _download_whisper_model — three failure-mode guards ───────────────
# The runtime auto-download for the Whisper.cpp GGML model had
# the same bug class prepare_bundle.sh guards against: no HTTP status check, no min-size validation, no
# socket timeout. Customer-impact case: a transient HF 503 returns an
# HTML body, gets renamed to ggml-large-v3-turbo-q5_0.bin, and survives
# every subsequent indexing run because `model_path.exists()` is true.
# whisper-cli then crashes with cryptic "magic number mismatch".

class _FakeUrlopen:
    """Minimal stand-in for urllib.request.urlopen() return value.
    Supports the iterator protocol via read() so the download loop
    drains it the same way it drains a real HTTP response."""
    def __init__(self, body: bytes, status: int = 200):
        self._body = body
        self.status = status
        self.headers = {"Content-Length": str(len(body))}
    def getcode(self): return self.status
    def read(self, n=None):
        if n is None:
            buf, self._body = self._body, b""
            return buf
        buf, self._body = self._body[:n], self._body[n:]
        return buf
    def __enter__(self): return self
    def __exit__(self, *a): pass


def test_whisper_download_rejects_http_non_200(monkeypatch, tmp_path):
    """HF returning 503 + an HTML maintenance page must abort the
    download (not silently rename HTML to .bin)."""
    from tern import audio as _audio_mod
    fake = _FakeUrlopen(b"<html>HF is down</html>", status=503)
    monkeypatch.setattr(_audio_mod.urllib.request, "urlopen",
                        lambda *a, **kw: fake)
    target = tmp_path / "ggml-large-v3-turbo-q5_0.bin"
    with pytest.raises(RuntimeError, match="HTTP 503"):
        _audio_mod._download_whisper_model(target)
    # Critical: no leftover at the target path; the next indexing run
    # must NOT skip the download by mistakenly thinking it's complete.
    assert not target.exists()
    assert not (tmp_path / "ggml-large-v3-turbo-q5_0.bin.part").exists()


def test_whisper_download_rejects_truncated_body(monkeypatch, tmp_path):
    """urlopen returns success but the body is way under 50 MB — the
    classic mid-stream disconnect. Without the min-size guard we'd
    rename the truncated blob and ship it as a 'model'."""
    from tern import audio as _audio_mod
    # 1 MB of zeros — clearly below the 50 MB floor (and well below any
    # legitimate whisper model's actual size).
    fake = _FakeUrlopen(b"\x00" * (1 * 1024 * 1024), status=200)
    monkeypatch.setattr(_audio_mod.urllib.request, "urlopen",
                        lambda *a, **kw: fake)
    target = tmp_path / "ggml-large-v3-turbo-q5_0.bin"
    with pytest.raises(RuntimeError, match="truncated"):
        _audio_mod._download_whisper_model(target)
    assert not target.exists()
    assert not (tmp_path / "ggml-large-v3-turbo-q5_0.bin.part").exists()


def test_whisper_download_unknown_model_name_fails_loud(tmp_path):
    """The URL table is closed — an unknown filename must raise rather
    than silently fetch nothing. Catches a future model-name typo."""
    from tern import audio as _audio_mod
    target = tmp_path / "not-a-real-model.bin"
    with pytest.raises(RuntimeError, match="not known"):
        _audio_mod._download_whisper_model(target)


# ─── WhisperTranscriber._coalesce — pure-Python segment merger ──────────
# Whisper outputs many short segments (sometimes 1-2 words each on
# punctuation-rich passages). The retrieval engine searches for whole
# phrases, so we coalesce adjacent short segments into ~8s chunks before
# storing them in transcript_fts. A future
# refactor that breaks the merge boundary or drops the tail would ship
# silently. Pure unit tests — no Whisper binary needed.


def test_coalesce_empty_input_returns_empty():
    """Edge: zero segments must not crash and must return [] cleanly."""
    from tern.audio import WhisperTranscriber
    from tern.models import TranscriptSegment
    assert WhisperTranscriber._coalesce([]) == []


def test_coalesce_single_segment_preserved():
    """One segment under the target threshold survives unchanged."""
    from tern.audio import WhisperTranscriber
    from tern.models import TranscriptSegment
    seg = TranscriptSegment(file_id=1, start_ms=0, end_ms=2000, text="hello world")
    out = WhisperTranscriber._coalesce([seg])
    assert len(out) == 1
    assert out[0].text == "hello world"
    assert out[0].start_ms == 0
    assert out[0].end_ms == 2000


def test_coalesce_short_segments_merge_into_one_chunk():
    """Five 1-second segments (total 5s) all fit under the default 8s
    target and merge into a single chunk. Without this merge, the
    transcript_fts table would have 5 separate rows for one sentence
    — search ranking would split the bm25 score across rows and
    snippet generation would produce 5 tiny snippets instead of one
    contextful one."""
    from tern.audio import WhisperTranscriber
    from tern.models import TranscriptSegment
    segments = [
        TranscriptSegment(file_id=1, start_ms=i * 1000, end_ms=(i + 1) * 1000,
                          text=f"word{i}")
        for i in range(5)
    ]
    out = WhisperTranscriber._coalesce(segments)
    assert len(out) == 1, f"5 short segments should coalesce to 1 chunk; got {len(out)}"
    assert out[0].text == "word0 word1 word2 word3 word4"
    assert out[0].start_ms == 0
    assert out[0].end_ms == 5000


def test_coalesce_splits_when_target_exceeded():
    """When the running chunk would exceed target_ms, flush it and
    start a new chunk. Verify with 12 1-second segments and the
    default 8s target — should produce 2 chunks (8s + 4s, or
    similar — exact boundary depends on the > vs >= check)."""
    from tern.audio import WhisperTranscriber
    from tern.models import TranscriptSegment
    segments = [
        TranscriptSegment(file_id=1, start_ms=i * 1000, end_ms=(i + 1) * 1000,
                          text=f"w{i}")
        for i in range(12)
    ]
    out = WhisperTranscriber._coalesce(segments, target_ms=8000)
    # Must produce more than one chunk (total 12s > 8s target)
    assert len(out) >= 2, f"12s of segments should split past 8s target; got {len(out)} chunks"
    # All segments accounted for — total span covers 0..12s
    assert out[0].start_ms == 0
    assert out[-1].end_ms == 12000
    # Text is preserved across chunks (no segments dropped)
    all_words = " ".join(c.text for c in out)
    for i in range(12):
        assert f"w{i}" in all_words, f"w{i} dropped from coalesced output"


def test_coalesce_tail_segment_flushed_even_when_under_target():
    """Regression guard: the trailing partial chunk must be emitted
    even though it didn't trigger a flush via the >-target threshold.
    Without the post-loop flush, the last bit of every transcript
    would be silently dropped — last sentence of every podcast
    missing from search."""
    from tern.audio import WhisperTranscriber
    from tern.models import TranscriptSegment
    # 3 segments, 1s each, well under 8s target — never triggers
    # the >-target flush mid-loop. Only the post-loop flush emits.
    segments = [
        TranscriptSegment(file_id=1, start_ms=0, end_ms=1000, text="alpha"),
        TranscriptSegment(file_id=1, start_ms=1000, end_ms=2000, text="beta"),
        TranscriptSegment(file_id=1, start_ms=2000, end_ms=3000, text="gamma"),
    ]
    out = WhisperTranscriber._coalesce(segments)
    assert len(out) == 1, f"3 short segments under target should produce 1 chunk; got {len(out)}"
    assert "alpha" in out[0].text
    assert "beta" in out[0].text
    assert "gamma" in out[0].text, "trailing segment dropped — post-loop flush missing"


# ─── WhisperTranscriber._parse_offset — defensive numeric coercion ────
# Pins the actual behavior: "00:01:23.456" timestamp strings are not
# supported — int("00:01:23.456") raises and returns None. Tests confirm the real contract so a future "let's
# add timestamp parsing" change knows what it'd be replacing.


def test_parse_offset_none_returns_none():
    """Whisper JSON sometimes omits the offset field entirely."""
    from tern.audio import WhisperTranscriber
    assert WhisperTranscriber._parse_offset(None) is None


def test_parse_offset_numeric_types_truncate_to_int():
    """Whisper-cli emits integer ms. Floats from any JSON-library
    quirk (some Python JSON decoders prefer float) are truncated."""
    from tern.audio import WhisperTranscriber
    assert WhisperTranscriber._parse_offset(5000) == 5000
    assert WhisperTranscriber._parse_offset(5000.7) == 5000  # truncation
    assert WhisperTranscriber._parse_offset(0) == 0
    assert WhisperTranscriber._parse_offset(-1) == -1  # negative passes


def test_parse_offset_numeric_string_parsed():
    """A JSON producer that emits offsets as strings ("5000") still
    parses cleanly via int()."""
    from tern.audio import WhisperTranscriber
    assert WhisperTranscriber._parse_offset("5000") == 5000


def test_parse_offset_timestamp_string_returns_none():
    """The DOCSTRING used to claim "00:01:23.456" timestamp support
    but the implementation only does int() — which raises on colons.
    Pins the actual behavior: anything int() can't parse returns None,
    NOT a parsed timestamp. Catches a future "let's add real timestamp
    parsing" refactor — if someone implements it, this test fails and
    the implementer remembers to update the docstring + test."""
    from tern.audio import WhisperTranscriber
    assert WhisperTranscriber._parse_offset("00:01:23.456") is None
    assert WhisperTranscriber._parse_offset("00:01:23") is None
    assert WhisperTranscriber._parse_offset("not-a-number") is None
    # Float-string also fails (int("5.5") raises). Worth pinning so a
    # future float-string Whisper variant doesn't silently parse.
    assert WhisperTranscriber._parse_offset("5.5") is None


# ─── _is_in_speech — VAD region containment ──────────────────────────────
# Cheap helper that drops Whisper hallucinations on silent intros / outros.
# Two real failure modes a future refactor could silently introduce:
#   1. Inclusive vs exclusive bounds — flipping the comparison to `<` instead
#      of `<=` would silently drop segments whose midpoint exactly hits a
#      region boundary (very common: VAD regions are usually quantized to
#      window boundaries that coincide with Whisper segment starts).
#   2. Short-circuit on first match — the `for ... return True` early exit
#      is load-bearing for performance on long files (50+ regions, 5000+
#      segments). A refactor that gathered all matches first would be O(N×M)
#      instead of O(N + match-pos).
# Untested before — this batch pins both.

def test_is_in_speech_inside_single_region():
    from tern.audio import _is_in_speech
    regions = [(1000, 5000)]
    # Midpoint strictly inside
    assert _is_in_speech(3000, regions) is True
    # Both boundaries are INCLUSIVE — a segment that lands on the exact
    # start or end of a VAD window is part of speech, not noise.
    assert _is_in_speech(1000, regions) is True  # start boundary
    assert _is_in_speech(5000, regions) is True  # end boundary


def test_is_in_speech_outside_returns_false():
    from tern.audio import _is_in_speech
    regions = [(1000, 5000)]
    assert _is_in_speech(999, regions) is False   # just before start
    assert _is_in_speech(5001, regions) is False  # just after end
    assert _is_in_speech(0, regions) is False     # well before
    assert _is_in_speech(100_000, regions) is False  # well after


def test_is_in_speech_multiple_regions_short_circuits_on_first_hit():
    """The for-loop returns True on the first matching region — important
    contract for the common case where most Whisper segments fall inside
    SOME speech region and we don't need to scan all of them."""
    from tern.audio import _is_in_speech
    regions = [(0, 1000), (3000, 5000), (8000, 12_000)]
    assert _is_in_speech(500, regions) is True       # in first
    assert _is_in_speech(4000, regions) is True      # in second
    assert _is_in_speech(10_000, regions) is True    # in third
    # In the GAP between regions — not in any → False.
    assert _is_in_speech(2000, regions) is False     # silent gap
    assert _is_in_speech(6000, regions) is False     # silent gap


def test_is_in_speech_empty_regions_list_returns_false():
    """Empty regions = no speech anywhere. The for loop simply never
    iterates, so the function returns False. This is the input shape
    `_parse_whisper_json` passes when VAD found ZERO speech in the
    file — in that case Whisper's hallucinated output is dropped
    wholesale, which matches the higher-level intent."""
    from tern.audio import _is_in_speech
    assert _is_in_speech(1000, []) is False
    assert _is_in_speech(0, []) is False
