"""The quota gate as the API exposes it.

`api/tests/test_licensing.py` covers the rules in isolation. This file
covers the wiring: that the state file is redirectable (so tests never
touch the real one under ~/Library), that the status endpoint tells the
user where they stand, and that /api/index actually refuses once the
trial is spent.
"""
import json

import pytest
from fastapi.testclient import TestClient

import main
from licensing import TRIAL_LIMIT_MS
from main import app

MINUTE = 60_000


@pytest.fixture(scope="module")
def client():
    """A client WITHOUT running the app lifespan.

    `with TestClient(app)` would run startup and, on exit, shutdown — and
    shutdown tears down state that the next test module's own lifespan
    does not fully rebuild. Entering it here made 47 tests in
    test_security_and_polish fail purely from ordering, while that file
    passes on its own.

    Nothing in this file needs the search engine; the only piece of
    lifespan state these paths touch is app.state.indexing, so seed that
    directly and leave the lifespan alone.
    """
    if not hasattr(app.state, "indexing"):
        app.state.indexing = {
            "running": False, "paused": False, "cancel_requested": False,
            "files_total": 0, "files_done": 0, "files_skipped": 0,
            "files_errored": 0, "current_file": None, "log": [],
            "stage": None, "stage_label": None, "stage_started_at": None,
            "stage_progress": None,
        }
    yield TestClient(app, base_url="http://127.0.0.1:18765")


@pytest.fixture
def state_dir(tmp_path, monkeypatch):
    """Redirect licence + trial state at call time, not import time.

    Without this the activation test writes a licence into the developer's
    real ~/Library/Application Support/Tern, which then makes every later
    run of the suite think the machine is licensed.
    """
    monkeypatch.setenv("TERN_STATE_DIR", str(tmp_path))
    # _LICENSE_FILE is a module-level name because existing tests
    # monkeypatch it directly; redirect it the same way rather than
    # changing that contract.
    monkeypatch.setattr(main, "_LICENSE_FILE", tmp_path / "license.json")
    return tmp_path


def _spend_whole_trial(state_dir):
    (state_dir / "trial.json").write_text(json.dumps({"used_ms": TRIAL_LIMIT_MS}))


# --- state location -------------------------------------------------------

def test_state_dir_is_resolved_per_call_not_at_import(state_dir):
    """If the path were captured at import time, no test could redirect it
    and every run would write to the developer's real Application Support
    folder."""
    assert main._state_dir() == state_dir
    assert main._trial_file().parent == state_dir


def test_trial_file_defaults_under_application_support(monkeypatch):
    monkeypatch.delenv("TERN_STATE_DIR", raising=False)
    assert main._trial_file().parts[-3:] == ("Application Support", "Tern", "trial.json")


# --- what the user is told -------------------------------------------------

def test_license_status_reports_a_fresh_trial(client, state_dir):
    body = client.get("/api/license/status").json()
    assert "trial" in body, "status must carry the quota, not just a badge"
    trial = body["trial"]
    assert trial["licensed"] is False
    assert trial["limit_ms"] == TRIAL_LIMIT_MS
    assert trial["used_ms"] == 0
    assert trial["remaining_ms"] == TRIAL_LIMIT_MS
    assert trial["exhausted"] is False


def test_license_status_reports_a_spent_trial(client, state_dir):
    _spend_whole_trial(state_dir)
    trial = client.get("/api/license/status").json()["trial"]
    assert trial["used_ms"] == TRIAL_LIMIT_MS
    assert trial["remaining_ms"] == 0
    assert trial["exhausted"] is True


def test_license_status_still_reports_unactivated_status(client, state_dir):
    """Adding the quota must not change the existing badge contract the
    sidebar and the licence modal already read."""
    body = client.get("/api/license/status").json()
    assert body["status"] == "unactivated"
    assert body["license_key"] is None


# --- the gate --------------------------------------------------------------

def test_index_is_refused_once_the_trial_is_spent(client, state_dir, tmp_path):
    _spend_whole_trial(state_dir)
    folder = tmp_path / "archive" / "episodes"
    folder.mkdir(parents=True)
    (folder / "ep01.mp3").write_bytes(b"")

    r = client.post("/api/index", json={"folder": str(folder)})
    assert r.status_code == 402, (
        f"expected 402 Payment Required, got {r.status_code}: {r.text[:200]}"
    )
    assert "trial" in r.json()["detail"].lower()


def test_the_refusal_explains_how_to_proceed(client, state_dir, tmp_path):
    _spend_whole_trial(state_dir)
    folder = tmp_path / "archive" / "episodes"
    folder.mkdir(parents=True)
    (folder / "ep01.mp3").write_bytes(b"")

    detail = client.post("/api/index", json={"folder": str(folder)}).json()["detail"]
    assert "licence key" in detail.lower() or "license key" in detail.lower()


def test_a_trial_with_room_left_is_not_refused_by_the_quota(client, state_dir, tmp_path):
    """It may fail for other reasons — missing binaries, empty folder — but
    the quota must not be the thing that stops it."""
    (state_dir / "trial.json").write_text(json.dumps({"used_ms": 10 * MINUTE}))
    folder = tmp_path / "archive" / "episodes"
    folder.mkdir(parents=True)
    (folder / "ep01.mp3").write_bytes(b"")

    r = client.post("/api/index", json={"folder": str(folder)})
    assert r.status_code != 402


def test_a_corrupt_trial_file_refuses_indexing(client, state_dir, tmp_path):
    """Deleting the counter must not be a way to reset the trial."""
    (state_dir / "trial.json").write_text("{ mangled")
    folder = tmp_path / "archive" / "episodes"
    folder.mkdir(parents=True)
    (folder / "ep01.mp3").write_bytes(b"")

    assert client.post("/api/index", json={"folder": str(folder)}).status_code == 402


def test_a_licensed_copy_is_never_refused_by_the_quota(client, state_dir, tmp_path):
    _spend_whole_trial(state_dir)
    (state_dir / "license.json").write_text(json.dumps({
        "license_key": "TERN-TEST-KEY", "is_valid": True,
        "email": "buyer@example.com", "validated_at": "2026-07-30T00:00:00Z",
    }))
    folder = tmp_path / "archive" / "episodes"
    folder.mkdir(parents=True)
    (folder / "ep01.mp3").write_bytes(b"")

    r = client.post("/api/index", json={"folder": str(folder)})
    assert r.status_code != 402


def test_a_licensed_copy_reports_no_limit(client, state_dir):
    _spend_whole_trial(state_dir)
    (state_dir / "license.json").write_text(json.dumps({
        "license_key": "TERN-TEST-KEY", "is_valid": True,
    }))
    trial = client.get("/api/license/status").json()["trial"]
    assert trial["licensed"] is True
    assert trial["limit_ms"] is None
    assert trial["exhausted"] is False


# --- per-file admission during a run -----------------------------------------
#
# The upfront /api/index check only proves the trial is not already spent.
# A user who queues forty episodes still has to be stopped at the file
# where the quota runs out, and the quota has to actually be charged.

def _fake_probe(duration_ms):
    return lambda path: {"duration_ms": duration_ms, "bit_rate": 0, "format_name": "mp3"}


def test_admit_allows_a_short_file_and_reports_its_duration(state_dir, monkeypatch):
    monkeypatch.setattr(main, "probe_media", _fake_probe(20 * MINUTE))
    allowed, reason, duration = main._quota_admit(main.Path("/tmp/ep.mp3"))
    assert allowed is True
    assert reason is None
    assert duration == 20 * MINUTE


def test_admit_refuses_a_file_that_does_not_fit_in_what_is_left(state_dir, monkeypatch):
    (state_dir / "trial.json").write_text(json.dumps({"used_ms": TRIAL_LIMIT_MS - 5 * MINUTE}))
    monkeypatch.setattr(main, "probe_media", _fake_probe(90 * MINUTE))
    allowed, reason, _ = main._quota_admit(main.Path("/tmp/long.mp3"))
    assert allowed is False
    assert reason and "trial" in reason.lower()


def test_admit_lets_photos_through_on_a_spent_trial(state_dir, monkeypatch):
    _spend_whole_trial(state_dir)
    called = []
    monkeypatch.setattr(main, "probe_media", lambda p: called.append(p) or {"duration_ms": 0})
    allowed, reason, duration = main._quota_admit(main.Path("/tmp/photo.jpg"))
    assert allowed is True
    assert duration == 0
    assert not called, "images have no timeline; probing them is wasted work"


def test_admit_fails_open_when_the_probe_itself_fails(state_dir, monkeypatch):
    """A corrupt file must not be reported to the user as a billing
    problem. Let index_file fail on it and report the real reason."""
    def _boom(path):
        raise RuntimeError("ffprobe exploded")
    monkeypatch.setattr(main, "probe_media", _boom)
    allowed, reason, duration = main._quota_admit(main.Path("/tmp/broken.mp3"))
    assert allowed is True
    assert reason is None
    assert duration == 0


def test_admit_never_blocks_a_licensed_copy(state_dir, monkeypatch):
    _spend_whole_trial(state_dir)
    (state_dir / "license.json").write_text(json.dumps(
        {"license_key": "K", "is_valid": True}))
    monkeypatch.setattr(main, "_LICENSE_FILE", state_dir / "license.json")
    monkeypatch.setattr(main, "probe_media", _fake_probe(600 * MINUTE))
    allowed, _, _ = main._quota_admit(main.Path("/tmp/huge.mp4"))
    assert allowed is True


# --- charging -----------------------------------------------------------------

def test_charging_spends_quota(state_dir):
    main._charge_trial(25 * MINUTE)
    assert main._current_trial_state()["used_ms"] == 25 * MINUTE


def test_charging_accumulates(state_dir):
    main._charge_trial(10 * MINUTE)
    main._charge_trial(15 * MINUTE)
    assert main._current_trial_state()["used_ms"] == 25 * MINUTE


def test_a_licensed_copy_is_never_charged(state_dir, monkeypatch):
    (state_dir / "license.json").write_text(json.dumps(
        {"license_key": "K", "is_valid": True}))
    monkeypatch.setattr(main, "_LICENSE_FILE", state_dir / "license.json")
    main._charge_trial(30 * MINUTE)
    assert not (state_dir / "trial.json").exists()


def test_charging_survives_an_unwritable_state_dir(state_dir, monkeypatch):
    """A failed counter write must not abort a run the user was already
    allowed to start."""
    def _boom(path, trial):
        raise OSError("read-only filesystem")
    monkeypatch.setattr(main.licensing, "save_trial", _boom)
    main._charge_trial(5 * MINUTE)  # must not raise


# --- activation carries the machine id --------------------------------------

def test_activation_sends_a_machine_id_for_seat_counting(client, state_dir, monkeypatch):
    """Per-seat pricing needs the server to tell machines apart. Without
    this field one key works on every Mac in the building."""
    sent = {}

    class _FakeResponse:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return json.dumps(
            {"is_valid": True, "email": "buyer@example.com"}).encode()

    def _fake_urlopen(req, timeout=None):
        sent["url"] = req.full_url
        sent["body"] = json.loads(req.data.decode())
        return _FakeResponse()

    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)

    monkeypatch.setattr(main, "_LICENSE_EXTRA_SERVERS", ("https://licence.example.com",))
    r = client.post("/api/license/activate", json={
        "license_key": "TERN-TEST-KEY",
        "server_url": "https://licence.example.com",
    })
    assert r.status_code == 200, r.text
    assert sent["body"]["license_key"] == "TERN-TEST-KEY"
    assert sent["body"].get("machine_id"), "no machine_id in the activation body"
    assert len(sent["body"]["machine_id"]) >= 16
    assert sent["body"]["app_version"]


def test_the_configured_url_is_the_endpoint_not_a_prefix(client, state_dir, monkeypatch):
    """The server URL is posted to verbatim.

    It used to have `/api/license/validate` appended, which assumed the
    licence server owned its own routing. A Supabase Edge Function is served
    at /functions/v1/<name> and cannot host that path, so the assumption made
    the deployed endpoint unreachable. Treating the setting as the full
    endpoint also lets a self-hosting customer put it anywhere.
    """
    sent = {}

    class _FakeResponse:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return json.dumps({"is_valid": True}).encode()

    def _fake_urlopen(req, timeout=None):
        sent["url"] = req.full_url
        return _FakeResponse()

    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)

    endpoint = "https://proj.supabase.co/functions/v1/license-validate"
    monkeypatch.setattr(main, "_LICENSE_EXTRA_SERVERS", (endpoint,))
    r = client.post("/api/license/activate",
                    json={"license_key": "K", "server_url": endpoint})
    assert r.status_code == 200, r.text
    assert sent["url"] == endpoint, (
        f"posted to {sent['url']!r}, expected the configured URL unchanged"
    )


def test_a_trailing_slash_on_the_endpoint_is_tolerated(client, state_dir, monkeypatch):
    sent = {}

    class _FakeResponse:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return json.dumps({"is_valid": True}).encode()

    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen",
                        lambda req, timeout=None: (sent.update(url=req.full_url), _FakeResponse())[1])

    monkeypatch.setattr(main, "_LICENSE_EXTRA_SERVERS",
                        ("https://proj.supabase.co/functions/v1/license-validate",))
    client.post("/api/license/activate", json={
        "license_key": "K",
        "server_url": "https://proj.supabase.co/functions/v1/license-validate/",
    })
    assert sent["url"] == "https://proj.supabase.co/functions/v1/license-validate"


def test_activation_without_a_configured_server_says_so(client, state_dir, monkeypatch):
    """No licence endpoint is compiled into the source; a build supplies one
    through TERN_LICENSE_SERVER. With neither that nor a server_url, the
    activation must not reach for some default host: it answers ok=false
    with a message that names the setting, and never opens a connection."""
    import main
    import urllib.request

    monkeypatch.setattr(main, "_LICENSE_DEFAULT_SERVER", "")

    def _no_network(*a, **k):
        raise AssertionError("activation opened a connection with no server configured")

    monkeypatch.setattr(urllib.request, "urlopen", _no_network)

    r = client.post("/api/license/activate", json={"license_key": "TERN-TEST-KEY"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is False
    assert "TERN_LICENSE_SERVER" in body["message"]
