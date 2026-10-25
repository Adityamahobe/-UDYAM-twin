"""Vibration health: features, validation, severity framework, training pipeline, inference, API.

SYNTHETIC TEST FIXTURES: every waveform and dataset in this file is generated here to
check the code paths. None of it is vibration data from a real machine, and the
threshold values in TEST_PROFILE are arbitrary test numbers, not ISO 10816/20816 values.
"""

import json
import math
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from hub import IoTHub
from ml.vibration.features import FEATURE_COLUMNS, G_M_S2, extract_features
from ml.vibration.inference import VibrationPredictor
from ml.vibration.monitor import VibrationMonitor
from ml.vibration.severity import UNAVAILABLE_UNCONFIGURED, SeverityAssessor
from ml.vibration.train import DatasetError, train
from config import get_config
from sim.factory import FactoryEngine

FS = 5120.0


def sine(amp=5.0, f=50.0, fs=FS, seconds=2.0):
    t = np.arange(int(fs * seconds)) / fs
    return amp * np.sin(2 * math.pi * f * t)


# --------------------------------------------------------------------------- features

def test_velocity_and_displacement_from_acceleration_match_analytic():
    amp, f = 5.0, 50.0
    fx = extract_features(sine(amp, f), FS, "m/s2", rpm=3000)
    w = 2 * math.pi * f
    assert fx.valid and not fx.issues
    assert fx.features["velocity_rms_mm_s"] == pytest.approx(amp / w / math.sqrt(2) * 1000, rel=1e-3)
    assert fx.features["disp_pp_um"] == pytest.approx(2 * amp / w ** 2 * 1e6, rel=1e-3)
    assert fx.features["accel_rms_m_s2"] == pytest.approx(amp / math.sqrt(2), rel=1e-4)
    assert fx.features["crest_factor"] == pytest.approx(math.sqrt(2), rel=1e-3)
    assert fx.features["kurtosis"] == pytest.approx(1.5, rel=1e-3)
    assert fx.features["dominant_freq_hz"] == pytest.approx(50.0)
    assert fx.features["dominant_order"] == pytest.approx(1.0)


def test_g_unit_conversion_and_dc_offset_removed():
    a = extract_features(sine(5.0), FS, "m/s2")
    g = extract_features(sine(5.0) / G_M_S2 + 1.0, FS, "g")  # +1 g gravity offset
    assert g.features["accel_rms_m_s2"] == pytest.approx(a.features["accel_rms_m_s2"], rel=1e-6)
    assert g.features["velocity_rms_mm_s"] == pytest.approx(a.features["velocity_rms_mm_s"], rel=1e-6)


def test_velocity_input_band_limits_and_skips_acceleration_features():
    v = sine(2.0 * math.sqrt(2), 30.0)  # 2 mm/s RMS velocity signal
    fx = extract_features(v, FS, "mm/s")
    assert fx.features["velocity_rms_mm_s"] == pytest.approx(2.0, rel=1e-3)
    assert fx.features["accel_rms_m_s2"] is None


def test_impulsive_signal_has_high_kurtosis():
    x = np.random.default_rng(1).normal(0, 0.05, 8192)
    x[::400] += 5.0
    assert extract_features(x, FS, "m/s2").features["kurtosis"] > 10


@pytest.mark.parametrize("fs", [0, -10, float("nan"), "abc", None])
def test_invalid_sampling_rate_rejected(fs):
    fx = extract_features(sine(), fs, "m/s2")
    assert not fx.valid and fx.issues[0].code == "INVALID_SAMPLING_RATE"
    assert all(v is None for v in fx.features.values())


def test_unknown_unit_rejected():
    fx = extract_features(sine(), FS, "furlongs")
    assert not fx.valid and fx.issues[0].code == "UNKNOWN_UNIT"


def test_missing_samples_not_interpolated():
    x = sine()
    x[100] = np.nan
    fx = extract_features(x, FS, "m/s2")
    assert not fx.valid and fx.issues[0].code == "NON_FINITE_SAMPLES"


def test_too_few_samples_and_flat_signal():
    assert extract_features([1.0] * 10, FS, "m/s2").issues[0].code == "TOO_FEW_SAMPLES"
    flat = extract_features(np.full(2048, 9.81), FS, "m/s2")
    assert not flat.valid and flat.issues[0].code == "FLAT_SIGNAL"


def test_clipping_detected():
    x = np.clip(sine(5.0), -3.0, 3.0)
    fx = extract_features(x, FS, "m/s2", sensor_range=3.0)
    assert not fx.valid and any(i.code == "CLIPPED" for i in fx.issues)


def test_low_sampling_rate_skips_frequency_features_only():
    fx = extract_features(sine(5.0, 5.0, fs=20.0, seconds=10), 20.0, "m/s2")
    assert fx.valid
    assert fx.features["accel_rms_m_s2"] is not None
    assert fx.features["velocity_rms_mm_s"] is None and fx.features["dominant_freq_hz"] is None
    assert any(i.code == "INSUFFICIENT_SAMPLING_RATE" for i in fx.issues)


def test_band_truncated_and_short_record_flagged():
    trunc = extract_features(sine(5.0, 50.0, fs=1000.0), 1000.0, "m/s2")
    assert trunc.effective_band_hz == (10.0, pytest.approx(390.625))
    assert any(i.code == "BAND_TRUNCATED" for i in trunc.issues)
    short = extract_features(sine(5.0, 50.0, seconds=0.2), FS, "m/s2")
    assert short.features["velocity_rms_mm_s"] is None
    assert any(i.code == "RECORD_TOO_SHORT" for i in short.issues)


def test_vibration_features_never_reuse_ai4i_columns():
    assert not set(FEATURE_COLUMNS) & {"air_temp_k", "process_temp_k", "torque_nm", "tool_wear_min", "failure"}


# --------------------------------------------------------------------------- severity

TEST_PROFILE = {
    "profile_id": "test-profile",
    "standard": "TEST FIXTURE (not ISO values)",
    "edition": "test",
    "machine_group": "test group",
    "quantity": "velocity_rms_mm_s",
    "band_hz": [10.0, 1000.0],
    "measurement_location": "bearing_housing",
    "mounting": "rigid",
    "rpm_range": [600, 3600],
    "zone_boundaries": {"A_B": 1.0, "B_C": 2.0, "C_D": 3.0},
    "verification": {"verified": True, "verified_by": "test", "source_reference": "test fixture"},
}
ASSIGN = {"VMC-01": {"profile_id": "test-profile", "measurement_location": "bearing_housing", "mounting": "rigid"}}


def assessor(profile=None, assign=None):
    return SeverityAssessor({"threshold_profiles": [profile or TEST_PROFILE], "machine_assignments": assign or ASSIGN})


def assess(a, v, **kw):
    kw = {"measurement_band_hz": [10.0, 1000.0], "rpm": 1500, "operating_state": "RUN", **kw}
    return a.assess("VMC-01", {"velocity_rms_mm_s": v}, **kw)


def test_shipped_config_assesses_vmc_provisionally():
    a = SeverityAssessor()  # config/machines.yaml
    assert a.status()["profiles_configured"] >= 1 and "VMC-01" in a.status()["machines_assigned"]
    r = a.assess("VMC-01", {"velocity_rms_mm_s": 2.33}, measurement_band_hz=[10, 1000], operating_state="RUN")
    assert r.status == "PROVISIONAL" and r.zone == "B"
    assert r.threshold == {"crossed": "A/B", "crossed_value": 1.4, "next": "B/C", "next_value": 2.8, "unit": "mm/s"}
    assert "not a certified assessment" in r.reason and "≥ A/B 1.4" in r.reason


@pytest.mark.parametrize("v,zone,crossed", [(1.0, "A", None), (1.4, "B", "A/B"), (2.8, "C", "B/C"), (4.5, "D", "C/D"), (6.7, "D", "C/D")])
def test_shipped_vmc_zones(v, zone, crossed):
    r = SeverityAssessor().assess("VMC-01", {"velocity_rms_mm_s": v}, measurement_band_hz=[10, 1000], operating_state="RUN")
    assert (r.zone, r.threshold["crossed"]) == (zone, crossed)


def test_runtime_machine_inherits_template_profile():
    a = SeverityAssessor()
    r = a.assess("VMC-02", {"velocity_rms_mm_s": 3.0}, measurement_band_hz=[10, 1000], template="cnc_mill")
    assert r.status == "PROVISIONAL" and r.zone == "C"
    assert a.assess("VMC-02", {"velocity_rms_mm_s": 3.0}).reason == UNAVAILABLE_UNCONFIGURED


def test_compressor_uses_flexible_foundation_profile():
    r = SeverityAssessor().assess("COMP-01", {"velocity_rms_mm_s": 2.8}, measurement_band_hz=[10, 1000], operating_state="RUN")
    assert r.zone == "B" and r.profile["mounting"] == "flexible"


def test_missing_classification_is_unavailable():
    r = assessor().assess("LATHE-01", {"velocity_rms_mm_s": 2.0})
    assert r.status == "UNAVAILABLE" and r.reason == UNAVAILABLE_UNCONFIGURED


@pytest.mark.parametrize("v,zone", [(0.5, "A"), (1.5, "B"), (2.5, "C"), (3.5, "D"), (3.0, "D")])
def test_configured_verified_profile_zones(v, zone):
    r = assess(assessor(), v)
    assert (r.status, r.zone) == ("ASSESSED", zone)


def test_unverified_profile_is_provisional():
    p = {**TEST_PROFILE, "verification": {"verified": False}}
    r = assess(assessor(p), 2.5)
    assert r.status == "PROVISIONAL" and r.zone == "C" and "not engineering-verified" in r.reason


def test_incomplete_profile_unavailable():
    p = {**TEST_PROFILE, "zone_boundaries": {"A_B": None, "B_C": None, "C_D": None}, "band_hz": [None, None]}
    r = assess(assessor(p), 2.5)
    assert r.status == "UNAVAILABLE" and "incomplete" in r.reason


def test_applicability_checks():
    a = assessor(assign={"VMC-01": {**ASSIGN["VMC-01"], "mounting": "flexible"}})
    assert assess(a, 2.0).status == "NOT_APPLICABLE"
    a = assessor()
    assert assess(a, 2.0, measurement_band_hz=None).status == "UNAVAILABLE"
    assert assess(a, 2.0, measurement_band_hz=[2.0, 1000.0]).status == "NOT_APPLICABLE"
    assert assess(a, 2.0, rpm=None).status == "UNAVAILABLE"
    assert assess(a, 2.0, rpm=5000).status == "NOT_APPLICABLE"
    assert assess(a, 2.0, operating_state="IDLE").status == "NOT_APPLICABLE"
    assert assess(a, 2.0, dq_flag="STALE").status == "UNAVAILABLE"
    assert assess(a, None).status == "NO_DATA"


# --------------------------------------------------------------------------- training (synthetic fixture)

def make_synthetic_dataset(root: Path, machines=6, per_class=4, synthetic=True) -> Path:
    """SYNTHETIC FIXTURE: two classes, 'healthy' (tones) vs 'impact' (tones + impacts)."""
    rng = np.random.default_rng(7)
    (root / "signals").mkdir(parents=True)
    rows = []
    for m in range(machines):
        for label in ("healthy", "impact"):
            for k in range(per_class):
                x = sine(0.5 + 0.1 * m, 25.0, fs=2560.0, seconds=1.6) + rng.normal(0, 0.05, 4096)
                if label == "impact":
                    x[::200] += 4.0
                rid = f"M{m}-{label}-{k}"
                np.save(root / "signals" / f"{rid}.npy", x)
                rows.append({"recording_id": rid, "machine_id": f"M{m}", "label": label, "sampling_rate_hz": 2560.0,
                             "unit": "m/s2", "signal_path": f"signals/{rid}.npy", "rpm": 1500,
                             "recorded_at": f"2026-01-{1 + k:02d}T00:00:00Z"})
    pd.DataFrame(rows).to_csv(root / "manifest.csv", index=False)
    (root / "dataset.json").write_text(json.dumps({
        "name": "pytest synthetic fixture", "source": "generated in tests/test_vibration.py",
        "synthetic": synthetic, "label_definition": "healthy vs impact (synthetic)",
    }))
    return root


def test_training_refuses_synthetic_without_flag(tmp_path):
    data = make_synthetic_dataset(tmp_path / "ds")
    with pytest.raises(DatasetError, match="synthetic"):
        train(data, tmp_path / "out")
    assert not (tmp_path / "out").exists()


def test_training_pipeline_on_synthetic_fixture(tmp_path):
    data = make_synthetic_dataset(tmp_path / "ds")
    rep = train(data, tmp_path / "out", allow_synthetic=True, seed=3)
    split = rep["split"]
    assert split["strategy"] == "machine"
    tr, va, te = (set(split["machines"][k]) for k in ("train", "validation", "test"))
    assert not (tr & va or tr & te or va & te)  # no machine in two splits
    assert set(rep["results"]) == {"majority_class_baseline", "logistic_regression_baseline", "random_forest"}
    for res in rep["results"].values():
        assert {"macro_precision", "macro_recall", "macro_f1", "confusion_matrix", "per_class"} <= set(res["test"])
    assert rep["dataset"]["synthetic"] is True
    assert not set(rep["feature_columns"]) & {"recording_id", "machine_id", "label", "recorded_at"}
    assert (tmp_path / "out" / "model.joblib").exists() and (tmp_path / "out" / "metrics.json").exists()

    pred = VibrationPredictor(tmp_path / "out" / "model.joblib")
    impact = extract_features(sine(0.5, 25.0, fs=2560.0, seconds=1.6) + np.where(np.arange(4096) % 200 == 0, 4.0, 0.0),
                              2560.0, "m/s2", rpm=1500).features
    assert pred.predict(impact)["status"] == "SYNTHETIC_MODEL"
    out = pred.predict(impact, allow_synthetic=True)
    assert out["status"] == "OK" and out["label"] in {"healthy", "impact"} and out["calibrated"] is False
    assert pred.predict({"rpm": 1500.0}, allow_synthetic=True)["status"] == "INSUFFICIENT_FEATURES"


def test_training_input_validation(tmp_path):
    data = make_synthetic_dataset(tmp_path / "ds", machines=2)
    man = pd.read_csv(data / "manifest.csv").drop(columns=["recorded_at"])
    man.to_csv(data / "manifest.csv", index=False)
    with pytest.raises(DatasetError, match="machine-level split"):
        train(data, tmp_path / "out", allow_synthetic=True)

    man[man["label"] == "healthy"].to_csv(data / "manifest.csv", index=False)
    with pytest.raises(DatasetError, match="2 classes"):
        train(data, tmp_path / "out", allow_synthetic=True)

    man.drop(columns=["unit"]).to_csv(data / "manifest.csv", index=False)
    with pytest.raises(DatasetError, match="missing columns"):
        train(data, tmp_path / "out", allow_synthetic=True)


def test_untrained_predictor_reports_training_not_configured(tmp_path):
    p = VibrationPredictor(tmp_path / "missing.joblib")
    assert p.info()["status"] == "TRAINING_NOT_CONFIGURED"
    assert p.predict({"velocity_rms_mm_s": 2.0})["status"] == "TRAINING_NOT_CONFIGURED"


def test_shipped_model_if_any_is_not_synthetic():
    info = VibrationPredictor().info()
    assert info["status"] in {"READY", "TRAINING_NOT_CONFIGURED"}
    if info["status"] == "READY":
        assert info["dataset"]["synthetic"] is False and info["dataset"]["source"]
        assert info["probability_calibrated"] is False


# --------------------------------------------------------------------------- monitor

@pytest.fixture
def engine_monitor(tmp_path):
    engine = FactoryEngine(seed=1, auto_demo=False)
    info = lambda mid: ({"template": engine.plants[mid].template, "name": engine.plants[mid].name,
                         "rated_kw": engine.plants[mid].nameplate.get("rated_kw"),
                         "bearing_model": get_config().ml(mid, engine.plants[mid].template)["bearing_model"]}
                        if mid in engine.plants else None)
    mon = VibrationMonitor(SeverityAssessor({}), VibrationPredictor(tmp_path / "none.joblib"), info)
    return engine, mon


def test_monitor_labels_simulated_stream(engine_monitor):
    engine, mon = engine_monitor
    for _ in range(5):
        mon.observe(engine.step())
    s = mon.snapshot("VMC-01")
    assert s["stream_velocity"]["provenance"] == "simulated"
    assert s["stream_velocity"]["band_hz"] is None
    assert s["severity"]["status"] == "UNAVAILABLE"
    # The CWRU bearing classifier is for bearing/motor assets only: not the mill, yes the compressor.
    assert s["ml"]["status"] == "NOT_APPLICABLE" and "cnc_mill" in s["ml"]["detail"]
    assert mon.snapshot("COMP-01")["ml"]["status"] == "TRAINING_NOT_CONFIGURED"
    assert len(s["trend"]["points"]) == 5
    assert s["operating"]["load_pct"] is not None


def test_monitor_handles_missing_measurements(engine_monitor):
    engine, mon = engine_monitor
    assert mon.snapshot("VMC-01")["data_quality"]["warnings"] == ["No telemetry received for this machine yet."]
    msgs = engine.step()
    for m in msgs:
        if m.get("machine_id") == "VMC-01" and m["type"] == "observed":
            m["vibration_rms_mm_s"] = None
    mon.observe(msgs)
    s = mon.snapshot("VMC-01")
    assert s["stream_velocity"]["value"] is None
    assert "Vibration velocity not reported." in s["data_quality"]["warnings"]
    assert s["trend"]["points"][-1][1] is None
    assert mon.snapshot("NOPE-01") is None


def test_monitor_waveform_rejected_when_invalid(engine_monitor):
    _, mon = engine_monitor
    out = mon.ingest_waveform("VMC-01", {"samples": [0.0] * 10, "sampling_rate_hz": 100, "unit": "g"},
                              provenance="simulated", source="test")
    assert out["valid"] is False
    assert mon.snapshot("VMC-01")["waveform"] is None


# --------------------------------------------------------------------------- API

@pytest.fixture
def client(monkeypatch):
    from api.app import app, iot_hub

    # Isolate from the real device registry in udyam_twin.sqlite: empty, in-memory only.
    monkeypatch.setattr(iot_hub, "devices", {})
    monkeypatch.setattr(iot_hub, "db_path", None)
    with TestClient(app) as c:
        yield c


def test_api_vibration_overview(client):
    data = client.get("/api/vibration").json()
    assert data["severity_config"]["profiles_configured"] >= 1
    assert data["ml_model"]["status"] == VibrationPredictor().info()["status"]
    ids = {m["machine_id"] for m in data["machines"]}
    assert {"VMC-01", "LATHE-01", "SAW-01"} <= ids
    for m in data["machines"]:
        # Every machine is classified (bug 1); before the first tick there is simply no telemetry yet.
        assert m["severity"]["reason"] != UNAVAILABLE_UNCONFIGURED
        assert m["severity"]["status"] in ("UNAVAILABLE", "PROVISIONAL", "NOT_APPLICABLE")
        if m["severity"]["status"] == "PROVISIONAL":
            assert m["stream_velocity"]["band_hz"] == [10, 1000]
            assert m["severity"]["basis"].startswith("stream scalar")
    assert client.get("/api/vibration/NOPE-01").status_code == 404


def test_api_waveform_upload_provenance(client):
    sim = client.post("/api/iot/devices", json={"device_id": "TEST-VIB-SIM", "machine_id": "LATHE-01", "simulated": True}).json()
    real = client.post("/api/iot/devices", json={"device_id": "TEST-VIB-HW", "machine_id": "SAW-01"}).json()
    body = {"sampling_rate_hz": 2560.0, "unit": "m/s2", "samples": sine(1.0, 25.0, fs=2560.0, seconds=1.6).tolist()}

    assert client.post("/api/iot/waveform", json=body, headers={"X-Device-Id": "TEST-VIB-SIM", "X-Device-Token": "bad"}).status_code == 401
    r = client.post("/api/iot/waveform", json=body, headers={"X-Device-Id": "TEST-VIB-SIM", "X-Device-Token": sim["token"]})
    assert r.status_code == 200 and r.json()["provenance"] == "simulated" and r.json()["valid"]
    r = client.post("/api/iot/waveform", json=body, headers={"X-Device-Id": "TEST-VIB-HW", "X-Device-Token": real["token"]})
    assert r.json()["provenance"] == "measured"

    snap = client.get("/api/vibration/LATHE-01").json()
    assert snap["waveform"]["provenance"] == "simulated"
    assert snap["waveform"]["features"]["dominant_freq_hz"] == pytest.approx(25.0, abs=1.0)
    assert snap["severity"]["basis_provenance"] == "simulated"

    bad = client.post("/api/iot/waveform", json={**body, "sampling_rate_hz": 0},
                      headers={"X-Device-Id": "TEST-VIB-SIM", "X-Device-Token": sim["token"]})
    assert bad.json()["valid"] is False


def test_api_recording_upload_requires_source(client):
    body = {"sampling_rate_hz": 2560.0, "unit": "g", "samples": (sine(1.0, 25.0, fs=2560.0, seconds=1.6) / G_M_S2).tolist()}
    assert client.post("/api/vibration/VMC-01/recording", json=body).status_code == 422
    r = client.post("/api/vibration/VMC-01/recording", json={**body, "source": "bench file x.csv"})
    assert r.status_code == 200 and r.json()["provenance"] == "recorded"
    assert client.post("/api/vibration/NOPE/recording", json={**body, "source": "x.csv"}).status_code == 404


def test_simulated_device_stays_sim_in_stream(tmp_path):
    engine = FactoryEngine(seed=1, auto_demo=False)
    hub = IoTHub(db_path=tmp_path / "h.sqlite",
                 template_of=lambda mid: engine.plants[mid].template if mid in engine.plants else None)
    engine.live_overlay = hub.overlay
    tok = hub.register("FAKE", "VMC-01", simulated=True)["token"]
    hub.ingest("FAKE", tok, {"vibration_rms_mm_s": 3.3}, "http")
    obs = next(m for m in engine.step() if m["type"] == "observed" and m["machine_id"] == "VMC-01")
    assert obs["source_mode"] == "SIM" and obs["vibration_rms_mm_s"] == 3.3
    assert IoTHub(db_path=tmp_path / "h.sqlite").get("FAKE")["simulated"] is True


def test_inference_domain_checks(tmp_path):
    data = make_synthetic_dataset(tmp_path / "ds")
    man = pd.read_csv(data / "manifest.csv")
    man["machine_type"] = "fixture_rig"
    man.to_csv(data / "manifest.csv", index=False)
    train(data, tmp_path / "out", allow_synthetic=True, seed=3)
    pred = VibrationPredictor(tmp_path / "out" / "model.joblib")
    assert pred.info()["input_spec"] == {"sampling_rate_hz": [2560.0], "machine_types": ["fixture_rig"]}
    fx = extract_features(sine(0.5, 25.0, fs=2560.0, seconds=1.6), 2560.0, "m/s2", rpm=1500).features

    other_fs = pred.predict(fx, allow_synthetic=True, context={"sampling_rate_hz": 12000})
    assert other_fs["status"] == "OUT_OF_DOMAIN" and other_fs["label"] is None
    other_machine = pred.predict(fx, allow_synthetic=True, context={"sampling_rate_hz": 2560, "machine_type": "cnc_mill"})
    assert other_machine["status"] == "OK_UNVALIDATED" and other_machine["label"] and "not validated" in other_machine["detail"]
    same = pred.predict(fx, allow_synthetic=True, context={"sampling_rate_hz": 2560, "machine_type": "fixture_rig"})
    assert same["status"] == "OK"
