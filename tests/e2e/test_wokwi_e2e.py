"""End-to-end: Wokwi virtual-device telemetry through a real MQTT broker into Real Data mode.

Real processes: the app (uvicorn), the dev broker (tools/mqtt_test_broker.py, amqtt), and a paho MQTT client that
publishes exactly the Wokwi sketch's messages (wokwi/udyam-virtual-machine/sample_message.json format, same topics,
same retained status and last will). This is a stand-in for the Wokwi simulator, not Wokwi itself.

    pytest -m e2e -s tests/e2e/test_wokwi_e2e.py
"""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import paho.mqtt.client as mqtt
import pytest

ROOT = Path(__file__).resolve().parents[2]
BROKER_PY = ROOT / ".venv-mqtt" / "bin" / "python"
SAMPLE = json.loads((ROOT / "wokwi" / "udyam-virtual-machine" / "sample_message.json").read_text())
PREFIX = "udyam-twin-e2e/virtual-machines"
pytestmark = [pytest.mark.e2e,
              pytest.mark.skipif(not BROKER_PY.exists(), reason="dev MQTT broker venv (.venv-mqtt) not installed")]


def _port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait(fn, timeout=30, what="condition"):
    t0, last = time.time(), None
    while time.time() - t0 < timeout:
        try:
            last = fn()
            if last:
                return last
        except Exception as err:  # noqa: BLE001
            last = err
        time.sleep(0.25)
    raise AssertionError(f"timed out waiting for {what}; last: {last!r}")


@pytest.fixture(scope="module")
def stack(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("wokwi-e2e")
    mport, hport = _port(), _port()
    broker = subprocess.Popen([str(BROKER_PY), str(ROOT / "tools" / "mqtt_test_broker.py"), "--port", str(mport)],
                              cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    assert broker.stdout.readline().startswith("READY")
    env = {k: v for k, v in os.environ.items() if not k.startswith(("UDYAM_", "GEMINI_"))}
    env.update({"UDYAM_DB_PATH": str(tmp / "e2e.sqlite"), "UDYAM_SYNTHETIC_DIR": str(tmp / "syn"),
                "UDYAM_REGISTRY_DIR": str(tmp / "reg"), "UDYAM_AUTO_DEMO": "0",
                "UDYAM_WOKWI_BROKER": "127.0.0.1", "UDYAM_WOKWI_PORT": str(mport), "UDYAM_WOKWI_TOPIC_PREFIX": PREFIX})
    log = open(tmp / "app.log", "w")
    app = subprocess.Popen([sys.executable, "-m", "uvicorn", "api.app:app", "--host", "127.0.0.1", "--port", str(hport)],
                           env=env, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT)
    http = httpx.Client(base_url=f"http://127.0.0.1:{hport}", timeout=20)
    try:
        _wait(lambda: http.get("/api/wokwi").json()["broker"]["state"] == "CONNECTED", 60, "app subscribed to the broker")
        yield http, mport
    finally:
        for proc in (app, broker):
            proc.send_signal(signal.SIGTERM)
            try:
                proc.wait(15)
            except subprocess.TimeoutExpired:
                proc.kill()
        http.close()


def _device(mport: int) -> mqtt.Client:
    """Connects like the sketch: last will 'offline' (retained) on the status topic, then 'online'."""
    c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="e2e-wokwi-standin")
    c.will_set(f"{PREFIX}/machine-001/status", "offline", qos=1, retain=True)
    c.connect("127.0.0.1", mport, keepalive=2)
    c.loop_start()
    c.publish(f"{PREFIX}/machine-001/status", "online", qos=1, retain=True).wait_for_publish(5)
    return c


def _send(c: mqtt.Client, seq: int, **over) -> None:
    p = {**SAMPLE, "seq": seq, "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), **over}
    c.publish(f"{PREFIX}/machine-001/telemetry", json.dumps(p), qos=1).wait_for_publish(5)


def test_wokwi_telemetry_reaches_real_data_mode_and_goes_stale(stack):
    http, mport = stack
    assert http.post("/api/operating-mode", json={"mode": "REAL_DATA"}).json()["mode"] == "REAL_DATA"
    dev = _device(mport)
    for i in range(1, 6):
        _send(dev, i, rpm=1500.0 + i, scenario="bearing_wear" if i > 3 else "normal",
              simulated_fault="bearing_wear" if i > 3 else None)
        time.sleep(0.5)
    w = _wait(lambda: (lambda x: x if x["online_machines"] == ["machine-001"] else None)(http.get("/api/wokwi").json()), 10,
              "virtual device online")
    m = w["machines"][0]
    assert m["state"] == "ONLINE" and m["source"] == "WOKWI_SIMULATOR" and m["last"]["seq"] == 5
    assert m["last"]["rpm"] == 1505.0 and m["last"]["scenario"] == "bearing_wear" and m["last"]["simulated_fault"] == "bearing_wear"
    assert m["device_status"] == "online" and m["monitoring"]["failure_risk"]["status"] == "NOT_APPLICABLE"
    om = http.get("/api/operating-mode").json()
    assert om["virtual"]["state"] == "CONNECTED" and om["connection"]["state"] == "NOT_CONFIGURED"  # real devices: none
    # an untrusted publisher on the same topic is rejected
    _send(dev, 6, source="REAL_MACHINE")
    _wait(lambda: http.get("/api/wokwi").json()["counters"]["rejected"] >= 1, 5, "rejection counted")
    # history and export
    now = datetime.now(timezone.utc)
    f = lambda d: d.isoformat(timespec="seconds").replace("+00:00", "Z")
    csv_text = http.get("/api/reports/export.csv", params={"section": "virtual_telemetry", "start": f(now - timedelta(minutes=5)),
                                                           "end": f(now + timedelta(minutes=1))}).text.strip().splitlines()
    assert len(csv_text) == 6 and all("WOKWI_SIMULATOR,VIRTUAL (Wokwi simulator)" in r for r in csv_text[1:])
    # stop sending: stale after a few seconds
    stale = _wait(lambda: (lambda x: x if x["machines"][0]["state"] == "STALE" else None)(http.get("/api/wokwi").json()), 15, "stale")
    assert stale["state"] == "STALE_DATA"
    # the device vanishes without a goodbye: the broker publishes its last will, the card goes offline
    dev._sock.close() if getattr(dev, "_sock", None) else None
    dev.loop_stop()
    off = _wait(lambda: (lambda x: x if x["machines"][0]["state"] == "OFFLINE" else None)(http.get("/api/wokwi").json()), 20,
                "offline via last will")
    assert off["machines"][0]["device_status"] == "offline" and off["machines"][0]["last"]["rpm"] is None
    print("\n[E2E WOKWI] " + json.dumps({"online_values": {k: m["last"][k] for k in ("rpm", "temp_c", "vibration_rms_mm_s", "current_a", "scenario")},
                                          "rejected": stale["counters"]["rejected"], "csv_rows": len(csv_text) - 1,
                                          "stale_state": stale["state"], "offline_status": off["machines"][0]["device_status"]}))


def test_testing_mode_is_unchanged_by_wokwi(stack):
    http, mport = stack
    http.post("/api/operating-mode", json={"mode": "TESTING"})
    dev = _device(mport)
    for i in range(10, 14):
        _send(dev, i, rpm=2999.0)
        time.sleep(0.4)
    snaps = http.get("/api/snapshots").json()
    snaps = snaps.values() if isinstance(snaps, dict) else snaps
    assert {s["machine_id"] for s in snaps} == {"VMC-01", "LATHE-01", "SAW-01", "COMP-01"}  # no virtual machine added
    assert all(s["mode"] == "SIM" for s in snaps) and all(s.get("rpm") != 2999.0 for s in snaps)
    reg = http.get("/api/sim/controls").json()
    assert all(m["default"] for m in reg["machines"]) and reg["run"]["state"] == "RUNNING"
    dev.loop_stop()
    dev.disconnect()
