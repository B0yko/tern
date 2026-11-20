"""The main window loads HTTP from the sidecar, not Tauri's asset protocol."""
import json
from html.parser import HTMLParser
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from main import app


class _Policies(HTMLParser):
    def __init__(self):
        super().__init__()
        self.meta = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "meta" and attrs.get("http-equiv", "").lower() == "content-security-policy":
            self.meta.append(attrs["content"])


@pytest.mark.parametrize("path", ["/", "/index.html"])
def test_desktop_csp(path):
    # Static files need no lifespan/model startup; use a non-default port.
    response = TestClient(app, base_url="http://127.0.0.1:18766").get(path)
    assert response.status_code == 200
    policies = _Policies()
    policies.feed(response.text)
    # All API, font, thumbnail and player URLs are relative to the selected
    # port. Only style attributes (not inline scripts or sheets) need an exception.
    document = "default-src 'self'; style-src-attr 'unsafe-inline'; object-src 'none'; base-uri 'none'; form-action 'self'"
    assert policies.meta == [document]
    # Browsers ignore frame-ancestors in a meta policy.
    header = response.headers["Content-Security-Policy"]
    assert header == "frame-ancestors 'none'"
    root = Path(__file__).resolve().parents[2]
    config = json.loads((root / "tauri/src-tauri/tauri.conf.json").read_text())
    assert config["app"]["security"]["csp"] == f"{document}; {header}"
