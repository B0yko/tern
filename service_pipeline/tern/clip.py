"""Clip extraction via ffmpeg."""
from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path


def _xml_escape(s: str) -> str:
    """Escape the five XML-reserved characters uniformly.

    The FCPXML writer below used to do this ad-hoc with three different
    partial escapes per call site:
      - src_attr: only `&` and `"`
      - name (stem): only `&`
      - snippet: `&`, `<`, `>`
      - project_name: NOTHING (passed straight into <event name="..."> and
        <project name="...">)
    A user with a project named `My & Sons "Show"` or a filename containing
    `<` produced malformed XML that Final Cut / DaVinci rejected as a
    corrupt import. Worse, a hostile local POST with
    `{"project_name": "x\"><event x=\""}` could inject arbitrary FCPXML
    structure into the output. One helper, applied at every interpolation
    site, closes both surfaces."""
    return (
        (s or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )


def _atomic_write_text(path: Path, text: str, *, encoding: str = "utf-8") -> None:
    """Atomic write via tempfile + fsync + os.replace.

    POSIX guarantees the target file contains EITHER the old contents OR
    the new contents — never a half-written state. Used by export_fcpxml
    so a process kill mid-write (signal, OOM, system sleep) doesn't leave
    a Final Cut user with a truncated XML that errors as "corrupt
    project" on import. Duplicated locally instead of imported from the
    api package because service_pipeline is the lower-level layer and
    must stay self-contained — same helper lives in api/main.py with
    the same docstring."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
    )
    try:
        with os.fdopen(fd, "w", encoding=encoding) as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
        tmp_path = None
    finally:
        if tmp_path and Path(tmp_path).exists():
            try: Path(tmp_path).unlink()
            except Exception: pass


def extract_clip(
    source_path: Path,
    out_path: Path,
    start_ms: int,
    end_ms: int,
    padding_ms: int = 1500,
    re_encode: bool = True,
    ffmpeg_binary: str = "ffmpeg",
) -> Path:
    """Extract a clip from source video/audio with timecode-accurate cut.

    Defaults to **re-encode = True** because stream copy snaps to the nearest
    I-frame and also extends the output to the next keyframe past `-t`, both
    of which produce wrong content for short search-result clips. Re-encoding
    with the hardware H.264 encoder costs well under a second for a typical
    5-second clip on M-series Macs, so frame accuracy is close to free.

    Args:
        source_path: Source media file.
        out_path: Output path (extension determines container).
        start_ms: Start time in ms.
        end_ms: End time in ms.
        padding_ms: Padding to add before/after for context (default 1.5s).
        re_encode: If True (default), re-encode for frame accuracy. If False,
            stream-copy (faster but snaps to keyframes and may include extra
            content past the requested end_ms).
        ffmpeg_binary: Path to ffmpeg. The API passes the bundled one; this
            used to be hardcoded to a bare "ffmpeg", which meant exports ran
            through whatever PATH resolved. On a customer Mac with no
            Homebrew that is nothing at all, so the export failed with
            ENOENT even though the app ships its own ffmpeg.
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)
    start_s = max(0, (start_ms - padding_ms) / 1000)
    end_s = (end_ms + padding_ms) / 1000
    duration_s = max(0.5, end_s - start_s)

    cmd = [ffmpeg_binary, "-y", "-hide_banner", "-loglevel", "error"]
    if re_encode:
        # OUTPUT seeking (-ss after -i): decode from start, discard until
        # start_s, then encode duration_s. Slower than input-seeking but
        # frame-accurate. We pre-seek with a coarse fast jump to within ~2s
        # of the target so we don't decode 3 minutes of video for a 5s clip.
        coarse_ss = max(0, start_s - 2)
        fine_ss = start_s - coarse_ss
        cmd.extend(["-ss", str(coarse_ss), "-i", str(source_path)])
        cmd.extend(["-ss", str(fine_ss), "-t", str(duration_s)])
        # VideoToolbox, not libx264. x264 is GPL, and shipping it inside a
        # closed-source product would put the whole work under GPL. This is
        # Apple's own H.264 encoder, licensed on every Mac we run on.
        #
        # It has no CRF mode and no x264 preset names; `-q:v` is the quality
        # knob, roughly 1-100. 75 is not a guess — measured on an 8-second
        # 1080p clip against a lossless reference:
        #
        #     x264 -crf 20 -preset fast    985 KB   49.63 dB   0.55 s
        #     h264_videotoolbox -q:v 65   2025 KB   45.83 dB
        #     h264_videotoolbox -q:v 75   3254 KB   49.65 dB   1.01 s
        #
        # So 75 buys back exactly the quality the GPL encoder gave, and costs
        # about 3x the bytes. That is the right trade here: these clips are
        # selects going onto someone's timeline to be graded and re-encoded,
        # where generation loss matters and a few MB does not.
        #
        # Note the hardware encoder is not faster on short clips — it lost to
        # x264 by roughly 2x above. It wins on CPU headroom, not wall clock.
        #
        # `-allow_sw 1` falls back to the software implementation when the
        # hardware encoder is busy, rather than failing the export outright.
        cmd.extend(["-c:v", "h264_videotoolbox", "-q:v", "75", "-allow_sw", "1"])
        cmd.extend(["-c:a", "aac", "-b:a", "192k"])
        cmd.extend(["-pix_fmt", "yuv420p"])  # broad player compatibility
    else:
        # stream copy: very fast, but may snap to nearest keyframe and run
        # past end_ms — leave this path for power users who want raw speed.
        cmd.extend(["-ss", str(start_s), "-i", str(source_path)])
        cmd.extend(["-t", str(duration_s)])
        cmd.extend(["-c", "copy", "-avoid_negative_ts", "make_zero"])

    cmd.append(str(out_path))
    # Timeout: 300s. A typical search-result clip is ~5-30s of source video
    # and re-encodes in ~1-5s on M-series Macs. 300s is the ceiling for
    # pathological cases (very-high-bitrate 4K source, slow disk). Without
    # a timeout, a hung ffmpeg (corrupted source, stalled network mount)
    # would block the export request thread indefinitely — the user's
    # Premiere/Resolve export hangs forever and they have to force-quit
    # the app. TimeoutExpired propagates so the export endpoint returns
    # a 500 the frontend can surface as "Export timed out".
    subprocess.run(cmd, check=True, capture_output=True, timeout=300)
    return out_path


def extract_audio_clip(
    source_path: Path,
    out_path: Path,
    start_ms: int,
    end_ms: int,
    padding_ms: int = 1500,
    ffmpeg_binary: str = "ffmpeg",
) -> Path:
    """Extract audio-only clip as MP3 (for podcast clip-sharing)."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    start_s = max(0, (start_ms - padding_ms) / 1000)
    end_s = (end_ms + padding_ms) / 1000
    duration_s = max(0.5, end_s - start_s)

    cmd = [
        ffmpeg_binary, "-y", "-hide_banner", "-loglevel", "error",
        "-ss", str(start_s),
        "-i", str(source_path),
        "-t", str(duration_s),
        "-vn",
        "-c:a", "libmp3lame",
        "-b:a", "192k",
        str(out_path),
    ]
    # Timeout: 120s. Audio extraction is much faster than video (no decode
    # of frame data), but a hung ffmpeg on a corrupted source could still
    # block forever. 120s ceiling is generous for the longest podcast
    # clip we'd ever ship. TimeoutExpired propagates to /api/clip/audio.
    subprocess.run(cmd, check=True, capture_output=True, timeout=120)
    return out_path


def export_fcpxml(
    hits: list,
    project_name: str,
    out_path: Path,
    fps: int | None = None,
) -> Path:
    """Generate FCPXML for Premiere/Resolve/Final Cut from a list of search hits.

    The sequence format is derived from the FIRST asset's actual resolution and
    frame rate (ffprobe), not hard-coded to 1080p30 — that mismatch made
    Final Cut Pro complain and DaVinci Resolve rescale every clip on import.
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)
    from .audio import probe_media

    # Group hits by file
    by_file: dict[str, list] = {}
    for hit in hits:
        by_file.setdefault(hit.file_path, []).append(hit)

    # Empty exports are a degenerate-but-valid case: write a structurally
    # valid FCPXML with no assets so importing apps don't blow up. Callers
    # that want a hard-fail can check `len(hits) == 0` themselves.
    if not by_file:
        width, height, fps_int = 1920, 1080, int(round(fps or 30))
    else:
        # Probe the first asset's actual dimensions + frame rate so the sequence
        # matches the source. Fallback to 1080p30 if probe fails.
        first_path = next(iter(by_file))
        width, height, src_fps = _probe_video_format(Path(first_path))
        if fps is None:
            fps = src_fps or 30
        fps_int = int(round(fps))

    assets_xml = []
    asset_id_counter = 1
    asset_map: dict[str, int] = {}

    for file_path in by_file:
        asset_id = asset_id_counter
        asset_map[file_path] = asset_id
        info = probe_media(Path(file_path))
        duration_s = info["duration_ms"] / 1000.0
        # Uniformly XML-escape: src (file URL path), name (stem). Both
        # land inside attribute values so all five reserved chars must
        # go through the same escape — a filename like `<bad>.mp4` or
        # `Sam & Co's "intro".mp4` would have produced malformed XML
        # under the prior partial-escape (`&` + `"` only).
        src_attr = _xml_escape(file_path)
        name = _xml_escape(Path(file_path).stem)
        assets_xml.append(
            f'    <asset id="r{asset_id}" name="{name}" '
            f'src="file://{src_attr}" duration="{duration_s}s" '
            f'hasVideo="1" hasAudio="1" format="r0"/>'
        )
        asset_id_counter += 1

    clips_xml = []
    offset_s = 0.0
    for file_path, file_hits in by_file.items():
        for hit in file_hits:
            asset_id = asset_map[file_path]
            start_s = hit.ts_ms / 1000.0
            dur_s = max(2.0, hit.duration_ms / 1000.0)
            # Snippet may carry FTS <mark>…</mark> from the search result —
            # strip the highlight tags BEFORE escaping (otherwise the
            # editor sees "&lt;mark&gt;word&lt;/mark&gt;" literal text).
            raw = (hit.snippet or "")
            raw = raw.replace("<mark>", "").replace("</mark>", "")
            snippet = _xml_escape(raw)
            # TRUNCATE FIRST, ESCAPE SECOND for the name="…" and
            # marker value="…" attributes. Pre-fix the slice ran on the
            # already-escaped string, so a hit with `&` at position ~36
            # would produce `Sam &amp` instead of `Sam &amp;` after
            # `snippet[:40]` — mid-entity truncation, which Final Cut
            # and DaVinci both reject with "corrupt FCPXML, missing ';'
            # in entity reference" and refuse to import the whole project.
            # By truncating the un-escaped raw text first, every char in
            # the slice maps to a complete entity once escaped — no
            # partial `&am` can ever appear in the output.
            #
            # The visible name in FCP/Resolve may be 1-4 chars shorter
            # than the prior code path produced (since `&` now becomes
            # `&amp;` and consumes 5 chars of the budget in the output
            # XML byte stream) — that's an acceptable display tradeoff
            # for correctness. The 40-char cap was about display length
            # in the FCP timeline anyway, not output byte budget.
            name = _xml_escape(raw[:40]) or "clip"
            # NEW: add a <marker> at the start of every clip so the matched
            # moment appears as a coloured marker on the FCP / DaVinci
            # timeline with its quote next to it. Editors browsing the
            # imported FCPXML see "pricing strategy", "Series A funding",
            # etc. on the timeline directly — no need to alt-tab back to
            # Tern to remember WHY each clip is here. The marker `value`
            # is also what shows in the FCP markers list; we prefix with
            # the source kind ("speech:" / "ocr:" / "visual:") so the
            # editor can filter by where the hit came from. <note> stays
            # for the longer snippet body (visible in the inspector).
            source_prefix = {
                "transcript": "speech: ",
                "ocr": "on-screen: ",
                "visual": "visual: ",
            }.get(getattr(hit, "source", ""), "")
            # Same truncate-then-escape pattern as `name` above to
            # avoid the mid-entity truncation bug. source_prefix is
            # plain ASCII so concatenation with raw preserves the
            # property that every position in the slice maps to a
            # complete entity once escaped.
            marker_value = _xml_escape((source_prefix + raw)[:120]) or "match"
            clips_xml.append(
                f'            <asset-clip ref="r{asset_id}" offset="{offset_s}s" '
                f'start="{start_s}s" duration="{dur_s}s" name="{name}">'
                f'<marker start="{start_s}s" duration="1/{fps_int}s" value="{marker_value}"/>'
                f'<note>{snippet}</note>'
                f'</asset-clip>'
            )
            offset_s += dur_s

    # project_name was previously interpolated RAW — a user POST with
    # `{"project_name": "Sam & Co \"Show\""}` produced malformed XML
    # that Final Cut / DaVinci rejected as a corrupt import. Worse, a
    # hostile POST with `"x\"><event x=\""` would inject arbitrary
    # FCPXML structure. Escape here, ONCE, just like every other
    # interpolated attribute string above.
    project_name_xml = _xml_escape(project_name)
    fcpxml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE fcpxml>\n'
        '<fcpxml version="1.10">\n'
        '  <resources>\n'
        f'    <format id="r0" name="FFVideoFormat{height}p{fps_int}" '
        f'frameDuration="1/{fps_int}s" width="{width}" height="{height}"/>\n'
        + "\n".join(assets_xml) + "\n"
        '  </resources>\n'
        '  <library>\n'
        f'    <event name="{project_name_xml}">\n'
        f'      <project name="{project_name_xml}">\n'
        '        <sequence format="r0">\n'
        '          <spine>\n'
        + "\n".join(clips_xml) + "\n"
        '          </spine>\n'
        '        </sequence>\n'
        '      </project>\n'
        '    </event>\n'
        '  </library>\n'
        '</fcpxml>\n'
    )
    _atomic_write_text(out_path, fcpxml)
    return out_path


def _probe_video_format(path: Path) -> tuple[int, int, int]:
    """Return (width, height, fps_int) from ffprobe. (1920, 1080, 30) on failure.

    Timeout: 30s. Same reasoning as probe_media — a stalled mount or
    corrupted source must not hang the FCPXML export forever. The
    `except Exception` block below catches TimeoutExpired naturally and
    returns the safe 1080p30 fallback, so the export still ships."""
    try:
        out = subprocess.run(
            [
                "ffprobe", "-v", "error", "-select_streams", "v:0",
                "-show_entries", "stream=width,height,r_frame_rate",
                "-of", "default=noprint_wrappers=1:nokey=1",
                str(path),
            ],
            capture_output=True, text=True, check=True, timeout=30,
        ).stdout.strip().splitlines()
        width = int(out[0]) if len(out) > 0 else 1920
        height = int(out[1]) if len(out) > 1 else 1080
        # r_frame_rate is "30/1" or "30000/1001" — eval safely
        if len(out) > 2 and "/" in out[2]:
            num, den = out[2].split("/")
            fps = max(1, round(float(num) / float(den)))
        else:
            fps = 30
        return width, height, fps
    except Exception:
        return 1920, 1080, 30
