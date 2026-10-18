# api/ (R3)

FastAPI app: `/ws/telemetry` (same messages and commands as `tools/fake_stream.py`), REST for faults / work orders / tool-wear entry / `POST /machines`, SQLite for events, work orders and parameter versions. Serves `web/` statically on port 8000.

Start by copying the WebSocket wiring from `tools/fake_stream.py`. Note: do not use `from __future__ import annotations` in FastAPI route modules (it once broke every WebSocket handshake with a 403).
