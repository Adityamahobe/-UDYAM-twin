Firebase JS SDK 10.12.2 (Apache License 2.0, Google LLC), vendored so the pages load without internet.

- Source: https://www.gstatic.com/firebasejs/10.12.2/firebase-app.js and firebase-auth.js (downloaded 2026-10-26)
- One change: firebase-auth.js imports `./firebase-app.js` instead of the gstatic URL, so both modules load from here.
- Used by web/login.js and the sign-in gate in web/index.html. Signing in itself still needs internet (Firebase).
