"""Live Records (GET /api/reports/recent): newest stored rows across telemetry, events, alerts and work orders."""

from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("UDYAM_TICKER", "0")
    monkeypatch.setenv("UDYAM_AUTO_DEMO", "0")
    from api.app import app, broadcast_messages, factory_engine
    with TestClient(app) as c:
        async def step(n):
            for _ in range(n):
                await broadcast_messages(factory_engine.step())
        yield c, lambda n: asyncio.run(step(n))
        factory_engine.command({"cmd": "reset"})  # the engine is shared: leave no fault or derate for later tests


def test_recent_records_merge_newest_first_and_filter(client):
    c, step = client
    c.post("/api/faults/inject", json={"machine_id": "VMC-01", "fault": "bearing_defect"})
    step(12)  # telemetry is recorded every 2 s (4 ticks)
    res = c.get("/api/reports/recent", params={"limit": 40}).json()
    entries = res["entries"]
    assert entries and len(entries) <= 40 and "exports contain" in res["note"]
    assert [e["ts"] for e in entries] == sorted((e["ts"] for e in entries), reverse=True)
    kinds = {e["kind"] for e in entries}
    assert {"telemetry", "event"} <= kinds
    tele = next(e for e in entries if e["kind"] == "telemetry" and e["machine_id"] == "VMC-01")
    assert tele["source"] in ("SIM", "REPLAY", "LIVE") and "rpm" in tele["summary"]
    only = c.get("/api/reports/recent", params={"kind": "events", "limit": 5}).json()["entries"]
    assert only and all(e["kind"] == "event" for e in only) and len(only) <= 5
    assert any("bearing_defect" in (e["summary"] or "") for e in c.get("/api/reports/recent", params={"kind": "events"}).json()["entries"])
    assert c.get("/api/reports/recent", params={"kind": "nope"}).status_code == 422
    assert c.get("/api/reports/recent", params={"limit": 0}).status_code == 422
