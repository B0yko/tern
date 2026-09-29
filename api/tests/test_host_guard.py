"""DNS-rebinding defences of the loopback API.

Two independent gates:

  - The Host header must be a loopback name on the port the server is
    listening on. A rebinding page reaches 127.0.0.1 under its own name, so
    its requests carry that name in Host and are refused.
  - /api/license/activate only posts to the configured licence server (or an
    entry of TERN_LICENSE_ALLOWED_SERVERS), never to a client-chosen host.
"""
import pytest
from fastapi.testclient import TestClient

import main
from main import _host_header_allowed, app

PORT = 18765


def _client(base_url: str) -> TestClient:
    # No `with`: the guard runs before any route, so the app lifespan is not
    # needed for /api/health.
    return TestClient(app, base_url=base_url)


# ─── _host_header_allowed ────────────────────────────────────────────────

@pytest.mark.parametrize("host", [
    f"127.0.0.1:{PORT}",
    f"localhost:{PORT}",
    f"LOCALHOST:{PORT}",
    f"[::1]:{PORT}",
])
def test_loopback_names_on_the_server_port_are_allowed(host):
    assert _host_header_allowed(host, PORT)


@pytest.mark.parametrize("host", [
    None,
    "",
    f"evil.example.com:{PORT}",
    f"127.0.0.1.evil.example.com:{PORT}",
    f"localhost.evil.example.com:{PORT}",
    f"evil.example.com:{PORT}@127.0.0.1",
    f"127.0.0.1:{PORT + 1}",          # right name, wrong port
    "127.0.0.1",                       # implicit port 80, server is on PORT
    f"127.0.0.1:{PORT}x",
    f"127.0.0.1:{PORT}:{PORT}",
    f"[::1]x:{PORT}",
    f"[::1:{PORT}",
    f"0.0.0.0:{PORT}",
    f"169.254.169.254:{PORT}",
])
def test_other_hosts_and_ports_are_refused(host):
    assert not _host_header_allowed(host, PORT)


def test_implicit_port_matches_only_a_server_on_port_80():
    assert _host_header_allowed("localhost", 80)
    assert not _host_header_allowed("localhost", PORT)


def test_without_a_server_port_only_the_name_is_checked():
    assert _host_header_allowed("localhost:1234", None)
    assert not _host_header_allowed("evil.example.com", None)


# ─── the middleware ──────────────────────────────────────────────────────

@pytest.mark.parametrize("base_url", [
    f"http://127.0.0.1:{PORT}",
    f"http://localhost:{PORT}",
])
def test_requests_with_a_loopback_host_are_served(base_url):
    r = _client(base_url).get("/api/health")
    assert r.status_code == 200, r.text


@pytest.mark.parametrize("host", [
    f"evil.example.com:{PORT}",
    "rebind.attacker.test",
    f"127.0.0.1:{PORT + 1}",
])
def test_rebound_hosts_get_400_before_any_route_runs(host):
    r = _client(f"http://127.0.0.1:{PORT}").get("/api/health", headers={"Host": host})
    assert r.status_code == 400
    assert r.json() == {"detail": "Invalid Host header"}


def test_a_rebound_host_cannot_reach_state_changing_routes():
    r = _client(f"http://127.0.0.1:{PORT}").post(
        "/api/index",
        json={"folder": "/tmp"},
        headers={"Host": "rebind.attacker.test"},
    )
    assert r.status_code == 400


def test_a_rebound_host_is_refused_even_for_cors_preflight():
    r = _client(f"http://127.0.0.1:{PORT}").options(
        "/api/health",
        headers={
            "Host": "rebind.attacker.test",
            "Origin": "http://rebind.attacker.test",
            "Access-Control-Request-Method": "GET",
        },
    )
    assert r.status_code == 400


def test_the_static_frontend_is_guarded_too():
    r = _client(f"http://127.0.0.1:{PORT}").get("/", headers={"Host": "rebind.attacker.test"})
    assert r.status_code == 400


def test_the_guard_is_the_outermost_middleware():
    """A refused request must not get as far as CORS or a route."""
    classes = [m.cls.__name__ for m in app.user_middleware]
    assert classes[0] == "_LoopbackHostGuard", classes


# ─── licence server allowlist ────────────────────────────────────────────

class _Ok:
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def read(self): return b'{"is_valid": true}'


@pytest.fixture
def licence_client(monkeypatch, tmp_path):
    monkeypatch.setattr(main, "_LICENSE_FILE", tmp_path / "license.json")
    monkeypatch.setattr(main, "_CRASH_LOG", tmp_path / "crash.log")
    monkeypatch.setattr(main, "_LICENSE_DEFAULT_SERVER", "https://licence.example.test/v1/validate")
    monkeypatch.setattr(main, "_LICENSE_EXTRA_SERVERS", ())
    return _client(f"http://127.0.0.1:{PORT}")


def _no_network(monkeypatch):
    import urllib.request
    calls = []

    def _fake(req, timeout=None):
        calls.append(req.full_url)
        return _Ok()

    monkeypatch.setattr(urllib.request, "urlopen", _fake)
    return calls


@pytest.mark.parametrize("url", [
    "https://attacker.example.net/collect",
    "http://127.0.0.1:9999/",
    "https://licence.example.test.attacker.net/v1/validate",
    "https://licence.example.test/other/path",
    "http://licence.example.test/v1/validate",  # scheme downgrade
])
def test_a_server_url_outside_the_allowlist_is_refused_without_a_request(
        licence_client, monkeypatch, url):
    calls = _no_network(monkeypatch)
    r = licence_client.post("/api/license/activate",
                            json={"license_key": "TERN-TEST-KEY", "server_url": url})
    assert r.status_code == 403, r.text
    assert calls == [], "the key was sent to a host outside the allowlist"


def test_the_configured_server_may_be_named_explicitly(licence_client, monkeypatch):
    calls = _no_network(monkeypatch)
    r = licence_client.post("/api/license/activate", json={
        "license_key": "TERN-TEST-KEY",
        "server_url": "HTTPS://Licence.Example.Test/v1/validate/",
    })
    assert r.status_code == 200, r.text
    assert calls == ["HTTPS://Licence.Example.Test/v1/validate"]


def test_an_allowlisted_extra_server_is_accepted(licence_client, monkeypatch):
    monkeypatch.setattr(main, "_LICENSE_EXTRA_SERVERS", ("https://self-hosted.example.test/validate",))
    calls = _no_network(monkeypatch)
    r = licence_client.post("/api/license/activate", json={
        "license_key": "TERN-TEST-KEY",
        "server_url": "https://self-hosted.example.test/validate",
    })
    assert r.status_code == 200, r.text
    assert calls == ["https://self-hosted.example.test/validate"]


def test_omitting_server_url_uses_the_configured_server(licence_client, monkeypatch):
    calls = _no_network(monkeypatch)
    r = licence_client.post("/api/license/activate", json={"license_key": "TERN-TEST-KEY"})
    assert r.status_code == 200, r.text
    assert calls == ["https://licence.example.test/v1/validate"]


def test_with_nothing_configured_no_server_url_is_accepted(licence_client, monkeypatch):
    monkeypatch.setattr(main, "_LICENSE_DEFAULT_SERVER", "")
    calls = _no_network(monkeypatch)
    r = licence_client.post("/api/license/activate", json={
        "license_key": "TERN-TEST-KEY",
        "server_url": "https://anything.example.net/",
    })
    assert r.status_code == 403
    assert calls == []


def test_the_allowlist_setting_is_a_comma_separated_list():
    assert main._parse_server_list(" https://a.example.test/x , ,https://b.example.test/y ") == (
        "https://a.example.test/x",
        "https://b.example.test/y",
    )
    assert main._parse_server_list("") == ()
