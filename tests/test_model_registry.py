"""Model registry and candidate training: versioning, the evaluation gate, gated promotion, rollback,
synthetic-data checks and the live twin picking up the deployed version."""

from __future__ import annotations

import json
import shutil
import time

import pytest
from fastapi.testclient import TestClient

from ml import synthetic
from ml.candidate import CandidateError, evaluate_gate, train_candidate
from ml.model_registry import ModelRegistry, RegistryError

PASS = {"passed": True, "checks": []}
FAIL = {"passed": False, "checks": [{"check": "synthetic_f1", "passed": False}]}


def _fake_candidate(reg: ModelRegistry, gate: dict) -> str:
    """Register a copy of the baseline as a candidate with a given gate (tests the mechanism, not the model)."""
    v = reg.next_version()
    (reg.root / v).mkdir(parents=True)
    shutil.copy2(reg.root / "v001" / "model.joblib", reg.root / v / "model.joblib")
    (reg.root / v / "metrics.json").write_text(json.dumps({"registry_version": v}))
    reg.add_candidate(v, {"gate": gate, "trained_on": ["test"]})
    return v


def test_registry_registers_the_shipped_model_as_baseline(tmp_path):
    reg = ModelRegistry(root=tmp_path / "reg")
    s = reg.state()
    assert s["production"] == "v001" and s["versions"][0]["kind"] == "baseline"
    assert reg.production_model_path().is_file() and s["versions"][0]["real_machine_data"] is False
    assert s["history"][0]["action"] == "register_baseline"


def test_gate_failures_cannot_be_promoted(tmp_path):
    reg = ModelRegistry(root=tmp_path / "reg")
    bad = _fake_candidate(reg, FAIL)
    assert reg.card(bad)["status"] == "rejected_by_gate"
    with pytest.raises(RegistryError, match="did not pass"):
        reg.promote(bad, by="t")
    with pytest.raises(RegistryError, match="never in production"):
        reg.rollback(by="t", to_version=bad)
    assert reg.production() == "v001"


def test_promote_and_rollback_keep_a_history(tmp_path):
    reg = ModelRegistry(root=tmp_path / "reg")
    with pytest.raises(RegistryError, match="no earlier"):
        reg.rollback(by="t")
    good = _fake_candidate(reg, PASS)
    assert reg.production() == "v001"  # registering a candidate never deploys it
    reg.promote(good, by="alice")
    assert reg.production() == good and reg.card("v001")["status"] == "archived"
    with pytest.raises(RegistryError, match="already in production"):
        reg.promote(good, by="alice")
    reg.rollback(by="bob")
    s = reg.state()
    assert s["production"] == "v001" and reg.card(good)["status"] == "archived"
    assert [(h["action"], h["version"], h.get("by")) for h in s["history"][-3:]] == [
        ("train_candidate", good, "operator"), ("promote", good, "alice"), ("rollback", "v001", "bob")]


def test_gate_rules():
    m = lambda f1, rec, auc, fpr: {"f1": f1, "recall": rec, "roc_auc": auc, "false_positive_rate": fpr}
    prod = {"ai4i_test": m(0.70, 0.80, 0.97, 0.02), "synthetic_test": m(0.40, 0.30, 0.85, 0.09)}
    enough = {"train_pos": 50, "test_pos": 20, "test_neg": 40}
    ok = {"ai4i_test": m(0.69, 0.78, 0.965, 0.025), "synthetic_test": m(0.46, 0.5, 0.9, 0.05)}
    assert evaluate_gate(prod, ok, enough)["passed"]
    for broken in ({"ai4i_test": m(0.67, 0.78, 0.965, 0.025)},   # AI4I F1 −0.03
                   {"ai4i_test": m(0.69, 0.76, 0.965, 0.025)},   # recall −0.04
                   {"ai4i_test": m(0.69, 0.78, 0.95, 0.025)},    # ROC-AUC −0.02
                   {"ai4i_test": m(0.69, 0.78, 0.965, 0.04)},    # FPR +2 points
                   {"synthetic_test": m(0.44, 0.5, 0.9, 0.05)}):  # synthetic gain only +0.04
        g = evaluate_gate(prod, {**ok, **broken}, enough)
        assert not g["passed"], broken
    assert not evaluate_gate(prod, ok, {"train_pos": 5, "test_pos": 20})["passed"]
    assert not evaluate_gate(prod, ok, {"train_pos": 50, "test_pos": 20, "test_neg": 0})["passed"]  # one-class test set


@pytest.fixture(scope="module")
def hot_dataset(tmp_path_factory):
    """A small synthetic dataset where the mill often runs over its temperature limit."""
    store = synthetic.SyntheticStore(root=tmp_path_factory.mktemp("synthetic"))
    ds = "synth-20261025-000000-abc123"
    synthetic.generate(synthetic.GeneratorConfig(episodes=10, minutes=5, seed=11, faults=["normal", "coolant_degradation"],
                                                 param_ranges={"ambient_temp_c": [24, 34], "cooling_capacity_pct": [60, 100]}),
                       store.root / ds, ds)
    return store, ds


def test_candidate_training_compares_with_production_on_held_out_rows(tmp_path, hot_dataset):
    store, ds = hot_dataset
    reg = ModelRegistry(root=tmp_path / "reg")
    card = train_candidate(reg, store, [ds])
    assert card["version"] == "v002" and reg.production() == "v001"  # trained, not deployed
    assert card["status"] == ("candidate" if card["gate"]["passed"] else "rejected_by_gate")
    assert {c["check"] for c in card["gate"]["checks"]} == {"synthetic_positives", "ai4i_f1", "ai4i_recall",
                                                             "ai4i_roc_auc", "ai4i_false_positive_rate", "synthetic_f1"}
    cmp = card["comparison"]
    assert cmp["production"]["version"] == "v001"
    assert cmp["production"]["ai4i_test"]["support_positive"] == cmp["candidate"]["ai4i_test"]["support_positive"] == 68
    met = reg.metrics("v002")
    assert met["dataset"]["real_machine_data"] is False and "has not learned from real machines" in met["honest_disclaimer"]
    assert met["dataset"]["ai4i_train_rows"] == 8000 and met["rule_baseline"]
    assert card["real_machine_data"] is False and any(synthetic.DATA_LABEL in t for t in card["trained_on"])


def test_candidate_refuses_unlabelled_or_altered_synthetic_data(tmp_path, hot_dataset):
    store, ds = hot_dataset
    bad_store = synthetic.SyntheticStore(root=tmp_path / "s")
    shutil.copytree(store.root / ds, bad_store.root / ds)
    reg = ModelRegistry(root=tmp_path / "reg")
    with open(bad_store.root / ds / "windows.csv", "a") as fh:
        fh.write("\n")
    with pytest.raises(CandidateError, match="checksum"):
        train_candidate(reg, bad_store, [ds])
    meta = json.loads((bad_store.root / ds / "metadata.json").read_text())
    meta["data_label"] = "REAL MACHINE DATA"
    (bad_store.root / ds / "metadata.json").write_text(json.dumps(meta))
    with pytest.raises(CandidateError, match="not labelled"):
        train_candidate(reg, bad_store, [ds])
    with pytest.raises(CandidateError, match="not found"):
        train_candidate(reg, bad_store, ["synth-20990101-000000-ffffff"])
    assert len(reg.state()["versions"]) == 1


@pytest.fixture
def client(monkeypatch, tmp_path, hot_dataset):
    monkeypatch.setenv("UDYAM_TICKER", "0")
    monkeypatch.setenv("UDYAM_AUTO_DEMO", "0")
    monkeypatch.setenv("UDYAM_OPERATOR_TOKEN", "pin")
    from api.app import app, model_registry, operating_mode, synthetic_store
    from api.operating_mode import TESTING
    from twin import model as twin_model
    store, _ = hot_dataset
    monkeypatch.setattr(model_registry, "root", tmp_path / "reg")
    monkeypatch.setattr(synthetic_store, "root", store.root)
    twin_model.ai4i_predictor.cache_clear()
    with TestClient(app) as c:
        yield c, model_registry
    operating_mode.set(TESTING, by="test teardown")
    twin_model.ai4i_predictor.cache_clear()


def test_api_train_promote_rollback(client, hot_dataset):
    from twin.model import ai4i_predictor
    c, reg = client
    _, ds = hot_dataset
    s = c.get("/api/models/ai4i").json()
    assert s["production"] == "v001" and s["gate_criteria"] and "real machines" in s["note"]
    assert any(d["dataset_id"] == ds for d in s["synthetic_datasets"])
    assert c.post("/api/models/ai4i/train", json={"datasets": []}).status_code == 422
    assert c.post("/api/models/ai4i/train", json={"datasets": [ds]}).status_code == 202
    t0 = time.time()
    while c.get("/api/models/ai4i").json()["training"]["state"] == "running" and time.time() - t0 < 120:
        time.sleep(0.2)
    job = c.get("/api/models/ai4i").json()["training"]
    assert job["state"] == "done" and job["version"] == "v002"
    assert c.get("/api/models/ai4i").json()["production"] == "v001"  # never auto-deployed

    good = _fake_candidate(reg, PASS)
    hdr = {"X-Operator-Token": "pin"}
    assert c.post(f"/api/models/ai4i/{good}/promote").status_code == 401  # operator needed
    if not reg.card("v002")["gate"]["passed"]:
        r = c.post("/api/models/ai4i/v002/promote", headers=hdr)
        assert r.status_code == 409 and "did not pass" in r.json()["detail"]["message"]
    c.post("/api/operating-mode", json={"mode": "REAL_DATA"})
    r = c.post(f"/api/models/ai4i/{good}/promote", headers=hdr)
    assert r.status_code == 409 and r.json()["detail"]["error"] == "real_data_read_only"
    c.post("/api/operating-mode", json={"mode": "TESTING"})
    r = c.post(f"/api/models/ai4i/{good}/promote", headers=hdr, json={"operator": "asha"})
    assert r.status_code == 200 and r.json()["production"] == good
    assert ai4i_predictor().model_path == reg.root / good / "model.joblib"  # the twin reloaded it
    assert c.get("/api/metrics").json()["registry_version"] == good
    assert any(e["kind"] == "MODEL" and "promoted" in e["title"] for e in c.get("/api/events?limit=10").json())
    r = c.post("/api/models/ai4i/rollback", headers=hdr)
    assert r.status_code == 200 and r.json()["production"] == "v001"
    assert ai4i_predictor().model_path == reg.root / "v001" / "model.joblib"
