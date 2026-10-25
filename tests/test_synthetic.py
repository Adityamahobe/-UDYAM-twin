"""Synthetic dataset generator: SYNTHETIC DIGITAL TWIN DATA labelling, reproducibility, limits, ground-truth
labels, isolation from the live simulation, and CSV / JSON export."""

from __future__ import annotations

import csv
import json
import time

import pytest
from fastapi.testclient import TestClient

from ml import synthetic
from ml.synthetic import DATA_LABEL, GeneratorConfig, GeneratorError, generate


def _rows(path):
    with open(path, newline="") as fh:
        return list(csv.DictReader(fh))


def test_every_row_and_file_is_labelled_synthetic(tmp_path):
    meta = generate(GeneratorConfig(episodes=2, minutes=3), tmp_path, "synth-x")
    assert meta["data_label"] == DATA_LABEL and meta["synthetic"] and meta["real_machine_data"] is False
    assert "no readings from real machines" in meta["notice"]
    win, eps = _rows(tmp_path / "windows.csv"), _rows(tmp_path / "episodes.csv")
    assert win and eps and all(r["data_label"] == DATA_LABEL for r in win + eps)
    assert meta["counts"]["windows"] == len(win) and meta["counts"]["episodes"] == 2
    assert meta["files"]["windows.csv"]["sha256"] and set(meta["features"]) <= set(win[0])


def test_same_config_and_seed_reproduce_identical_rows(tmp_path):
    cfg = dict(episodes=3, minutes=3, seed=7, param_ranges={"ambient_temp_c": [20, 40], "load_pct": [80, 120]})
    a = generate(GeneratorConfig(**cfg), tmp_path / "a", "synth-a")
    b = generate(GeneratorConfig(**cfg), tmp_path / "b", "synth-b")
    c = generate(GeneratorConfig(**{**cfg, "seed": 8}), tmp_path / "c", "synth-c")
    assert a["files"] == b["files"]
    assert a["files"]["windows.csv"]["sha256"] != c["files"]["windows.csv"]["sha256"]


def test_sampled_parameters_stay_inside_ranges_and_power_limit(tmp_path):
    rng = {"spindle_speed_pct": [100, 120], "load_pct": [100, 130], "cooling_capacity_pct": [60, 80]}
    generate(GeneratorConfig(machine_id="LATHE-01", episodes=6, minutes=1, param_ranges=rng), tmp_path, "synth-l")
    for e in _rows(tmp_path / "episodes.csv"):
        assert 100 <= float(e["spindle_speed_pct"]) <= 120 and 60 <= float(e["cooling_capacity_pct"]) <= 80
        # the 7.5 kW lathe: 30 N·m × load × 1800 rpm × speed must fit the rated power
        kw = 30 * float(e["load_pct"]) / 100 * 1800 * float(e["spindle_speed_pct"]) / 100 * 2 * 3.14159 / 60 / 1000
        assert kw <= 7.5 + 1e-6


@pytest.mark.parametrize("bad, words", [
    ({"machine_id": "SAW-01"}, "machine_id"),
    ({"episodes": 0}, "episodes"),
    ({"minutes": 90}, "minutes"),
    ({"episodes": 200, "minutes": 60}, "limit per dataset"),
    ({"faults": ["meteor"]}, "faults"),
    ({"faults": []}, "faults"),
    ({"param_ranges": {"spindle_speed_pct": [30, 90]}}, "outside the limit"),
    ({"param_ranges": {"load_pct": [120, 80]}}, "min ≤ max"),
    ({"param_ranges": {"pressure_bar": [1, 2]}}, "not a parameter"),
    ({"extra_ranges": {"noise_scale": [0.1, 5]}}, "outside"),
    ({"machine_id": "LATHE-01", "param_ranges": {"spindle_speed_pct": [120, 120], "load_pct": [130, 130]}}, "rated"),
])
def test_out_of_limit_configs_are_refused(bad, words):
    with pytest.raises(GeneratorError, match=words):
        GeneratorConfig(**bad).validate()


def test_labels_are_the_injected_ground_truth(tmp_path):
    generate(GeneratorConfig(episodes=2, minutes=4, faults=["normal"]), tmp_path / "n", "synth-n")
    assert {r["label_fault"] for r in _rows(tmp_path / "n" / "windows.csv")} == {"normal"}
    generate(GeneratorConfig(episodes=2, minutes=12, faults=["overload"]), tmp_path / "o", "synth-o")
    eps = {int(e["episode"]): float(e["onset_s"]) for e in _rows(tmp_path / "o" / "episodes.csv")}
    for r in _rows(tmp_path / "o" / "windows.csv"):
        onset = eps[int(r["episode"])]
        before = float(r["t_s"]) <= onset
        assert r["label_fault"] == ("normal" if before else "overload"), r["t_s"]
        assert (float(r["since_onset_s"]) == -1.0) == (r["label_fault"] == "normal")


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setenv("UDYAM_TICKER", "0")
    monkeypatch.setenv("UDYAM_AUTO_DEMO", "0")
    from api.app import app, factory_engine, operating_mode, synthetic_store
    from api.operating_mode import TESTING
    monkeypatch.setattr(synthetic_store, "root", tmp_path / "synthetic")
    with TestClient(app) as c:
        yield c, factory_engine
    operating_mode.set(TESTING, by="test teardown")


def synthetic_root(_c):
    from api.app import synthetic_store
    return synthetic_store.root


def _wait(c, timeout=60):
    t0 = time.time()
    while time.time() - t0 < timeout:
        j = c.get("/api/synthetic/job").json()
        if j["state"] != "running":
            return j
        time.sleep(0.1)
    raise AssertionError("generation did not finish")


def test_api_generates_without_touching_the_live_simulation(client):
    c, eng = client
    for _ in range(6):
        eng.step()
    before = (eng.t, eng.rng.getstate(), dict(eng.sim_params), {m: s["ts"] for m, s in eng.snapshots.items()},
              eng.event_seq, eng.plants["VMC-01"].process_temp_k)
    o = c.get("/api/synthetic/options").json()
    assert o["data_label"] == DATA_LABEL and {m["machine_id"] for m in o["machines"]} == {"VMC-01", "LATHE-01"}
    r = c.post("/api/synthetic/generate", json={"episodes": 3, "minutes": 2, "name": "smoke <b>",
                                                "param_ranges": {"cooling_capacity_pct": [60, 100]}})
    assert r.status_code == 202 and r.json()["state"] == "running"
    j = _wait(c)
    assert j["state"] == "done" and j["done"] == 3
    after = (eng.t, eng.rng.getstate(), dict(eng.sim_params), {m: s["ts"] for m, s in eng.snapshots.items()},
             eng.event_seq, eng.plants["VMC-01"].process_temp_k)
    assert before == after
    assert all(tw.risk_model is None for tw in eng.twins.values())  # the AI4I skip is per generator twin only
    ds = c.get("/api/synthetic/datasets").json()["datasets"]
    assert len(ds) == 1 and ds[0]["data_label"] == DATA_LABEL and ds[0]["name"] == "smoke b"


def test_api_export_formats_and_errors(client):
    c, _ = client
    assert c.post("/api/synthetic/generate", json={"episodes": 2, "minutes": 2}).status_code == 202
    ds_id = _wait(c)["dataset_id"]
    url = f"/api/synthetic/datasets/{ds_id}/download"
    csv_r = c.get(url, params={"part": "windows", "format": "csv"})
    assert csv_r.status_code == 200 and csv_r.headers["x-data-label"] == DATA_LABEL
    assert f'SYNTHETIC_{ds_id}_windows.csv' in csv_r.headers["content-disposition"]
    lines = csv_r.text.strip().splitlines()
    assert lines[0].startswith("data_label,") and all(l.startswith(DATA_LABEL + ",") for l in lines[1:])
    js = c.get(url, params={"part": "episodes", "format": "json"}).json()
    assert js["data_label"] == DATA_LABEL and len(js["rows"]) == 2 and isinstance(js["rows"][0]["episode"], int)
    meta = c.get(url, params={"part": "metadata", "format": "json"}).json()
    assert meta["synthetic"] and meta["config"]["episodes"] == 2
    assert c.get(url, params={"part": "metadata", "format": "csv"}).status_code == 400
    assert c.get("/api/synthetic/datasets/..%2F..%2Fapi").status_code in (400, 404)
    assert c.get("/api/synthetic/datasets/synth-20990101-000000-abcdef").status_code == 404
    r = c.post("/api/synthetic/generate", json={"episodes": 2, "minutes": 2, "param_ranges": {"load_pct": [10, 200]}})
    assert r.status_code == 422 and "outside the limit" in r.json()["detail"]["message"]
    assert c.delete(f"/api/synthetic/datasets/{ds_id}").json() == {"deleted": ds_id}
    assert c.get("/api/synthetic/datasets").json()["datasets"] == []


def test_generation_is_allowed_in_real_data_mode_and_changes_nothing(client):
    c, eng = client
    c.post("/api/operating-mode", json={"mode": "REAL_DATA"})
    assert c.post("/api/synthetic/generate", json={"episodes": 1, "minutes": 1}).status_code == 202
    assert _wait(c)["state"] == "done"
    assert c.get("/api/operating-mode").json()["mode"] == "REAL_DATA"


def test_cancel_keeps_nothing(client):
    c, _ = client
    c.post("/api/synthetic/generate", json={"episodes": 40, "minutes": 10})
    assert c.post("/api/synthetic/generate", json={"episodes": 1, "minutes": 1}).status_code == 409
    c.post("/api/synthetic/job/cancel")
    j = _wait(c)
    assert j["state"] == "cancelled"
    assert c.get("/api/synthetic/datasets").json()["datasets"] == []
    assert not list(synthetic_root(c).glob(".synth-*"))  # the partial folder is removed
