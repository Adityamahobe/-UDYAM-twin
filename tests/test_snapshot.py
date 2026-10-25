"""Step 2: snapshot single source of truth, UTC clock, derate on setpoint, calibration throttle, cooling alert."""

from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from pathlib import Path

import jsonschema
import pytest
from fastapi.testclient import TestClient

from config import get_config
from ml.vibration.inference import VibrationPredictor
from ml.vibration.monitor import VibrationMonitor
from ml.vibration.severity import SeverityAssessor
from sim.factory import FactoryEngine

SCHEMA = json.loads((Path(__file__).resolve().parent.parent / "docs" / "schema.json").read_text(encoding="utf-8"))
VALIDATOR = jsonschema.Draft202012Validator(SCHEMA)
T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


def run(engine: FactoryEngine, ticks: int) -> list[list[dict]]:
    return [engine.step() for _ in range(ticks)]


def coolant_engine(**kw) -> FactoryEngine:
    eng = FactoryEngine(seed=42, auto_demo=False, start=T0, **kw)
    eng.command({"cmd": "inject_fault", "machine_id": "VMC-01", "fault": "coolant_degradation"})
    return eng


# ----------------------------------------------------------------- clock
def test_default_clock_is_real_utc():
    eng = FactoryEngine(auto_demo=False)
    ts = next(m["ts"] for m in eng.step() if m["type"] == "observed")
    when = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    assert abs((datetime.now(timezone.utc) - when).total_seconds()) < 5
    assert eng.clock == "wall_utc"


def test_fixed_start_gives_deterministic_sim_time():
    eng = FactoryEngine(auto_demo=False, start=T0)
    ticks = run(eng, 4)
    assert eng.clock == "sim_time"
    assert ticks[-1][0]["ts"] == "2026-01-01T00:00:02.000Z"
    assert {m["clock"] for m in ticks[-1] if m["type"] == "snapshot"} == {"sim_time"}


# ----------------------------------------------------------------- snapshot
def test_one_valid_snapshot_per_machine_per_tick():
    eng = FactoryEngine(seed=42, start=T0)
    eng.command({"cmd": "start_scenario", "scenario": "vibration_fault"})
    eng.command({"cmd": "inject_fault", "machine_id": "COMP-01", "fault": "pressure_drop"})
    for msgs in run(eng, 300):
        snaps = [m for m in msgs if m["type"] == "snapshot"]
        assert sorted(s["machine_id"] for s in snaps) == sorted(eng.plants)
        for s in snaps:
            errors = list(VALIDATOR.iter_errors(s))
            assert not errors, errors[0].message


def test_snapshot_agrees_with_observed_frame():
    eng = FactoryEngine(seed=3, auto_demo=False, start=T0)
    for msgs in run(eng, 20):
        obs = {m["machine_id"]: m for m in msgs if m["type"] == "observed"}
        for s in (m for m in msgs if m["type"] == "snapshot"):
            o = obs[s["machine_id"]]
            assert s["ts"] == o["ts"]
            for k in ("rpm", "vibration_rms_mm_s", "power_kw", "current_a"):
                assert s[k] == o.get(k), k
            assert s["state"] == o["plc_state"]
            if o.get("process_temp_k") is not None:
                assert s["temp_c"] == pytest.approx(o["process_temp_k"] - 273.15, abs=0.01)
            rated = eng.plants[s["machine_id"]].nameplate["rated_kw"]
            assert s["load_pct"] == pytest.approx(100 * o["power_kw"] / rated, abs=0.1)


def test_energy_is_integrated_from_power():
    eng = FactoryEngine(seed=5, auto_demo=False, start=T0)
    total = 0.0
    for msgs in run(eng, 120):
        o = next(m for m in msgs if m["type"] == "observed" and m["machine_id"] == "VMC-01")
        total += o["power_kw"] * 0.5 / 3600.0
    assert eng.snapshots["VMC-01"]["energy_kwh"] == pytest.approx(total, abs=1e-4)


def test_snapshot_severity_matches_vibration_panel():
    """The Vibration panel (REST) and the snapshot must give the same zone for the stream value."""
    eng = FactoryEngine(seed=42, auto_demo=False, start=T0)
    info = lambda mid: {"template": eng.plants[mid].template, "name": eng.plants[mid].name,  # noqa: E731
                        "rated_kw": eng.plants[mid].nameplate["rated_kw"],
                        "stream_band_hz": get_config().stream_band_hz(mid, eng.plants[mid].template)}
    mon = VibrationMonitor(SeverityAssessor(), VibrationPredictor(Path("/nonexistent.joblib")), info)
    eng.command({"cmd": "inject_fault", "machine_id": "VMC-01", "fault": "bearing_defect"})
    for msgs in run(eng, 30):
        mon.observe(msgs)
        for s in (m for m in msgs if m["type"] == "snapshot"):
            panel = mon.snapshot(s["machine_id"], trend=False)
            assert panel["stream_velocity"]["value"] == s["vibration_rms_mm_s"]
            assert (panel["severity"]["status"], panel["severity"]["zone"]) == (s["severity"]["status"], s["severity"]["zone"])


# ----------------------------------------------------------------- derate
def test_manual_derate_scales_setpoint_exactly():
    eng = FactoryEngine(seed=1, auto_demo=False, start=T0)
    eng.command({"cmd": "set_derate", "machine_id": "VMC-01", "pct": 20})
    s = run(eng, 2)[-1]
    snap = next(m for m in s if m["type"] == "snapshot" and m["machine_id"] == "VMC-01")
    assert snap["state"] == "DERATE" and snap["derate_pct"] == 20
    assert snap["rpm_setpoint"] == pytest.approx(snap["rpm_nominal"] * 0.8)
    assert snap["rpm_setpoint"] == 1232.0


def test_auto_derate_alert_matches_snapshot():
    eng = coolant_engine()  # coolant loss → forecast → auto-derate
    alert = None
    for msgs in run(eng, 400):
        for m in msgs:
            if m["type"] == "event" and m["kind"] == "ALERT" and "auto-derated" in m["title"]:
                alert = m
        if alert:
            break
    assert alert is not None
    d = alert["data"]
    assert (d["rpm_setpoint_from"], d["rpm_setpoint_to"]) == (1540.0, 1232.0)
    assert "1540 → 1232 rpm" in alert["title"] and "(SIM only)" in alert["title"]
    for key in ("machine_id", "cause", "evidence", "recommended_action", "priority"):
        assert d["work_order_draft"][key]
    nxt = next(m for m in eng.step() if m["type"] == "snapshot" and m["machine_id"] == "VMC-01")
    assert nxt["state"] == "DERATE" and nxt["rpm_setpoint"] == d["rpm_setpoint_to"]


# ----------------------------------------------------------------- calibration throttle + cooling alert
def test_calibration_events_are_throttled():
    lim = get_config().thresholds("VMC-01")
    eng = coolant_engine()
    cal = []
    for i, msgs in enumerate(run(eng, 600)):  # 300 s
        cal += [(i * 0.5, m) for m in msgs if m["type"] == "event" and m["kind"] == "CALIBRATION"]
    assert 1 <= len(cal) <= math.ceil(300 / lim["calibration_event_min_interval_s"])
    for (t1, a), (t2, b) in zip(cal, cal[1:]):
        assert t2 - t1 >= lim["calibration_event_min_interval_s"]
        assert b["data"]["from"] == a["data"]["to"]  # collapsed events chain without gaps
    for _, ev in cal:
        assert abs(ev["data"]["to"] - ev["data"]["from"]) >= lim["calibration_event_min_delta"] - 1e-9


def test_cooling_loss_raises_exactly_one_alert():
    lim = get_config().thresholds("VMC-01")
    eng = coolant_engine()
    alerts, snaps = [], []
    for msgs in run(eng, 600):
        alerts += [m for m in msgs if m["type"] == "event" and m.get("event_id", "").endswith("EV_COOLING_VMC-01")]
        snaps += [m for m in msgs if m["type"] == "snapshot" and m["machine_id"] == "VMC-01"]
    assert len(alerts) == 1
    a = alerts[0]
    assert a["kind"] == "ALERT" and a["severity"] == "WARN"
    assert a["data"]["cooling_loss_pct"] >= lim["cooling_loss_alert_pct"]
    assert a["measured"] and a["inferred"] and a["suggested"].startswith("Inspect")
    assert a["data"]["work_order_draft"]["priority"] == "High"
    assert max(s["calibration"]["cooling_loss_pct"] for s in snaps) >= lim["cooling_loss_alert_pct"]


def test_cooling_alert_rearms_after_reset():
    eng = coolant_engine()
    run(eng, 400)
    assert eng.twins["VMC-01"].cooling_alerted
    eng.command({"cmd": "reset"})
    assert not eng.twins["VMC-01"].cooling_alerted


# ----------------------------------------------------------------- event hygiene
def test_event_ids_unique_and_temperatures_in_celsius():
    eng = coolant_engine()
    ids = set()
    for msgs in run(eng, 290):
        for m in (m for m in msgs if m["type"] == "event"):
            assert m["event_id"] not in ids
            ids.add(m["event_id"])
            text = " ".join(str(m.get(k, "")) for k in ("title", "measured", "inferred"))
            if "temp" in text.lower():
                assert "°C" in text and " K" not in text.replace(" K)", ""), text


# ----------------------------------------------------------------- API
def test_api_snapshots_and_websocket_push():
    from api.app import app
    with TestClient(app) as c:
        with c.websocket_connect("/ws/telemetry") as ws:
            assert ws.receive_json()["type"] == "hello"
            snap = None
            for _ in range(60):
                msg = ws.receive_json()
                if msg.get("type") == "snapshot":
                    snap = msg
                    break
        assert snap is not None and snap["clock"] == "wall_utc"
        rest = c.get("/api/snapshots").json()
        assert {"VMC-01", "LATHE-01", "SAW-01", "COMP-01"} <= {s["machine_id"] for s in rest}
