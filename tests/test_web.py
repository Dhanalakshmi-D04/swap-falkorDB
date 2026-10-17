"""M9 web UI tests: the single page, its one vendored asset and its wiring are served.

Static file serving and the `/cypher` catalogue need no graph, so these run without FalkorDB.
The last test cross-checks the DOM: every element id `app.js` reaches for must exist in
`index.html`, so a rename in the markup cannot silently break the page.
"""

from __future__ import annotations

import re

from fastapi.testclient import TestClient

from swap.api import WEB_DIR, app

client = TestClient(app)

INDEX = WEB_DIR / "index.html"
APP_JS = WEB_DIR / "app.js"

#: The section anchors the sticky navigation points at, and the panels the page promises.
SECTION_IDS = ("topnav", "main", "hero", "how", "result", "swaps", "leftout", "whatif", "plan", "fail", "hood")

#: `$("id")` reaches for an element; `$$("...")` is a selector, so it is deliberately excluded.
_DOLLAR_ID = re.compile(r'(?<!\$)\$\("([^"]+)"\)')
_ID_ATTR = re.compile(r'id="([^"]+)"')
_ID_SHAPE = re.compile(r"^[A-Za-z][\w-]*$")


def test_index_page_is_served() -> None:
    response = client.get("/")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    body = response.text
    assert "Synthetic patients only. Not medical advice." in body
    assert "/app.js" in body
    assert "/vendor/cytoscape.min.js" in body
    assert 'id="ask-agent"' in body and 'id="q"' in body


def test_key_section_ids_are_present() -> None:
    body = client.get("/").text
    for section in SECTION_IDS:
        assert f'id="{section}"' in body, f"missing section id {section!r}"


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


def test_index_has_no_external_urls() -> None:
    """No CDN, no web fonts: everything the page loads is served from this repo."""
    body = INDEX.read_text(encoding="utf-8")
    assert "http://" not in body
    assert "https://" not in body
    assert '="//' not in body
    assert "@import" not in body


def test_cypher_endpoint_lists_the_queries() -> None:
    response = client.get("/cypher")
    assert response.status_code == 200
    queries = response.json()["queries"]
    assert isinstance(queries, dict) and queries
    assert "find_cycles_3" in queries  # a tool query
    assert "loader_pairs" in queries  # a loader query
    assert all(isinstance(name, str) and isinstance(query, str) for name, query in queries.items())


def test_every_element_id_used_by_app_js_exists_in_index() -> None:
    referenced = {
        match for match in _DOLLAR_ID.findall(APP_JS.read_text(encoding="utf-8"))
        if _ID_SHAPE.match(match)
    }
    defined = set(_ID_ATTR.findall(INDEX.read_text(encoding="utf-8")))

    assert len(referenced) > 20, "expected app.js to reference many element ids"
    missing = sorted(referenced - defined)
    assert not missing, f"app.js references ids absent from index.html: {missing}"


_DATA_TERM = re.compile(r'data-term="([^"]+)"')
_TERM_CALL = re.compile(r'\bterm\("([^"]+)"')
_GLOSSARY_KEY = re.compile(r'^  (\w+): \["', re.MULTILINE)
_STEP_LINK = re.compile(r'<a href="#/(\w+)" data-step="(\w+)"')
_PANEL = re.compile(r'data-panel="(\w+)"')


def test_every_glossary_term_used_by_the_page_is_defined() -> None:
    """A `data-term` or `term("...")` without a glossary entry would be a dead underlined word."""
    js = APP_JS.read_text(encoding="utf-8")
    defined = set(_GLOSSARY_KEY.findall(js))
    used = set(_DATA_TERM.findall(INDEX.read_text(encoding="utf-8"))) | set(_TERM_CALL.findall(js))

    assert len(defined) >= 10, "expected a plain-language glossary"
    assert not used - defined, f"terms without a glossary entry: {sorted(used - defined)}"


def test_every_step_in_the_stepper_has_a_panel_and_a_route() -> None:
    html = INDEX.read_text(encoding="utf-8")
    js = APP_JS.read_text(encoding="utf-8")
    links = _STEP_LINK.findall(html)
    panels = set(_PANEL.findall(html))

    assert len(links) == 7
    assert all(route == step for route, step in links)
    assert {step for _, step in links} == panels
    for _, step in links:
        assert f'id: "{step}"' in js, f"router does not know step {step!r}"


def test_site_and_readme_no_longer_list_the_solver_as_a_limit() -> None:
    """Decision of 2026-10-17: the 'happens outside the database' limitation is not stated."""
    readme = (WEB_DIR.parent / "README.md").read_text(encoding="utf-8").lower()
    for text in (INDEX.read_text(encoding="utf-8").lower(), readme):
        assert "outside the database" not in text
        assert "outside falkordb" not in text
