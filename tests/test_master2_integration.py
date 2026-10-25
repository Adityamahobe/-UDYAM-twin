"""Unit and integration tests for Master Prompt 2 features:
- Multi-asset digital twin (VMC-01, LATHE-01, SAW-01, COMP-01)
- Common-cause utility propagation
- Interactive MSME economics and sensitivity formulas
- ML evaluation metrics & rule baseline integrity
"""

import json
from pathlib import Path
import pytest
from fastapi.testclient import TestClient

from api.app import app
from api.db import get_economics, update_economics
from sim.factory import FactoryEngine
from ml.ai4i import AI4IPredictor
from ml.rules import evaluate_ai4i_rules


@pytest.fixture
def client():
    return TestClient(app)


def test_four_machine_initialization():
    """FactoryEngine starts the 4 machines of the factory model."""
    engine = FactoryEngine(seed=42, auto_demo=False, machines=4)
    expected_ids = {"VMC-01", "LATHE-01", "SAW-01", "COMP-01"}
    assert set(engine.plants.keys()) == expected_ids
    assert set(engine.twins.keys()) == expected_ids

    # Verify templates
    assert engine.plants["VMC-01"].template == "cnc_mill"
    assert engine.plants["LATHE-01"].template == "cnc_lathe"
    assert engine.plants["SAW-01"].template == "bar_saw"
    assert engine.plants["COMP-01"].template == "air_compressor"


def test_common_cause_pressure_drop_propagation():
    """Verify that when COMP-01 suffers pressure drop, common-cause event is emitted and CNCs are affected."""
    engine = FactoryEngine(seed=42, auto_demo=False, machines=4)
    comp = engine.plants["COMP-01"]

    # Inject pressure drop on compressor
    engine.command({"cmd": "inject_fault", "machine_id": "COMP-01", "fault": "pressure_drop"})

    # Step simulation until pressure drops below 6.0 bar
    common_cause_emitted = False
    for _ in range(30):
        frames = engine.step()
        for f in frames:
            if f.get("type") == "event" and f.get("kind") == "COMMON_CAUSE":
                common_cause_emitted = True
                assert "COMP-01" in f.get("machine_id", "")
                assert "VMC-01" in f.get("data", {}).get("affected", [])
                assert "LATHE-01" in f.get("data", {}).get("affected", [])

    assert common_cause_emitted, "Expected COMMON_CAUSE event was not emitted when compressor pressure dropped"


def test_economics_calculation_and_update(client):
    """Test MSME economics API GET, POST, and formula calculations."""
    resp = client.get("/api/economics")
    assert resp.status_code == 200
    data = resp.json()
    assert "assumptions" in data
    assert "formulas" in data
    assert data["assumptions"]["downtime_cost_per_hr"] > 0

    # Test update
    update_payload = {
        "downtime_cost_per_hr": 3000.0,
        "electricity_tariff_per_kwh": 9.0,
        "inr_per_part": 500.0,
    }
    resp = client.post("/api/economics", json=update_payload)
    assert resp.status_code == 200
    assert resp.json()["status"] == "updated"

    # Verify updated values in GET
    resp2 = client.get("/api/economics")
    assumptions = resp2.json()["assumptions"]
    assert assumptions["downtime_cost_per_hr"] == 3000.0
    assert assumptions["electricity_tariff_per_kwh"] == 9.0
    assert assumptions["inr_per_part"] == 500.0

    # Reset back to nominal
    client.post("/api/economics", json={
        "downtime_cost_per_hr": 2500.0,
        "electricity_tariff_per_kwh": 8.5,
        "inr_per_part": 450.0,
    })


def test_ml_metrics_and_rule_baseline(client):
    """Metrics come from the real AI4I file and are reported per class, without inflated claims."""
    resp = client.get("/api/metrics")
    assert resp.status_code == 200
    data = resp.json()
    assert data["vibration_and_current_excluded_from_ml"] is True
    assert data["probability_calibrated"] is False
    assert data["dataset"]["rows"] == 10000 and data["dataset"]["failure_rate"] == 0.0339
    assert set(data["per_class"]) == {"machine_failure", "TWF", "HDF", "PWF", "OSF", "RNF"}
    for m in data["per_class"].values():
        assert {"precision", "recall", "f1", "roc_auc", "pr_auc", "support_positive"} <= set(m)
    rf = data["random_forest"]
    assert rf["f1"] == data["per_class"]["machine_failure"]["f1"]
    assert set(rf["feature_importances"]) == {"air_temp_k", "process_temp_k", "rpm", "torque_nm", "tool_wear_min"}
    assert "rule_baseline" in data and "honest_disclaimer" in data


def test_ml_predictor_inference():
    """Risk is a percentage; high torque + wear (AI4I overstrain/power region) scores above nominal."""
    predictor = AI4IPredictor()
    nominal = predictor.predict(air_temp_k=300.0, process_temp_k=310.0, rpm=1500.0, torque_nm=38.0, tool_wear_min=20.0)
    assert nominal["status"] == "OK" and 0.0 <= nominal["failure_risk_pct"] <= 100.0
    degraded = predictor.predict(air_temp_k=300.0, process_temp_k=311.0, rpm=1500.0, torque_nm=65.0, tool_wear_min=215.0)
    assert degraded["failure_risk_pct"] > nominal["failure_risk_pct"]
    assert degraded["top_features"][0]["feature"] in ("torque_nm", "tool_wear_min")
