"""Wokwi virtual devices: message validation, device states, storage and export, the Real Data mode API,
and isolation from the Testing-mode simulation."""

from __future__ import annotations

import asyncio
import copy
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from hub.wokwi import DEFAULT_PREFIX, VirtualDeviceStore, WokwiBridge, validate

ROOT = Path(__file__).resolve().parent.parent
SAMPLE = json.loads((ROOT / "wokwi" / "udyam-virtual-machine" / "sample_message.json").read_text())
TOPIC = f"{DEFAULT_PREFIX}/machine-001/telemetry"
T0 = datetime(2026, 10, 26, 10, 15, 31, tzinfo=timezone.utc)


def msg(**over) -> bytes:
    p = copy.deepcopy(SAMPLE)
    p.update(over)
    return json.dumps(p).encode()


def test_the_firmware_message_format_is_accepted():
    rec, why = validate(TOPIC, msg(), DEFAULT_PREFIX, T0)
    assert why is None and rec["machine_id"] == "machine-001" and rec["source"] == "WOKWI_SIMULATOR"
    assert rec["rpm"] == 1538.4 and rec["temp_c"] == 38.1 and rec["current_a"] == 6.42 and rec["scenario"] == "normal"
    assert rec["ts_source"] == "device" and rec["ts"].startswith("2026-10-26T10:15:31")  # stored at receive time


@pytest.mark.parametrize("raw, topic, words", [
    (b"not json", TOPIC, "not valid JSON"),
    (b"[1,2]", TOPIC, "not a JSON object"),
    (msg(schema="other"), TOPIC, "schema"),
    (msg(source="REAL_MACHINE"), TOPIC, "source must be"),
    (msg(virtual=False), TOPIC, "virtual=true"),
    (msg(machine_id="Machine 1"), TOPIC, "machine_id must be"),
    (msg(machine_id="machine-002"), TOPIC, "does not match the topic"),
    (msg(rpm=99999), TOPIC, "rpm missing or outside"),
    (msg(current_a="lots"), TOPIC, "current_a missing or outside"),
    (msg(vibration_rms_mm_s=None), TOPIC, "vibration_rms_mm_s"),
    (msg(scenario="explode"), TOPIC, "scenario must be"),
    (msg(simulated_fault="gremlins"), TOPIC, "simulated_fault"),
    (msg(seq=-1), TOPIC, "seq"),
    (msg(), f"{DEFAULT_PREFIX}/machine-001/other", "unexpected topic"),
    (msg(), "someone/else/machine-001/telemetry", "unexpected topic"),
    (b"{" + b" " * 5000 + b"}", TOPIC, "larger than"),
])
def test_bad_messages_are_rejected_with_a_reason(raw, topic, words):
    rec, why = validate(topic, raw, DEFAULT_PREFIX, T0)
    assert rec is None and words in why


def test_sensor_dropout_and_an_implausible_device_clock():
    rec, _ = validate(TOPIC, msg(temp_c=None, scenario="sensor_dropout", simulated_fault="temperature_sensor_dropout"),
                      DEFAULT_PREFIX, T0)
    assert rec["temp_c"] is None
    rec, _ = validate(TOPIC, msg(ts="2001-01-01T00:00:00Z"), DEFAULT_PREFIX, T0)
    assert rec["device_ts"] is None and "clock off" in rec["ts_source"]


def test_store_states_storage_and_limits(tmp_path):
    s = VirtualDeviceStore(tmp_path / "w.sqlite")
    s.init()
    assert s.ingest(TOPIC, msg(seq=1), DEFAULT_PREFIX, T0)
    assert s.ingest(TOPIC, msg(seq=2), DEFAULT_PREFIX, T0 + timedelta(milliseconds=100)) is None  # too frequent
    assert s.counters()["last_reject"]["reason"] == "messages too frequent"
    assert s.ingest(TOPIC, msg(seq=3), DEFAULT_PREFIX, T0 + timedelta(seconds=2))
    snap = lambda dt: s.snapshot(T0 + dt)[0]
    assert snap(timedelta(seconds=3))["state"] == "ONLINE"
    stale = snap(timedelta(seconds=10))
    assert stale["state"] == "STALE" and stale["last"]["rpm"] == 1538.4
    gone = snap(timedelta(seconds=40))
    assert gone["state"] == "OFFLINE" and gone["last"]["rpm"] is None  # old values are not shown as current
    s.status_message(f"{DEFAULT_PREFIX}/machine-001/status", b"offline", DEFAULT_PREFIX, T0 + timedelta(seconds=2.5))
    assert snap(timedelta(seconds=3))["state"] == "OFFLINE"  # the last will makes it offline at once
    rows = s.query("2026-10-26T00:00:00.000Z", "2026-10-27T00:00:00.000Z")
    assert [r["seq"] for r in rows] == [1, 3] and all(r["source"] == "WOKWI_SIMULATOR" for r in rows)
    assert all(r["data_label"].startswith("VIRTUAL") for r in rows)
    for i in range(10):  # at most 10 virtual machines are tracked
        s.ingest(f"{DEFAULT_PREFIX}/m-{i:02d}/telemetry", msg(machine_id=f"m-{i:02d}"), DEFAULT_PREFIX, T0)
    assert len(s.snapshot(T0)) == 10 and "more than 10" in s.counters()["last_reject"]["reason"]


def test_bridge_routes_status_and_telemetry(tmp_path):
    s = VirtualDeviceStore(None)
    b = WokwiBridge(s, "broker.example", prefix=DEFAULT_PREFIX)
    assert b.topics == [f"{DEFAULT_PREFIX}/+/telemetry", f"{DEFAULT_PREFIX}/+/status"]
    b.handle(TOPIC, msg())
    b.handle(f"{DEFAULT_PREFIX}/machine-001/status", b"online")
    d = s.snapshot()[0]
    assert d["device_status"] == "online" and d["machine_id"] == "machine-001"
    assert "password" not in json.dumps(b.status())


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setenv("UDYAM_TICKER", "0")
    monkeypatch.setenv("UDYAM_AUTO_DEMO", "0")
    monkeypatch.delenv("UDYAM_WOKWI_BROKER", raising=False)
    import sys
    import api.app  # noqa: F401
    appmod = sys.modules["api.app"]
    store = VirtualDeviceStore(tmp_path / "virtual.sqlite")
    monkeypatch.setattr(appmod, "wokwi_store", store)
    with TestClient(appmod.app) as c:
        async def step(n):
            for _ in range(n):
                await appmod.broadcast_messages(appmod.factory_engine.step())
        yield c, appmod, store, lambda n: asyncio.run(step(n))
    appmod.wokwi_ref.pop("bridge", None)
    appmod.operating_mode.set("TESTING", by="test teardown")


def test_api_not_configured_by_default(client):
    c, appmod, _, _ = client
    w = c.get("/api/wokwi").json()
    assert w["state"] == "NOT_CONFIGURED" and w["machines"] == [] and "not a measurement of a real machine" in w["notice"]
    om = c.get("/api/operating-mode").json()
    assert om["virtual"]["state"] == "NOT_CONFIGURED" and om["connection"]["state"] == "NOT_CONFIGURED"


def test_api_shows_virtual_device_without_touching_the_simulation(client):
    c, appmod, store, step = client
    bridge = WokwiBridge(store, "broker.example")
    bridge.state = "CONNECTED"
    appmod.wokwi_ref["bridge"] = bridge
    eng = appmod.factory_engine
    step(4)
    before = (eng.t, eng.rng.getstate(), sorted(eng.plants), {m: s["rpm"] for m, s in eng.snapshots.items()})
    bridge.handle(TOPIC, msg(ts=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")))
    after = (eng.t, eng.rng.getstate(), sorted(eng.plants), {m: s["rpm"] for m, s in eng.snapshots.items()})
    assert before == after and "machine-001" not in eng.plants  # Wokwi never enters the simulated plant
    w = c.get("/api/wokwi").json()
    assert w["state"] == "CONNECTED" and w["online_machines"] == ["machine-001"]
    m = w["machines"][0]
    assert m["state"] == "ONLINE" and m["source"] == "WOKWI_SIMULATOR" and m["last"]["current_a"] == 6.42
    assert m["monitoring"]["vibration_severity"]["status"] in ("UNAVAILABLE", "NOT_APPLICABLE")
    assert m["monitoring"]["failure_risk"]["status"] == "NOT_APPLICABLE"
    om = c.get("/api/operating-mode").json()
    assert om["connection"]["state"] == "NOT_CONFIGURED" and om["connection"]["live_machines"] == []  # not a real device
    assert om["virtual"]["state"] == "CONNECTED"
    # Testing-mode snapshots stay pure simulation
    step(4)
    assert all(s["mode"] == "SIM" for s in eng.snapshots.values())
    # history: CSV export, Excel sheet and Live Records
    now = datetime.now(timezone.utc)
    rng = {"start": (now - timedelta(minutes=5)).isoformat().replace("+00:00", "Z"),
           "end": (now + timedelta(minutes=1)).isoformat().replace("+00:00", "Z")}
    lines = c.get("/api/reports/export.csv", params={"section": "virtual_telemetry", **rng}).text.strip().splitlines()
    assert lines[0].startswith("ts,device_ts,machine_id,source,data_label") and len(lines) == 2
    assert "WOKWI_SIMULATOR,VIRTUAL (Wokwi simulator)" in lines[1]
    sim = c.get("/api/reports/export.csv", params={"section": "telemetry", **rng}).text
    assert "machine-001" not in sim
    rec = c.get("/api/reports/recent", params={"kind": "telemetry"}).json()["entries"]
    assert any(e["source"] == "WOKWI" and e["machine_id"] == "machine-001" for e in rec)


def test_status_sent_before_the_first_telemetry_is_kept():
    """The sketch publishes "online" (and the broker replays the retained status) before any telemetry."""
    s = VirtualDeviceStore(None)
    s.status_message(f"{DEFAULT_PREFIX}/machine-001/status", b"online", DEFAULT_PREFIX)
    s.ingest(TOPIC, msg(), DEFAULT_PREFIX, T0)
    assert s.snapshot(T0)[0]["device_status"] == "online"


def test_dashboard_gets_the_wokwi_circuit_from_diagram_json(client):
    """Real Data mode draws the same circuit as the Wokwi project (parts, positions, wires) from its diagram.json."""
    c, _, _, _ = client
    d = c.get("/api/wokwi/diagram").json()
    on_disk = json.loads((ROOT / "wokwi" / "udyam-virtual-machine" / "diagram.json").read_text())
    assert d["parts"] == on_disk["parts"] and d["connections"] == on_disk["connections"]
    assert {p["type"] for p in d["parts"]} >= {"wokwi-dht22", "wokwi-mpu6050", "wokwi-potentiometer", "wokwi-led"}
    assert (ROOT / "web" / "vendor" / "wokwi-elements" / "LICENSE").read_text().startswith("The MIT License")


def test_a_late_last_will_does_not_hide_a_restarted_device():
    """Restarting the sketch: the old connection's last will ("offline") can arrive after the new one is sending."""
    s = VirtualDeviceStore(None)
    st = f"{DEFAULT_PREFIX}/machine-001/status"
    s.status_message(st, b"online", DEFAULT_PREFIX, T0)
    s.ingest(TOPIC, msg(seq=1), DEFAULT_PREFIX, T0 + timedelta(seconds=1))
    s.status_message(st, b"offline", DEFAULT_PREFIX, T0 + timedelta(seconds=0.5))  # will for the old session, sent earlier
    s.ingest(TOPIC, msg(seq=2), DEFAULT_PREFIX, T0 + timedelta(seconds=3))
    s.status_message(st, b"offline", DEFAULT_PREFIX, T0 + timedelta(seconds=2))   # delivered late, older than seq 2
    assert s.snapshot(T0 + timedelta(seconds=4))[0]["state"] == "ONLINE"
    s.status_message(st, b"offline", DEFAULT_PREFIX, T0 + timedelta(seconds=5))   # newer than any telemetry: really gone
    d = s.snapshot(T0 + timedelta(seconds=6))[0]
    assert d["state"] == "OFFLINE" and d["device_status"] == "offline"
