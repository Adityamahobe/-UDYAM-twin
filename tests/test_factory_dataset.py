"""Factory datasets: recorded readings, windows, line state and labels from run designs, the run-level train/test
split, the quality checks, and the API (collect, list, preview, downloads, delete)."""

from __future__ import annotations

import csv
import io
import json
import time
import zipfile

import pytest
from fastapi.testclient import TestClient

from ml import factory_dataset as fd
from sim.factory_run import RunSpec, Variation

ECON = {"inr_per_part": 450.0, "electricity_tariff_per_kwh": 8.5}
BEARING = {"at_min": 2, "action": "inject_fault", "machine_id": "LATHE-01", "fault": "bearing_defect", "severity": 1.2}
AIR = {"at_min": 2, "action": "inject_fault", "machine_id": "COMP-01", "fault": "pressure_drop", "severity": 1.0}
VAR = Variation(0.5, [0.8, 1.2], 0)


def _rows(path):
    with open(path, newline="") as fh:
        return list(csv.DictReader(fh))


@pytest.fixture(scope="module")
def ds(tmp_path_factory):
    out = tmp_path_factory.mktemp("fds") / "d"
    parts = [fd.Part(RunSpec("Bearing", 5, 3, True, [BEARING, {"at_min": 4, "action": "repair", "machine_id": "LATHE-01"}]), 3, VAR),
             fd.Part(RunSpec("Air", 5, 5, True, [AIR]), 3, VAR),
             fd.Part(RunSpec("Normal", 5, 9, True, []), 2, None)]
    meta = fd.collect(parts, ECON, out, "fds-test", "t", test_share=0.34, split_seed=1)
    return out, meta


def test_files_rows_and_labels(ds):
    out, meta = ds
    assert meta["data_label"] == fd.DATA_LABEL and meta["real_machine_data"] is False
    c = meta["counts"]
    # 8 runs × 5 min × 4 machines: readings every 2 s, windows every 10 s once 60 s are buffered
    assert c["runs"] == 8 and c["readings"] == 8 * 150 * 4 and c["windows"] == 8 * 25 * 4
    for name, info in meta["files"].items():
        assert (out / name).stat().st_size == info["bytes"]
    windows = _rows(out / "windows.csv")
    assert all(w["data_label"] == fd.DATA_LABEL for w in windows)
    assert set(meta["features"]) <= set(windows[0])
    lathe = [w for w in windows if w["run_id"] == "p1-r001" and w["machine_id"] == "LATHE-01"]
    faulty = [w for w in lathe if w["label_fault"] == "bearing_defect"]
    assert faulty and all(w["label_cause_machine"] == "LATHE-01" and w["label_knock_on"] == "0" for w in faulty)
    assert float(faulty[0]["since_onset_s"]) >= 0 and lathe[-1]["label_fault"] == "normal"  # repaired before the end
    # a compressor pressure drop labels the CNCs as a knock-on of COMP-01, not the saw (it uses no air)
    air = [w for w in windows if w["run_id"] == "p2-r001" and float(w["t_s"]) > 200]
    assert {w["label_fault"] for w in air if w["machine_id"] in ("VMC-01", "LATHE-01")} == {"pressure_drop"}
    assert {w["label_knock_on"] for w in air if w["machine_id"] == "VMC-01"} == {"1"}
    assert {w["label_cause_machine"] for w in air if w["machine_id"] in ("VMC-01", "COMP-01")} == {"COMP-01"}
    assert {w["label_fault"] for w in air if w["machine_id"] == "SAW-01"} == {"normal"}
    labels = _rows(out / "labels.csv")
    assert len(labels) == 6 and {l["fault"] for l in labels} == {"bearing_defect", "pressure_drop"}
    line = _rows(out / "line.csv")
    assert len(line) == 8 * 30 and {"throughput_parts_per_hr", "bottleneck", "air_supply_ok", "buffer_VMC-01"} <= set(line[0])
    assert any(r["air_supply_ok"] == "0" for r in line if r["run_id"] == "p2-r001")
    ttf = [float(w["minutes_to_limit"]) for w in windows]
    assert all(0 <= m <= fd.HORIZON_MIN for m in ttf)


def test_split_is_by_whole_run_and_covers_each_fault_group(ds):
    out, meta = ds
    train, test = set(meta["split"]["train"]), set(meta["split"]["test"])
    assert train and test and not (train & test) and len(train | test) == 8
    for f in ("readings", "windows", "line", "events", "labels", "runs"):
        for r in _rows(out / f"{f}.csv"):
            assert r["split"] == ("test" if r["run_id"] in test else "train")
    runs = {r["run_id"]: r for r in _rows(out / "runs.csv")}
    for part in ("1", "2", "3"):  # every group of runs (same faults) is split on its own
        assert {runs[r]["split"] for r in runs if runs[r]["part"] == part} == {"train", "test"}
    checks = {c["check"]: c["status"] for c in meta["checks"]}
    assert checks["Split by whole run"] == "pass" and checks["Labels on both sides"] == "pass"


def test_collection_is_reproducible(tmp_path):
    parts = [fd.Part(RunSpec("Air", 3, 5, True, [{**AIR, "at_min": 1}]), 2, VAR)]
    a = fd.collect(parts, ECON, tmp_path / "a", "fds-a", test_share=0.5)
    b = fd.collect(parts, ECON, tmp_path / "b", "fds-b", test_share=0.5)
    assert {k: v["sha256"] for k, v in a["files"].items()} == {k: v["sha256"] for k, v in b["files"].items()}


def test_checks_flag_missing_faults_and_missing_test_set(tmp_path):
    meta = fd.collect([fd.Part(RunSpec("Normal", 2, 1, True, []), 2, None)], ECON, tmp_path / "n", "fds-n", test_share=0)
    st = {c["check"]: c["status"] for c in meta["checks"]}
    assert st["Fault labels"] == "warn" and st["Test set"] == "warn"


@pytest.mark.parametrize("parts, words", [
    ([], "1–10 parts"),
    ([fd.Part(RunSpec(minutes=30), 101)], "runs must be"),
    ([fd.Part(RunSpec(minutes=60), 60)], "simulated minutes"),
    ([fd.Part(RunSpec(minutes=5, events=[{**BEARING, "machine_id": "SAW-01"}]), 2)], "can have"),
    ([fd.Part(RunSpec(minutes=5), 2, Variation(0, [0.2, 1.0], 0))], "severity range"),
])
def test_recipe_limits(parts, words):
    with pytest.raises(fd.DatasetError, match=words):
        fd.check_recipe(parts, None)


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setenv("UDYAM_TICKER", "0")
    monkeypatch.setenv("UDYAM_AUTO_DEMO", "0")
    import sys
    import api.app  # noqa: F401
    appmod = sys.modules["api.app"]
    monkeypatch.setattr(appmod, "factory_datasets", fd.FactoryDatasetStore(root=tmp_path / "factory"))
    with TestClient(appmod.app) as c:
        yield c, appmod


def test_api_collect_preview_download_delete(client):
    c, appmod = client
    bad = c.post("/api/factory-dataset/collect", json={"parts": [{"spec": {"minutes": 60}, "runs": 60}]})
    assert bad.status_code == 422 and "simulated minutes" in bad.json()["detail"]["message"]
    t_before = appmod.factory_engine.t
    body = {"name": "API test", "test_share": 0.5, "parts": [
        {"spec": {"name": "Air", "minutes": 3, "events": [{**AIR, "at_min": 1}]}, "runs": 2, "fault_time_jitter_min": 0.5},
        {"spec": {"name": "Normal", "minutes": 3}, "runs": 2}]}
    r = c.post("/api/factory-dataset/collect", json=body)
    assert r.status_code == 202
    t0 = time.time()
    while (j := c.get("/api/factory-dataset/job").json())["state"] == "running" and time.time() - t0 < 60:
        time.sleep(0.1)
    assert j["state"] == "done" and appmod.factory_engine.t == t_before  # the live simulation did not move
    ds_id = j["dataset_id"]
    lst = c.get("/api/factory-dataset/datasets").json()["datasets"]
    assert lst[0]["dataset_id"] == ds_id and lst[0]["counts"]["runs"] == 4 and lst[0]["name"] == "API test"
    meta = c.get(f"/api/factory-dataset/datasets/{ds_id}").json()
    p = c.get(f"/api/factory-dataset/datasets/{ds_id}/preview", params={"run_id": "p1-r001", "machine_id": "VMC-01"}).json()
    assert p["run_id"] == "p1-r001" and len(p["points"]) == 90 and {pt["label_fault"] for pt in p["points"]} == {"normal", "pressure_drop"}
    test_runs = set(meta["split"]["test"])
    w = c.get(f"/api/factory-dataset/datasets/{ds_id}/download", params={"part": "windows", "split": "test"})
    rows = list(csv.DictReader(io.StringIO(w.text)))
    assert w.headers["x-data-label"] == fd.DATA_LABEL and rows and {r["run_id"] for r in rows} == test_runs
    z = zipfile.ZipFile(io.BytesIO(c.get(f"/api/factory-dataset/datasets/{ds_id}/download").content))
    names = {n.split("/")[-1] for n in z.namelist()}
    assert names == {"metadata.json", "readings.csv", "windows.csv", "line.csv", "events.csv", "labels.csv", "runs.csv"}
    assert json.loads(z.read(f"SYNTHETIC_{ds_id}/metadata.json"))["data_label"] == fd.DATA_LABEL
    assert c.get("/api/factory-dataset/datasets/bad-id").status_code == 400
    assert c.delete(f"/api/factory-dataset/datasets/{ds_id}").status_code == 200
    assert c.get(f"/api/factory-dataset/datasets/{ds_id}").status_code == 404
