"""The demo must run with no internet: no runtime references to external hosts in the web UI."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

WEB = Path(__file__).resolve().parent.parent / "web"
EXTERNAL = re.compile(r"""(?:src|href)\s*=\s*["']https?://|url\(\s*["']?https?://|@import\s+["']?https?://|from\s+["']https?://""")


@pytest.mark.parametrize("path", sorted(p for p in WEB.rglob("*") if p.suffix in {".html", ".js", ".css"}
                                        and "vendor" not in p.parts))
def test_no_external_runtime_references(path):
    hits = EXTERNAL.findall(path.read_text(encoding="utf-8"))
    assert not hits, f"{path.name} loads from the internet: {hits}"


def test_vendored_three_js_is_served():
    from api.app import app
    with TestClient(app) as c:
        for f in ("three.min.js", "OrbitControls.js", "GLTFLoader.js"):
            r = c.get(f"/static/vendor/three/{f}")
            assert r.status_code == 200 and len(r.content) > 20_000
        assert "/static/vendor/three/three.min.js" in c.get("/").text
