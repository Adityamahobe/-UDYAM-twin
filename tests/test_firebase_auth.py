"""Firebase sign-in backend: public web config, token verification on /api/auth/me, and the off switch."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api import firebase_auth as F

GOOD = "good-token"


def fake_verify(token, project):
    if token != GOOD:
        raise ValueError("bad token")
    return {"sub": "uid-1", "email": "a@b.com", "email_verified": True, "name": "A B", "role": "ADMIN",
            "firebase": {"sign_in_provider": "google.com"}}


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("FIREBASE_API_KEY", "public-web-key")
    monkeypatch.setenv("FIREBASE_PROJECT_ID", "udyam-test")
    app = FastAPI()
    app.include_router(F.build_router(verify=fake_verify))
    return TestClient(app)


def test_config_is_public_and_complete(client):
    cfg = client.get("/api/auth/config").json()
    assert cfg["enabled"] is True and cfg["providers"] == ["password", "google.com"]
    assert cfg["firebase"] == {"apiKey": "public-web-key", "authDomain": "udyam-test.firebaseapp.com",
                               "projectId": "udyam-test", "appId": None}


def test_me_verifies_the_token_and_ignores_client_roles(client):
    r = client.get("/api/auth/me", headers={"Authorization": f"Bearer {GOOD}"})
    assert r.status_code == 200
    assert r.json()["user"] == {"uid": "uid-1", "email": "a@b.com", "email_verified": True, "name": "A B",
                                "provider": "google.com", "role": "USER"}  # a role claim is never trusted
    for hdr in ({}, {"Authorization": "Bearer nope"}, {"Authorization": f"Basic {GOOD}"}, {"Authorization": "Bearer "}):
        r = client.get("/api/auth/me", headers=hdr)
        assert r.status_code == 401 and r.headers["www-authenticate"] == "Bearer"
        assert r.json()["detail"]["error"] == "unauthorized" and "nope" not in r.text


def test_sign_in_off_without_config(monkeypatch):
    monkeypatch.delenv("FIREBASE_API_KEY", raising=False)
    monkeypatch.delenv("FIREBASE_PROJECT_ID", raising=False)
    app = FastAPI()
    app.include_router(F.build_router(verify=fake_verify))
    c = TestClient(app)
    assert c.get("/api/auth/config").json() == {"enabled": False, "firebase": None, "providers": ["password", "google.com"]}
    r = c.get("/api/auth/me", headers={"Authorization": f"Bearer {GOOD}"})
    assert r.status_code == 503 and r.json()["detail"]["error"] == "auth_not_configured"


def test_real_verifier_rejects_a_forged_token():
    with pytest.raises(Exception):
        F._google_verify("eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJ4In0.c2ln", "udyam-test")


def test_routes_are_mounted_and_the_login_page_is_served():
    from api.app import app
    paths = set(app.openapi()["paths"])
    assert {"/api/auth/config", "/api/auth/me"} <= paths
    assert not {"/api/auth/login", "/api/auth/logout"} & paths  # the old Admin login is replaced
    c = TestClient(app)
    page = c.get("/static/login.html")
    assert page.status_code == 200 and "/static/login.js" in page.text
