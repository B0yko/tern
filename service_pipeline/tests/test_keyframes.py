"""Scene-cut keyframe extraction must work on the current ffmpeg.

ffmpeg 8 removed `-vsync`; the old command exited 8 on every video and the
indexer silently fell back to 10 s interval frames, which miss short shots
(the "butterfly" hero query ranked a title card over the actual butterfly
close-up). This test generates a video with hard cuts and asserts the
extractor returns scene-cut keyframes at the cut times, so a future flag
regression fails here instead of degrading every index.
"""

import subprocess

import pytest

from tern.vision import KeyframeExtractor


@pytest.mark.needs_tools("ffmpeg", "ffprobe")
def test_scene_cuts_extracted_with_current_ffmpeg(tmp_path):
    video = tmp_path / "cuts.mp4"
    # Three 2-second textured segments: hard cuts at 2 s and 4 s score
    # far above the 0.3 threshold. (Solid-color segments are not used:
    # ffmpeg 8's scene scorer misses some plain-color transitions.)
    subprocess.run(
        [
            "ffmpeg", "-y", "-v", "error",
            "-f", "lavfi", "-i", "testsrc2=s=320x240:d=2:r=10",
            "-f", "lavfi", "-i", "smptebars=s=320x240:d=2:r=10",
            "-f", "lavfi", "-i", "testsrc=s=320x240:d=2:r=10",
            "-filter_complex", "[0:v][1:v][2:v]concat=n=3:v=1:a=0",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", str(video),
        ],
        check=True,
        capture_output=True,
    )
    out_dir = tmp_path / "keyframes"
    out_dir.mkdir()

    kfs = KeyframeExtractor().extract(video, out_dir)
    ts = sorted(t for t, _ in kfs)

    assert any(1500 <= t <= 2500 for t in ts), f"cut at 2 s missed: {ts}"
    assert any(3500 <= t <= 4500 for t in ts), f"cut at 4 s missed: {ts}"
    assert all(p.exists() for _, p in kfs)
