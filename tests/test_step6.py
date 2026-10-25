"""Step 6: OEE / energy / CO₂ / ₹ from ticks, REPLAY + LIVE guard, Reports + CSV, Add Your Machine."""

from __future__ import annotations

import asyncio
import csv
import io
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from sim.analytics import Analytics
from sim.factory import FactoryEngine

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


# ----------------------------------------------------------------- analytics maths (synthetic frames)
def frame(mid, tpl, state, rpm, nominal, power, parts, risk="LOW", ts="2026-01-01T00:00:00.000Z"):
    return [
        {"type": "observed", "machine_id": mid, "parts_done": parts},
        {"type": "snapshot", "machine_id": mid, "template": tpl, "ts": ts, "mode": "SIM", "state": state,
         "rpm": rpm, "rpm_nominal": nominal, "power_kw": power, "risk": {"rules": risk}, "forecast": {},
         "severity": {"zone": "A"}, "signals": {}},
    ]


def test_oee_availability_performance_quality_and_energy():
    a = Analytics()
    parts = 0
    for i in range(100):
        state = "TRIP" if i < 25 else "RUN"            # 75 % available
        rpm = 0.0 if state == "TRIP" else 1232.0       # 80 % speed while running
        if i in (40, 60, 80, 99):
            parts += 1                                 # 4 parts …
        risk = "HIGH" if i == 99 else "LOW"            # … the last one made while risk is HIGH
        a.observe(frame("VMC-01", "cnc_mill", state, rpm, 1540.0, 7.2, parts, risk))
    o = a.summary()["machines"]["VMC-01"]
    assert o["oee"]["availability"] == 0.75
    assert o["oee"]["performance"] == pytest.approx(0.8)
    assert o["oee"]["quality"] == 0.75 and o["oee"]["parts_quality_hold"] == 1
    assert o["oee"]["oee"] == pytest.approx(0.75 * 0.8 * 0.75, abs=1e-4)
    assert o["energy_kwh"] == pytest.approx(7.2 * 0.5 * 100 / 3600, abs=1e-5)
    assert o["mean_power_kw"] == pytest.approx(7.2)


def test_blocked_station_counts_as_performance_loss():
    a = Analytics()
    for i in range(10):
        a.observe([{"type": "line", "stations": [{"machine_id": "SAW-01", "state": "BLOCKED" if i < 5 else "RUNNING"}]},
                   *frame("SAW-01", "bar_saw", "RUN", 900.0, 900.0, 1.5, 0)])
    assert a.summary()["machines"]["SAW-01"]["oee"]["performance"] == pytest.approx(0.5)


def test_site_energy_is_the_sum_of_the_machines():
    a = Analytics()
    a.observe(frame("VMC-01", "cnc_mill", "RUN", 1540, 1540, 6.0, 0) + frame("COMP-01", "air_compressor", "RUN", None, None, 4.0, None))
    assert a.summary()["site_energy_kwh"] == pytest.approx(10.0 * 0.5 / 3600, abs=1e-6)


def test_reset_clears_window_not_session():
    a = Analytics()
    a.observe(frame("VMC-01", "cnc_mill", "RUN", 1540, 1540, 6.0, 0))
    a.observe([{"type": "event", "kind": "RESET"}])
    assert a.summary("window")["machines"] == {} and a.summary("session")["machines"]["VMC-01"]["energy_kwh"] > 0


def test_vibration_fault_lowers_vmc_oee_in_the_engine():
    eng = FactoryEngine(seed=42, start=T0)
    healthy, faulty = Analytics(), Analytics()
    for _ in range(240):
        healthy.observe(eng.step())
    eng.command({"cmd": "reset"})
    eng.command({"cmd": "start_scenario", "scenario": "vibration_fault"})
    for _ in range(240):
        faulty.observe(eng.step())
    h, f = (x.summary()["machines"]["VMC-01"]["oee"] for x in (healthy, faulty))
    assert h["performance"] > 0.99 and f["performance"] < h["performance"] - 0.1  # 20 % derate
    assert f["oee"] < h["oee"]


# ----------------------------------------------------------------- API fixtures
@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("UDYAM_AUTO_DEMO", "0")
    monkeypatch.setenv("UDYAM_TICKER", "0")
    from api.app import app, factory_engine
    with TestClient(app) as c:
        c.post("/api/auto_demo?on=false")
        c.post("/api/mode", json={"mode": "SIM"})
        yield c
        for spec in list(factory_engine.custom_machines):
            c.delete(f"/api/machines/{spec['machine_id']}")
        c.post("/api/mode", json={"mode": "SIM"})


def drive(seconds: float) -> None:
    from api.app import broadcast_messages, factory_engine

    async def go():
        for _ in range(int(seconds / 0.5)):
            await broadcast_messages(factory_engine.step())
    asyncio.run(go())


def test_api_analytics_factors_and_illustrative_roi(client):
    client.post("/api/reset")
    drive(30)
    r = client.get("/api/analytics").json()
    f, s = r["factors"], r["site"]
    assert s["co2_kg"] == pytest.approx(s["energy_kwh"] * f["grid_co2_kg_per_kwh"], abs=1e-4)
    assert s["cost_inr"] == pytest.approx(s["energy_kwh"] * f["tariff_inr_per_kwh"], abs=1e-3)
    assert "CEA" in f["grid_co2_source"]
    roi = r["roi"]
    a = roi["assumptions"]
    assert roi["label"] == r["projection"]["label"] == "illustrative estimate"
    assert roi["payback_months"] == pytest.approx(
        a["sensor_nodes"] * a["sensor_node_cost_inr"]
        / (a["baseline_downtime_h_per_month"] * a["assumed_downtime_reduction_pct"] / 100 * a["downtime_cost_per_hr_inr"]),
        abs=0.01)
    assert "not a measured or guaranteed saving" in roi["note"]
    assert s["line_oee"]["oee"] is not None
    # Factors are editable and flow straight into the numbers.
    old = client.get("/api/economics").json()["assumptions"]["cea_co2_factor"]
    try:
        client.post("/api/economics", json={"cea_co2_factor": 0.82})
        r2 = client.get("/api/analytics").json()
        assert r2["factors"]["grid_co2_kg_per_kwh"] == 0.82
        assert r2["site"]["co2_kg"] == pytest.approx(r2["site"]["energy_kwh"] * 0.82, abs=1e-4)
        assert client.post("/api/economics", json={"assumed_downtime_reduction_pct": 150}).status_code == 422
    finally:
        client.post("/api/economics", json={"cea_co2_factor": old})


# ----------------------------------------------------------------- REPLAY / LIVE
def test_replay_streams_ai4i_rows_with_labels():
    eng = FactoryEngine(seed=1, start=T0)
    eng.command({"cmd": "set_mode", "mode": "REPLAY", "start_row": 0})
    seen, events = [], []
    for _ in range(30):
        msgs = eng.step()
        events += [m for m in msgs if m["type"] == "event"]
        seen.append(eng.snapshots["VMC-01"])
    assert [s["replay"]["udi"] for s in seen] == list(range(1, 31))
    first = seen[0]
    assert (first["mode"], first["clock"], first["source_label"]) == ("REPLAY", "replay_time", "Replayed dataset (REPLAY)")
    assert first["rpm"] == 1551 and first["temp_c"] == pytest.approx(308.6 - 273.15, abs=0.01)  # AI4I row UDI 1
    assert first["vibration_rms_mm_s"] is None and first["current_a"] is None  # not in AI4I, not invented
    assert set(first["replay"]["labels"]) == {"machine_failure", "TWF", "HDF", "PWF", "OSF", "RNF"}
    assert {e["kind"] for e in events}.isdisjoint({"CALIBRATION", "DRIFT"})
    assert not any(e["kind"] == "ALERT" and "auto-derated" in e["title"] for e in events)
    assert eng.snapshots["LATHE-01"]["mode"] == "SIM"  # only the target mill is replayed
    eng.command({"cmd": "set_mode", "mode": "SIM"})
    eng.step()
    assert eng.snapshots["VMC-01"]["mode"] == "SIM" and eng.snapshots["VMC-01"]["replay"] is None


def test_replay_marks_held_out_rows():
    from sim.replay import Ai4iReplay
    r = Ai4iReplay()
    assert len(r.held_out) == 2000


def test_replay_scores_each_row_on_its_own():
    eng = FactoryEngine(seed=1, start=T0)
    eng.command({"cmd": "set_mode", "mode": "REPLAY", "start_row": 3575})
    risks = {}
    for _ in range(8):
        eng.step()
        s = eng.snapshots["VMC-01"]
        risks[s["replay"]["udi"]] = (s["replay"]["labels"]["PWF"], s["risk"]["ml"]["failure_risk_pct"])
    assert risks[3580][0] == 1 and risks[3580][1] > 90  # a real AI4I power failure row


def test_api_mode_and_live_guard(client):
    assert client.post("/api/mode", json={"mode": "LIVE"}).status_code == 409
    assert client.get("/api/mode").json()["live"]["message"] == "No live device connected"
    assert client.post("/api/mode", json={"mode": "NOPE"}).status_code == 422
    r = client.post("/api/mode", json={"mode": "REPLAY", "start_row": 10}).json()
    assert r["mode"] == "REPLAY" and r["replay"]["row"] == 10 and r["replay"]["machine_id"] == "VMC-01"
    assert client.post("/api/mode", json={"mode": "REPLAY", "machine_id": "LATHE-01"}).status_code == 409
    drive(1)
    assert client.get("/api/mode").json()["sources"]["VMC-01"] == "REPLAY"
    assert client.post("/api/mode", json={"mode": "SIM"}).json()["mode"] == "SIM"
    client.post("/api/auto_demo?on=true")
    client.post("/api/mode", json={"mode": "REPLAY"})
    assert client.get("/api/auto_demo").json()["on"] is False  # REPLAY stops the (SIM) Auto Demo


def test_live_mode_allowed_only_with_a_real_online_device(client, monkeypatch):
    from api.app import iot_hub
    monkeypatch.setattr(iot_hub, "devices", {})
    monkeypatch.setattr(iot_hub, "db_path", None)
    sim = client.post("/api/iot/devices", json={"device_id": "T-SIM", "machine_id": "SAW-01", "simulated": True}).json()
    client.post("/api/iot/telemetry", json={"vibration_rms_mm_s": 1.1},
                headers={"X-Device-Id": "T-SIM", "X-Device-Token": sim["token"]})
    assert client.post("/api/mode", json={"mode": "LIVE"}).status_code == 409  # a simulated device is not LIVE
    real = client.post("/api/iot/devices", json={"device_id": "T-HW", "machine_id": "LATHE-01"}).json()
    client.post("/api/iot/telemetry", json={"vibration_rms_mm_s": 1.2},
                headers={"X-Device-Id": "T-HW", "X-Device-Token": real["token"]})
    r = client.post("/api/mode", json={"mode": "LIVE"})
    assert r.status_code == 200 and r.json()["mode"] == "LIVE" and r.json()["live"]["machines"] == ["LATHE-01"]


# ----------------------------------------------------------------- Reports
def test_reports_and_csv_export(client):
    client.post("/api/reset")
    client.post("/api/scenarios/vibration_fault/start")
    drive(20)
    rep = client.get("/api/reports?trend_minutes=5").json()
    assert {"window", "session", "trends", "alerts", "work_orders"} <= set(rep)
    assert rep["trends"]["VMC-01"] and {"ts", "mode", "rpm", "temp_c", "power_kw", "zone"} <= set(rep["trends"]["VMC-01"][0])
    assert any(a["machine_id"] == "VMC-01" for a in rep["alerts"])
    for section, col in (("summary", "metric"), ("energy", "energy_kwh"), ("oee", "oee"), ("trends", "vibration_rms_mm_s"),
                         ("alerts", "alert_id"), ("work_orders", "wo_id")):
        r = client.get(f"/api/reports/export.csv?section={section}")
        assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
        assert f"udyam-twin-{section}-" in r.headers["content-disposition"]
        rows = list(csv.DictReader(io.StringIO(r.text)))
        assert col in rows[0] if rows else col in r.text.splitlines()[0]
    summary = {r["metric"]: r for r in csv.DictReader(io.StringIO(client.get("/api/reports/export.csv").text))}
    assert summary["site_co2"]["unit"] == "kg" and "illustrative" in summary["payback_months"]["basis"]
    assert client.get("/api/reports/export.csv?section=nope").status_code == 422


# ----------------------------------------------------------------- Add Your Machine
NEW_VMC = {"template": "cnc_mill", "name": "Haas VF-2", "rated_kw": 15, "rated_rpm": 2000, "foundation": "rigid",
           "iso_class": "ISO 10816-3 Group 2, rigid", "zone_boundaries": {"A_B": 1.8, "B_C": 3.5, "C_D": 5.0},
           "thresholds": {"process_temp_limit_k": 316.0}}


@pytest.mark.parametrize("body,needle", [
    ({"template": "robot_arm"}, "template"),
    ({"template": "conveyor"}, "template"),
    ({"template": "cnc_mill", "name": "x"}, "name"),
    ({"template": "cnc_mill", "rated_kw": 0}, "rated_kw"),
    ({"template": "cnc_mill", "rated_rpm": 5000, "nameplate": {"max_rpm": 3000}}, "exceeds max_rpm"),
    ({"template": "air_compressor", "rated_rpm": 1500}, "does not apply"),
    ({"template": "cnc_mill", "zone_boundaries": {"A_B": 3, "B_C": 2, "C_D": 5}}, "strictly increasing"),
    ({"template": "cnc_mill", "thresholds": {"bogus": 1}}, "unknown thresholds"),
    ({"template": "cnc_mill", "thresholds": {"tool_wear_warn_min": 500}}, "must hold"),
    ({"template": "cnc_mill", "foundation": "floating"}, "foundation"),
])
def test_add_machine_validation(client, body, needle):
    r = client.post("/api/machines", json=body)
    assert r.status_code == 422 and needle in str(r.json()["detail"])


def test_added_machine_appears_everywhere_and_survives_reset(client):
    from api.app import factory_engine
    r = client.post("/api/machines", json=NEW_VMC).json()
    mid = r["machine_id"]
    assert r["config"]["thresholds"]["process_temp_limit_k"] == 316.0 and r["config"]["nameplate"]["rated_kw"] == 15
    drive(3)
    snap = next(s for s in client.get("/api/snapshots").json() if s["machine_id"] == mid)
    assert snap["rpm_nominal"] == 2000 and snap["severity"]["profile_id"] == f"custom-{mid.lower()}"
    assert snap["severity"]["zone"] == "A" and "Operator-entered" in snap["severity"]["label"]
    assert snap["risk"]["ml"]["status"] == "OK"  # a CNC mill gets the AI4I model like VMC-01
    assert any(m["machine_id"] == mid and m["custom"] for m in client.get("/api/registry").json()["machines"])
    assert client.get(f"/api/vibration/{mid}").json()["severity"]["status"] == "PROVISIONAL"
    assert mid in client.get("/api/analytics").json()["machines"]
    assert mid in client.get("/api/config").json()["runtime_machines"]
    site = client.get("/api/analytics").json()["machines"]
    assert site[mid]["energy_kwh"] > 0  # counted in the site energy

    client.post("/api/reset")
    drive(1)
    assert mid in factory_engine.plants and mid in {s["machine_id"] for s in client.get("/api/snapshots").json()}

    # A server restart re-creates it from SQLite.
    from api.app import _restore_custom_machines
    from config import get_config
    get_config().unregister_machine(mid)
    factory_engine.custom_machines.clear()
    factory_engine.command({"cmd": "reset"})
    assert mid not in factory_engine.plants
    _restore_custom_machines()
    factory_engine.command({"cmd": "reset"})
    assert mid in factory_engine.plants and factory_engine.plants[mid].nameplate["rpm_nominal"] == 2000

    assert client.delete("/api/machines/VMC-01").status_code == 409
    assert client.delete(f"/api/machines/{mid}").json()["status"] == "removed"
    assert mid not in factory_engine.plants and client.delete(f"/api/machines/{mid}").status_code == 404


def test_legacy_dashboard_form_still_works(client):
    """The current dashboard sends {template, name, nameplate: {rated_kw, max_rpm}} over the WebSocket."""
    r = client.post("/api/machines", json={"template": "air_compressor", "name": "Screw Compressor 2",
                                           "nameplate": {"rated_kw": 11.0, "max_rpm": 8.0}})
    assert r.status_code == 200 and r.json()["config"]["nameplate"]["rated_kw"] == 11.0
    drive(1)
    from api.app import factory_engine
    assert r.json()["machine_id"] in factory_engine.snapshots  # extra compressors now tick too
    with client.websocket_connect("/ws/telemetry") as ws:
        ws.receive_json(), ws.receive_json()
        ws.send_json({"cmd": "add_machine", "template": "cnc_mill", "name": "x"})
        err = ws.receive_json()
        while err.get("type") != "error":
            err = ws.receive_json()
        assert err["cmd"] == "add_machine" and "name" in str(err["detail"])
