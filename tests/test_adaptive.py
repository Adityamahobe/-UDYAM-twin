"""Adaptive learning: input-drift monitoring of the failure-risk model, operator-confirmed labels, and candidate
training that uses only confirmed labels."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from ml import adaptive, synthetic
from ml.adaptive import DriftMonitor, LabelError, LabelStore, TrainingReference, ai4i_training_inputs


@pytest.fixture(scope="module")
def reference():
    return TrainingReference(ai4i_training_inputs(), "v001")


def _run(eng, mon, n):
    for _ in range(n):
        mon.observe(eng.step())


def test_drift_and_coverage_follow_the_machine(reference):
    from sim.factory import FactoryEngine
    eng, mon = FactoryEngine(), DriftMonitor(lambda: reference)
    _run(eng, mon, 100)
    early = mon.evaluate("VMC-01", "SIM")
    assert early["coverage"]["status"] == "INSUFFICIENT_DATA"
    _run(eng, mon, 1100)
    calm = mon.evaluate("VMC-01", "SIM")
    assert calm["drift"]["status"] == "OK" and calm["coverage"]["status"] != "OUT_OF_DISTRIBUTION"
    assert set(r["machine_id"] for r in mon.status()) == {"VMC-01"}  # only machines that use the AI4I model
    mon.take_events()
    eng.set_params("VMC-01", {"ambient_temp_c": 38, "cooling_capacity_pct": 60, "spindle_speed_pct": 110})
    _run(eng, mon, 900)
    hot = mon.evaluate("VMC-01", "SIM")
    assert hot["coverage"]["status"] == "OUT_OF_DISTRIBUTION" and hot["drift"]["status"] == "DRIFT"
    assert "Nothing is retrained automatically" in hot["recommendation"]
    events = mon.take_events()
    assert len(events) == 1 and events[0]["data"]["not_a_fault"] and events[0]["severity"] == "WARN"
    # an intended change: the operator makes the new conditions the baseline, so machine drift clears
    mon.set_baseline("VMC-01", "SIM", by="test")
    assert mon.evaluate("VMC-01", "SIM")["drift"]["status"] == "OK"
    # a simulation reset starts a fresh baseline for simulated data
    mon.observe([{"type": "event", "kind": "RESET", "ts": "2999-01-01T00:00:00Z"}])
    st = mon.status()[0]
    assert st["coverage"]["status"] == "INSUFFICIENT_DATA" and "0 complete samples" in st["message"]
    assert len(mon.buffers[("VMC-01", "SIM")]) == (100 + 1100 + 900) // 4  # kept, so what happened before the reset can still be labelled


def test_baseline_needs_enough_data(reference):
    mon = DriftMonitor(lambda: reference)
    with pytest.raises(ValueError, match="at least"):
        mon.set_baseline("VMC-01", "SIM", by="t")


def _obs(i, source="SIM", live_fields=None, **over):
    o = {"type": "observed", "machine_id": "VMC-01", "template": "cnc_mill", "source_mode": source,
         "ts": (datetime(2026, 10, 25, 10, 0, tzinfo=timezone.utc) + timedelta(seconds=i / 2)).isoformat().replace("+00:00", "Z"),
         "air_temp_k": 300.0, "process_temp_k": 311.0, "rpm": 1540.0, "torque_nm": 40.0, "tool_wear_min": 100.0}
    if live_fields is not None:
        o["live_fields"] = live_fields
    return {**o, **over}


def test_live_samples_count_only_when_the_device_sent_every_input(reference):
    mon = DriftMonitor(lambda: reference)
    mon.observe([_obs(i, "LIVE", ["process_temp_k", "rpm"]) for i in range(40)])
    mon.observe([_obs(i, "SIM") for i in range(40)])
    live = list(mon.buffers[("VMC-01", "LIVE")])
    assert live and not any(s["complete"] for s in live)  # torque etc. were the simulation's
    assert all(s["complete"] for s in mon.buffers[("VMC-01", "SIM")])
    mon.observe([_obs(i, "LIVE", list(adaptive.FEATURES)) for i in range(40, 80)])
    assert sum(s["complete"] for s in mon.buffers[("VMC-01", "LIVE")]) == 10


def test_label_store_rules(tmp_path):
    store = LabelStore(tmp_path / "l.sqlite")
    store.init()
    t0 = datetime(2026, 10, 25, 10, tzinfo=timezone.utc)
    ok = [{"ts": "2026-10-25T10:00:00Z", "complete": True, "x": {f: 1.0 for f in adaptive.FEATURES}}]
    partial = [{"ts": "2026-10-25T10:00:00Z", "complete": False, "x": {f: 1.0 for f in adaptive.FEATURES}}]
    kw = dict(machine_id="VMC-01", source="SIM", start=t0, end=t0 + timedelta(minutes=5), operator="asha")
    for bad, words in [({"label": "broken"}, "label must be"),
                       ({"label": "normal", "fault_type": "bearing"}, "fault type"),
                       ({"label": "failure", "end": t0 + timedelta(minutes=45)}, "30 minutes"),
                       ({"label": "failure", "samples": []}, "no SIM readings"),
                       ({"label": "failure", "samples": partial, "source": "LIVE"}, "simulated values")]:
        with pytest.raises(LabelError, match=words):
            store.create(**{**kw, "samples": ok, **bad})
    lab = store.create(**kw, label="failure", fault_type="bearing", samples=ok + partial)
    assert lab["n_samples"] == 1 and lab["data_origin"] == "simulation" and lab["label_text"] == "Confirmed failure"
    rows = store.training_rows()
    assert len(rows) == 1 and rows[0]["machine_failure"] == 1 and rows[0]["source"] == "SIM"
    store.retract(lab["id"], "bob")
    assert store.training_rows() == [] and store.list(include_retracted=True)[0]["retracted_by"] == "bob"
    with pytest.raises(LabelError, match="already retracted"):
        store.retract(lab["id"], "bob")


@pytest.fixture
def client(monkeypatch, tmp_path, reference):
    monkeypatch.setenv("UDYAM_TICKER", "0")
    monkeypatch.setenv("UDYAM_AUTO_DEMO", "0")
    monkeypatch.setenv("UDYAM_OPERATOR_TOKEN", "pin")
    import sys
    import api.app  # noqa: F401 - the package attribute is the FastAPI app, so take the module itself
    from api.operating_mode import TESTING
    appmod = sys.modules["api.app"]
    mon = DriftMonitor(lambda: reference)
    monkeypatch.setattr(appmod, "drift_monitor", mon)
    monkeypatch.setattr(appmod.label_store, "db_path", tmp_path / "labels.sqlite")
    with TestClient(appmod.app) as c:
        async def step(n):
            for _ in range(n):
                await appmod.broadcast_messages(appmod.factory_engine.step())
        yield c, mon, lambda n: asyncio.run(step(n))
    appmod.operating_mode.set(TESTING, by="test teardown")
    appmod.factory_engine.reset_params()


def test_api_drift_and_labels(client):
    c, mon, step = client
    step(600)
    s = c.get("/api/adaptive").json()
    assert s["drift"][0]["machine_id"] == "VMC-01" and s["label_counts"]["active"] == 0 and "not a fault" in s["note"]
    hdr = {"X-Operator-Token": "pin"}
    body = {"machine_id": "VMC-01", "label": "normal", "minutes": 3}
    assert c.post("/api/adaptive/labels", json=body).status_code == 401  # an operator must confirm
    r = c.post("/api/adaptive/labels", json=body, headers=hdr)
    assert r.status_code == 201 and r.json()["source"] == "SIM" and r.json()["n_samples"] > 10
    assert c.post("/api/adaptive/labels", json={**body, "label": "maybe"}, headers=hdr).status_code == 422
    r = c.post("/api/adaptive/labels", json={**body, "machine_id": "SAW-01"}, headers=hdr)
    assert r.status_code == 422 and "no SIM readings" in r.json()["detail"]["message"]  # the saw has no failure model
    assert any(e["kind"] == "LABEL" for e in c.get("/api/events?limit=10").json())
    csv_text = c.get("/api/adaptive/labels/export?format=csv").text
    assert csv_text.startswith("ts,label_id,source,data_origin") and "SIMULATION (operator-confirmed)" in csv_text
    lid = c.get("/api/adaptive").json()["labels"][0]["id"]
    assert c.post(f"/api/adaptive/labels/{lid}/retract", headers=hdr).json()["status"] == "RETRACTED"
    assert c.get("/api/adaptive").json()["label_counts"]["active"] == 0


def test_api_drift_events_are_information_not_alerts(client):
    c, mon, step = client
    step(800)
    before = {a["alert_id"] for a in c.get("/api/alerts?include_resolved=true").json()}
    c.post("/api/sim/params", json={"machine_id": "VMC-01", "params": {"ambient_temp_c": 30, "spindle_speed_pct": 115}})
    step(700)
    ev = [e for e in c.get("/api/events?limit=200").json() if e["kind"] == "DATA_DRIFT"]
    assert ev and ev[0]["data"]["not_a_fault"]
    new_alerts = [a for a in c.get("/api/alerts?include_resolved=true").json() if a["alert_id"] not in before]
    assert not any(a["kind"] == "DATA_DRIFT" for a in new_alerts)
    r = c.post("/api/adaptive/baseline", json={"machine_id": "VMC-01", "source": "SIM"})
    assert r.status_code == 200
    assert c.post("/api/adaptive/baseline", json={"machine_id": "LATHE-01", "source": "SIM"}).status_code == 409


def test_candidate_uses_only_confirmed_labels_and_says_where_they_came_from(tmp_path):
    from ml.candidate import train_candidate
    from ml.model_registry import ModelRegistry
    store = synthetic.SyntheticStore(root=tmp_path / "syn")
    ds = "synth-20261025-000000-abc124"
    synthetic.generate(synthetic.GeneratorConfig(episodes=8, minutes=5, seed=3, faults=["normal"],
                                                 param_ranges={"ambient_temp_c": [24, 34], "cooling_capacity_pct": [60, 100]}),
                       store.root / ds, ds)
    labels = LabelStore(tmp_path / "l.sqlite")
    labels.init()
    t0 = datetime(2026, 10, 25, 10, tzinfo=timezone.utc)
    sample = lambda temp: [{"ts": "2026-10-25T10:00:00Z", "complete": True,
                            "x": {"air_temp_k": 300.0, "process_temp_k": temp, "rpm": 1500.0, "torque_nm": 45.0, "tool_wear_min": 80.0}}]
    labels.create(machine_id="VMC-01", source="SIM", start=t0, end=t0 + timedelta(minutes=2), label="false_alarm",
                  samples=sample(311.0), operator="t")
    reg = ModelRegistry(root=tmp_path / "reg")
    empty = LabelStore(tmp_path / "empty.sqlite")
    empty.init()
    with pytest.raises(Exception, match="no operator-confirmed"):
        train_candidate(reg, store, [ds], label_store=empty, use_labels=True)
    card = train_candidate(reg, store, [ds], label_store=labels, use_labels=True)
    assert card["confirmed_labels"]["labels"] == 1 and card["real_machine_data"] is False
    assert any(c["check"] == "confirmed_f1" and c.get("skipped") for c in card["gate"]["checks"])
    assert "has not learned from real machines" in reg.metrics(card["version"])["honest_disclaimer"]
    labels.create(machine_id="VMC-01", source="LIVE", start=t0, end=t0 + timedelta(minutes=2), label="failure",
                  samples=sample(318.0), operator="t")
    card = train_candidate(reg, store, [ds], label_store=labels, use_labels=True)
    assert card["real_machine_data"] is True and "1 training samples from real machines" in card["trained_on"][-1]
    assert "operator-confirmed samples from real machines" in reg.metrics(card["version"])["honest_disclaimer"]
    assert (reg.root / card["version"] / "reference.npz").is_file()  # drift monitoring knows what it was trained on
    without = train_candidate(reg, store, [ds], label_store=labels, use_labels=False)
    assert without["confirmed_labels"] is None and without["real_machine_data"] is False
