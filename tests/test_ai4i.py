"""Step 4: AI4I 2020 failure-risk model on the real dataset, ML applicability, tool-wear RUL."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import jsonschema
import pytest

from ml.ai4i import DATA_PATH, METRICS_PATH, MODES, AI4IPredictor, DatasetError, load_dataset, sha256
from sim.factory import FactoryEngine
from sim.plant import TOOL_WEAR_ACCEL

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
SCHEMA = json.loads((Path(__file__).resolve().parent.parent / "docs" / "schema.json").read_text(encoding="utf-8"))
NOMINAL = dict(air_temp_k=300.0, process_temp_k=311.0, rpm=1540.0, torque_nm=40.0, tool_wear_min=100.0)


@pytest.fixture(scope="module")
def predictor():
    p = AI4IPredictor()
    assert p.ready, p.load_error
    return p


def engine(**kw) -> FactoryEngine:
    return FactoryEngine(seed=42, auto_demo=False, start=T0, **kw)


def run(eng: FactoryEngine, ticks: int) -> None:
    for _ in range(ticks):
        eng.step()


# ----------------------------------------------------------------- dataset + metrics
def test_dataset_is_the_uci_file():
    df = load_dataset()
    assert len(df) == 10000 and int(df["machine_failure"].sum()) == 339
    assert {m: int(df[m].sum()) for m in MODES} == {"TWF": 46, "HDF": 115, "PWF": 95, "OSF": 98, "RNF": 19}


def test_loader_refuses_other_files(tmp_path):
    with pytest.raises(DatasetError, match="not found"):
        load_dataset(tmp_path / "missing.csv")
    fake = tmp_path / "ai4i2020.csv"
    fake.write_text(DATA_PATH.read_text(encoding="utf-8-sig").replace("298.1", "298.2", 1), encoding="utf-8")
    with pytest.raises(DatasetError, match="SHA-256"):
        load_dataset(fake)


def test_metrics_file_reports_real_held_out_scores():
    m = json.loads(METRICS_PATH.read_text(encoding="utf-8"))
    assert m["dataset"]["sha256"] == sha256(DATA_PATH) and m["dataset"]["synthetic_source"] is True
    assert m["split"] == {"strategy": "stratified on Machine failure", "test_size": 0.2, "seed": 42,
                          "train_rows": 8000, "test_rows": 2000}
    assert m["probability_calibrated"] is False and "not a calibrated probability" in m["honest_disclaimer"]
    assert m["features"] == ["air_temp_k", "process_temp_k", "rpm", "torque_nm", "tool_wear_min"]
    mf = m["per_class"]["machine_failure"]
    assert mf["support_positive"] == 68  # 3.39 % of 2,000
    for target, s in m["per_class"].items():
        for k in ("precision", "recall", "f1", "roc_auc", "pr_auc"):
            assert 0.0 <= s[k] <= 1.0, (target, k)
    # The machine-failure F1 on real AI4I is nowhere near the retired synthetic ~0.99.
    assert mf["f1"] < 0.9
    assert m["per_class"]["RNF"]["f1"] == 0.0


# ----------------------------------------------------------------- predictor
def test_risk_output_shape(predictor):
    r = predictor.predict(**NOMINAL)
    assert r["status"] == "OK" and 0 <= r["failure_risk_pct"] <= 100
    assert r["calibrated_probability"] is False and "not a calibrated probability" in r["note"]
    assert set(r["modes_pct"]) == set(MODES)


def test_overstrain_region_raises_risk_and_explains_it(predictor):
    nominal = predictor.predict(**NOMINAL)["failure_risk_pct"]
    r = predictor.predict(**{**NOMINAL, "torque_nm": 62.0})  # PWF region: ~10 kW at 1540 rpm
    assert r["failure_risk_pct"] > nominal + 20
    assert r["top_features"][0]["feature"] == "torque_nm" and r["top_features"][0]["unit"] == "N·m"
    assert r["top_mode"]["mode"] == "PWF"


def test_temperatures_reported_in_celsius_and_out_of_range_flagged(predictor):
    r = predictor.predict(**{**NOMINAL, "process_temp_k": 315.4})  # coolant-fault temperature
    assert r["status"] == "OUT_OF_RANGE"
    [oor] = r["out_of_range"]
    assert oor["feature"] == "process_temp_k" and oor["unit"] == "°C" and oor["value"] == pytest.approx(42.25)


def test_random_failure_never_headlines(predictor):
    for wear in (0, 60, 120, 180, 240):
        assert predictor.predict(**{**NOMINAL, "tool_wear_min": wear})["top_mode"]["mode"] != "RNF"


def test_missing_model_is_unavailable_not_faked(tmp_path):
    p = AI4IPredictor(tmp_path / "none.joblib")
    r = p.predict(**NOMINAL)
    assert r["status"] == "UNAVAILABLE" and r["failure_risk_pct"] is None
    assert "train_ai4i" in p.info()["detail"]


# ----------------------------------------------------------------- twin integration
def test_failure_model_only_on_configured_machines():
    eng = engine()
    run(eng, 6)
    vmc, lathe = eng.snapshots["VMC-01"]["risk"], eng.snapshots["LATHE-01"]["risk"]
    assert vmc["ml"]["status"] == "OK" and vmc["model"] == pytest.approx(vmc["ml"]["failure_risk_pct"] / 100, abs=1e-3)
    assert lathe["ml"]["status"] == "NOT_APPLICABLE" and lathe["model"] is None
    for mid in ("SAW-01", "COMP-01"):
        assert eng.snapshots[mid]["risk"]["model"] is None


def test_risk_is_reevaluated_every_two_seconds():
    eng = engine()
    stamps = []
    for _ in range(12):
        eng.step()
        stamps.append(eng.snapshots["VMC-01"]["risk"]["ml"]["evaluated_at"])
    assert len(set(stamps)) == 3  # ticks 1, 5, 9


def test_overload_fault_raises_snapshot_risk():
    eng = engine()
    run(eng, 10)
    healthy = eng.snapshots["VMC-01"]["risk"]["ml"]["failure_risk_pct"]
    eng.command({"cmd": "inject_fault", "machine_id": "VMC-01", "fault": "overload"})
    run(eng, 40)  # the extra torque builds up over ~8 s (first-order lag)
    r = eng.snapshots["VMC-01"]["risk"]["ml"]
    assert r["failure_risk_pct"] > healthy + 20 and r["top_mode"]["mode"] == "PWF"


# ----------------------------------------------------------------- RUL
def test_rul_needs_a_trend_then_states_its_method():
    eng = engine()
    eng.step()
    rul = eng.snapshots["VMC-01"]["rul"]
    assert rul["status"] == "INSUFFICIENT_DATA" and rul["rul_minutes"] is None
    run(eng, 30)
    rul = eng.snapshots["VMC-01"]["rul"]
    assert rul["status"] == "OK" and rul["label"] == "estimate" and "Linear least-squares" in rul["method"]
    assert rul["limit_min"] == 235.0
    # SIM wear is 0.05 min per 0.5 s tick at nominal speed → 360 min/h.
    assert rul["wear_rate_min_per_h"] == pytest.approx(360, rel=0.05)
    assert rul["rul_minutes"] == pytest.approx(rul["remaining_tool_min"] / 360 * 60, rel=0.05)
    jsonschema.validate(eng.snapshots["VMC-01"], SCHEMA)
    assert eng.snapshots["SAW-01"]["rul"] is None


def test_accelerated_wear_shortens_rul():
    base, fast = engine(), engine()
    fast.command({"cmd": "inject_fault", "machine_id": "VMC-01", "fault": "tool_wear_accel"})
    run(base, 140)
    run(fast, 140)
    b, f = base.snapshots["VMC-01"]["rul"], fast.snapshots["VMC-01"]["rul"]
    assert f["wear_rate_min_per_h"] == pytest.approx(TOOL_WEAR_ACCEL * b["wear_rate_min_per_h"], rel=0.1)
    assert f["rul_minutes"] < b["rul_minutes"] / 3


def test_tool_change_restarts_the_trend():
    eng = engine()
    run(eng, 30)
    eng.plants["VMC-01"].tool_wear_min = 0.0
    eng.step()
    assert eng.snapshots["VMC-01"]["rul"]["status"] == "INSUFFICIENT_DATA"


def test_tool_wear_forecast_uses_the_rul_number():
    eng = engine()
    eng.plants["VMC-01"].tool_wear_min = 205.0
    run(eng, 30)
    s = eng.snapshots["VMC-01"]
    assert s["forecast"]["warning"] == "TWF_RISK"
    assert s["forecast"]["minutes_to_warning"] == s["rul"]["rul_minutes"]
