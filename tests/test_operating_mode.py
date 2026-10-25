"""Operating mode (TESTING / REAL DATA): connection states, read-only guard, clean switching, separate storage."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from api.export import iso_ms
from api.operating_mode import REAL_DATA, TESTING, OperatingMode, ReadOnlyError, connection_state
from hub.mqtt_bridge import MqttBridge


def _dev(status, simulated=False, machine="LATHE-01", age=1.0):
    return {"device_id": f"D-{status}", "simulated": simulated, "machine_id": machine, "status": status,
            "last_seen": None if status == "NEVER_SEEN" else "2026-10-25T10:00:00Z", "age_s": age}


def test_connection_states():
    mq = lambda state, err=None: {"enabled": True, "state": state, "broker": "localhost:1883", "topic": "udyam/+/telemetry",
                                  "last_error": err}
    assert connection_state(None, [])["state"] == "NOT_CONFIGURED"
    assert connection_state(mq("CONNECTING"), [])["state"] == "CONNECTING"
    assert connection_state(mq("DISCONNECTED"), [])["state"] == "DISCONNECTED"
    err = connection_state(mq("ERROR", "connection refused"), [])
    assert err["state"] == "ERROR" and "connection refused" in err["message"]
    idle = connection_state(mq("CONNECTED"), [])
    assert idle["state"] == "CONNECTED" and not idle["verified_live"] and "no real device" in idle["message"]
    live = connection_state(mq("CONNECTED"), [_dev("ONLINE")])
    assert live["state"] == "CONNECTED" and live["verified_live"] and live["live_machines"] == ["LATHE-01"]
    stale = connection_state(mq("CONNECTED"), [_dev("STALE", age=12.0)])
    assert stale["state"] == "STALE_DATA" and not stale["verified_live"] and "not live" in stale["message"]
    # a broker that dropped while data had arrived before still reads as stale data, never as live
    assert connection_state(mq("DISCONNECTED"), [_dev("OFFLINE", age=45)])["state"] == "STALE_DATA"
    # simulated stand-ins (tools/fake_device) never count as real
    fake = connection_state(None, [_dev("ONLINE", simulated=True)])
    assert fake["state"] == "NOT_CONFIGURED" and fake["devices"]["simulated_stand_ins"] == 1
    assert "password" not in str(live) and "UDYAM_MQTT_PASS" not in str(live)


def test_operating_mode_guard():
    m = OperatingMode()
    assert m.mode == TESTING and not m.is_real
    m.guard("anything")  # testing: allowed
    assert m.set(REAL_DATA) and not m.set(REAL_DATA)
    with pytest.raises(ReadOnlyError) as err:
        m.guard("Fault injection")
    assert err.value.detail()["error"] == "real_data_read_only"
    with pytest.raises(ValueError):
        m.set("LIVE")


def test_mqtt_bridge_states():
    class Hub:
        def ingest(self, *a, **k):
            return {}
    b = MqttBridge(Hub(), "localhost")
    assert b.status()["state"] == "NOT_STARTED"
    ok, bad = SimpleNamespace(is_failure=False), SimpleNamespace(is_failure=True)
    client = SimpleNamespace(subscribe=lambda *a, **k: None)
    b._on_connect(client, None, None, ok)
    assert b.state == "CONNECTED" and b.connected
    b._on_disconnect(client, None, None, bad)
    assert b.state == "DISCONNECTED" and not b.connected and b.last_error
    b._on_connect(client, None, None, bad)
    assert b.state == "ERROR"
    b.handle("udyam/ESP32-01/telemetry", b'{"token": "x", "temp_c": 39.0}')
    assert b.status()["last_message_at"] and b.received == 1


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("UDYAM_TICKER", "0")
    monkeypatch.setenv("UDYAM_AUTO_DEMO", "0")
    monkeypatch.setenv("UDYAM_OPERATOR_TOKEN", "pin")
    from api.app import app, broadcast_messages, factory_engine, iot_hub, operating_mode
    monkeypatch.setattr(iot_hub, "devices", {})
    monkeypatch.setattr(iot_hub, "db_path", None)
    with TestClient(app) as c:
        async def step(n):
            for _ in range(n):
                await broadcast_messages(factory_engine.step())
        yield c, factory_engine, lambda n: asyncio.run(step(n))
    operating_mode.set(TESTING, by="test teardown")


def test_switch_api_defaults_to_testing_and_reports_connection(client):
    c, _, _ = client
    s = c.get("/api/operating-mode").json()
    assert s["mode"] == "TESTING" and not s["read_only"] and s["connection"]["state"] == "NOT_CONFIGURED"
    assert c.post("/api/operating-mode", json={"mode": "LIVE"}).status_code == 422
    r = c.post("/api/operating-mode", json={"mode": "REAL_DATA"}).json()
    assert r["mode"] == "REAL_DATA" and r["read_only"] and r["history"][-1]["mode"] == "REAL_DATA"
    assert c.post("/api/operating-mode", json={"mode": "TESTING"}).json()["mode"] == "TESTING"


def test_real_data_mode_refuses_every_simulator_control(client):
    c, eng, _ = client
    c.post("/api/operating-mode", json={"mode": "REAL_DATA"})
    refused = [
        ("post", "/api/faults/inject", {"json": {"machine_id": "VMC-01", "fault": "bearing_defect"}}),
        ("post", "/api/faults/clear", {"json": {}}),
        ("post", "/api/derate", {"json": {"machine_id": "VMC-01", "pct": 20}}),
        ("post", "/api/reset", {}),
        ("post", "/api/scenarios/over_temperature/start", {}),
        ("post", "/api/scenarios/resolve", {}),
        ("post", "/api/auto_demo?on=true", {}),
        ("post", "/api/mode", {"json": {"mode": "REPLAY", "machine_id": "VMC-01"}}),
    ]
    for method, url, kw in refused:
        r = getattr(c, method)(url, **kw)
        assert r.status_code == 409, (url, r.status_code, r.text)
        assert r.json()["detail"]["error"] == "real_data_read_only"
    assert not eng.plants["VMC-01"].faults.active_faults and eng.plants["VMC-01"].derate_pct == 0.0
    # monitoring and human records still work
    assert c.post("/api/auto_demo?on=false").status_code == 200
    assert c.get("/api/analytics").status_code == 200
    assert c.post("/api/work-orders", json={"machine_id": "VMC-01", "title": "Inspect spindle", "priority": "Low"}).status_code in (200, 201)
    # back in TESTING the same controls work again
    c.post("/api/operating-mode", json={"mode": "TESTING"})
    assert c.post("/api/faults/inject", json={"machine_id": "VMC-01", "fault": "bearing_defect"}).status_code == 200
    assert "bearing_defect" in eng.plants["VMC-01"].faults.active_faults


def test_entering_real_data_resets_the_background_simulation(client):
    c, eng, step = client
    c.post("/api/scenarios/over_temperature/start")
    step(4)
    assert eng.plants["VMC-01"].faults.active_faults
    c.post("/api/operating-mode", json={"mode": "REAL_DATA"})
    assert not eng.plants["VMC-01"].faults.active_faults and not eng.active_scenarios and eng.mode == "SIM"
    assert any(e["kind"] == "MODE" and "Real data" in e["title"] for e in c.get("/api/events?limit=10").json())


def test_websocket_simulator_commands_are_refused_in_real_data_mode(client):
    c, eng, _ = client
    c.post("/api/operating-mode", json={"mode": "REAL_DATA"})
    with c.websocket_connect("/ws/telemetry") as ws:
        ws.receive_json(), ws.receive_json()  # hello, registry
        ws.send_json({"cmd": "inject_fault", "machine_id": "VMC-01", "fault": "overload"})
        err = ws.receive_json()
        assert err["type"] == "error" and err["detail"]["error"] == "real_data_read_only"
        ws.send_json({"cmd": "set_derate", "machine_id": "VMC-01", "pct": 40})
        assert ws.receive_json()["detail"]["error"] == "real_data_read_only"
    assert not eng.plants["VMC-01"].faults.active_faults and eng.plants["VMC-01"].derate_pct == 0.0


def test_derate_approval_refused_in_real_data_mode(client):
    c, eng, _ = client
    from api.app import advisor
    p = advisor.tool_propose_action("VMC-01", "derate", "test", derate_pct=20)
    c.post("/api/operating-mode", json={"mode": "REAL_DATA"})
    r = c.post(f"/api/chat/proposals/{p['proposal_id']}/approve", headers={"X-Operator-Token": "pin"})
    assert r.status_code == 409 and r.json()["detail"]["error"] == "real_data_read_only"
    assert eng.plants["VMC-01"].derate_pct == 0.0
    assert advisor.proposals[p["proposal_id"]]["status"] == "pending_approval"  # not consumed


def test_real_telemetry_is_stored_apart_from_simulated(client):
    c, eng, step = client
    t0 = datetime.now(timezone.utc) - timedelta(seconds=2)
    tok = c.post("/api/iot/devices", json={"device_id": "REAL-T1", "machine_id": "LATHE-01"}).json()["token"]
    hdr = {"X-Device-Id": "REAL-T1", "X-Device-Token": tok}
    assert c.post("/api/iot/telemetry", json={"temp_c": 38.5, "current_a": 2.1}, headers=hdr).status_code == 200
    step(8)
    rng = {"start": iso_ms(t0), "end": iso_ms(datetime.now(timezone.utc) + timedelta(seconds=2))}
    real = c.get("/api/reports/export.csv", params={"section": "real_telemetry", **rng}).text.strip().splitlines()
    sim = c.get("/api/reports/export.csv", params={"section": "telemetry", **rng}).text.strip().splitlines()
    assert len(real) > 1 and all(",LATHE-01,LIVE," in row for row in real[1:])
    assert len(sim) > 1 and not any(",LIVE," in row for row in sim[1:])
    s = c.get("/api/operating-mode").json()["connection"]
    assert s["state"] == "CONNECTED" and s["live_machines"] == ["LATHE-01"] and s["verified_live"]


def test_overlay_names_the_fields_a_real_device_actually_sent():
    from hub.core import IoTHub
    hub = IoTHub(template_of=lambda mid: "cnc_lathe")
    tok = hub.register("REAL-1", "LATHE-01")["token"]
    hub.ingest("REAL-1", tok, {"temp_c": 38.0, "rpm": 1790}, transport="http")
    out = hub.overlay({"machine_id": "LATHE-01", "torque_nm": 30.0, "tool_wear_min": 70.0, "rpm": 1800})
    assert out["source_mode"] == "LIVE" and "rpm" in out["live_fields"] and "process_temp_k" in out["live_fields"]
    assert "torque_nm" not in out["live_fields"]  # still the simulation's value: the dashboard blanks it in REAL DATA mode


def test_live_machine_is_never_scored_on_partly_simulated_inputs():
    """A real device on the AI4I mill that does not send torque / tool wear gets no failure risk at all."""
    from hub.core import IoTHub
    from sim.factory import FactoryEngine
    eng = FactoryEngine()
    hub = IoTHub(template_of=lambda mid: eng.plants[mid].template)
    eng.live_overlay = hub.overlay
    tok = hub.register("REAL-VMC", "VMC-01")["token"]
    hub.ingest("REAL-VMC", tok, {"temp_c": 38.5, "rpm": 1500, "air_temp_c": 27.0}, transport="http")
    snap = None
    for _ in range(8):
        snap = next(m for m in eng.step() if m.get("type") == "snapshot" and m["machine_id"] == "VMC-01")
    ml = (snap.get("risk") or {}).get("ml") or {}
    assert snap["mode"] == "LIVE" and ml.get("failure_risk_pct") is None
    assert ml.get("status") == "NO_DATA" and "torque_nm, tool_wear_min" in ml["detail"]
