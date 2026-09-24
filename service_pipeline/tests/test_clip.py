"""Tests for tern.clip module (clip extraction + FCPXML export)."""
from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

import pytest

from tern.clip import export_fcpxml, extract_audio_clip, extract_clip
from tern.models import SearchHit


# ─── extract_clip / extract_audio_clip arithmetic edges ────────────────
# These don't invoke ffmpeg. They monkeypatch subprocess.run to a capturing
# stub so we can assert the COMMAND that gets built — pin the padding /
# clamping / duration math against regressions without paying the (slow,
# flaky, platform-dependent) cost of a real ffmpeg invocation. End-to-end
# behavior is covered by qa_smoke.

@pytest.fixture
def fake_ffmpeg(monkeypatch, tmp_path):
    """Replace subprocess.run with a capture stub + touch the output path
    so the post-call .stat() doesn't fail. Returns the list of captured
    argv-arrays."""
    captured: list[list[str]] = []
    def _fake_run(cmd, check=True, capture_output=False, **kw):
        captured.append(list(cmd))
        out = Path(cmd[-1])
        out.parent.mkdir(parents=True, exist_ok=True)
        out.touch()
        class _R:
            returncode = 0
        return _R()
    monkeypatch.setattr(subprocess, "run", _fake_run)
    return captured


def _last_arg_after(cmd: list[str], flag: str) -> str | None:
    """Last value following `flag` in cmd. extract_clip emits -ss twice
    (coarse pre-seek then fine seek); we want the second one."""
    last = None
    for i, tok in enumerate(cmd):
        if tok == flag and i + 1 < len(cmd):
            last = cmd[i + 1]
    return last


def test_extract_clip_normal_range(fake_ffmpeg, tmp_path):
    src = tmp_path / "src.mp4"; src.touch()
    out = tmp_path / "out.mp4"
    extract_clip(src, out, start_ms=10_000, end_ms=15_000, padding_ms=1500)
    cmd = fake_ffmpeg[0]
    # padding 1500ms: start_s=8.5, end_s=16.5, duration=8.0
    assert _last_arg_after(cmd, "-t") == "8.0", f"duration wrong: {cmd}"
    # Re-encode mode is signalled by an explicit video encoder rather than
    # `-c copy`. Asserting on the encoder NAME here would just duplicate
    # test_video_clip_encodes_with_videotoolbox, and it is what made this
    # test fail when the GPL encoder was swapped out.
    assert "-c:v" in cmd, "expected re-encode mode by default"
    assert "copy" not in cmd


def test_extract_clip_start_negative_clamps_to_zero(fake_ffmpeg, tmp_path):
    """start_ms - padding_ms goes negative → start_s must clamp to 0,
    NOT pass a negative -ss to ffmpeg (which would error 'invalid time')."""
    src = tmp_path / "src.mp4"; src.touch()
    out = tmp_path / "out.mp4"
    # start_ms=500, padding=1500 → start_s = -1.0 raw → clamp to 0
    extract_clip(src, out, start_ms=500, end_ms=4500, padding_ms=1500)
    cmd = fake_ffmpeg[0]
    # All -ss values must be ≥ 0 (clamp guard).
    for i, tok in enumerate(cmd):
        if tok == "-ss" and i + 1 < len(cmd):
            assert float(cmd[i + 1]) >= 0, f"negative -ss leaked: {cmd[i + 1]}"


def test_extract_clip_end_before_start_yields_minimum_duration(fake_ffmpeg, tmp_path):
    """Degenerate input: end_ms < start_ms. Raw duration would be
    negative; the function must clamp to 0.5s minimum rather than
    emit a negative -t (ffmpeg errors '0 duration')."""
    src = tmp_path / "src.mp4"; src.touch()
    out = tmp_path / "out.mp4"
    extract_clip(src, out, start_ms=10_000, end_ms=5_000, padding_ms=0)
    cmd = fake_ffmpeg[0]
    dur = float(_last_arg_after(cmd, "-t"))
    assert dur >= 0.5, f"duration must clamp to 0.5s minimum, got {dur}"


def test_extract_audio_clip_uses_libmp3lame_and_strips_video(fake_ffmpeg, tmp_path):
    """Audio extraction must produce MP3 (libmp3lame) and explicitly
    drop video (-vn). Without -vn ffmpeg may copy the source container's
    video atoms into the 'audio' output for some codecs."""
    src = tmp_path / "src.mp4"; src.touch()
    out = tmp_path / "out.mp3"
    extract_audio_clip(src, out, start_ms=5_000, end_ms=10_000, padding_ms=1500)
    cmd = fake_ffmpeg[0]
    assert "libmp3lame" in cmd, f"audio must use libmp3lame: {cmd}"
    assert "-vn" in cmd, f"missing -vn would leak video stream: {cmd}"
    assert "-b:a" in cmd and cmd[cmd.index("-b:a") + 1] == "192k"


def test_export_fcpxml_empty():
    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "empty.fcpxml"
        result = export_fcpxml(hits=[], project_name="Test", out_path=out)
        assert result.exists()
        content = result.read_text()
        assert "<fcpxml" in content
        assert "<spine>" in content


@pytest.mark.needs_tools("say", "ffmpeg", "ffprobe")
def test_export_fcpxml_with_hits():
    with tempfile.TemporaryDirectory() as td:
        # Create a tiny test audio so probe_media succeeds
        import subprocess
        test_audio = Path(td) / "test.wav"
        # Use macOS `say` to make a real audio file (5 sec)
        subprocess.run(["say", "test", "-o", str(test_audio.with_suffix(".aiff"))], check=False)
        if test_audio.with_suffix(".aiff").exists():
            subprocess.run(["ffmpeg", "-y", "-i", str(test_audio.with_suffix(".aiff")),
                          str(test_audio)], capture_output=True, check=False)

        if not test_audio.exists():
            pytest.skip("Could not generate test audio for FCPXML test")

        out = Path(td) / "test.fcpxml"
        hits = [
            SearchHit(
                file_id=1,
                file_path=str(test_audio),
                ts_ms=1000,
                duration_ms=3000,
                snippet="test snippet 1",
                source="transcript",
                score=0.9,
            ),
            SearchHit(
                file_id=1,
                file_path=str(test_audio),
                ts_ms=5000,
                duration_ms=2000,
                snippet="test snippet 2",
                source="transcript",
                score=0.85,
            ),
        ]
        result = export_fcpxml(hits=hits, project_name="Test Project", out_path=out)
        assert result.exists()
        content = result.read_text()
        assert "Test Project" in content
        assert "<asset" in content
        assert "<asset-clip" in content
        assert "test snippet 1" in content


@pytest.mark.needs_tools("say", "ffmpeg", "ffprobe")
def test_export_fcpxml_escapes_xml_chars():
    """Make sure snippets with <, >, & get properly escaped."""
    with tempfile.TemporaryDirectory() as td:
        import subprocess
        test_audio = Path(td) / "test.wav"
        subprocess.run(["say", "test", "-o", str(test_audio.with_suffix(".aiff"))], check=False)
        if test_audio.with_suffix(".aiff").exists():
            subprocess.run(["ffmpeg", "-y", "-i", str(test_audio.with_suffix(".aiff")),
                          str(test_audio)], capture_output=True, check=False)

        if not test_audio.exists():
            pytest.skip("Could not generate test audio")

        out = Path(td) / "escape.fcpxml"
        hits = [
            SearchHit(
                file_id=1, file_path=str(test_audio),
                ts_ms=0, duration_ms=2000,
                snippet="contains <mark>highlighted</mark> & special chars",
                source="transcript", score=0.9,
            )
        ]
        export_fcpxml(hits=hits, project_name="Escape Test", out_path=out)
        assert out.exists()
        body = out.read_text()
        # <mark>…</mark> stripped (highlight tags shouldn't appear as
        # literal text in the editor's notes column).
        assert "<mark>" not in body and "</mark>" not in body
        # & became &amp; — guards against malformed XML rejecting the
        # whole project on FCP import.
        assert "&amp;" in body
        # XML must actually parse — catches a future bug where we
        # accidentally emit invalid markup.
        import xml.etree.ElementTree as ET
        ET.fromstring(body)


@pytest.mark.needs_tools("say", "ffmpeg", "ffprobe")
def test_export_fcpxml_adds_timeline_markers_with_source_prefix():
    """Each clip must carry a <marker> with the snippet + a
    `speech:` / `on-screen:` / `visual:` prefix so the editor sees the
    matched quote and its source ON the FCP / DaVinci timeline (not
    just in the inspector's notes column)."""
    with tempfile.TemporaryDirectory() as td:
        import subprocess
        test_audio = Path(td) / "test.wav"
        subprocess.run(["say", "test", "-o", str(test_audio.with_suffix(".aiff"))], check=False)
        if test_audio.with_suffix(".aiff").exists():
            subprocess.run(["ffmpeg", "-y", "-i", str(test_audio.with_suffix(".aiff")),
                          str(test_audio)], capture_output=True, check=False)
        if not test_audio.exists():
            pytest.skip("Could not generate test audio")

        out = Path(td) / "markers.fcpxml"
        hits = [
            SearchHit(file_id=1, file_path=str(test_audio), ts_ms=1000,
                      duration_ms=2000, snippet="pricing strategy",
                      source="transcript", score=0.9),
            SearchHit(file_id=1, file_path=str(test_audio), ts_ms=4000,
                      duration_ms=2000, snippet="Stanford University",
                      source="ocr", score=0.8),
            SearchHit(file_id=1, file_path=str(test_audio), ts_ms=7000,
                      duration_ms=2000, snippet="orange cat",
                      source="visual", score=0.7),
        ]
        export_fcpxml(hits=hits, project_name="Markers Test", out_path=out)
        body = out.read_text()
        # One <marker> per hit.
        assert body.count("<marker") == 3, (
            f"expected 3 markers (one per hit), got {body.count('<marker')}"
        )
        # Each source's prefix appears at least once.
        assert "speech: pricing strategy" in body
        assert "on-screen: Stanford University" in body
        assert "visual: orange cat" in body
        # XML parses cleanly.
        import xml.etree.ElementTree as ET
        ET.fromstring(body)


def test_export_fcpxml_escapes_project_name(tmp_path):
    """project_name lands inside <event name="..."> AND <project name="...">.
    Pre-fix it was interpolated RAW — a user POST with a quote/ampersand
    in the title produced malformed XML that Final Cut / DaVinci rejected
    on import; a hostile POST like `"x\\">malicious<event x=\\""` could
    inject arbitrary structure. Verify all 5 XML-reserved chars round-trip
    safely via the new _xml_escape helper."""
    import xml.etree.ElementTree as ET
    out = tmp_path / "evil_name.fcpxml"
    nasty = 'Sam & Co\'s "Hot Take" <intro>'
    export_fcpxml(hits=[], project_name=nasty, out_path=out)
    body = out.read_text()
    # MUST parse — that's the headline contract that import-into-Final-Cut depends on
    tree = ET.fromstring(body)
    # AND the round-tripped value must equal the original (escape was reversible)
    event = tree.find(".//{*}event") or tree.find(".//event")
    assert event is not None, "no <event> in output"
    assert event.get("name") == nasty, (
        f"project_name round-trip failed: stored={event.get('name')!r} vs input={nasty!r}"
    )


def test_export_fcpxml_escapes_filename_with_special_chars(tmp_path, monkeypatch):
    """File paths with `<`, `>`, `&`, `"`, or `'` (rare but legal on macOS)
    must round-trip safely through both <asset src="..."> and <asset
    name="..."> attributes. Pre-fix, only `&` + `"` were escaped — a
    file named `<bad>.mp4` would crash the XML parser on import.

    Monkeypatches probe_media because export_fcpxml shells out to ffprobe
    to discover duration; the synthetic path doesn't exist on disk."""
    import xml.etree.ElementTree as ET
    from tern import audio as _audio_mod
    monkeypatch.setattr(_audio_mod, "probe_media",
                        lambda p: {"duration_ms": 60_000, "bit_rate": 0, "format_name": "mp4"})

    out = tmp_path / "weird_path.fcpxml"
    weird = "/tmp/Sam & <Co>'s \"podcast\".mp4"
    class _FakeHit:
        file_path = weird
        ts_ms = 0
        duration_ms = 2000
        snippet = "test"
        source = "visual"
    export_fcpxml(hits=[_FakeHit()], project_name="x", out_path=out)
    body = out.read_text()
    # Must parse — that's the contract that FCP/DaVinci import depends on
    tree = ET.fromstring(body)
    # Asset's src attribute should round-trip the full path (the parser
    # unescapes &amp; / &lt; / etc back to literals)
    asset = tree.find(".//{*}asset") or tree.find(".//asset")
    assert asset is not None
    src = asset.get("src", "")
    assert src.endswith(weird), f"src round-trip failed: {src!r} should end with {weird!r}"


def test_export_fcpxml_no_mid_entity_truncation_in_name_attribute(tmp_path, monkeypatch):
    """Regression: `name = snippet[:40]` truncated the ALREADY-escaped
    snippet, so a snippet with `&` at position ~36 would slice mid-entity
    and emit `name="aaa…aaa&am"` — Final Cut and DaVinci both refuse to
    import any FCPXML that contains a malformed entity reference in an
    attribute value (the error message is "corrupt FCPXML, missing ';'
    in entity reference"; the whole project fails to load).

    Fix: truncate the raw (un-escaped) text first, then escape — every
    char in the slice maps to a complete entity once escaped, so no
    partial `&am` can appear in the output.

    Craft the input so that the 40-char post-escape boundary lands
    EXACTLY at the start of an `&amp;` entity: 39 plain chars + `&`.
    Pre-fix `name[39]` was `&` and got cut alone; post-fix the raw
    text is truncated first so `&` becomes a complete `&amp;`."""
    import xml.etree.ElementTree as ET
    from tern import audio as _audio_mod
    monkeypatch.setattr(_audio_mod, "probe_media",
                        lambda p: {"duration_ms": 60_000, "bit_rate": 0, "format_name": "mp4"})

    out = tmp_path / "midcut.fcpxml"
    # 39 plain chars + "&" + tail. Pre-fix the post-escape slice at 40
    # would land "aaa…aaa&" (40 chars, bare `&`); post-fix the pre-escape
    # slice at 40 lands "aaa…aaa&" then escapes to "aaa…aaa&amp;" (44).
    cruel = ("a" * 39) + "&" + "trailing context that gets dropped"

    class _FakeHit:
        file_path = "/tmp/x.mp4"
        ts_ms = 0
        duration_ms = 2000
        snippet = cruel
        source = "transcript"

    export_fcpxml(hits=[_FakeHit()], project_name="midcut", out_path=out)
    body = out.read_text()

    # Headline contract: the whole document must parse. Pre-fix, the
    # snippet[:40] slice cut INSIDE the `&amp;` entity at the boundary,
    # leaving `name="aaa…aaa&"` (40 chars: 39 'a' + bare `&`) in the
    # raw XML body. ET.fromstring would raise ParseError("not well-
    # formed (invalid token)") because a bare `&` is illegal in an
    # XML attribute value — it MUST start an entity reference.
    # Post-fix, the raw text is truncated first then escaped, so the
    # `&` becomes a complete `&amp;` in the output and the document
    # parses cleanly.
    tree = ET.fromstring(body)

    # Drill into the parsed attribute to confirm the `&` from the input
    # actually round-tripped (ElementTree unescapes `&amp;` back to `&`
    # when reading attribute values). If the truncate-then-escape path
    # regressed to truncate-after-escape, the body still wouldn't parse
    # at all so we'd never reach this assertion — but pin it anyway as
    # a positive-existence check on the boundary char.
    clip = tree.find(".//{*}asset-clip") or tree.find(".//asset-clip")
    assert clip is not None, f"no <asset-clip> in output: {body[:400]}"
    name = clip.get("name", "")
    assert name.endswith("&"), (
        f"the `&` at position 39 of the raw snippet should survive "
        f"truncate-then-escape and end up as the last char of the "
        f"parsed attribute (ElementTree unescapes `&amp;` to `&`); "
        f"got: {name!r}"
    )

    # Defense-in-depth: scan the RAW XML body bytes for the specific
    # pre-fix failure signature `&am"` — a `&` followed by `am` then
    # the attribute-closing quote (i.e., the `&amp;` entity got chopped
    # at position 4). A pure ET.fromstring assertion would catch any
    # malformed entity, but this targeted byte check makes a future
    # regression's failure message immediately legible.
    assert '&am"' not in body, (
        f"FCPXML body contains a truncated `&am\"` (mid-entity cut) — "
        f"the truncate-then-escape fix has regressed. Body sample: "
        f"...{body[max(0, body.find('&am'))-20:body.find('&am')+20]}..."
    )


def test_export_fcpxml_no_mid_entity_truncation_in_marker_value(tmp_path, monkeypatch):
    """Same mid-entity truncation bug existed for the marker `value=`
    attribute: pre-fix `marker_value = (source_prefix + snippet)[:120]`
    sliced after escaping, so a long snippet with `&` near the 120
    boundary produced `value="speech: …&am"`. Pin the same parse-and-
    boundary-char contract here as for `name`."""
    import xml.etree.ElementTree as ET
    from tern import audio as _audio_mod
    monkeypatch.setattr(_audio_mod, "probe_media",
                        lambda p: {"duration_ms": 60_000, "bit_rate": 0, "format_name": "mp4"})

    out = tmp_path / "midcut_marker.fcpxml"
    # "speech: " prefix is 8 chars. We want the `&` to be the LAST char
    # of (prefix + raw)[:120] — i.e. at position 119. 119 - 8 = 111
    # plain chars before the `&`. (Pre-fix the slice ran AFTER escaping,
    # so the `&` would be at position 118-122 of the escaped form and
    # the slice would cut mid-entity around `&am|p;`.)
    cruel = ("b" * 111) + "&" + "trailing dropped tail goes here forever"

    class _FakeHit:
        file_path = "/tmp/x.mp4"
        ts_ms = 0
        duration_ms = 2000
        snippet = cruel
        source = "transcript"

    export_fcpxml(hits=[_FakeHit()], project_name="midcut", out_path=out)
    body = out.read_text()
    # Headline: the document must parse. See the matching name-attr
    # test for the full failure-mode write-up.
    tree = ET.fromstring(body)
    marker = tree.find(".//{*}marker") or tree.find(".//marker")
    assert marker is not None, f"no <marker> in output: {body[:400]}"
    value = marker.get("value", "")
    # Parsed value: 8-char "speech: " prefix + 112 'b's + literal `&`
    # (post-fix). ElementTree unescaped `&amp;` to `&`.
    assert value.endswith("&"), (
        f"the `&` at position 120 of (prefix + raw) should survive "
        f"truncate-then-escape and end up as the last char of value; "
        f"got: {value!r}"
    )
    assert '&am"' not in body, (
        f"FCPXML body contains a truncated `&am\"` (mid-entity cut) — "
        f"truncate-then-escape regressed on marker value."
    )


# ─── _xml_escape — direct unit tests ─────────────────────────────────────
# The helper is reusable beyond export_fcpxml (any future code emitting
# XML attributes / text). The existing FCPXML tests exercise it indirectly
# through full ffmpeg-backed end-to-end runs; these are sub-ms unit checks
# that pin the per-character contract so a future "simplify by dropping the
# apostrophe escape" can't sneak through without a loud failure.


def test_xml_escape_handles_all_five_reserved_chars():
    """The five XML-reserved chars are & < > " '. All must be escaped
    to their canonical entity forms."""
    from tern.clip import _xml_escape
    # Independent characters — each must produce its own entity
    assert _xml_escape("&") == "&amp;"
    assert _xml_escape("<") == "&lt;"
    assert _xml_escape(">") == "&gt;"
    assert _xml_escape('"') == "&quot;"
    assert _xml_escape("'") == "&apos;"


def test_xml_escape_ordering_is_safe_for_ampersand():
    """`&` must be escaped FIRST so it doesn't double-escape the entity
    output of the other replacements (`&lt;` would become `&amp;lt;`
    if `<` were processed before `&`). Pin the order by feeding mixed
    input and checking the result has no `&amp;l` / `&amp;g` artifacts."""
    from tern.clip import _xml_escape
    assert _xml_escape("a<b&c>d") == "a&lt;b&amp;c&gt;d"
    # The DOUBLE-escape failure mode would produce `a&amp;lt;b&amp;amp;c&amp;gt;d`
    assert "&amp;lt;" not in _xml_escape("<&>")
    assert "&amp;gt;" not in _xml_escape("<&>")


def test_xml_escape_preserves_safe_chars():
    """Plain ASCII letters, digits, spaces, unicode must pass through
    unchanged — escape is for the FIVE reserved chars only."""
    from tern.clip import _xml_escape
    s = "Sam Altman 0.1.2 — поиск 한글 😀"
    assert _xml_escape(s) == s


def test_xml_escape_handles_none_and_empty():
    """Edge: None / empty input must return empty string (not crash on
    `None.replace(...)`). The `(s or "")` guard at the top of the helper
    is what enables this — `_xml_escape(None)` should be safe."""
    from tern.clip import _xml_escape
    assert _xml_escape(None) == ""
    assert _xml_escape("") == ""


def test_xml_escape_round_trips_through_etree_parser():
    """End-to-end safety: feed every reserved char through escape →
    embed in an XML attribute → parse with ElementTree → must round-trip
    back to the original string. Catches a future regression that
    escapes correctly but uses non-canonical entity names a stdlib
    parser doesn't recognise."""
    from tern.clip import _xml_escape
    import xml.etree.ElementTree as ET
    payload = "& < > \" ' all five at once"
    xml = f'<x name="{_xml_escape(payload)}"/>'
    tree = ET.fromstring(xml)
    assert tree.get("name") == payload


# ─── subprocess timeout pinning ──────────────────────────────────────────
# Pre-fix, extract_clip / extract_audio_clip / _probe_video_format all
# called ffmpeg / ffprobe with NO timeout. A hung subprocess (corrupted
# source, stalled network mount, killed-but-zombie ffmpeg child) would
# block the request thread forever — the user's export request just
# never returns and the toast spins indefinitely. These tests pin the
# ceiling values so a refactor can't silently drop them.


def test_extract_clip_passes_300s_timeout_to_subprocess(tmp_path, monkeypatch):
    """extract_clip must invoke subprocess.run with timeout=300. Captures
    the call kwargs and asserts. A future refactor that removes the
    timeout (or sets a tiny value that breaks legitimate large clips)
    must fail loudly."""
    captured = {}
    def _fake_run(cmd, **kw):
        captured.update(kw)
        Path(cmd[-1]).touch()
        class _R:
            returncode = 0
        return _R()
    monkeypatch.setattr(subprocess, "run", _fake_run)
    src = tmp_path / "src.mp4"; src.touch()
    out = tmp_path / "out.mp4"
    extract_clip(src, out, start_ms=1000, end_ms=5000)
    assert captured.get("timeout") == 300, (
        f"extract_clip must pass timeout=300 to subprocess.run, got {captured.get('timeout')!r}"
    )


def test_extract_audio_clip_passes_120s_timeout_to_subprocess(tmp_path, monkeypatch):
    """extract_audio_clip must invoke subprocess.run with timeout=120.
    Same hang-prevention contract as extract_clip; audio is faster so
    the ceiling is lower."""
    captured = {}
    def _fake_run(cmd, **kw):
        captured.update(kw)
        Path(cmd[-1]).touch()
        class _R:
            returncode = 0
        return _R()
    monkeypatch.setattr(subprocess, "run", _fake_run)
    src = tmp_path / "src.mp3"; src.touch()
    out = tmp_path / "out.mp3"
    extract_audio_clip(src, out, start_ms=1000, end_ms=5000)
    assert captured.get("timeout") == 120, (
        f"extract_audio_clip must pass timeout=120 to subprocess.run, got {captured.get('timeout')!r}"
    )


def test_extract_clip_propagates_timeout_expired(tmp_path, monkeypatch):
    """When ffmpeg hangs past the 300s ceiling, subprocess.run raises
    TimeoutExpired. extract_clip must let that propagate — the export
    endpoint catches it as a 500 the frontend surfaces as 'Export timed
    out'. A `try: ... except: pass` regression here would silently
    return an empty output file the caller treats as success."""
    def _fake_run(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, kw.get("timeout", 300))
    monkeypatch.setattr(subprocess, "run", _fake_run)
    src = tmp_path / "src.mp4"; src.touch()
    out = tmp_path / "out.mp4"
    with pytest.raises(subprocess.TimeoutExpired):
        extract_clip(src, out, start_ms=1000, end_ms=5000)


def test_probe_video_format_swallows_timeout_returns_default(tmp_path, monkeypatch):
    """_probe_video_format wraps ffprobe in a try/except. A TimeoutExpired
    must be caught (by the broad `except Exception`) and return the safe
    (1920, 1080, 30) fallback — that way FCPXML export still ships even
    if the first asset's probe hangs. Without this, a hung ffprobe at
    line 281 would propagate up and crash the export endpoint."""
    from tern.clip import _probe_video_format
    def _fake_run(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, kw.get("timeout", 30))
    monkeypatch.setattr(subprocess, "run", _fake_run)
    src = tmp_path / "src.mp4"; src.touch()
    result = _probe_video_format(src)
    assert result == (1920, 1080, 30), (
        f"_probe_video_format must return (1920, 1080, 30) on TimeoutExpired, got {result!r}"
    )


def test_probe_media_passes_30s_timeout_to_subprocess(tmp_path, monkeypatch):
    """probe_media (audio.py) must pass timeout=30 to ffprobe. ffprobe
    normally completes in <1s but a stalled network mount could hang it
    forever — every call site (ingest, FCPXML export, /api/files
    metadata) would hang with it. Pin the ceiling."""
    from tern import audio as _audio_mod
    captured = {}
    def _fake_run(cmd, **kw):
        captured.update(kw)
        class _R:
            returncode = 0
            stdout = '{"format": {"duration": "10.0", "bit_rate": "128000", "format_name": "mp3"}}'
        return _R()
    monkeypatch.setattr(_audio_mod.subprocess, "run", _fake_run)
    src = tmp_path / "src.mp3"; src.touch()
    _audio_mod.probe_media(src)
    assert captured.get("timeout") == 30, (
        f"probe_media must pass timeout=30 to subprocess.run, got {captured.get('timeout')!r}"
    )


# ─── licence-safe encoding ──────────────────────────────────────────────
# The bundled ffmpeg is LGPL, which means no libx264 and no libx265: those
# are GPL, and shipping them inside a closed-source product would oblige us
# to publish this source to every customer. H.264 encoding comes from
# VideoToolbox instead, which is Apple's own licensed encoder.
#
# These pin the encoder choice at the command level, because the failure
# mode is silent and expensive: a build that "works on my Mac" against a
# Homebrew ffmpeg, and a licence violation the moment it ships.

GPL_ENCODERS = ("libx264", "libx265", "libxvid")


def test_video_clip_uses_no_gpl_encoder(fake_ffmpeg, tmp_path):
    extract_clip(Path("src.mp4"), tmp_path / "out.mp4", 10_000, 14_000)
    cmd = fake_ffmpeg[0]
    for enc in GPL_ENCODERS:
        assert enc not in cmd, (
            f"{enc} is GPL. Shipping it inside a proprietary app forces the "
            f"whole work under GPL. Use an LGPL-safe encoder."
        )


def test_video_clip_encodes_with_videotoolbox(fake_ffmpeg, tmp_path):
    extract_clip(Path("src.mp4"), tmp_path / "out.mp4", 10_000, 14_000)
    cmd = fake_ffmpeg[0]
    assert "-c:v" in cmd
    assert cmd[cmd.index("-c:v") + 1] == "h264_videotoolbox"


def test_video_clip_does_not_pass_crf_to_videotoolbox(fake_ffmpeg, tmp_path):
    """VideoToolbox has no CRF mode. Passing -crf makes ffmpeg reject the
    whole command, so the export fails for every customer rather than
    quietly producing a worse file."""
    extract_clip(Path("src.mp4"), tmp_path / "out.mp4", 10_000, 14_000)
    cmd = fake_ffmpeg[0]
    assert "-crf" not in cmd
    assert "-preset" not in cmd, "x264 preset names are meaningless to VideoToolbox"


def test_video_clip_still_sets_a_quality_target(fake_ffmpeg, tmp_path):
    """VideoToolbox is markedly less efficient than x264: measured on an 8s
    1080p clip, q:v 65 gave 45.8 dB where x264 crf 20 gave 49.6 dB. The
    value has to be high enough to buy that quality back, or every export
    silently degrades compared to the GPL build it replaced."""
    extract_clip(Path("src.mp4"), tmp_path / "out.mp4", 10_000, 14_000)
    cmd = fake_ffmpeg[-1]
    assert "-q:v" in cmd or "-b:v" in cmd
    if "-q:v" in cmd:
        assert int(cmd[cmd.index("-q:v") + 1]) >= 70, (
            "below ~70 the hardware encoder falls short of the quality the "
            "old x264 default produced"
        )


def test_video_clip_allows_software_fallback(fake_ffmpeg, tmp_path):
    """Without -allow_sw the export fails outright when the hardware encoder
    is busy, which on a shared edit machine is not rare."""
    extract_clip(Path("src.mp4"), tmp_path / "out.mp4", 10_000, 14_000)
    cmd = fake_ffmpeg[-1]
    assert "-allow_sw" in cmd


def test_audio_clip_keeps_libmp3lame(fake_ffmpeg, tmp_path):
    """LAME is LGPL, not GPL, so it stays. This pins that nobody 'fixes'
    the licence by removing it unnecessarily."""
    extract_audio_clip(Path("src.mp3"), tmp_path / "out.mp3", 10_000, 14_000)
    cmd = fake_ffmpeg[-1]
    assert "libmp3lame" in cmd


# ─── the bundled binary must actually be the one that runs ──────────────
# vision.py's KeyframeExtractor already takes an ffmpeg path and uses it.
# clip.py hardcoded "ffmpeg", so exports went through whatever PATH
# resolved — the Homebrew GPL build on a dev machine, and nothing at all
# on a customer Mac without Homebrew, where the export dies on ENOENT
# despite the app shipping its own ffmpeg.

def test_extract_clip_uses_the_configured_binary(fake_ffmpeg, tmp_path):
    extract_clip(Path("src.mp4"), tmp_path / "out.mp4", 1_000, 5_000,
                 ffmpeg_binary="/opt/Tern.app/resources/bin/ffmpeg")
    assert fake_ffmpeg[-1][0] == "/opt/Tern.app/resources/bin/ffmpeg"


def test_extract_audio_clip_uses_the_configured_binary(fake_ffmpeg, tmp_path):
    extract_audio_clip(Path("src.mp3"), tmp_path / "out.mp3", 1_000, 5_000,
                       ffmpeg_binary="/opt/Tern.app/resources/bin/ffmpeg")
    assert fake_ffmpeg[-1][0] == "/opt/Tern.app/resources/bin/ffmpeg"


def test_clip_helpers_still_default_to_path_lookup(fake_ffmpeg, tmp_path):
    """Dev runs and the CLI have no bundle to point at."""
    extract_clip(Path("src.mp4"), tmp_path / "out.mp4", 1_000, 5_000)
    assert fake_ffmpeg[-1][0] == "ffmpeg"
