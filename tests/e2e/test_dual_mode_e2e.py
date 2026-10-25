"""End-to-end tests for the dual-mode system (the brief's TEST 1–9), against real processes:

- the app: `uvicorn api.app:app` in a subprocess with its normal 500 ms ticker,
- a local MQTT broker: tools/mqtt_test_broker.py (amqtt, in the dev-only .venv-mqtt) with a password,
- clients: HTTP (httpx), the dashboard WebSocket (websockets), and MQTT (paho) for the "device".

Every run uses a temporary database, synthetic-data folder and model registry, a random broker password and a
random operator token, so nothing real is touched. Run with:  pytest -m e2e -s tests/e2e
Skipped (not faked) when the broker venv is missing.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import secrets
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import paho.mqtt.client as mqtt
import pytest
from websockets.sync.client import connect as ws_connect

ROOT = Path(__file__).resolve().parents[2]
BROKER_PY = ROOT / ".venv-mqtt" / "bin" / "python"
pytestmark = [pytest.mark.e2e,
              pytest.mark.skipif(not BROKER_PY.exists(), reason="dev MQTT broker venv (.venv-mqtt) not installed")]

DEVICE = "E2E-ESP32"
DEVICE_MACHINE = "LATHE-01"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait(fn, timeout=30.0, every=0.25, what="condition"):
    t0 = time.time()
    last = None
    while time.time() - t0 < timeout:
        try:
            last = fn()
            if last:
                return last
        except Exception as err:  # noqa: BLE001 - keep polling until timeout
            last = err
        time.sleep(every)
    raise AssertionError(f"timed out waiting for {what}; last: {last!r}")


def _range() -> dict:
    """Export range around now (the export API allows at most 31 days)."""
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    f = lambda d: d.isoformat(timespec="seconds").replace("+00:00", "Z")
    return {"start": f(now - timedelta(hours=2)), "end": f(now + timedelta(minutes=5))}


def _report(test: str, **facts) -> None:
    print(f"\n[E2E {test}] " + json.dumps(facts, default=str))


class Stack:
    def __init__(self, tmp: Path):
        self.tmp = tmp
        self.mqtt_port, self.http_port = _free_port(), _free_port()
        self.mqtt_user, self.mqtt_pass = "udyam-e2e", secrets.token_urlsafe(16)
        self.operator = secrets.token_urlsafe(12)
        self.broker: subprocess.Popen | None = None
        self.app: subprocess.Popen | None = None
        self.http = httpx.Client(base_url=f"http://127.0.0.1:{self.http_port}", timeout=30)

    def start_broker(self) -> None:
        env = {**os.environ, "UDYAM_MQTT_TEST_PASS": self.mqtt_pass}
        self.broker = subprocess.Popen([str(BROKER_PY), str(ROOT / "tools" / "mqtt_test_broker.py"), "--port",
                                        str(self.mqtt_port), "--user", self.mqtt_user], env=env, cwd=ROOT,
                                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        line = self.broker.stdout.readline()
        assert line.startswith("READY"), line

    def stop_broker(self) -> None:
        if self.broker and self.broker.poll() is None:
            self.broker.send_signal(signal.SIGTERM)
            self.broker.wait(timeout=10)

    def start_app(self) -> None:
        env = {k: v for k, v in os.environ.items() if not k.startswith(("UDYAM_", "GEMINI_"))}
        env.update({
            "UDYAM_DB_PATH": str(self.tmp / "e2e.sqlite"), "UDYAM_SYNTHETIC_DIR": str(self.tmp / "synthetic"),
            "UDYAM_REGISTRY_DIR": str(self.tmp / "registry"), "UDYAM_AUTO_DEMO": "0",
            "UDYAM_OPERATOR_TOKEN": self.operator, "UDYAM_MQTT_HOST": "127.0.0.1", "UDYAM_MQTT_PORT": str(self.mqtt_port),
            "UDYAM_MQTT_USER": self.mqtt_user, "UDYAM_MQTT_PASS": self.mqtt_pass,
        })
        self.log = open(self.tmp / "app.log", "w")
        self.app = subprocess.Popen([sys.executable, "-m", "uvicorn", "api.app:app", "--host", "127.0.0.1", "--port",
                                     str(self.http_port)], env=env, cwd=ROOT, stdout=self.log, stderr=subprocess.STDOUT)
        _wait(lambda: self.http.get("/api/operating-mode").status_code == 200, 60, what="app start")

    def stop(self) -> None:
        if self.app and self.app.poll() is None:
            self.app.send_signal(signal.SIGTERM)
            self.app.wait(timeout=15)
        self.stop_broker()
        self.http.close()

    # helpers ---------------------------------------------------------------
    def get(self, path, **kw):
        r = self.http.get(path, **kw)
        r.raise_for_status()
        return r.json()

    def post(self, path, json=None, **kw):
        return self.http.post(path, json=json, **kw)

    def conn(self):
        return self.get("/api/operating-mode")["connection"]

    def snapshot(self, mid):
        snaps = self.get("/api/snapshots")
        return snaps[mid] if isinstance(snaps, dict) else next(s for s in snaps if s["machine_id"] == mid)

    def frames(self, seconds: float, want=("observed",)) -> list[dict]:
        out = []
        with ws_connect(f"ws://127.0.0.1:{self.http_port}/ws/telemetry", open_timeout=10) as ws:
            t0 = time.time()
            while time.time() - t0 < seconds:
                try:
                    m = json.loads(ws.recv(timeout=1))
                except TimeoutError:
                    continue
                if m.get("type") in want:
                    out.append(m)
        return out

    def mqtt_client(self, cid: str) -> mqtt.Client:
        c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=cid)
        c.username_pw_set(self.mqtt_user, self.mqtt_pass)
        c.connect("127.0.0.1", self.mqtt_port)
        c.loop_start()
        return c


@pytest.fixture(scope="module")
def stack(tmp_path_factory):
    s = Stack(tmp_path_factory.mktemp("e2e"))
    s.start_broker()
    s.start_app()
    try:
        _wait(lambda: s.conn()["broker"]["state"] == "CONNECTED", 30, what="app connected to the MQTT broker")
        yield s
    finally:
        s.stop()


def test_1_testing_mode_parameters_reach_state_telemetry_and_3d(stack):
    s = stack
    assert s.get("/api/operating-mode")["mode"] == "TESTING"
    r = s.post("/api/sim/params", json={"machine_id": "VMC-01", "params": {"spindle_speed_pct": 80, "ambient_temp_c": 35}})
    assert r.status_code == 200, r.text
    vals = next(m for m in r.json()["machines"] if m["machine_id"] == "VMC-01")["values"]
    assert vals["spindle_speed_pct"] == 80 and vals["ambient_temp_c"] == 35
    time.sleep(3)
    obs = [f for f in s.frames(4) if f["machine_id"] == "VMC-01"]  # the frames the cards and the 3D view render
    assert obs, "no observed frames on the dashboard WebSocket"
    rpm = sum(o["rpm"] for o in obs) / len(obs)
    air_c = sum(o["air_temp_k"] for o in obs) / len(obs) - 273.15
    # setpoint = nominal × 80 % × (1 − any derate the twin applied; at 35 °C it may derate for over-temperature)
    for o in obs:
        assert o["rpm_setpoint"] == pytest.approx(1540 * 0.8 * (1 - o["derate_pct"] / 100), abs=1)
        assert abs(o["rpm"] - o["rpm_setpoint"]) < 40
    assert 34.0 < air_c < 36.5
    snap = s.snapshot("VMC-01")
    assert snap["mode"] == "SIM" and abs(snap["rpm"] - obs[-1]["rpm_setpoint"]) < 40
    _wait(lambda: any(e["kind"] == "SIM_PARAMS" for e in s.get("/api/events", params={"limit": 50})), 5, what="SIM_PARAMS event")
    _report("TEST 1", ws_frames=len(obs), mean_rpm=round(rpm, 1), setpoint=obs[-1]["rpm_setpoint"], derate_pct=obs[-1]["derate_pct"],
            air_temp_c=round(air_c, 2), snapshot_rpm=snap["rpm"])
    assert s.post("/api/sim/params/reset").status_code == 200


def test_2_injected_fault_produces_expected_telemetry_and_scenario_labels(stack):
    s = stack
    assert s.post("/api/scenarios/over_temperature/start").status_code == 200
    snap = _wait(lambda: (lambda x: x if x.get("active_scenario") == "over_temperature" else None)(s.snapshot("VMC-01")), 10,
                 what="scenario label on the snapshot")
    obs = [f for f in s.frames(3) if f["machine_id"] == "VMC-01"]
    assert obs and all(o["source_mode"] == "SIM" for o in obs) and {o["active_fault"] for o in obs} - {None}
    ev = s.get("/api/events", params={"limit": 50})
    inj = next(e for e in ev if e["kind"] == "FAULT_INJECTED")
    assert inj["data"]["scenario"] == "over_temperature" and inj["data"]["faults"]
    # the generator labels windows with the injected fault (ground truth), never inferred
    cfg = {"episodes": 2, "minutes": 3, "faults": ["coolant_degradation"], "seed": 5}
    assert s.post("/api/synthetic/generate", json=cfg).status_code == 202
    job = _wait(lambda: (lambda j: j if j["state"] != "running" else None)(s.get("/api/synthetic/job")), 60, what="generation")
    rows = list(csv.DictReader(io.StringIO(s.http.get(f"/api/synthetic/datasets/{job['dataset_id']}/download",
                                                      params={"part": "windows"}).text)))
    labels = {r["label_fault"] for r in rows}
    assert labels <= {"normal", "coolant_degradation"} and "coolant_degradation" in labels
    assert all(r["data_label"] == "SYNTHETIC DIGITAL TWIN DATA" for r in rows)
    _report("TEST 2", active_scenario=snap["active_scenario"], active_faults=sorted({o["active_fault"] for o in obs} - {None}),
            event=inj["title"][:80], synthetic_labels=sorted(labels), windows=len(rows))
    assert s.post("/api/scenarios/resolve").status_code == 200


def test_3_dataset_generate_export_reload_and_validate(stack):
    s = stack
    cfg = {"episodes": 15, "minutes": 6, "seed": 11, "name": "e2e hot shop",
           "param_ranges": {"ambient_temp_c": [24, 34], "cooling_capacity_pct": [60, 100]}}
    assert s.post("/api/synthetic/generate", json=cfg).status_code == 202
    job = _wait(lambda: (lambda j: j if j["state"] != "running" else None)(s.get("/api/synthetic/job")), 120, what="generation")
    assert job["state"] == "done", job
    ds = job["dataset_id"]
    url = f"/api/synthetic/datasets/{ds}/download"
    meta = s.get(url, params={"part": "metadata", "format": "json"})
    raw = s.http.get(url, params={"part": "windows", "format": "csv"})
    assert f"SYNTHETIC_{ds}_windows.csv" in raw.headers["content-disposition"]
    exported = s.tmp / f"{ds}_windows.csv"
    exported.write_bytes(raw.content)
    assert hashlib.sha256(exported.read_bytes()).hexdigest() == meta["files"]["windows.csv"]["sha256"]
    with open(exported, newline="") as fh:  # reload from disk
        reader = csv.DictReader(fh)
        header, rows = reader.fieldnames, list(reader)
    assert header == meta["columns"]["windows"] and len(rows) == meta["counts"]["windows"]
    assert all(r["data_label"] == "SYNTHETIC DIGITAL TWIN DATA" for r in rows)
    faults = set(s.get("/api/synthetic/options")["faults"])
    assert {r["label_fault"] for r in rows} <= faults
    for r in rows:
        float(r["temp_mean"]), float(r["minutes_to_limit"]), int(r["limit_within_10min"])
    js = s.get(url, params={"part": "episodes", "format": "json"})
    assert js["data_label"] == "SYNTHETIC DIGITAL TWIN DATA" and len(js["rows"]) == 15
    assert meta["synthetic"] is True and meta["real_machine_data"] is False
    _report("TEST 3", dataset=ds, windows=len(rows), columns=len(header), labels=dict(meta["counts"]["windows_by_label"]),
            sha256_ok=True, episodes_over_limit_from_settings=meta["counts"]["episodes_over_limit_from_settings"])
    stack.dataset_id = ds


def test_4_candidate_trained_evaluated_on_held_out_data_and_compared(stack):
    s = stack
    ds = getattr(stack, "dataset_id", None)
    assert ds, "TEST 3 must run first"
    before = s.get("/api/models/ai4i")
    assert before["production"] == "v001"
    assert s.post("/api/models/ai4i/train", json={"datasets": [ds]}).status_code == 202
    job = _wait(lambda: (lambda j: j if j["state"] != "running" else None)(s.get("/api/models/ai4i")["training"]), 180,
                what="candidate training")
    assert job["state"] == "done", job
    reg = s.get("/api/models/ai4i")
    card = next(v for v in reg["versions"] if v["version"] == job["version"])
    cmp = card["comparison"]
    assert reg["production"] == "v001"  # trained, not deployed
    assert cmp["production"]["version"] == "v001"
    assert cmp["production"]["ai4i_test"]["support_positive"] == cmp["candidate"]["ai4i_test"]["support_positive"] == 68
    assert cmp["candidate"]["synthetic_test"]["support_positive"] == cmp["production"]["synthetic_test"]["support_positive"]
    assert len(card["gate"]["checks"]) == 6 and card["real_machine_data"] is False
    pick = lambda m: {k: m.get(k) for k in ("f1", "recall", "precision", "roc_auc", "false_positive_rate", "support_positive")}
    _report("TEST 4", version=card["version"], status=card["status"], gate_passed=card["gate"]["passed"],
            failed_checks=[c["check"] for c in card["gate"]["checks"] if not c["passed"]],
            ai4i_v001=pick(cmp["production"]["ai4i_test"]), ai4i_candidate=pick(cmp["candidate"]["ai4i_test"]),
            synthetic_v001=pick(cmp["production"]["synthetic_test"]), synthetic_candidate=pick(cmp["candidate"]["synthetic_test"]))
    if card["gate"]["passed"]:  # promotion needs the operator; the gate already passed
        assert s.post(f"/api/models/ai4i/{card['version']}/promote").status_code == 401


def test_5_mqtt_telemetry_is_received_and_displayed(stack):
    s = stack
    r = s.post("/api/iot/devices", json={"device_id": DEVICE, "machine_id": DEVICE_MACHINE})
    assert r.status_code == 201, r.text
    token = r.json()["token"]
    assert s.post("/api/operating-mode", json={"mode": "REAL_DATA"}).json()["mode"] == "REAL_DATA"
    dev = s.mqtt_client("e2e-device")
    stack.device = (dev, token)
    payload = {"token": token, "temp_c": 41.25, "rpm": 1650.0, "vibration_rms_mm_s": 1.9, "current_a": 6.2}
    for _ in range(6):
        dev.publish(f"udyam/{DEVICE}/telemetry", json.dumps(payload), qos=1).wait_for_publish(5)
        time.sleep(0.5)
    c = _wait(lambda: (lambda x: x if x["verified_live"] else None)(s.conn()), 10, what="verified live connection")
    assert c["state"] == "CONNECTED" and c["live_machines"] == [DEVICE_MACHINE] and c["broker"]["received"] >= 6
    snap = s.snapshot(DEVICE_MACHINE)
    obs = [f for f in s.frames(2) if f["machine_id"] == DEVICE_MACHINE]
    assert snap["mode"] == "LIVE" and obs and obs[-1]["source_mode"] == "LIVE"
    assert obs[-1]["rpm"] == 1650.0 and obs[-1]["process_temp_k"] == pytest.approx(41.25 + 273.15, abs=0.01)
    assert {"rpm", "process_temp_k"} <= set(obs[-1]["live_fields"]) and "torque_nm" not in obs[-1]["live_fields"]
    time.sleep(2.5)  # telemetry is recorded every 2 s
    real = s.http.get("/api/reports/export.csv", params={"section": "real_telemetry", **_range()}).text.strip().splitlines()
    assert len(real) > 1 and all(f",{DEVICE_MACHINE},LIVE," in row for row in real[1:])
    _report("TEST 5", broker=c["broker"]["address"], broker_state=c["broker"]["state"], received=c["broker"]["received"],
            live_machines=c["live_machines"], snapshot_mode=snap["mode"], rpm=obs[-1]["rpm"],
            temp_c=round(obs[-1]["process_temp_k"] - 273.15, 2), live_fields=obs[-1]["live_fields"], real_rows=len(real) - 1)


def test_6_broker_down_is_reported_as_disconnected_or_stale(stack):
    s = stack
    stack.stop_broker()
    c = _wait(lambda: (lambda x: x if x["broker"]["state"] in ("DISCONNECTED", "ERROR") else None)(s.conn()), 20,
              what="broker disconnect noticed")
    stale = _wait(lambda: (lambda x: x if x["state"] == "STALE_DATA" else None)(s.conn()), 20, what="stale data state")
    assert not stale["verified_live"] and stale["live_machines"] == [] and "not live" in stale["message"]
    obs = [f for f in s.frames(2) if f["machine_id"] == DEVICE_MACHINE]
    assert obs and obs[-1]["dq_flag"] in ("STALE", "MISSING")  # last values are flagged, not presented as fresh
    _report("TEST 6", broker_state=c["broker"]["state"], broker_error=c["broker"]["last_error"], state=stale["state"],
            message=stale["message"], dq_flag=obs[-1]["dq_flag"])
    # the broker comes back: the app reconnects by itself
    stack.start_broker()
    back = _wait(lambda: (lambda x: x if x["broker"]["state"] == "CONNECTED" else None)(s.conn()), 40, what="reconnect")
    dev, token = stack.device
    dev.publish(f"udyam/{DEVICE}/telemetry", json.dumps({"token": token, "temp_c": 40.0, "rpm": 1600.0}), qos=1)
    live = _wait(lambda: (lambda x: x if x["verified_live"] else None)(s.conn()), 15, what="live again")
    _report("TEST 6 (recovery)", broker_state=back["broker"]["state"], state=live["state"], live_machines=live["live_machines"])


def test_7_synthetic_data_cannot_be_confused_with_real_telemetry(stack):
    s = stack
    dev, token = stack.device
    for _ in range(4):
        dev.publish(f"udyam/{DEVICE}/telemetry", json.dumps({"token": token, "temp_c": 40.5, "rpm": 1610.0}), qos=1)
        time.sleep(0.5)
    om = s.get("/api/operating-mode")
    assert om["mode"] == "REAL_DATA" and om["read_only"]
    obs = s.frames(2)
    by_mid = {}
    for o in obs:
        by_mid[o["machine_id"]] = o["source_mode"]
    assert by_mid[DEVICE_MACHINE] == "LIVE" and all(v == "SIM" for k, v in by_mid.items() if k != DEVICE_MACHINE)
    rng = _range()
    real = s.http.get("/api/reports/export.csv", params={"section": "real_telemetry", **rng}).text.strip().splitlines()[1:]
    sim = s.http.get("/api/reports/export.csv", params={"section": "telemetry", **rng}).text.strip().splitlines()[1:]
    assert real and all(",LIVE," in r for r in real) and sim and not any(",LIVE," in r for r in sim)
    # a simulated stand-in device is never counted as a real one
    sim_dev = s.post("/api/iot/devices", json={"device_id": "E2E-STANDIN", "machine_id": "SAW-01", "simulated": True}).json()
    s.post("/api/iot/telemetry", json={"rpm": 900.0}, headers={"X-Device-Id": "E2E-STANDIN", "X-Device-Token": sim_dev["token"]})
    c = s.conn()
    assert "SAW-01" not in c["live_machines"] and c["devices"]["simulated_stand_ins"] >= 1
    # synthetic datasets carry their label everywhere; the registry says no version learned from real machines
    assert all(d["data_label"] == "SYNTHETIC DIGITAL TWIN DATA" for d in s.get("/api/synthetic/datasets")["datasets"])
    assert all(not v.get("real_machine_data") for v in s.get("/api/models/ai4i")["versions"])
    # back to TESTING: the simulation shows again, real rows stay in their own table
    assert s.post("/api/operating-mode", json={"mode": "TESTING"}).json()["mode"] == "TESTING"
    _report("TEST 7", sources=by_mid, real_rows=len(real), sim_rows=len(sim), simulated_stand_ins=c["devices"]["simulated_stand_ins"],
            live_machines=c["live_machines"])


def test_8_testing_controls_never_send_anything_to_equipment(stack):
    s = stack
    seen: list[str] = []
    spy = s.mqtt_client("e2e-spy")
    spy.on_message = lambda cl, u, m: seen.append(m.topic)
    spy.subscribe("#", qos=1)
    time.sleep(1)
    actions = [
        ("/api/sim/params", {"machine_id": DEVICE_MACHINE, "params": {"spindle_speed_pct": 110, "load_pct": 110}}),
        ("/api/sim/params", {"machine_id": "VMC-01", "params": {"cooling_capacity_pct": 60}}),
        ("/api/faults/inject", {"machine_id": DEVICE_MACHINE, "fault": "overload"}),
        ("/api/derate", {"machine_id": DEVICE_MACHINE, "pct": 20}),
        ("/api/sim/run", {"action": "pause"}), ("/api/sim/run", {"action": "resume"}),
    ]
    for path, body in actions:
        assert s.post(path, json=body).status_code == 200, path
    time.sleep(3)
    spy.loop_stop()
    app_topics = [t for t in seen if not t.startswith("$SYS")]
    assert app_topics == [], app_topics  # nothing was published by the app, to any topic
    # the device's machine keeps showing the device's own readings, not the simulated settings
    dev, token = stack.device
    dev.publish(f"udyam/{DEVICE}/telemetry", json.dumps({"token": token, "temp_c": 40.1, "rpm": 1600.0}), qos=1)
    time.sleep(1)
    o = [f for f in s.frames(1.5) if f["machine_id"] == DEVICE_MACHINE][-1]
    assert o["rpm"] == 1600.0
    # static check: no MQTT publish or downlink call anywhere in the app code
    offenders = [str(p.relative_to(ROOT)) for d in ("api", "hub", "sim", "twin", "ml") for p in (ROOT / d).rglob("*.py")
                 if ".publish(" in p.read_text(encoding="utf-8")]
    assert offenders == [], offenders
    # in REAL DATA mode the same controls are refused outright
    s.post("/api/operating-mode", json={"mode": "REAL_DATA"})
    refused = [s.post(path, json=body).status_code for path, body in actions[:4]]
    assert refused == [409] * 4
    s.post("/api/operating-mode", json={"mode": "TESTING"})
    s.post("/api/sim/params/reset")
    _report("TEST 8", mqtt_messages_from_app=len(app_topics), sys_topics_seen=len(seen) - len(app_topics),
            device_rpm_after_sim_change=o["rpm"], publish_calls_in_code=offenders, real_mode_status=refused)


def test_9_existing_features_still_work(stack):
    s = stack
    page = s.http.get("/")
    assert page.status_code == 200
    for anchor in ("machineGrid", "eventLog", "alertList", "exportPanel", "rlPanel", "globalModeBadge", "simControlsPanel",
                   "whatifPanel", "synthPanel", "modelPanel", "adaptivePanel"):
        assert f'id="{anchor}"' in page.text, anchor
    for js in ("app.js", "three_mill.js", "mode.js", "reports.js", "rl.js", "chat.js", "whatif.js"):
        assert s.http.get(f"/static/{js}").status_code == 200, js
    with ws_connect(f"ws://127.0.0.1:{s.http_port}/ws/telemetry", open_timeout=10) as ws:
        kinds = [json.loads(ws.recv(timeout=5))["type"] for _ in range(12)]
    assert kinds[:2] == ["hello", "registry"] and "snapshot" in kinds
    assert {sn["machine_id"] for sn in (lambda x: x.values() if isinstance(x, dict) else x)(s.get("/api/snapshots"))} >= {"VMC-01", "LATHE-01", "SAW-01", "COMP-01"}
    # alerts and work orders: raise an alert through a fault scenario, make a work order from it, move it along
    assert s.post("/api/scenarios/vibration_fault/start").status_code == 200
    alerts = _wait(lambda: s.get("/api/alerts"), 90, what="an alert from the fault scenario")
    a = alerts[0]
    wo = s.post(f"/api/alerts/{a['alert_id']}/work-order")
    assert wo.status_code in (200, 201), wo.text
    body = wo.json()
    wo_id = body.get("wo_id") or (body.get("work_order") or {}).get("wo_id")
    assert wo_id, body
    upd = s.http.patch(f"/api/work-orders/{wo_id}", json={"status": "Acknowledged", "notes": "E2E"})
    assert upd.status_code == 200 and upd.json()["status"] == "Acknowledged", upd.text
    ev = s.get("/api/events", params={"limit": 200})
    assert {"FAULT_INJECTED", "ALERT"} <= {e["kind"] for e in ev} or {"FAULT_INJECTED", "ANOMALY"} <= {e["kind"] for e in ev}
    rng = _range()
    xlsx = s.http.get("/api/reports/export.xlsx", params=rng)
    assert xlsx.status_code == 200 and xlsx.content[:2] == b"PK"
    for path in ("/api/analytics", "/api/metrics", "/api/rl", "/api/vibration", "/api/scenarios", "/api/work-orders",
                 "/api/reports/export.csv?section=alerts"):
        assert s.http.get(path).status_code == 200, path
    _report("TEST 9", ws_first_frames=kinds[:4], alerts=len(alerts), alert=a["title"][:70], work_order=wo_id,
            events=len(ev), xlsx_bytes=len(xlsx.content))
