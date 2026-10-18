# web/ (R2 + R4)

Three.js scenes (R2) and dashboard (R4). Bundle three.js locally, with no CDN: the venue may have no internet.

Develop against `ws://localhost:8765/ws/telemetry` (`python -m tools.fake_stream`) or replay `samples/demo_stream.jsonl`. When `api/` is ready, the URL changes to port 8000 and nothing else changes.
