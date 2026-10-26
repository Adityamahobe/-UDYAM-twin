// Login page: Firebase Authentication with email + password (sign in, create account, reset) and Google.
// The Firebase web config comes from GET /api/auth/config (public identifiers set in the server's .env).
import { initializeApp } from "/static/vendor/firebase/firebase-app.js";
import {
  getAuth, onAuthStateChanged, signInWithEmailAndPassword, createUserWithEmailAndPassword,
  sendPasswordResetEmail, sendEmailVerification, signInWithPopup, GoogleAuthProvider,
} from "/static/vendor/firebase/firebase-auth.js";

const el = id => document.getElementById(id);
let creating = false;

// only same-site paths, never another origin
const params = new URLSearchParams(location.search);
const raw = params.get("next") || "/";
const next = raw.startsWith("/") && !raw.startsWith("//") ? raw : "/";

function msg(text, tone) {
  el("loginMsg").textContent = text;
  el("loginMsg").className = `login-msg${tone ? ` ${tone}` : ""}`;
}

const ERRORS = {
  "auth/invalid-credential": "Wrong email or password.",
  "auth/invalid-email": "Enter a valid email address.",
  "auth/user-disabled": "This account has been disabled.",
  "auth/email-already-in-use": "An account with this email already exists. Sign in instead.",
  "auth/weak-password": "Use a stronger password (at least 6 characters).",
  "auth/too-many-requests": "Too many attempts. Wait a few minutes and try again.",
  "auth/popup-closed-by-user": "Google sign-in was closed before it finished.",
  "auth/popup-blocked": "The browser blocked the Google sign-in window. Allow pop-ups and try again.",
  "auth/network-request-failed": "No connection to Firebase. Check the internet connection.",
  "auth/operation-not-allowed": "This sign-in method is not enabled in the Firebase project.",
  "auth/unauthorized-domain": "This address is not an authorised domain in the Firebase project.",
};
const explain = err => ERRORS[err && err.code] || "Sign-in failed. Try again.";

function setCreating(on) {
  creating = on;
  el("confirmRow").hidden = !on;
  el("loginForgot").hidden = on;
  el("loginTitle").textContent = on ? "Create an account" : "Sign in to the digital twin";
  el("loginSubmit").textContent = on ? "Create account" : "Sign in";
  el("loginToggle").textContent = on ? "I already have an account" : "Create an account";
  el("loginPassword").autocomplete = on ? "new-password" : "current-password";
  msg("");
}

function busy(on) {
  ["loginSubmit", "loginGoogle"].forEach(id => { el(id).disabled = on; });
}

async function start() {
  let cfg;
  try {
    cfg = await fetch("/api/auth/config").then(r => r.json());
  } catch {
    return msg("Cannot reach the UDYAM-TWIN server.", "err");
  }
  if (!cfg.enabled) {
    busy(true);
    return msg("Sign-in is not configured on this server (FIREBASE_API_KEY and FIREBASE_PROJECT_ID). The dashboard is open without login.", "err");
  }
  const auth = getAuth(initializeApp(cfg.firebase));
  onAuthStateChanged(auth, user => { if (user) location.replace(next); });

  el("loginForm").onsubmit = async e => {
    e.preventDefault();
    const email = el("loginEmail").value.trim(), pw = el("loginPassword").value;
    if (!email || !pw) return msg("Enter your email and password.", "err");
    if (creating && pw !== el("loginPassword2").value) return msg("The passwords do not match.", "err");
    busy(true);
    try {
      if (creating) {
        const cred = await createUserWithEmailAndPassword(auth, email, pw);
        await sendEmailVerification(cred.user).catch(() => {});
      } else {
        await signInWithEmailAndPassword(auth, email, pw);
      }
      msg("Signed in. Opening the dashboard…", "ok");
    } catch (err) {
      msg(explain(err), "err");
    } finally {
      busy(false);
    }
  };
  el("loginGoogle").onclick = async () => {
    busy(true);
    try {
      await signInWithPopup(auth, new GoogleAuthProvider());
    } catch (err) {
      msg(explain(err), "err");
    } finally {
      busy(false);
    }
  };
  el("loginToggle").onclick = () => setCreating(!creating);
  el("loginForgot").onclick = async () => {
    const email = el("loginEmail").value.trim();
    if (!email) return msg("Enter your email first, then choose Forgot password.", "err");
    try {
      await sendPasswordResetEmail(auth, email);
    } catch (err) {
      if (err && err.code === "auth/invalid-email") return msg(explain(err), "err");
    }
    msg("If an account exists for this email, a reset link has been sent.", "ok");  // never reveals whether it exists
  };
}

start();
