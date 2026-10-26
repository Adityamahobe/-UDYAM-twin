# Sign-in API (Firebase Authentication)

The contract for the frontend. Implementation: `api/firebase_auth.py` (backend), `web/login.html` and `web/login.js` (the login page of the temporary frontend).

## How it works

1. **The browser signs in with the Firebase web SDK.** Two methods:
   - **Email and password:** sign in, create an account, or reset the password.
   - **Google.**

   Anyone can create an account. Every signed-in user gets the role **USER**.
2. **The browser gets the Firebase ID token** with `await user.getIdToken()`. The token lasts one hour, and the SDK refreshes it automatically.
3. **API calls send the token** in the header `Authorization: Bearer <Firebase ID token>`.
4. **The backend verifies the token** against Google's public keys: its signature, issuer, audience (your project ID) and expiry. The backend needs no Firebase secret.
5. **Sign-out happens in the browser**, with `signOut(auth)` from the Firebase SDK. There is no logout endpoint.

**Roles** come only from the server, never from the client.

**What's protected now**
- The dashboard (`/`) sends anyone who isn't signed in to `/static/login.html`.
- The API endpoints and the WebSocket are **not protected yet**. For now, only `GET /api/auth/me` checks the token. Protect other endpoints with the `require_user` dependency in `api/firebase_auth.py` once the real frontend sends the token on every request.

## Endpoints

### `GET /api/auth/config` (public)

The Firebase web config the browser needs to start the SDK.

```json
{
  "enabled": true,
  "firebase": { "apiKey": "...", "authDomain": "my-project.firebaseapp.com", "projectId": "my-project", "appId": "..." },
  "providers": ["password", "google.com"]
}
```

- **`enabled: false`** means sign-in is off: the server has no `FIREBASE_API_KEY` or `FIREBASE_PROJECT_ID`.
- **These values are not secrets.** Firebase documents them as public identifiers.

### `GET /api/auth/me`

Header: `Authorization: Bearer <Firebase ID token>`.

`200 OK`:

```json
{ "user": { "uid": "...", "email": "a@b.com", "email_verified": true, "name": "A B", "provider": "google.com", "role": "USER" } }
```

## Errors

Every error body has the shape `{"detail": {"error": "<code>", "message": "<text for the user>"}}`.

| Status | `error` | When |
|---|---|---|
| `401` | `unauthorized` | The header is missing or malformed, or the token is invalid, expired, or from another Firebase project. Sent with `WWW-Authenticate: Bearer`. When you get it, refresh the token and retry once; if it fails again, send the user to the login page. |
| `503` | `auth_not_configured` | The server has no `FIREBASE_PROJECT_ID`. |

Firebase handles repeated failed logins itself, answering with `auth/too-many-requests`.

## Environment variables (server `.env`)

| Variable | Required | Value |
|---|---|---|
| `FIREBASE_API_KEY` | yes | The web app's `apiKey` |
| `FIREBASE_PROJECT_ID` | yes | The project ID; it is also the expected audience of every token |
| `FIREBASE_AUTH_DOMAIN` | no | Defaults to `<project id>.firebaseapp.com` |
| `FIREBASE_APP_ID` | no | The web app's `appId` |

## Setting up Firebase (once)

1. **Create a project and register a web app** in the [Firebase console](https://console.firebase.google.com), then copy the config values into `.env`.
2. **Turn on the sign-in methods:** Authentication → Sign-in method → enable **Email/Password** and **Google**.
3. **Authorise your domains:** in Authentication → Settings → Authorized domains, add every domain the app is served from. `localhost` is there by default.
4. **Restart the server.**
