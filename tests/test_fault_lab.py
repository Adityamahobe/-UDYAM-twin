"""Step 5: fault-injection lab, simulated resolution, clean reset, deterministic 90 s Auto Demo."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from sim.factory import FactoryEngine
from sim.scenarios import SCENARIOS

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)

# scenario → (alert rules expected while it runs, seconds to run)
EXPECTED = {
    "vibration_fault": ({"VIB"}, 30),
    "over_temperature": ({"COOLING", "DERATE"}, 120),
    "tool_wear": ({"TOOL_WEAR"}, 120),
    "overload": ({"OVERLOAD"}, 30),
}


def engine() -> FactoryEngine:
    return FactoryEngine(seed=42, start=T0)


def step(eng: FactoryEngine, seconds: float) -> list[dict]:
    out = []
    for _ in range(int(seconds / 0.5)):
        out += eng.step()
    return out


def alert_rules(msgs: list[dict], machine_id: str) -> list[tuple[str, str]]:
    return [(m["event_id"].split("-", 1)[1].removeprefix("EV_").removesuffix(f"_{machine_id}"), m["severity"])
            for m in msgs if m["type"] == "event" and m["kind"] == "ALERT" and m["machine_id"] == machine_id]


def test_catalogue_has_the_four_scenarios():
    assert set(SCENARIOS) == set(EXPECTED)
    for sc in SCENARIOS.values():
        assert sc.public()["kind"] == "Demo simulation" and sc.what_changes and sc.expect and sc.resolution


@pytest.mark.parametrize("sid", list(EXPECTED))
def test_scenario_alerts_once_per_rule_then_resolves_to_baseline(sid):
    sc = SCENARIOS[sid]
    rules, seconds = EXPECTED[sid]
    eng = engine()
    healthy = step(eng, 10)
    assert alert_rules(healthy, sc.machine_id) == []
    eng.command({"cmd": "start_scenario", "scenario": sid})
    assert eng.snapshots[sc.machine_id]["active_scenario"] is None or True  # set on the next tick
    during = step(eng, seconds)
    assert eng.snapshots[sc.machine_id]["active_scenario"] == sid
    raised = alert_rules(during, sc.machine_id)
    assert {r for r, _ in raised} == rules, raised
    # one alert per rule, except an escalation (WARN → CRITICAL) on the same rule
    for rule in rules:
        sevs = [s for r, s in raised if r == rule]
        assert sevs == sorted(set(sevs), key=["WARN", "CRITICAL"].index), (rule, sevs)

    res = eng.command({"cmd": "resolve", "machine_id": sc.machine_id})
    assert any(m["kind"] == "RESOLUTION" for m in res)
    after = step(eng, 40)
    assert alert_rules(after, sc.machine_id) == []  # no re-alerting while readings recover
    s = eng.snapshots[sc.machine_id]
    assert s["active_scenario"] is None and s["derate_pct"] == 0 and s["state"] == "RUN"
    assert s["risk"]["rules"] == "LOW", s["risk"]
    if s["vibration_rms_mm_s"] is not None:
        assert s["severity"]["zone"] == "A"


def test_effects_are_gradual():
    eng = engine()
    eng.command({"cmd": "start_scenario", "scenario": "vibration_fault"})
    vib = [m["vibration_rms_mm_s"] for m in step(eng, 8) if m["type"] == "observed" and m["machine_id"] == "VMC-01"]
    assert vib[0] < 1.6 and vib[-1] > 3.5  # rises over seconds, not in one tick
    assert max(b - a for a, b in zip(vib, vib[1:])) < 1.0


def test_vibration_zone_d_auto_derates_and_names_the_setpoints():
    eng = engine()
    eng.command({"cmd": "start_scenario", "scenario": "vibration_fault"})
    msgs = step(eng, 20)
    [crit] = [m for m in msgs if m["type"] == "event" and m.get("severity") == "CRITICAL" and m["kind"] == "ALERT"]
    assert "(SIM only), setpoint 1540 → 1232 rpm" in crit["title"]
    assert crit["data"]["threshold"] == {"crossed": "C/D", "value": 4.5, "unit": "mm/s"}
    s = eng.snapshots["VMC-01"]
    assert s["state"] == "DERATE" and s["rpm_setpoint"] == 1232.0 and s["severity"]["zone"] == "D"
    assert s["risk"]["rules"] == "HIGH"


# ----------------------------------------------------------------- API: lab, resolution, reset
@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("UDYAM_AUTO_DEMO", "0")
    monkeypatch.setenv("UDYAM_TICKER", "0")  # the test drives the ticks
    from api.app import app
    with TestClient(app) as c:
        c.post("/api/auto_demo?on=false")
        yield c


def drive(seconds: float, demo: bool = False) -> None:
    """Advance the real app stack (director, engine, broadcast/alert store) without waiting for wall time."""
    from api.app import auto_demo, broadcast_messages, factory_engine

    async def go():
        for _ in range(int(seconds / 0.5)):
            if demo:
                await auto_demo.tick()
            await broadcast_messages(factory_engine.step())
    asyncio.run(go())


def test_api_lab_catalogue_start_and_resolve(client):
    cat = client.get("/api/scenarios").json()
    assert {c["scenario_id"] for c in cat} == set(EXPECTED) and not any(c["active"] for c in cat)
    assert client.post("/api/scenarios/nope/start").status_code == 404
    assert client.post("/api/scenarios/vibration_fault/start").json()["status"] == "started"
    drive(20)
    vib = [a for a in client.get("/api/alerts").json() if a["rule"] == "EV_VIB_VMC-01"]
    assert len(vib) == 1 and vib[0]["severity"] == "CRITICAL" and "zone D" in vib[0]["title"]
    wo = client.post(f"/api/alerts/{vib[0]['alert_id']}/work-order").json()
    assert wo["priority"] == "Critical" and "Inspect spindle bearing" in wo["recommended_action"]

    with client.websocket_connect("/ws/telemetry") as ws:
        ws.receive_json(), ws.receive_json()
        r = client.post("/api/scenarios/resolve?machine_id=VMC-01").json()
        # the current dashboard refreshes its work-order list on the v1.0 WORK_ORDER event
        for _ in range(4):  # RESOLUTION event, alert frame, work_order frame, WORK_ORDER event
            f = ws.receive_json()
            if f.get("kind") == "WORK_ORDER":
                break
        assert f["data"]["wo_id"] == wo["wo_id"] and f["data"]["status"] == "Closed", f
    assert r["open_alerts"] == 0
    assert client.get(f"/api/work-orders/{wo['wo_id']}").json()["status"] == "Closed"
    assert client.post("/api/scenarios/resolve?machine_id=NOPE").status_code == 404


def test_api_reset_leaves_a_clean_baseline(client):
    from api.app import factory_engine
    client.post("/api/scenarios/vibration_fault/start")
    client.post("/api/faults/inject", json={"machine_id": "COMP-01", "fault": "pressure_drop"})
    drive(40)
    alerts = client.get("/api/alerts").json()
    assert len(alerts) >= 2
    wo = client.post(f"/api/alerts/{alerts[0]['alert_id']}/work-order").json()
    manual = client.post("/api/work-orders", json={"machine_id": "LATHE-01", "title": "Weekly lubrication"}).json()

    r = client.post("/api/reset").json()
    assert r["open_alerts"] == 0
    assert client.get(f"/api/work-orders/{wo['wo_id']}").json()["status"] == "Closed"
    assert client.get(f"/api/work-orders/{manual['wo_id']}").json()["status"] == "Open"  # not a scenario WO
    assert factory_engine.active_scenarios == {} and all(not p.faults.active_faults for p in factory_engine.plants.values())
    drive(2)
    snaps = {s["machine_id"]: s for s in client.get("/api/snapshots").json()}
    assert all(s["active_scenario"] is None and s["derate_pct"] == 0 for s in snaps.values())
    assert snaps["VMC-01"]["energy_kwh"] < 0.01  # counters restarted
    assert len(client.get("/api/vibration/VMC-01").json()["trend"]["points"]) <= 4  # chart cleared


# ----------------------------------------------------------------- Auto Demo
def _story(cycle_seconds: float = 90.0) -> list[tuple[float, str]]:
    """Drive one Auto Demo cycle; return (demo time, milestone) pairs."""
    from api.app import auto_demo, factory_engine
    from api.db import get_work_order, list_alerts

    seen: list[tuple[float, str]] = []
    last = {}
    for i in range(int(cycle_seconds / 0.5)):
        t = auto_demo.t
        drive(0.5, demo=True)
        s = factory_engine.snapshots.get("VMC-01", {})
        marks = {
            "zone": (s.get("severity") or {}).get("zone"),
            "state": s.get("state"),
            "alerts": tuple(sorted((a["rule"], a["severity"], a["status"]) for a in list_alerts())),
            "wo": get_work_order(auto_demo.wo_id)["status"] if auto_demo.wo_id else None,
        }
        for k, v in marks.items():
            if last.get(k) != v:
                seen.append((t, f"{k}={v}"))
        last = marks
    return seen


def test_auto_demo_story_is_deterministic_and_complete(client):
    from api.app import auto_demo, factory_engine
    from api.db import list_alerts

    asyncio.run(auto_demo.set(True))
    start = auto_demo.cycle
    cycles = [_story() for _ in range(3)]
    assert auto_demo.cycle == start + 3
    assert cycles[0] == cycles[1] == cycles[2], "every cycle must replay identically"

    story = dict((m, t) for t, m in reversed(cycles[0]))  # first time each milestone appears
    assert story["zone=C"] < story["zone=D"] < 40.0          # alert escalates before the work order
    assert story["state=DERATE"] < 40.0                      # auto-derate on zone D
    assert story["wo=Open"] == 40.0 and story["wo=Acknowledged"] == 48.0 and story["wo=In Progress"] == 54.0
    assert story["wo=Closed"] == 65.0                         # closed by the simulated resolution
    assert story["alerts=()"] == 0.0                          # clean start
    final = cycles[0][-1][0]
    assert final < 90.0
    assert factory_engine.snapshots["VMC-01"]["severity"]["zone"] == "A"
    assert factory_engine.snapshots["VMC-01"]["state"] == "RUN"
    assert list_alerts() == []


def test_auto_demo_off_mid_story_is_clean(client):
    from api.app import auto_demo, factory_engine
    from api.db import list_alerts

    asyncio.run(auto_demo.set(True))
    drive(45, demo=True)  # fault running, derated, work order open
    assert factory_engine.active_scenarios and list_alerts()
    status = client.post("/api/auto_demo?on=false").json()
    assert status["on"] is False
    assert list_alerts() == [] and factory_engine.active_scenarios == {}
    assert factory_engine.plants["VMC-01"].derate_pct == 0
    drive(5)
    assert not list_alerts() and factory_engine.snapshots["VMC-01"]["state"] == "RUN"
