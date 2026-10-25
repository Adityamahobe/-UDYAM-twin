"""Testing-mode machine parameters and run control: limits, effects on the simulated plant, pause / stop /
timed runs, and the Real Data read-only guard."""

from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from sim import controls
from sim.factory import FactoryEngine


def _settle(eng: FactoryEngine, n: int = 600):
    for _ in range(n):
        eng.step()
    return eng.plants["VMC-01"], eng.snapshots["VMC-01"]


def test_defaults_leave_the_simulation_unchanged():
    a, b = FactoryEngine(), FactoryEngine()
    b.set_params("VMC-01", controls.defaults_for("cnc_mill"))
    assert "VMC-01" not in b.sim_params  # defaults are not stored as custom
    for _ in range(200):
        oa, ob = a.step(), b.step()
    pick = lambda out: [(m["machine_id"], m.get("power_kw"), m.get("process_temp_k"), m.get("vibration_rms_mm_s"))
                        for m in out if m.get("type") == "observed"]
    assert pick(oa) == pick(ob)


def test_limits_are_refused_not_clamped():
    eng = FactoryEngine()
    for bad, words in [({"spindle_speed_pct": 200}, "outside the limit"),
                       ({"ambient_temp_c": -5}, "outside the limit"),
                       ({"cooling_capacity_pct": float("nan")}, "outside the limit"),
                       ({"load_pct": "heavy"}, "must be a number"),
                       ({"pressure_bar": 9}, "not a parameter")]:
        with pytest.raises(controls.ParamError, match=words):
            eng.set_params("VMC-01", bad)
    with pytest.raises(controls.ParamError, match="not a parameter"):
        eng.set_params("COMP-01", {"load_pct": 110})  # compressors have no cutting load
    # combined limit: the lathe's 7.5 kW spindle can't do 120 % speed at 130 % load
    with pytest.raises(controls.ParamError, match="rated 7.5 kW"):
        eng.set_params("LATHE-01", {"spindle_speed_pct": 120, "load_pct": 130})
    assert eng.sim_params == {} and eng.plants["LATHE-01"].speed_scale == 1.0


def test_parameters_flow_through_to_readings_and_the_twin():
    base_p, _ = _settle(FactoryEngine())
    hot = FactoryEngine()
    hot.auto_derate = False
    ev = hot.set_params("VMC-01", {"ambient_temp_c": 38, "cooling_capacity_pct": 60})
    assert ev[0]["kind"] == "SIM_PARAMS" and ev[0]["data"]["simulation_only"]
    hot_p, _ = _settle(hot)
    assert hot_p.air_temp_k > base_p.air_temp_k + 10
    assert hot_p.process_temp_k > base_p.process_temp_k + 8
    # the twin is not told: it recalibrates its cooling estimate from the temperatures
    assert hot.twins["VMC-01"].calibrator.h_cool < 0.95

    slow = FactoryEngine()
    slow.set_params("VMC-01", {"spindle_speed_pct": 70})
    _, s = _settle(slow, 40)
    assert s["rpm"] == pytest.approx(0.7 * 1540, rel=0.03)

    shaky = FactoryEngine()
    shaky.set_params("SAW-01", {"extra_vibration_mm_s": 3})
    _settle(shaky, 20)
    assert shaky.snapshots["SAW-01"]["vibration_rms_mm_s"] > 3.5


def test_parameters_survive_reset_and_can_be_restored():
    eng = FactoryEngine()
    eng.set_params("VMC-01", {"spindle_speed_pct": 80})
    eng.reset()
    assert eng.plants["VMC-01"].speed_scale == pytest.approx(0.8)
    ev = eng.reset_params()
    assert eng.sim_params == {} and eng.plants["VMC-01"].speed_scale == 1.0
    assert any(e["kind"] == "SIM_PARAMS" for e in ev)


def test_pause_resume_stop_and_timed_run():
    eng = FactoryEngine()
    _settle(eng, 10)
    eng.plants["VMC-01"].faults.inject("overload", eng.t)
    eng.run_control("pause")
    t = eng.t
    assert eng.step() == [] and eng.t == t  # frozen
    eng.run_control("resume")
    assert eng.step() and eng.t == t + 0.5
    eng.run_control("stop")
    assert eng.run_state == "STOPPED" and not eng.plants["VMC-01"].faults.active_faults  # clean baseline
    assert eng.step() == []
    eng.run_control("start")
    eng.run_control("run_for", minutes=1)
    ticks = 0
    while eng.running and ticks < 1000:
        out = eng.step()
        ticks += 1
    assert ticks == 120 and eng.run_state == "PAUSED"
    assert any(e.get("kind") == "SIM_RUN" and "finished" in e["title"] for e in out)
    for bad in (0, -1, 241, None):
        with pytest.raises(controls.ParamError):
            eng.run_control("run_for", minutes=bad)


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("UDYAM_TICKER", "0")
    monkeypatch.setenv("UDYAM_AUTO_DEMO", "0")
    from api.app import app, broadcast_messages, factory_engine, operating_mode
    from api.operating_mode import TESTING
    with TestClient(app) as c:
        async def step(n):
            for _ in range(n):
                await broadcast_messages(factory_engine.step())
        yield c, factory_engine, lambda n: asyncio.run(step(n))
    operating_mode.set(TESTING, by="test teardown")
    factory_engine.run_control("start")
    factory_engine.reset_params()


def test_api_sets_parameters_and_reports_limits(client):
    c, eng, step = client
    s = c.get("/api/sim/controls").json()
    vmc = next(m for m in s["machines"] if m["machine_id"] == "VMC-01")
    assert s["simulation_only"] and vmc["default"] and {p["key"] for p in vmc["specs"]} == set(controls.PARAMS)
    speed = next(p for p in vmc["specs"] if p["key"] == "spindle_speed_pct")
    assert (speed["min"], speed["max"]) == (40.0, 120.0)
    r = c.post("/api/sim/params", json={"machine_id": "VMC-01", "params": {"spindle_speed_pct": 300}})
    assert r.status_code == 422 and r.json()["detail"]["error"] == "parameter_out_of_limits"
    assert c.post("/api/sim/params", json={"machine_id": "NOPE-9", "params": {}}).status_code == 404
    r = c.post("/api/sim/params", json={"machine_id": "VMC-01", "params": {"spindle_speed_pct": 75}}).json()
    assert next(m for m in r["machines"] if m["machine_id"] == "VMC-01")["values"]["spindle_speed_pct"] == 75
    step(30)
    assert eng.snapshots["VMC-01"]["rpm"] == pytest.approx(0.75 * 1540, rel=0.03)
    assert any(e["kind"] == "SIM_PARAMS" for e in c.get("/api/events?limit=20").json())
    assert c.post("/api/sim/params/reset?machine_id=VMC-01").json()["machines"][0]["default"]


def test_api_run_control(client):
    c, eng, step = client
    assert c.post("/api/sim/run", json={"action": "pause"}).json()["run"]["state"] == "PAUSED"
    t = eng.t
    step(5)
    assert eng.t == t
    assert c.post("/api/sim/run", json={"action": "run_for", "minutes": 0.5}).json()["run"]["remaining_s"] == 30.0
    step(60)
    assert c.get("/api/sim/controls").json()["run"]["state"] == "PAUSED"
    assert c.post("/api/sim/run", json={"action": "run_for", "minutes": 999}).status_code == 422
    assert c.post("/api/sim/run", json={"action": "explode"}).status_code == 422
    assert c.post("/api/sim/run", json={"action": "stop"}).json()["run"]["state"] == "STOPPED"
    # turning the Auto Demo on runs the simulation again
    c.post("/api/auto_demo?on=true")
    assert eng.run_state == "RUNNING"
    c.post("/api/auto_demo?on=false")


def test_real_data_mode_refuses_parameters_and_run_control(client):
    c, eng, _ = client
    c.post("/api/sim/run", json={"action": "pause"})
    c.post("/api/operating-mode", json={"mode": "REAL_DATA"})
    assert eng.run_state == "RUNNING"  # real readings arrive through the engine tick, so it runs
    for url, body in [("/api/sim/params", {"machine_id": "VMC-01", "params": {"load_pct": 120}}),
                      ("/api/sim/run", {"action": "pause"}),
                      ("/api/sim/run", {"action": "stop"})]:
        r = c.post(url, json=body)
        assert r.status_code == 409 and r.json()["detail"]["error"] == "real_data_read_only", url
    assert c.post("/api/sim/params/reset").status_code == 409
    assert eng.sim_params == {} and eng.running
    assert c.get("/api/sim/controls").status_code == 200  # reading is still fine


def test_event_ids_stay_unique_across_restarts(tmp_path):
    """A restarted engine numbers events after the stored ones, so new events are not dropped as duplicates."""
    from api.db import get_events, init_db, log_event, max_event_seq
    db = tmp_path / "t.sqlite"
    init_db(db)
    assert max_event_seq(db) == 0
    for eid in ("E00007", "E00012-EV_CAL_VMC-01", "WEIRD"):
        log_event({"event_id": eid, "ts": "2026-10-25T10:00:00.000Z", "kind": "INFO", "severity": "INFO", "title": eid}, db)
    assert max_event_seq(db) == 12
    eng = FactoryEngine()
    eng.event_seq = max(eng.event_seq, max_event_seq(db))
    for ev in eng.set_params("VMC-01", {"load_pct": 110}):
        log_event(ev, db)
    assert get_events(1, db)[0]["kind"] == "SIM_PARAMS"
