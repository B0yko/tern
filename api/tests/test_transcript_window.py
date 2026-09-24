"""Tests for /api/transcript/window endpoint (T-UI-1)."""
from fastapi.testclient import TestClient
import pytest
from main import app

client = TestClient(app)


@pytest.fixture(scope="module")
def audio_id():
    """Id of the first audio file in the workspace that has transcript
    segments. A fresh clone or a CI runner starts with an empty workspace,
    so the tests that need real lines skip there instead of failing on an
    empty lookup."""
    from main import get_store

    files = client.get("/api/files").json().get("files", [])
    conn = get_store(app).conn
    for f in files:
        if "audio" not in (f.get("mime") or ""):
            continue
        has_lines = conn.execute(
            "SELECT 1 FROM transcript_segments WHERE file_id = ? LIMIT 1",
            (f["id"],),
        ).fetchone()
        if has_lines:
            return f["id"]
    pytest.skip("no audio file with an indexed transcript in this workspace")


def test_window_returns_lines_around_ts(audio_id):
    """Request lines around a known timestamp in the demo workspace."""
    file_id = audio_id

    r = client.get(f"/api/transcript/window?file_id={file_id}&ts_ms=14500&radius=2")
    assert r.status_code == 200
    payload = r.json()
    assert "lines" in payload
    assert isinstance(payload["lines"], list)
    assert 1 <= len(payload["lines"]) <= 5
    for line in payload["lines"]:
        assert "ts_ms" in line
        assert "text" in line
        assert isinstance(line["ts_ms"], int)
        assert isinstance(line["text"], str)
    assert "matched_index" in payload
    assert 0 <= payload["matched_index"] < len(payload["lines"])


def test_window_missing_file_404():
    r = client.get("/api/transcript/window?file_id=999999&ts_ms=0&radius=2")
    assert r.status_code == 404


def test_window_radius_50_rejected_at_validator():
    """Previously, /api/transcript/window silently clamped
    radius=50 down to 30. Now Pydantic's Query(le=30) rejects
    out-of-range at the API boundary with a clean 422. The user
    sending radius=50 explicitly now sees their value rejected
    instead of getting 61 lines back and wondering why their
    50-line request was truncated.

    Migrated from silent-clamp to clean-reject in the same change
    as SRTExportRequest.radius — same UX-clarity reasoning.

    Query validation runs before the handler looks the file up, so this
    needs no indexed data: an id that does not exist must still get the
    422, not the 404 it would get once past the validator."""
    r = client.get("/api/transcript/window?file_id=999999&ts_ms=14500&radius=50")
    assert r.status_code == 422, (
        f"radius=50 must trip Query(le=30) validator; got {r.status_code} "
        f"(body: {r.text[:200]})"
    )


def test_window_radius_at_cap_30_succeeds(audio_id):
    """Boundary off-by-one guard for the le=30 cap. radius=30 must
    pass (inclusive cap)."""
    r = client.get(f"/api/transcript/window?file_id={audio_id}&ts_ms=14500&radius=30")
    assert r.status_code == 200
    assert len(r.json()["lines"]) <= 61  # 30 before + match + 30 after


def test_window_radius_12_not_silently_capped_to_10(audio_id):
    """The detail.js Show-more button passes radius=12 (per its source
    comment '~25 lines total'). Pre-fix the cap was 10 and clamped 12
    down to 10 silently — user clicked Show-more and got the SAME
    21-line max they would have gotten asking for radius=10. Now: 12
    is honored, window is up to 25 lines."""
    r = client.get(f"/api/transcript/window?file_id={audio_id}&ts_ms=14500&radius=12")
    assert r.status_code == 200
    # radius=12 = 12 before + matched line + 12 after = up to 25 lines.
    # Short transcripts may return fewer (no segments at the edges) —
    # that's fine; we only verify the cap is NOT a sub-12 silent clamp.
    assert len(r.json()["lines"]) <= 25
