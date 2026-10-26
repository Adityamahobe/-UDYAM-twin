"""Factory Run Designer: event validation, fast runs with ground-truth labels and detection, batches, exports,
and live playback on the live simulation (Testing mode only)."""

from __future__ import annotations

import asyncio
import time

import pytest
from fastapi.testclient import TestClient

from sim.factory_run import PRESETS, RunError, RunSpec, Variation, batch, machines_of, run, validate, vary

ECON = {"inr_per_part": 450.0, "electricity_tariff_per_kwh": 8.5}
FAULT = {"at_min": 3, "action": "inject_fault", "machine_id": "LATHE-01", "fault": "bearing_defect", "severity": 1.2}


@pytest.mark.parametrize("ev, words", [
    ({"at_min": 30, "action": "repair", "machine_id": "VMC-01"}, "within the run"),
    ({"at_min": 1, "action": "explode", "machine_id": "VMC-01"}, "action must be"),
    ({"at_min": 1, "action": "repair", "machine_id": "NOPE-1"}, "unknown machine"),
    ({"at_min": 1, "action": "inject_fault", "machine_id": "SAW-01", "fault": "bearing_defect"}, "can have: blade_wear"),
    ({"at_min": 1, "action": "inject_fault", "machine_id": "VMC-01", "fault": "overload", "severity": 3}, "severity"),
    ({"at_min": 1, "action": "set_params", "machine_id": "VMC-01", "params": {"spindle_speed_pct": 300}}, "outside the limit"),
    ({"at_min": 1, "action": "set_params", "machine_id": "VMC-01", "params": {}}, "at least one parameter"),
    ({"at_min": 1, "action": "derate", "machine_id": "VMC-01", "pct": 80}, "1–50"),
])
def test_invalid_events_are_refused(ev, words):
    with pytest.raises(RunError, match=words):
        validate(RunSpec(minutes=30, events=[ev]), machines_of())


def test_presets_are_valid_and_runs_are_reproducible():
    m = machines_of()
    for p in PRESETS.values():
        validate(RunSpec(minutes=p["minutes"], events=p["events"]), m)
    a = run(RunSpec(minutes=6, events=[FAULT]), ECON)
    b = run(RunSpec(minutes=6, events=[FAULT]), ECON)
    assert a["kpis"] == b["kpis"] and a["trends"] == b["trends"] and a["label"] == "SYNTHETIC DIGITAL TWIN DATA"


def test_labels_follow_the_events_and_detection_is_measured():
    r = run(RunSpec(minutes=12, events=[FAULT, {"at_min": 9, "action": "repair", "machine_id": "LATHE-01"},
                                        {"at_min": 2, "action": "derate", "machine_id": "VMC-01", "pct": 30}]), ECON)
    lab = r["labels"][0]
    assert (lab["machine_id"], lab["fault"], lab["severity"], lab["start_min"], lab["end_min"]) == ("LATHE-01", "bearing_defect", 1.2, 3.0, 9.0)
    assert lab["detected"] and 0 <= lab["detection_delay_min"] < 1 and "Vibration" in lab["first_alert"]
    lathe = r["trends"]["LATHE-01"]
    assert any("bearing_defect" in p["faults"] for p in lathe if 3 < p["t_min"] < 9)
    assert not any(p["faults"] for p in lathe if p["t_min"] > 9.2)  # repaired
    assert all(p["derate_pct"] == 30 for p in r["trends"]["VMC-01"] if p["t_min"] > 2.5)
    assert r["kpis"]["faults_detected"] == r["kpis"]["faults_injected"] == 1
    calm = run(RunSpec(minutes=6), ECON)
    assert calm["labels"] == [] and calm["kpis"]["faults_injected"] == 0


def test_self_healing_off_means_no_auto_derate():
    cool = {"at_min": 1, "action": "inject_fault", "machine_id": "VMC-01", "fault": "coolant_degradation", "severity": 1.5}
    on = run(RunSpec(minutes=12, self_healing=True, events=[cool]), ECON)
    off = run(RunSpec(minutes=12, events=[cool, {"at_min": 0, "action": "self_healing", "on": False}]), ECON)
    assert any((p["derate_pct"] or 0) > 0 for p in on["trends"]["VMC-01"])
    assert not any((p["derate_pct"] or 0) > 0 for p in off["trends"]["VMC-01"])


def test_batch_variation_is_reproducible_and_bounded():
    m = machines_of()
    base = RunSpec(minutes=10, events=[FAULT, {"at_min": 1, "action": "set_params", "machine_id": "VMC-01", "params": {"load_pct": 110}}])
    v = Variation(fault_time_jitter_min=2, severity_range=[0.8, 1.2], param_jitter_pct=10)
    s1, s2 = vary(base, v, 3, m), vary(base, v, 3, m)
    assert s1.events == s2.events and s1.seed == base.seed + 3
    for i in range(5):
        s = vary(base, v, i, m)
        f = next(e for e in s.events if e["action"] == "inject_fault")
        p = next(e for e in s.events if e["action"] == "set_params")
        assert 1 <= f["at_min"] <= 5 and 0.8 <= f["severity"] <= 1.2 and 99 <= p["params"]["load_pct"] <= 121
    b = batch(base, v, 4, ECON)
    d = b["detection"]["LATHE-01 · bearing_defect"]
    assert b["runs"] == 4 and d["injected"] == 4 and 0 <= d["detection_rate"] <= 1 and len(b["per_run"]) == 4
    with pytest.raises(RunError):
        batch(base, v, 1, ECON)


def test_added_machines_take_part():
    custom = [{"machine_id": "VMC-02", "template": "cnc_mill", "name": "Second mill", "x": 12.0, "y": 0.0}]
    r = run(RunSpec(minutes=3, events=[{"at_min": 1, "action": "inject_fault", "machine_id": "VMC-02",
                                         "fault": "overload", "severity": 1.0}]), ECON, custom)
    assert "VMC-02" in r["machines"] and r["labels"][0]["machine_id"] == "VMC-02"


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("UDYAM_TICKER", "0")
    monkeypatch.setenv("UDYAM_AUTO_DEMO", "0")
    import sys
    import api.app  # noqa: F401
    appmod = sys.modules["api.app"]
    with TestClient(appmod.app) as c:
        async def step(n):
            for _ in range(n):
                await appmod.factory_playback.tick()
                await appmod.broadcast_messages(appmod.factory_engine.step())
        yield c, appmod, lambda n: asyncio.run(step(n))
    asyncio.run(appmod.factory_playback.stop())
    appmod.operating_mode.set("TESTING", by="test teardown")
    appmod.factory_engine.command({"cmd": "reset"})
    appmod.factory_engine.reset_params()


def _wait(c):
    t0 = time.time()
    while time.time() - t0 < 60:
        j = c.get("/api/factory-run/job").json()
        if j["state"] != "running":
            return j
        time.sleep(0.1)
    raise AssertionError("run did not finish")


def test_api_fast_run_batch_and_exports(client):
    c, appmod, _ = client
    o = c.get("/api/factory-run/options").json()
    assert {m["machine_id"] for m in o["machines"]} >= {"VMC-01", "COMP-01"} and "compressor_mid_shift" in o["presets"]
    bad = c.post("/api/factory-run/run", json={"minutes": 5, "events": [{**FAULT, "machine_id": "SAW-01"}]})
    assert bad.status_code == 422 and "can have" in bad.json()["detail"]["message"]
    t_before = appmod.factory_engine.t
    assert c.post("/api/factory-run/run", json={"minutes": 5, "events": [FAULT]}).status_code == 202
    j = _wait(c)
    assert j["state"] == "done" and appmod.factory_engine.t == t_before  # the live simulation did not move
    r = c.get(f"/api/factory-run/results/{j['id']}").json()
    assert r["labels"][0]["fault"] == "bearing_defect"
    csv_lines = c.get(f"/api/factory-run/results/{j['id']}/export").text.strip().splitlines()
    assert csv_lines[0].startswith("data_label,run,seed,t_min,machine_id") and "bearing_defect" in "\n".join(csv_lines)
    assert c.post("/api/factory-run/batch", json={"spec": {"minutes": 4, "events": [FAULT]}, "runs": 3,
                                                   "fault_time_jitter_min": 1, "severity_range": [0.8, 1.2]}).status_code == 202
    jb = _wait(c)
    assert jb["state"] == "done" and jb["total"] == 3
    labels = c.get(f"/api/factory-run/results/{jb['id']}/export").text.strip().splitlines()
    assert labels[0].startswith("data_label,run,seed,machine_id,fault") and len(labels) == 4


def test_live_playback_applies_events_on_time_and_respects_real_data_mode(client):
    c, appmod, step = client
    eng = appmod.factory_engine
    spec = {"name": "live test", "minutes": 2, "events": [
        {"at_min": 0.5, "action": "inject_fault", "machine_id": "VMC-01", "fault": "overload", "severity": 1.0},
        {"at_min": 1, "action": "set_params", "machine_id": "LATHE-01", "params": {"spindle_speed_pct": 80}}]}
    assert c.post("/api/factory-run/live/start", json=spec).json()["state"] == "running"
    step(50)  # 25 s: the first event (30 s) has not happened yet
    assert "overload" not in eng.plants["VMC-01"].faults.active_faults
    step(12)
    assert "overload" in eng.plants["VMC-01"].faults.active_faults and eng.plants["LATHE-01"].speed_scale == 1.0
    step(62)
    assert eng.plants["LATHE-01"].speed_scale == pytest.approx(0.8)
    assert any("Run event at 1 min" in e["title"] for e in c.get("/api/events?limit=30").json())
    step(130)
    assert c.get("/api/factory-run/live").json()["state"] == "finished"
    # Real Data mode: refused, and a running playback stops
    assert c.post("/api/factory-run/live/start", json=spec).json()["state"] == "running"
    c.post("/api/operating-mode", json={"mode": "REAL_DATA"})
    assert c.get("/api/factory-run/live").json()["state"].startswith("stopped")
    r = c.post("/api/factory-run/live/start", json=spec)
    assert r.status_code == 409 and r.json()["detail"]["error"] == "real_data_read_only"
