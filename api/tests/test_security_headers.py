"""Security headers on the loopback API.

app/index.html carries a Content-Security-Policy in a <meta> tag, but browsers
ignore `frame-ancestors` there, so its "Tern can never be iframed" promise did
nothing: another page could frame the UI and overlay it. The same policy is now
also sent as a response header, together with X-Frame-Options for browsers that
predate frame-ancestors.
"""
import re
from pathlib import Path

from fastapi.testclient import TestClient

import main
from main import app

PORT = 18765
INDEX_HTML = Path(__file__).resolve().parents[2] / "app" / "index.html"


def _client() -> TestClient:
    # No `with`: nothing here needs the app lifespan.
    return TestClient(app, base_url=f"http://127.0.0.1:{PORT}")


def test_api_responses_carry_the_security_headers():
    r = _client().get("/api/health")
    assert r.headers["content-security-policy"] == main._CSP
    assert "frame-ancestors 'none'" in r.headers["content-security-policy"]
    assert r.headers["x-frame-options"] == "DENY"
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["referrer-policy"] == "no-referrer"


def test_the_ui_page_carries_the_security_headers():
    r = _client().get("/")
    assert r.status_code == 200
    assert r.headers["content-security-policy"] == main._CSP
    assert r.headers["x-frame-options"] == "DENY"


def test_the_meta_policy_is_the_header_policy_without_frame_ancestors():
    """Two copies of one policy must not drift apart. The one difference is
    frame-ancestors, which only works as a header: in a <meta> tag browsers
    ignore it and log a console error."""
    html = INDEX_HTML.read_text(encoding="utf-8")
    m = re.search(r'<meta http-equiv="Content-Security-Policy" content="([^"]+)"', html)
    assert m, "app/index.html lost its Content-Security-Policy meta tag"
    meta = m.group(1)
    assert "frame-ancestors" not in meta
    assert meta + "; frame-ancestors 'none'" == main._CSP


def test_the_generated_api_docs_keep_their_own_defaults():
    # Swagger UI loads its scripts from a CDN, which the app policy would block.
    r = _client().get("/docs")
    assert r.status_code == 200
    assert "content-security-policy" not in r.headers


def test_the_ui_page_is_revalidated_not_heuristically_cached():
    """index.html is served for "/" and had no Cache-Control, so a browser or
    webview could keep an old copy after an update while the CSS and JS it
    references (already `no-cache`) were new."""
    for path in ("/", "/index.html", "/main.js", "/tokens.css"):
        r = _client().get(path)
        assert r.status_code == 200, path
        assert r.headers.get("cache-control") == "no-cache", (path, r.headers.get("cache-control"))


def test_only_the_docs_paths_are_exempt_not_anything_that_starts_with_them():
    """/docs and /redoc load their scripts from a CDN, so they keep their own
    defaults. A path that merely begins with the same letters is not exempt."""
    assert "content-security-policy" not in _client().get("/redoc").headers
    r = _client().get("/docsfoo")
    assert r.status_code == 404
    assert r.headers["content-security-policy"] == main._CSP
    assert r.headers["x-frame-options"] == "DENY"
