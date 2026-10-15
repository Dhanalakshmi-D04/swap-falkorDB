"""M9 web UI tests: the single page and its one vendored asset are served, without ever touching
FalkorDB (static file serving needs no graph)."""

from __future__ import annotations

from fastapi.testclient import TestClient

from swap.api import WEB_DIR, app

client = TestClient(app)


def test_index_page_is_served() -> None:
    response = client.get("/")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    body = response.text
    assert "Synthetic patients only. Not medical advice." in body
    assert "/app.js" in body
    assert "/vendor/cytoscape.min.js" in body


def test_static_assets_are_served() -> None:
    css = client.get("/style.css")
    assert css.status_code == 200
    assert len(css.text) > 0
    js = client.get("/app.js")
    assert js.status_code == 200
    assert len(js.text) > 0


def test_vendored_cytoscape_is_served_and_non_empty() -> None:
    response = client.get("/vendor/cytoscape.min.js")
    assert response.status_code == 200
    assert len(response.content) > 0


def test_vendor_holds_only_cytoscape() -> None:
    assert sorted(p.name for p in (WEB_DIR / "vendor").iterdir()) == ["cytoscape.min.js"]
