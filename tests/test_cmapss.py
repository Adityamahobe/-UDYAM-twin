"""NASA C-MAPSS RUL module: scoring, target, per-condition normalisation, causal features, pipeline, API."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from ml.cmapss import COLUMNS, OUT, RUL_CAP, Normaliser, add_target, features, nasa_score, rmse, run_subset


def test_phm08_score_penalises_late_predictions_more():
    assert nasa_score([10], [10]) == 0.0
    late, early = nasa_score([20], [10]), nasa_score([0], [10])
    assert late == pytest.approx(np.e - 1) and early == pytest.approx(np.exp(10 / 13) - 1) and late > early
    assert rmse([1, 3], [1, 1]) == pytest.approx(np.sqrt(2))


def _engine(unit, cycles, cond_values=((0.0, 0.0, 100.0),), rng=None, end_offset=0):
    rng = rng or np.random.default_rng(unit)
    rows = []
    for c in range(1, cycles + 1):
        op = cond_values[c % len(cond_values)]
        wear = max(0.0, (c - (cycles + end_offset) * 0.5) / (cycles + end_offset))  # degradation in the second half
        sens = [500 + 10 * op[0] + 5 * wear * (i % 3 - 1) + rng.normal(0, 0.05) for i in range(21)]
        sens[0] = 518.67  # a constant sensor, as in the real data
        rows.append([unit, c, *op, *sens])
    return rows


def test_target_cap_condition_grouping_and_causal_features():
    df = pd.DataFrame(_engine(1, 200) + _engine(2, 150), columns=COLUMNS)
    t = add_target(df)
    assert t["rul"].max() == RUL_CAP and t.loc[t.unit == 2, "rul"].iloc[-1] == 0
    neg = pd.DataFrame({"op1": [-0.0007, 0.0004], "op2": [-0.0004, 0.0003], "op3": [100.0, 100.0]})
    assert Normaliser.condition(neg).nunique() == 1  # -0.0 and 0.0 are the same condition
    norm = Normaliser().fit(t)
    assert "s1" not in norm.sensors  # constant sensor dropped
    z = norm.transform(df)
    full = features(z, norm.sensors)
    part = features(z[z.cycle <= 50], norm.sensors)  # features at cycle 50 must not see later cycles
    a = full[(full.unit == 1) & (full.cycle == 50)].drop(columns=["unit"]).to_numpy()
    b = part[(part.unit == 1) & (part.cycle == 50)].drop(columns=["unit"]).to_numpy()
    assert np.allclose(a, b)


def test_pipeline_runs_and_beats_the_mean_baseline(tmp_path):
    rng = np.random.default_rng(0)
    conds = ((0.0, 0.0, 100.0), (20.0, 0.7, 100.0))
    train = [r for u in range(1, 31) for r in _engine(u, int(rng.integers(150, 260)), conds, rng)]
    test_rows, ruls = [], []
    for u in range(1, 21):
        life = int(rng.integers(150, 260))
        cut = int(rng.integers(60, life - 5))
        test_rows += _engine(u, cut, conds, rng, end_offset=life - cut)
        ruls.append(life - cut)
    for name, rows in (("train", train), ("test", test_rows)):
        pd.DataFrame(rows).to_csv(tmp_path / f"{name}_FD001.txt", sep=" ", header=False, index=False)
    pd.Series(ruls).to_csv(tmp_path / "RUL_FD001.txt", header=False, index=False)
    r = run_subset("FD001", tmp_path)
    assert r["train_engines"] == 30 and r["test_engines"] == 20 and r["operating_conditions"] == 2
    assert r["test"]["gradient_boosting"]["rmse_true_capped_125"] < r["test"]["predict_training_mean"]["rmse_true_capped_125"]
    assert len(r["predictions"]["true_rul"]) == 20


@pytest.mark.skipif(not OUT.exists(), reason="run python -m ml.cmapss first")
def test_api_serves_the_real_results(monkeypatch):
    monkeypatch.setenv("UDYAM_TICKER", "0")
    from api.app import app
    with TestClient(app) as c:
        r = c.get("/api/datasets/cmapss").json()
        assert [s["subset"] for s in r["subsets"]] == ["FD001", "FD002", "FD003", "FD004"]
        assert "predictions" not in r["subsets"][0] and "CNC" in r["label"]
        fd1 = r["subsets"][0]
        assert fd1["test_engines"] == 100 and fd1["test"]["gradient_boosting"]["rmse"] < fd1["test"]["predict_training_mean"]["rmse"]
        p = c.get("/api/datasets/cmapss", params={"predictions": "true"}).json()["subsets"][0]["predictions"]
        assert len(p["true_rul"]) == len(p["predicted_rul"]) == 100
