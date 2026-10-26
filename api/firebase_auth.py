"""Sign-in with Firebase Authentication (email + password and Google). See docs/AUTH_API.md.

- The browser signs in with the Firebase web SDK (web/login.html) and sends its Firebase ID token as
  `Authorization: Bearer <id token>`. This module verifies the token's signature, issuer, audience (the project ID)
  and expiry with Google's public keys (google-auth), so no Firebase secret or service account is needed here.
- GET /api/auth/config gives the browser the Firebase *web* config (API key, auth domain, project ID, app ID).
  Firebase documents these values as public identifiers, not secrets. With FIREBASE_API_KEY or FIREBASE_PROJECT_ID
  unset, sign-in is off and the dashboard opens without login (enabled: false).
- Every signed-in user has the role USER; roles are never taken from the client.
- `require_user` is the dependency for protecting endpoints later; existing endpoints are not protected yet.
- Tokens are never logged or returned.
"""

from __future__ import annotations

import os
from typing import Any, Callable, Dict, Optional

from fastapi import APIRouter, Depends, Header, HTTPException

ROLE = "USER"


def firebase_config() -> Dict[str, Optional[str]]:
    project = (os.environ.get("FIREBASE_PROJECT_ID") or "").strip() or None
    return {
        "apiKey": (os.environ.get("FIREBASE_API_KEY") or "").strip() or None,
        "authDomain": (os.environ.get("FIREBASE_AUTH_DOMAIN") or "").strip() or (f"{project}.firebaseapp.com" if project else None),
        "projectId": project,
        "appId": (os.environ.get("FIREBASE_APP_ID") or "").strip() or None,
    }


def _google_verify(token: str, project_id: str) -> Dict[str, Any]:
    """Verify a Firebase ID token with Google's public keys; raises ValueError when it is not valid."""
    from google.auth.transport import requests as google_requests
    from google.oauth2 import id_token

    claims = id_token.verify_firebase_token(token, google_requests.Request(), audience=project_id)
    if not claims or claims.get("iss") != f"https://securetoken.google.com/{project_id}" or not claims.get("sub"):
        raise ValueError("token not issued for this Firebase project")
    return claims


def _unauthorized(message: str) -> HTTPException:
    return HTTPException(status_code=401, detail={"error": "unauthorized", "message": message},
                         headers={"WWW-Authenticate": "Bearer"})


def build_router(verify: Callable[[str, str], Dict[str, Any]] = _google_verify) -> APIRouter:
    router = APIRouter()

    def require_user(authorization: Optional[str] = Header(default=None)) -> Dict[str, Any]:
        project = firebase_config()["projectId"]
        if not project:
            raise HTTPException(status_code=503, detail={"error": "auth_not_configured",
                                                         "message": "Sign-in is not configured on the server."})
        scheme, _, token = (authorization or "").partition(" ")
        if scheme.lower() != "bearer" or not token.strip() or len(token) > 4096:
            raise _unauthorized("Sign in first: send Authorization: Bearer <Firebase ID token>.")
        try:
            claims = verify(token.strip(), project)
        except Exception:  # noqa: BLE001 - any verification failure is the same 401, without details
            raise _unauthorized("The sign-in is invalid or has expired. Sign in again.")
        fb = claims.get("firebase") or {}
        return {"uid": claims["sub"], "email": claims.get("email"), "email_verified": bool(claims.get("email_verified")),
                "name": claims.get("name"), "provider": fb.get("sign_in_provider"), "role": ROLE}

    router.require_user = require_user

    @router.get("/api/auth/config")
    def config():
        cfg = firebase_config()
        return {"enabled": bool(cfg["apiKey"] and cfg["projectId"]), "firebase": cfg if cfg["apiKey"] else None,
                "providers": ["password", "google.com"]}

    @router.get("/api/auth/me")
    def me(user: Dict[str, Any] = Depends(require_user)):
        return {"user": user}

    return router


router = build_router()
require_user = router.require_user
