"""Contract tests: everything the fake stream emits must validate against docs/schema.json.

The real core (sim/ + twin/ + api/) must pass the same schema checks before it replaces the fake.
"""

import json
from pathlib import Path

import jsonschema
import pytest

from tools.fake_factory import FakeFactory

SCHEMA = json.loads((Path(__file__).resolve().parent.parent / "docs" / "schema.json").read_text(encoding="utf-8"))
VALIDATOR = jsonschema.Draft202012Validator(SCHEMA)


def run(factory: FakeFactory, ticks: int) -> list[dict]:
    out = []
    for _ in range(ticks):
        out += factory.step()
    return out


def assert_valid(messages: list[dict]) -> None:
    for msg in messages:
        errors = list(VALIDATOR.iter_errors(msg))
        assert not errors, f"{msg.get('type')} invalid: {errors[0].message}\n{msg}"


def test_full_demo_loop_validates():
    f = FakeFactory()
    assert_valid([f.hello(), f.registry()])
    msgs = run(f, 600)  # 300 s: two full auto-demo loops
    assert_valid(msgs)
    kinds = {m["kind"] for m in msgs if m["type"] == "event"}
    assert {"FAULT_INJECTED", "DRIFT", "CALIBRATION", "ALERT", "COMMON_CAUSE", "ANOMALY", "RESET"} <= kinds


@pytest.mark.parametrize("fault,machine", [
    ("coolant_degradation", "VMC-01"), ("bearing_defect", "VMC-01"), ("overload", "LATHE-01"),
    ("tool_wear_accel", "VMC-01"), ("sensor_stuck", "VMC-01"), ("sensor_dropout", "VMC-01"),
    ("pressure_drop", "COMP-01"), ("blade_wear", "SAW-01"),
])
def test_every_fault_validates(fault, machine):
    f = FakeFactory(auto_demo=False)
    assert_valid(f.command({"cmd": "inject_fault", "machine_id": machine, "fault": fault}))
    assert_valid(run(f, 200))


def test_onboarding_adds_machine():
    f = FakeFactory(machines=3, auto_demo=False)
    assert "COMP-01" not in {m["machine_id"] for m in f.registry()["machines"]}
    msgs = f.command({"cmd": "add_machine", "template": "air_compressor", "name": "Air Compressor"})
    assert_valid(msgs)
    assert any(m["type"] == "event" and m["kind"] == "MACHINE_ADDED" for m in msgs)
    assert "COMP-01" in {m["machine_id"] for m in f.registry()["machines"]}
    assert_valid(run(f, 20))


def test_deterministic_for_seed():
    a, b = run(FakeFactory(seed=7), 300), run(FakeFactory(seed=7), 300)
    assert a == b


def test_calibration_converges_without_overshoot():
    f = FakeFactory(auto_demo=False)
    f.command({"cmd": "inject_fault", "machine_id": "VMC-01", "fault": "coolant_degradation"})
    run(f, 400)
    mill = f.machines["VMC-01"]
    assert abs(mill.h_est - mill.h_true) < 0.06
    assert mill.h_est >= mill.h_true - 0.02, "calibrator overshot below the true value"


def test_calibration_freezes_on_anomaly_and_bad_sensor():
    for fault in ("bearing_defect", "sensor_stuck"):
        f = FakeFactory(auto_demo=False)
        f.command({"cmd": "inject_fault", "machine_id": "VMC-01", "fault": "coolant_degradation"})
        f.command({"cmd": "inject_fault", "machine_id": "VMC-01", "fault": fault})
        msgs = run(f, 300)
        twins = [m for m in msgs if m["type"] == "twin" and m["machine_id"] == "VMC-01"]
        assert twins[-1]["params"]["frozen"], fault
        assert not any(m["type"] == "event" and m["kind"] == "CALIBRATION" for m in msgs), fault


def test_websocket_server_handshake_and_command():
    """Guards the FastAPI wiring (a bad type annotation once made every handshake fail with 403)."""
    from fastapi.testclient import TestClient

    from tools.fake_stream import build_app

    with TestClient(build_app(seed=42, machines=3, auto_demo=False)) as client:
        with client.websocket_connect("/ws/telemetry") as ws:
            assert ws.receive_json()["type"] == "hello"
            assert ws.receive_json()["type"] == "registry"
            ws.send_json({"cmd": "add_machine", "template": "air_compressor", "name": "Air Compressor"})
            seen = set()
            for _ in range(80):
                msg = ws.receive_json()
                assert_valid([msg])
                seen.add(msg.get("kind") or msg["type"])
                if "MACHINE_ADDED" in seen and "observed" in seen:
                    break
            assert {"MACHINE_ADDED", "observed"} <= seen
