"""The demo videos must download sharp enough for the visual channel.

YouTube's progressive mp4 ("best[ext=mp4]") tops out at 360p, and SigLIP
scores on 360p frames fall below the search noise floor: the picture-only
hero query ("butterfly") returned 0 hits on a fresh demo until the format
selector was fixed to merge a <=720p video-only stream with m4a audio.
These tests pin the selector so the regression cannot come back through
a "simplification" of the format string.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_real_videos_selector_leads_with_merged_720p():
    """Every yt-dlp selector in demo/real_videos/download.sh must prefer
    bestvideo+bestaudio over the progressive stream, which is 360p-only."""
    script = (ROOT / "demo" / "real_videos" / "download.sh").read_text()
    selectors = re.findall(r"-f '([^']+)'", script)
    assert selectors, "no yt-dlp format selectors found in download.sh"
    for sel in selectors:
        assert sel.startswith("bestvideo[ext=mp4][height<=720]+bestaudio[ext=m4a]"), (
            f"selector {sel!r} can fall back to YouTube's 360p progressive mp4; "
            "picture-only search then scores below the noise floor"
        )
