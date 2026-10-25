"""IMS bearing run-to-failure pipeline: ISO 20816-3 zones, features per snapshot, training and the API."""

from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from ml.vibration.datasets.ims import FS_HZ, _features_for_file, zone_of
from ml.vibration.train_ims import OUT, add_features, early_warning, rul_check, zone_progression

ZONES = {"A_B": 1.4, "B_C": 2.8, "C_D": 4.5}


def test_zone_of_uses_the_iso_20816_boundaries():
    assert [zone_of(v, ZONES) for v in (0.5, 1.4, 2.79, 2.8, 4.5, 30.0)] == ["A", "B", "B", "C", "D", "D"]
    assert zone_of(None, ZONES) is None and zone_of(float("nan"), ZONES) is None


def _sine_file(path, amps_g, f_hz=160.0):
    t = np.arange(20480) / FS_HZ
    np.savetxt(path, np.column_stack([a * np.sin(2 * math.pi * f_hz * t) for a in amps_g]), delimiter="\t", fmt="%.6f")
    return str(path)


def test_velocity_rms_matches_the_analytic_value(tmp_path):
    # a g sine at f Hz: velocity RMS = a·9.80665 / (2πf) / √2, in mm/s
    path = _sine_file(tmp_path / "2004.02.12.10.32.39", [0.1, 0.2, 0.3, 0.4])
    rows = _features_for_file((2, path))
    expected = [a * 9.80665 / (2 * math.pi * 160.0) / math.sqrt(2) * 1000 for a in (0.1, 0.2, 0.3, 0.4)]
    assert [r["bearing"] for r in rows] == [1, 2, 3, 4]
    for r, e in zip(rows, expected):
        assert r["velocity_rms_mm_s"] == pytest.approx(e, rel=0.02)


def test_test_1_uses_the_larger_of_x_and_y(tmp_path):
    path = _sine_file(tmp_path / "2003.10.22.12.06.24", [0.1, 0.3, 0.2, 0.2, 0.1, 0.1, 0.4, 0.05])
    rows = {r["bearing"]: r for r in _features_for_file((1, path))}
    one = 0.1 * 9.80665 / (2 * math.pi * 160.0) / math.sqrt(2) * 1000  # velocity RMS for 0.1 g
    assert rows[1]["velocity_rms_mm_s"] == pytest.approx(3 * one, rel=0.02)
    assert rows[4]["velocity_rms_mm_s"] == pytest.approx(4 * one, rel=0.02)
    assert len(rows[1]["velocity_rms_channels_mm_s"].split("|")) == 2


def _synthetic(test, failing, hours=60, step=1.0):
    rows = []
    n = int(hours / step)
    for bearing in (1, 2):
        for i in range(n):
            h = i * step
            grow = 1.0 + (np.exp((h - hours + 15) / 3.0) if bearing == failing else 0.0)
            v = 0.5 * min(grow, 40.0)
            rows.append({"test": test, "bearing": bearing, "file": f"{test}-{i}", "hours": h, "hours_to_end": hours - step - h,
                         "velocity_rms_mm_s": v, "accel_rms_m_s2": v * 2, "accel_peak_m_s2": v * 6, "crest_factor": 3.0,
                         "kurtosis": 3.0 * min(grow, 5.0), "dominant_freq_hz": 230.0, "disp_pp_um": v * 4,
                         "failed": bearing == failing, "failure_mode": "outer_race" if bearing == failing else None,
                         "zone": zone_of(v, ZONES)})
    return rows


def test_training_functions_on_synthetic_run_to_failure():
    df = add_features(pd.DataFrame(_synthetic(1, 1) + _synthetic(2, 2) + _synthetic(3, 1)))
    zp = {(z["test"], z["bearing"]): z for z in zone_progression(df)}
    assert zp[(1, 1)]["failed"] and zp[(1, 1)]["final_zone"] == "D" and zp[(1, 2)]["final_zone"] == "A"
    assert zp[(1, 1)]["first_reached_h"]["C"] is not None and zp[(1, 2)]["first_reached_h"]["C"] is None
    ew = early_warning(df, horizon_h=10)
    assert {f["held_out_test"] for f in ew["random_forest"]["folds"]} == {1, 2, 3}
    assert ew["rules"]["zone_c_or_worse"]["overall"]["false_alarm_rate_healthy_bearings"] == 0.0
    leads = [lt for lt in ew["rules"]["velocity_2x_baseline"]["lead_times"] if lt["failed"]]
    assert len(leads) == 3 and all(lt["first_alarm_hours_before_end"] is not None for lt in leads)
    assert ew["random_forest"]["overall"]["recall"] > 0.5
    assert rul_check(df)["failed_bearings"] == 3


@pytest.mark.skipif(not OUT.exists(), reason="run python -m ml.vibration.train_ims first")
def test_api_serves_the_real_ims_results(monkeypatch):
    monkeypatch.setenv("UDYAM_TICKER", "0")
    from api.app import app
    with TestClient(app) as c:
        r = c.get("/api/datasets/ims").json()
        assert len(r["zone_progression"]) == 12 and sum(z["failed"] for z in r["zone_progression"]) == 4
        assert r["dataset"]["zone_profile"] == "demo-iso20816-3-g2-rigid" and "trends" not in r
        t = c.get("/api/datasets/ims", params={"trends": "true"}).json()["trends"]
        assert set(t) >= {"T1-B3", "T1-B4", "T2-B1", "T3-B3"}
