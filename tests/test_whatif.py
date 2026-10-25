"""What-if comparisons: same-seed scenarios on separate engines, validation, cause-and-effect checks, honest
alert wording when self-healing is off, and the API (live simulation untouched)."""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from sim.whatif import RESULT_LABEL, Scenario, WhatIfError, compare, run_scenario, validate

ECON = {"inr_per_part": 450.0, "electricity_tariff_per_kwh": 8.5, "cea_co2_factor": 0.716}
FASTER = {m: {"spindle_speed_pct": 115} for m in ("VMC-01", "LATHE-01", "SAW-01")}
COOLANT = {"machine_id": "VMC-01", "fault": "coolant_degradation", "at_min": 1}


def test_identical_scenarios_give_identical_outcomes():
    a = run_scenario(Scenario("A"), 5, 7, "VMC-01", ECON)
    b = run_scenario(Scenario("B"), 5, 7, "VMC-01", ECON)
    assert a["kpis"] == b["kpis"] and a["trend"] == b["trend"]


@pytest.mark.parametrize("scenarios, minutes, focus, words", [
    ([Scenario("only")], 5, "VMC-01", "2–4 scenarios"),
    ([Scenario(f"s{i}") for i in range(5)], 5, "VMC-01", "2–4 scenarios"),
    ([Scenario("a"), Scenario("a")], 5, "VMC-01", "distinct name"),
    ([Scenario("a"), Scenario("b")], 90, "VMC-01", "1–60"),
    ([Scenario("a"), Scenario("b")], 5, "NOPE-01", "unknown machine"),
    ([Scenario("a"), Scenario("b", params={"VMC-01": {"spindle_speed_pct": 300}})], 5, "VMC-01", "outside the limit"),
    ([Scenario("a"), Scenario("b", params={"LATHE-01": {"spindle_speed_pct": 120, "load_pct": 130}})], 5, "VMC-01", "rated"),
    ([Scenario("a"), Scenario("b", fault={"machine_id": "VMC-01", "fault": "meteor", "at_min": 1})], 5, "VMC-01", "fault needs"),
    ([Scenario("a"), Scenario("b", fault={**COOLANT, "at_min": 5})], 5, "VMC-01", "within the run"),
])
def test_invalid_comparisons_are_refused(scenarios, minutes, focus, words):
    with pytest.raises(WhatIfError, match=words):
        validate(scenarios, minutes, focus)


def test_outcomes_follow_the_settings():
    r = compare([Scenario("Baseline"), Scenario("Faster", params=FASTER),
                 Scenario("Coolant, no healing", fault=COOLANT, auto_derate=False),
                 Scenario("Coolant, healing", fault=COOLANT)], 12, 42, "VMC-01", ECON)
    assert r["label"] == RESULT_LABEL and r["baseline"] == "Baseline"
    base, fast, nohe, heal = (s["kpis"] for s in r["scenarios"])
    # faster spindles: more parts (the line cycle follows spindle speed) and more energy
    assert fast["line_parts"] > base["line_parts"] and fast["site_energy_kwh"] > base["site_energy_kwh"]
    assert r["scenarios"][1]["delta_vs_baseline"]["line_parts"] == fast["line_parts"] - base["line_parts"]
    # the coolant fault heats the mill; self-healing derates it and spends less time over the limit
    assert nohe["focus_peak_temp_c"] > base["focus_peak_temp_c"]
    assert nohe["focus_min_derated"] == 0 and heal["focus_min_derated"] > 0
    assert heal["focus_min_over_temp_limit"] < nohe["focus_min_over_temp_limit"]
    # with self-healing off, alerts must not claim a derate was applied
    titles = [a["title"] for a in r["scenarios"][2]["alerts"]]
    assert not any("auto-derated" in t for t in titles) and any("not applied" in t for t in titles)
    assert any("auto-derated" in a["title"] for a in r["scenarios"][3]["alerts"])


def test_default_spindle_speed_leaves_the_line_unchanged():
    from sim.factory import FactoryEngine
    a, b = FactoryEngine(seed=3), FactoryEngine(seed=3)
    b.set_params("VMC-01", {"spindle_speed_pct": 100})
    for _ in range(400):
        la = next(m for m in a.step() if m["type"] == "line")
        lb = next(m for m in b.step() if m["type"] == "line")
    assert la["throughput_parts_per_hr"] == lb["throughput_parts_per_hr"]
    assert a.plants["VMC-01"].parts_done == b.plants["VMC-01"].parts_done


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("UDYAM_TICKER", "0")
    monkeypatch.setenv("UDYAM_AUTO_DEMO", "0")
    from api.app import app, factory_engine, operating_mode
    from api.operating_mode import TESTING
    with TestClient(app) as c:
        yield c, factory_engine
    operating_mode.set(TESTING, by="test teardown")


def _wait(c):
    t0 = time.time()
    while time.time() - t0 < 120:
        j = c.get("/api/whatif/job").json()
        if j["state"] != "running":
            return j
        time.sleep(0.1)
    raise AssertionError("comparison did not finish")


def test_api_runs_a_comparison_without_touching_the_live_simulation(client):
    c, eng = client
    o = c.get("/api/whatif/options").json()
    assert o["label"] == RESULT_LABEL and {m["machine_id"] for m in o["machines"]} >= {"VMC-01", "COMP-01"}
    bad = c.post("/api/whatif/run", json={"scenarios": [{"name": "a"}, {"name": "b", "params": {"VMC-01": {"load_pct": 999}}}]})
    assert bad.status_code == 422 and "outside the limit" in bad.json()["detail"]["message"]
    assert c.post("/api/whatif/run", json={"scenarios": [{"name": "a"}]}).status_code == 422
    c.post("/api/operating-mode", json={"mode": "REAL_DATA"})  # allowed in both modes: nothing live changes
    for _ in range(4):
        eng.step()
    before = (eng.t, eng.rng.getstate(), dict(eng.sim_params), eng.event_seq)
    r = c.post("/api/whatif/run", json={"minutes": 3, "scenarios": [{"name": "Baseline"}, {"name": "Faster", "params": FASTER}]})
    assert r.status_code == 202
    j = _wait(c)
    assert j["state"] == "done"
    assert (eng.t, eng.rng.getstate(), dict(eng.sim_params), eng.event_seq) == before
    res = c.get(f"/api/whatif/results/{j['id']}").json()
    assert [s["name"] for s in res["scenarios"]] == ["Baseline", "Faster"] and res["scenarios"][1]["delta_vs_baseline"]
    assert c.get("/api/whatif/results").json()[0]["id"] == j["id"]
    csv_text = c.get(f"/api/whatif/results/{j['id']}/export?format=csv").text.splitlines()
    assert csv_text[0].startswith("label,scenario,line_parts") and "delta_line_parts" in csv_text[0]
    assert csv_text[1].startswith(RESULT_LABEL) and len(csv_text) == 3
    assert c.get("/api/whatif/results/whatif-nope").status_code == 404
