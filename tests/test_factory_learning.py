"""Factory learning loop: fault diagnosis, time to limit and the RL policy trained on factory datasets, evaluated on
held-out runs only, gated, never deployed automatically, promoted by an operator in Testing mode, with rollback."""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from ml import factory_dataset as fd, factory_learning as fl
from ml.maintenance_rl import POLICY_PATH, QPolicy
from sim.factory_run import RunSpec, Variation

ECON = {"inr_per_part": 450.0, "electricity_tariff_per_kwh": 8.5}
VAR = Variation(0.5, [0.8, 1.3], 0)


def _ev(at, mid, fault):
    return {"at_min": at, "action": "inject_fault", "machine_id": mid, "fault": fault, "severity": 1.0}


@pytest.fixture(scope="module")
def data(tmp_path_factory):
    store = fd.FactoryDatasetStore(root=tmp_path_factory.mktemp("factory"))
    ds = "fds-20261026-000000-abc123"
    fd.collect([fd.Part(RunSpec("Bearing", 6, 3, True, [_ev(1.5, "LATHE-01", "bearing_defect")]), 4, VAR),
                fd.Part(RunSpec("Air", 6, 5, True, [_ev(1.5, "COMP-01", "pressure_drop")]), 4, VAR),
                fd.Part(RunSpec("Coolant", 6, 9, False, [_ev(1.0, "VMC-01", "coolant_degradation")]), 4, VAR)],
               ECON, store.root / ds, ds, "learn", test_share=0.5, split_seed=2)
    return store, ds


def test_fault_model_learns_from_train_runs_and_is_gated_on_test_runs(tmp_path, data):
    store, ds = data
    reg = fl.registries(tmp_path)["fault"]
    card = fl.train_candidate("fault", reg, store, [ds])
    assert card["version"] == "v001" and reg.production() is None  # trained, not deployed
    assert card["status"] == ("candidate" if card["gate"]["passed"] else "rejected_by_gate")
    m = reg.metrics("v001")
    meta = store.metadata(ds)
    assert sorted(r.split(":")[1] for r in m["split"]["test_run_ids"]) == sorted(meta["split"]["test"])
    assert m["split"]["train_runs"] == len(meta["split"]["train"]) and m["real_machine_data"] is False
    r = m["results"]
    assert {"candidate", "majority_baseline"} <= set(r) and "production" not in r
    assert r["candidate"]["rows"] == sum(meta["counts"]["test_windows_by_label"].values())
    assert r["candidate"]["macro_f1"] > r["majority_baseline"]["macro_f1"]
    checks = {c["check"] for c in card["gate"]["checks"]}
    assert {"faults_in_test", "macro_f1", "macro_f1_vs_majority", "macro_f1_vs_production"} <= checks
    assert any(c.startswith("recall_") for c in checks)
    assert "has not learned from real machines" in m["honest_disclaimer"]


def test_ttf_compares_with_the_twin_forecast(tmp_path, data):
    store, ds = data
    reg = fl.registries(tmp_path)["ttf"]
    card = fl.train_candidate("ttf", reg, store, [ds])
    r = reg.metrics(card["version"])["results"]
    assert {"candidate", "twin_forecast_baseline", "always_censored_baseline"} <= set(r)
    assert r["candidate"]["rows"] == r["twin_forecast_baseline"]["rows"]
    assert r["always_censored_baseline"]["within_10min"]["recall"] == 0.0
    assert {c["check"] for c in card["gate"]["checks"]} >= {"positives", "within_10min_recall", "within_10min_precision"}


def test_rl_fine_tunes_the_current_policy_and_is_compared_on_held_out_scenarios(tmp_path, data):
    store, ds = data
    reg = fl.registries(tmp_path)["rl"]
    assert reg.production() == "v001" and reg.card("v001")["kind"] == "baseline"  # the shipped policy
    scen = fl.rl_scenarios(store, [ds])
    meta = store.metadata(ds)
    assert len(scen["train"]) == 2 * len(meta["split"]["train"]) and len(scen["test"]) == 2 * len(meta["split"]["test"])
    assert {s["scenario"] for s in scen["train"]} >= {"vibration_fault", "air_pressure_drop", "over_temperature"}
    assert all(0 <= s["inject_at"] <= fl.RL_LAST_ONSET for s in scen["train"])
    card = fl.train_candidate("rl", reg, store, [ds], episodes=200)
    m = reg.metrics(card["version"])
    assert set(m["results"]) >= {"candidate", "production", "rule_based", "rule_with_inspection", "always_continue"}
    assert m["results"]["production"]["version"] == "v001"
    assert m["results"]["candidate"]["all"]["shifts"] == len(scen["test"]) * fl.RL_EVAL_SEEDS
    assert QPolicy.load(reg.model_path(card["version"])).meta["from_version"] == "v001"
    assert reg.production() == "v001"


def test_refuses_altered_or_unsplit_data(tmp_path, data):
    store, ds = data
    with pytest.raises(fl.LearningError, match="not found"):
        fl.load_windows(store, ["fds-20990101-000000-ffffff"])
    nosplit = fd.FactoryDatasetStore(root=tmp_path / "f")
    ds2 = "fds-20261026-000001-abc123"
    fd.collect([fd.Part(RunSpec("Normal", 2, 1, True, []), 2, None)], ECON, nosplit.root / ds2, ds2, test_share=0)
    with pytest.raises(fl.LearningError, match="train and test runs"):
        fl.train_candidate("fault", fl.registries(tmp_path)["fault"], nosplit, [ds2])
    p = nosplit.root / ds2 / "windows.csv"
    p.write_text(p.read_text().replace("normal", "bearing_defect", 1))
    with pytest.raises(fl.LearningError, match="checksum"):
        fl.load_windows(nosplit, [ds2])


@pytest.fixture
def client(monkeypatch, tmp_path, data):
    monkeypatch.setenv("UDYAM_TICKER", "0")
    monkeypatch.setenv("UDYAM_AUTO_DEMO", "0")
    monkeypatch.setenv("UDYAM_OPERATOR_TOKEN", "pin")
    import sys
    import api.app  # noqa: F401
    appmod = sys.modules["api.app"]
    store, _ = data
    for kind, reg in fl.registries(tmp_path / "reg").items():
        monkeypatch.setitem(appmod.factory_registries, kind, reg)
    monkeypatch.setattr(appmod.factory_datasets, "root", store.root)
    with TestClient(appmod.app) as c:
        yield c, appmod
    appmod.operating_mode.set("TESTING", by="test teardown")
    appmod.rl_agent.policy = QPolicy.load(POLICY_PATH)


def _train(c, kind, body):
    assert c.post(f"/api/factory-learning/{kind}/train", json=body).status_code == 202
    t0 = time.time()
    while (s := c.get("/api/factory-learning").json())["training"]["state"] == "running" and time.time() - t0 < 120:
        time.sleep(0.1)
    return s


def test_api_train_promote_rollback_and_live_readout(client, data):
    c, appmod = client
    _, ds = data
    s = c.get("/api/factory-learning").json()
    assert set(s["models"]) == {"fault", "ttf", "rl"} and s["models"]["rl"]["production"] == "v001"
    assert s["models"]["fault"]["production"] is None and any(d["dataset_id"] == ds for d in s["datasets"])
    assert c.post("/api/factory-learning/fault/train", json={"datasets": []}).status_code == 422
    assert c.post("/api/factory-learning/nope/train", json={"datasets": [ds]}).status_code == 404
    s = _train(c, "fault", {"datasets": [ds]})
    job = s["training"]
    assert job["state"] == "done" and job["version"] == "v001" and s["models"]["fault"]["production"] is None
    hdr = {"X-Operator-Token": "pin"}
    assert c.post("/api/factory-learning/fault/v001/promote").status_code == 401  # operator needed
    if not job["gate_passed"]:
        r = c.post("/api/factory-learning/fault/v001/promote", headers=hdr)
        assert r.status_code == 409 and "did not pass" in r.json()["detail"]["message"]
        pytest.skip("this small dataset's fault candidate did not pass its gate")
    c.post("/api/operating-mode", json={"mode": "REAL_DATA"})
    r = c.post("/api/factory-learning/fault/v001/promote", headers=hdr)
    assert r.status_code == 409 and r.json()["detail"]["error"] == "real_data_read_only"
    c.post("/api/operating-mode", json={"mode": "TESTING"})
    r = c.post("/api/factory-learning/fault/v001/promote", headers=hdr, json={"operator": "asha"})
    assert r.status_code == 200 and r.json()["models"]["fault"]["production"] == "v001"
    # the live readout uses the production model once 60 s of live data are buffered
    for _ in range(130):
        appmod.live_factory_models.observe(appmod.factory_engine.step())
    live = c.get("/api/factory-learning/live").json()
    vmc = next(m for m in live["machines"] if m["machine_id"] == "VMC-01")
    assert live["models"]["fault"] == "v001" and vmc["fault"]["label"] in appmod.factory_registries["fault"].metrics("v001")["results"]["labels"]
    assert any(e["kind"] == "MODEL" and "Fault diagnosis model promoted" in e["title"] for e in c.get("/api/events?limit=10").json())
    r = c.post("/api/factory-learning/fault/rollback", headers=hdr)
    assert r.status_code == 409  # no earlier production version: rollback refuses rather than guessing


def test_api_rl_promotion_switches_the_live_recommendations(client, data):
    c, appmod = client
    _, ds = data
    s = _train(c, "rl", {"datasets": [ds], "episodes": 200})
    job = s["training"]
    assert job["state"] == "done" and job["version"] == "v002"
    reg = appmod.factory_registries["rl"]
    if not job["gate_passed"]:
        r = c.post("/api/factory-learning/rl/v002/promote", headers={"X-Operator-Token": "pin"})
        assert r.status_code == 409
        pytest.skip("this run's RL candidate did not pass its gate")
    hdr = {"X-Operator-Token": "pin"}
    assert c.post("/api/factory-learning/rl/v002/promote", headers=hdr).status_code == 200
    assert appmod.rl_agent.policy.q == QPolicy.load(reg.model_path("v002")).q
    r = c.post("/api/factory-learning/rl/rollback", headers=hdr)
    assert r.status_code == 200 and r.json()["models"]["rl"]["production"] == "v001"
    assert appmod.rl_agent.policy.q == QPolicy.load(reg.model_path("v001")).q
