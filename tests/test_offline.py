"""The demo must run with no internet: no runtime references to external hosts in the web UI."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

WEB = Path(__file__).resolve().parent.parent / "web"
# What the page loads at runtime must be local: src=, a stylesheet/preload <link href=>, url(), @import, ES imports.
# A plain <a href="https://…"> is a link a person can follow (citations), not something the page loads.
EXTERNAL = re.compile(r"""src\s*=\s*["']https?://|<link\b[^>]*href\s*=\s*["']https?://|url\(\s*["']?https?://|@import\s+["']?https?://|from\s+["']https?://|import\(\s*["'`]https?://""")


@pytest.mark.parametrize("path", sorted(p for p in WEB.rglob("*") if p.suffix in {".html", ".js", ".css"}
                                        and "vendor" not in p.parts))
def test_no_external_runtime_references(path):
    """Scripts, stylesheets, images and media load from this server; plain <a href> links may point anywhere."""
    hits = EXTERNAL.findall(path.read_text(encoding="utf-8"))
    assert not hits, f"{path.name} loads from the internet: {hits}"


def test_vendored_three_js_is_served():
    from api.app import app
    with TestClient(app) as c:
        for f in ("three.min.js", "OrbitControls.js", "GLTFLoader.js"):
            r = c.get(f"/static/vendor/three/{f}")
            assert r.status_code == 200 and len(r.content) > 20_000
        assert "/static/vendor/three/three.min.js" in c.get("/").text


def test_offline_rule_still_catches_runtime_loads():
    for bad in ('<script src="https://cdn.example/x.js">', '<link rel="stylesheet" href="https://fonts.example/a.css">',
                '<img src="http://x/y.png">', "background: url('https://x/y.jpg')", 'import x from "https://x/m.js"',
                '<source src="https://x/v.webm">'):
        assert EXTERNAL.search(bad), bad
    assert not EXTERNAL.search('<a href="https://doi.org/10.24432/C5HS5C" target="_blank">DOI</a>')
