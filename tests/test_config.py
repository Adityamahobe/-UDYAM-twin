"""config/machines.yaml and config/loader.py: validation, resolution, and that the code uses it."""

from __future__ import annotations

import copy

import pytest
import yaml
from fastapi.testclient import TestClient

from config import ConfigError, MachineConfig, get_config, load_config
from config.loader import CONFIG_PATH
from ml.rules import evaluate_ai4i_rules
from ml.vibration.severity import SeverityAssessor
from sim.factory import FactoryEngine
from twin.model import vibration_risk

RAW = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))


def mutated(fn) -> dict:
    raw = copy.deepcopy(RAW)
    fn(raw)
    return raw


def test_shipped_file_loads():
    cfg = load_config()
    assert set(cfg.templates) == {"cnc_mill", "cnc_lathe", "bar_saw", "air_compressor"}
    assert cfg.prefixes()["cnc_mill"] == "VMC"


def test_vmc01_has_requested_demo_zones_and_band():
    p = get_config().vibration_profile("VMC-01")
    assert p["zone_boundaries"] == {"A_B": 1.4, "B_C": 2.8, "C_D": 4.5}
    assert p["band_hz"] == [10, 1000]
    assert p["verified"] is False
    assert "not a certified assessment" in p["label"]
    assert get_config().stream_band_hz("VMC-01") == [10, 1000]


def test_every_configured_machine_has_a_severity_assignment():
    sev = get_config().severity_config()
    assert set(sev["machine_assignments"]) == set(get_config().machines)
    assert set(sev["template_assignments"]) == set(get_config().templates)


def test_machine_override_merges_over_template():
    raw = mutated(lambda r: r["machines"]["VMC-01"].update({"thresholds": {"process_temp_limit_k": 320.0}}))
    cfg = MachineConfig(raw)
    th = cfg.thresholds("VMC-01")
    assert th["process_temp_limit_k"] == 320.0
    assert th["tool_wear_limit_min"] == RAW["templates"]["cnc_mill"]["thresholds"]["tool_wear_limit_min"]
    assert cfg.thresholds("LATHE-01")["process_temp_limit_k"] == 315.5


def test_runtime_machine_resolves_from_template():
    assert get_config().thresholds("VMC-07", "cnc_mill") == get_config().thresholds("VMC-01")
    with pytest.raises(ConfigError):
        get_config().machine("VMC-07")


@pytest.mark.parametrize("breaker,msg", [
    (lambda r: r["vibration_profiles"]["demo-iso20816-3-g2-rigid"]["zone_boundaries"].update({"B_C": 1.0}), "strictly increasing"),
    (lambda r: r["vibration_profiles"]["demo-iso20816-3-g2-rigid"]["zone_boundaries"].pop("C_D"), "numeric A_B, B_C, C_D"),
    (lambda r: r["vibration_profiles"]["demo-iso20816-3-g2-rigid"].update({"band_hz": [1000, 10]}), "band_hz"),
    (lambda r: r["vibration_profiles"]["demo-iso20816-3-g2-rigid"].update({"quantity": "rpm"}), "quantity"),
    (lambda r: r["templates"]["cnc_mill"]["vibration"].update({"profile": "nope"}), "not defined"),
    (lambda r: r["templates"]["cnc_mill"]["vibration"].update({"foundation": "flexible"}), "requires mounting"),
    (lambda r: r["templates"]["cnc_mill"]["thresholds"].pop("process_temp_limit_k"), "process_temp_limit_k"),
    (lambda r: r["templates"]["cnc_mill"]["thresholds"].update({"tool_wear_warn_min": 300.0}), "must hold"),
    (lambda r: r["templates"]["air_compressor"]["thresholds"].update({"pressure_warn_bar": 5.0}), "must hold"),
    (lambda r: r["templates"]["cnc_mill"]["thresholds"].update({"cooling_loss_alert_pct": 150.0}), "between 0 and 100"),
    (lambda r: r["templates"]["bar_saw"]["nameplate"].update({"rated_kw": 0}), "rated_kw"),
    (lambda r: r["machines"]["VMC-01"].update({"template": "robot"}), "not a defined template"),
    (lambda r: r["site"].update({"grid_co2_kg_per_kwh": -1}), "grid_co2_kg_per_kwh"),
])
def test_invalid_config_is_rejected(breaker, msg):
    with pytest.raises(ConfigError, match=msg):
        MachineConfig(mutated(breaker))


def test_missing_and_malformed_files(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "none.yaml")
    bad = tmp_path / "bad.yaml"
    bad.write_text("site: [unclosed", encoding="utf-8")
    with pytest.raises(ConfigError, match="invalid YAML"):
        load_config(bad)


@pytest.mark.parametrize("v,risk", [(2.2, "LOW"), (2.8, "MEDIUM"), (4.49, "MEDIUM"), (4.5, "HIGH"), (6.7, "HIGH")])
def test_vibration_risk_follows_vmc_zones(v, risk):
    assert vibration_risk(v, get_config().risk_zones("VMC-01")) == risk


def test_rules_engine_uses_configured_vibration_zones():
    kw = dict(air_temp_k=300.0, process_temp_k=310.0, rpm=1500.0, torque_nm=40.0, tool_wear_min=50.0)
    assert evaluate_ai4i_rules(**kw, vibration_rms_mm_s=2.2).risk_level == "LOW"
    assert evaluate_ai4i_rules(**kw, vibration_rms_mm_s=3.0).risk_level == "MEDIUM"
    assert evaluate_ai4i_rules(**kw, vibration_rms_mm_s=4.6).risk_level == "HIGH"


def test_engine_machines_carry_config_limits():
    eng = FactoryEngine(seed=1, auto_demo=False)
    vmc = eng.plants["VMC-01"]
    assert vmc.limits == get_config().thresholds("VMC-01")
    assert vmc.plc.vib_trip_threshold == vmc.limits["vibration_trip_mm_s"]
    assert eng.twins["VMC-01"].vib_zones == (2.8, 4.5)
    eng.command({"cmd": "add_machine", "template": "cnc_mill", "name": "Second VMC"})
    assert eng.plants["VMC-02"].limits == vmc.limits


def _zone(assessor, obs):
    return assessor.assess(obs["machine_id"], {"velocity_rms_mm_s": obs["vibration_rms_mm_s"]},
                           measurement_band_hz=[10, 1000], operating_state=obs["plc_state"],
                           template=obs["template"]).zone


@pytest.mark.parametrize("seed", [1, 42, 7])
def test_healthy_baseline_is_zone_a_on_every_tick(seed):
    """2 minutes healthy: every machine stays in zone A on every tick and shows no vibration risk."""
    a = SeverityAssessor()
    eng = FactoryEngine(seed=seed, auto_demo=False)
    for _ in range(240):
        for m in eng.step():
            if m["type"] == "observed" and m["vibration_rms_mm_s"] is not None:  # the panel has no sensor
                assert _zone(a, m) == "A", (m["machine_id"], m["vibration_rms_mm_s"])
            elif m["type"] == "twin":
                assert "vibration_rms_mm_s" not in m["risk"]["top_signals"], m["machine_id"]


def test_bearing_fault_climbs_gradually_to_zone_d_and_high_risk():
    a = SeverityAssessor()
    eng = FactoryEngine(seed=42, auto_demo=False)
    for _ in range(10):
        eng.step()
    eng.command({"cmd": "inject_fault", "machine_id": "VMC-01", "fault": "bearing_defect"})
    zones, risks = [], []
    for _ in range(60):  # 30 s
        for m in eng.step():
            if m.get("machine_id") != "VMC-01":
                continue
            if m["type"] == "observed" and m["plc_state"] != "TRIP":
                zones.append(_zone(a, m))
            elif m["type"] == "twin":
                risks.append(m["risk"]["rules"])
    order = "ABCD"
    first = {z: zones.index(z) for z in order if z in zones}
    assert list(first) == list(order) and first["A"] < first["B"] < first["C"] < first["D"]  # gradual, in order
    assert first["D"] <= 30  # zone D within 15 s
    assert set(zones[-20:]) == {"D"} and set(risks[-20:]) == {"HIGH"}


def test_api_config_endpoint():
    from api.app import app
    with TestClient(app) as c:
        data = c.get("/api/config").json()
    assert data["site"]["grid_co2_kg_per_kwh"] == get_config().site["grid_co2_kg_per_kwh"]
    assert data["machines"]["VMC-01"]["vibration"]["profile"] == "demo-iso20816-3-g2-rigid"
    assert data["vibration_profiles"]["demo-iso20816-3-g2-rigid"]["zone_boundaries"]["C_D"] == 4.5


def test_iso_20816_profiles_and_legacy_ids():
    from config import get_config
    cfg = get_config()
    rigid = cfg.profiles["demo-iso20816-3-g2-rigid"]
    assert rigid["standard"].startswith("ISO 20816-3") and rigid["zone_boundaries"] == {"A_B": 1.4, "B_C": 2.8, "C_D": 4.5}
    # specs stored before the relabel still resolve to the same boundaries
    assert cfg.profiles["demo-iso10816-3-g2-rigid"]["zone_boundaries"] == rigid["zone_boundaries"]
    assert cfg.vibration_profile("VMC-01", "cnc_mill")["profile_id"] == "demo-iso20816-3-g2-rigid"
