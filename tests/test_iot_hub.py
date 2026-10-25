"""IoT hub: device auth, normalisation, data quality, LIVE overlay, REST/WS ingestion."""

import json
from pathlib import Path

import jsonschema
import pytest
from fastapi.testclient import TestClient

from hub import IoTHub, HubError, STALE_S, OFFLINE_S
from hub.core import STUCK_WINDOW
from hub.mqtt_bridge import MqttBridge
from hub.normalize import normalize
from sim.factory import FactoryEngine

SCHEMA = json.loads((Path(__file__).resolve().parent.parent / "docs" / "schema.json").read_text())


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


@pytest.fixture
def engine_hub(tmp_path):
    engine = FactoryEngine(seed=1, auto_demo=False)
    clock = Clock()
    hub = IoTHub(
        db_path=tmp_path / "hub.sqlite",
        template_of=lambda mid: engine.plants[mid].template if mid in engine.plants else None,
        clock=clock,
    )
    engine.live_overlay = hub.overlay
    return engine, hub, clock


def observed(msgs, mid):
    return next(m for m in msgs if m["type"] == "observed" and m["machine_id"] == mid)


def test_normalize_units_and_template_filter():
    fields, dq = normalize({"temp_c": 38.0, "vib_rms": 2.0, "pressure_bar": 7, "rssi": -50}, "cnc_mill", temp_offset_k=1.0)
    assert dq == "OK"
    assert fields == {"process_temp_k": 312.15, "vibration_rms_mm_s": 2.0}

    fields, _ = normalize({"temp_c": 60.0, "pressure_bar": 7.1}, "air_compressor")
    assert fields == {"discharge_temp_c": 60.0, "pressure_bar": 7.1}


def test_normalize_dq_flags():
    assert normalize({"temp_c": 85.0}, "cnc_mill") == ({"process_temp_k": None}, "MISSING")
    assert normalize({"temp_c": -127.0}, "cnc_mill") == ({"process_temp_k": None}, "OUT_OF_RANGE")
    assert normalize({"temp_c": "nan"}, "cnc_mill")[1] == "MISSING"


def test_register_auth_and_persistence(engine_hub, tmp_path):
    engine, hub, _ = engine_hub
    dev = hub.register("ESP32-01", "VMC-01")
    assert dev["token"] and dev["status"] == "NEVER_SEEN"

    with pytest.raises(HubError) as e:
        hub.ingest("ESP32-01", "wrong", {"temp_c": 30}, "http")
    assert e.value.status == 401
    with pytest.raises(HubError) as e:
        hub.register("ESP32-02", "VMC-01")
    assert e.value.status == 409
    with pytest.raises(HubError) as e:
        hub.register("ESP32-03", "NOPE-01")
    assert e.value.status == 404

    reloaded = IoTHub(db_path=tmp_path / "hub.sqlite", template_of=hub.template_of)
    assert reloaded.ingest("ESP32-01", dev["token"], {"temp_c": 38}, "http")["dq_flag"] == "OK"
    rotated = reloaded.rotate_token("ESP32-01")["token"]
    with pytest.raises(HubError):
        reloaded.ingest("ESP32-01", dev["token"], {"temp_c": 38}, "http")
    reloaded.ingest("ESP32-01", rotated, {"temp_c": 38}, "http")


def test_overlay_marks_live_and_validates(engine_hub):
    engine, hub, _ = engine_hub
    token = hub.register("ESP32-01", "VMC-01")["token"]

    assert observed(engine.step(), "VMC-01")["source_mode"] == "SIM"

    hub.ingest("ESP32-01", token, {"temp_c": 40.0, "vibration_rms_mm_s": 1.7, "current_a": 3.3}, "http")
    msgs = engine.step()
    obs = observed(msgs, "VMC-01")
    assert obs["source_mode"] == "LIVE"
    assert obs["process_temp_k"] == 313.15
    assert obs["vibration_rms_mm_s"] == 1.7
    assert obs["dq_flag"] == "OK"
    assert obs["rpm"] is not None  # unmeasured signals still come from the simulator
    assert observed(msgs, "LATHE-01")["source_mode"] == "SIM"
    for m in msgs:
        jsonschema.validate(m, SCHEMA)


def test_stale_then_offline(engine_hub):
    engine, hub, clock = engine_hub
    token = hub.register("ESP32-01", "VMC-01")["token"]
    hub.ingest("ESP32-01", token, {"temp_c": 40.0}, "http")

    clock.t += STALE_S + 0.1
    obs = observed(engine.step(), "VMC-01")
    assert obs["dq_flag"] == "STALE" and obs["process_temp_k"] == 313.15

    clock.t += OFFLINE_S
    msgs = engine.step()
    obs = observed(msgs, "VMC-01")
    assert obs["source_mode"] == "LIVE" and obs["dq_flag"] == "MISSING" and obs["process_temp_k"] is None
    jsonschema.validate(obs, SCHEMA)
    assert hub.get("ESP32-01")["status"] == "OFFLINE"


def test_stuck_detection(engine_hub):
    _, hub, _ = engine_hub
    token = hub.register("ESP32-01", "VMC-01")["token"]
    for _ in range(STUCK_WINDOW - 1):
        assert hub.ingest("ESP32-01", token, {"temp_c": 30.0}, "http")["dq_flag"] == "OK"
    assert hub.ingest("ESP32-01", token, {"temp_c": 30.0}, "http")["dq_flag"] == "STUCK"
    assert hub.ingest("ESP32-01", token, {"temp_c": 30.0625}, "http")["dq_flag"] == "OK"


def test_unbind_returns_to_sim(engine_hub):
    engine, hub, _ = engine_hub
    token = hub.register("ESP32-01", "VMC-01")["token"]
    hub.ingest("ESP32-01", token, {"temp_c": 40.0}, "http")
    hub.bind("ESP32-01", None)
    assert observed(engine.step(), "VMC-01")["source_mode"] == "SIM"


def test_mqtt_handle_without_broker(engine_hub):
    _, hub, _ = engine_hub
    token = hub.register("ESP32-01", "VMC-01")["token"]
    bridge = MqttBridge(hub, "localhost")
    bridge.handle("udyam/ESP32-01/telemetry", json.dumps({"token": token, "temp_c": 39.0}).encode())
    bridge.handle("udyam/ESP32-01/telemetry", b"{\"token\": \"bad\"}")
    bridge.handle("udyam/ESP32-01/telemetry", b"not json")
    assert (bridge.received, bridge.rejected) == (3, 2)
    dev = hub.get("ESP32-01")
    assert dev["last_transport"] == "mqtt" and "token" not in dev["last_raw"]


# ---------------------------------------------------------------- API

@pytest.fixture
def client(monkeypatch):
    from api.app import app, iot_hub

    # Isolate from the real device registry in udyam_twin.sqlite: empty, in-memory only.
    monkeypatch.setattr(iot_hub, "devices", {})
    monkeypatch.setattr(iot_hub, "db_path", None)
    with TestClient(app) as c:
        yield c


def test_api_device_lifecycle(client):
    res = client.post("/api/iot/devices", json={"device_id": "TEST-HTTP", "machine_id": "LATHE-01"})
    assert res.status_code == 201
    token = res.json()["token"]

    bad = client.post("/api/iot/telemetry", json={"temp_c": 38}, headers={"X-Device-Id": "TEST-HTTP", "X-Device-Token": "x"})
    assert bad.status_code == 401

    ok = client.post("/api/iot/telemetry", json={"temp_c": 38.0, "current_a": 2.0}, headers={"X-Device-Id": "TEST-HTTP", "X-Device-Token": token})
    assert ok.status_code == 200 and ok.json()["dq_flag"] == "OK"

    tare = client.post("/api/iot/devices/TEST-HTTP/tare", json={})
    assert tare.status_code == 200
    assert tare.json()["temp_offset_k"] != 0

    status = client.get("/api/iot/status").json()
    assert status["bound"]["LATHE-01"] == "TEST-HTTP"
    assert "LATHE-01" in client.get("/api/health").json()["live_machines"]

    assert client.post("/api/iot/devices/TEST-HTTP/bind", json={"machine_id": "NOPE"}).status_code == 404
    assert client.delete("/api/iot/devices/TEST-HTTP").status_code == 200
    assert client.get("/api/iot/devices/TEST-HTTP").status_code == 404


def test_api_device_websocket(client):
    token = client.post("/api/iot/devices", json={"device_id": "TEST-WS", "machine_id": "SAW-01"}).json()["token"]
    with client.websocket_connect(f"/ws/device/TEST-WS?token={token}") as ws:
        ws.send_text(json.dumps({"vibration_rms_mm_s": 1.9, "current_a": 4.0}))
        ack = json.loads(ws.receive_text())
        assert ack["ok"] and ack["accepted"] == ["current_a", "vibration_rms_mm_s"]
        ws.send_text("garbage")
        assert json.loads(ws.receive_text())["ok"] is False

    with pytest.raises(Exception):
        with client.websocket_connect("/ws/device/TEST-WS?token=bad") as ws:
            ws.receive_text()


def test_iot_page_served(client):
    res = client.get("/iot")
    assert res.status_code == 200 and "IoT Hub" in res.text
