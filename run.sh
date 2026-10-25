#!/usr/bin/env bash
# UDYAM-TWIN: one command to set up and start everything.
#
#   ./run.sh            prepare (venv, packages, AI4I data, model) and start on http://localhost:8000
#   ./run.sh --check    prepare only, then exit
#   ./run.sh --test     prepare, then run the test suite
#
# Environment: PORT (8000), HOST (0.0.0.0 so the operator view works on a phone on the same Wi-Fi),
# UDYAM_AUTO_DEMO=0 to start with the Auto Demo off, UDYAM_MQTT_HOST to enable the MQTT bridge.
# Internet is needed only the first time (packages, AI4I CSV); after that everything runs offline.
set -euo pipefail
cd "$(dirname "$0")"

# Server-side settings and secrets (e.g. GEMINI_API_KEY) from .env, if present; see .env.example
if [ -f .env ]; then set -a; . ./.env; set +a; fi

PORT="${PORT:-8000}"
HOST="${HOST:-0.0.0.0}"
AI4I_CSV="ml/data/ai4i2020.csv"
AI4I_ZIP_URL="https://archive.ics.uci.edu/static/public/601/ai4i+2020+predictive+maintenance+dataset.zip"
AI4I_SHA256="dc6630cd9b1f0f853922fad78a1b6436570d3f1ec863f1dd5c4340ac56bc8a8e"
MODEL="models/ai4i_rf.joblib"

say()  { printf '\033[1;34m[udyam]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[udyam]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31m[udyam]\033[0m %s\n' "$*" >&2; exit 1; }

sha256() { if command -v sha256sum >/dev/null; then sha256sum "$1" | cut -d' ' -f1; else shasum -a 256 "$1" | cut -d' ' -f1; fi; }

# 1. Python virtual environment
if [ ! -x .venv/bin/python ]; then
  PY="$(command -v python3 || true)"
  [ -n "$PY" ] || die "python3 not found (3.10 or newer is required)"
  "$PY" -c 'import sys; sys.exit(sys.version_info < (3, 10))' || die "Python 3.10+ required, found $("$PY" --version)"
  say "Creating .venv"
  "$PY" -m venv .venv
fi
PY=.venv/bin/python

# 2. Packages (skipped when already installed, so this works offline after the first run)
if ! "$PY" -c 'import fastapi, uvicorn, numpy, pandas, sklearn, joblib, yaml, scipy, pytest, httpx, jsonschema, google.genai' 2>/dev/null; then
  say "Installing Python packages (needs internet once)"
  "$PY" -m pip install --quiet --upgrade pip || true
  "$PY" -m pip install --quiet -r requirements-dev.txt || die "pip install failed; check your internet connection"
fi

# 3. AI4I 2020 dataset (UCI, CC BY 4.0), checked against its SHA-256
if [ ! -f "$AI4I_CSV" ] || [ "$(sha256 "$AI4I_CSV")" != "$AI4I_SHA256" ]; then
  say "Fetching the AI4I 2020 dataset from UCI"
  TMP="$(mktemp -d)"
  if curl -fsSL --max-time 60 -o "$TMP/ai4i.zip" "$AI4I_ZIP_URL" && unzip -q -o "$TMP/ai4i.zip" -d "$TMP/x" \
     && [ "$(sha256 "$TMP/x/ai4i2020.csv")" = "$AI4I_SHA256" ]; then
    mkdir -p "$(dirname "$AI4I_CSV")"
    cp "$TMP/x/ai4i2020.csv" "$AI4I_CSV" && chmod 644 "$AI4I_CSV"
    say "AI4I 2020 saved to $AI4I_CSV (checksum OK)"
  else
    warn "Could not fetch a verified AI4I file (offline?). Download it from"
    warn "  https://archive.ics.uci.edu/dataset/601/ai4i+2020+predictive+maintenance+dataset"
    warn "and save ai4i2020.csv to $AI4I_CSV. Without it REPLAY mode is unavailable."
  fi
  rm -rf "$TMP"
fi

# 4. Failure-risk model (trained on AI4I; about 5 s)
if [ ! -f "$MODEL" ]; then
  if [ -f "$AI4I_CSV" ]; then
    say "Training the AI4I failure-risk model"
    "$PY" -m ml.train_ai4i
  else
    warn "No $MODEL and no AI4I data: the failure-risk panel will say 'unavailable'."
  fi
fi

case "${1:-}" in
  --check) say "Ready. Start with ./run.sh"; exit 0 ;;
  --test)  exec "$PY" -m pytest -q ;;
esac

say "Dashboard   http://localhost:$PORT"
say "Operator    http://localhost:$PORT/operator"
say "API docs    http://localhost:$PORT/docs"
exec "$PY" -m uvicorn api.app:app --host "$HOST" --port "$PORT" --log-level info
