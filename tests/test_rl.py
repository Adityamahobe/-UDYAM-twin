"""RL maintenance policy: state, reward, environment, Q-learning, the live feedback loop and its API."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from ml.maintenance_rl import (ACTIONS, AIR_MIN_BAR, DECISION_TICKS, METRICS_PATH, POLICY_PATH, REWARD, TEMP_LIMIT_C,
                               FeedbackAgent, Outcome, QPolicy, VmcEnv, discretize, reward, rule_inspect_policy,
                               rule_policy, zone_of)
from sim.factory import FactoryEngine

T0 = datetime(2026, 10, 26, 10, 0, tzinfo=timezone.utc)


def test_state_encoding():
    assert [zone_of(v) for v in (1.0, 1.4, 3.0, 4.5)] == ["A", "B", "C", "D"]
    f = {"temp_c": TEMP_LIMIT_C - 2, "temp_trend_c": 0.8, "zone": "A", "load_pct": 60, "tool_wear_min": 100, "derate_pct": 0}
    assert discretize(f) == "m2|up|AB|ok|t0|d0|a?|i-"
    hot = {**f, "temp_c": TEMP_LIMIT_C + 0.2, "temp_trend_c": -0.9, "zone": "D", "load_pct": 90, "tool_wear_min": 210,
           "derate_pct": 20, "air_bar": AIR_MIN_BAR - 0.5, "inspection": "fault"}
    assert discretize(hot) == "m0|dn|D|hi|t2|d20|a2|iF"
    assert discretize({**f, "air_bar": 7.0, "inspection": "ok"}) == "m2|up|AB|ok|t0|d0|a0|iO"
    assert discretize({}) == "m?|st|AB|ok|t?|d0|a?|i-"


def test_reward_accounts_parts_penalties_maintenance_and_trip():
    o = Outcome(parts=2 / 3)
    assert reward(o, "continue") == pytest.approx(REWARD["inr_per_part"] * 2 / 3, abs=0.01)
    assert reward(o, "maintain") == -REWARD["maintenance_inr"]  # a planned stop makes no parts that interval
    assert reward(o, "inspect") == pytest.approx(REWARD["inr_per_part"] * 2 / 3 * (1 - REWARD["inspection_output_loss"])
                                                 - REWARD["inspection_inr"], abs=0.01)
    hot = Outcome(parts=2 / 3, over_temp_s=5.0)
    assert reward(hot, "continue") == pytest.approx(reward(o, "continue") - 5 * REWARD["over_temp_inr_per_s"], abs=0.01)
    trip = Outcome(trip=True)
    assert reward(trip, "continue") == -REWARD["trip_downtime_h"] * REWARD["downtime_cost_inr_per_hr"]
    assert Outcome(zone_d_s=0.5).adverse and not Outcome(zone_c_s=5).adverse


def _run(env: VmcEnv, policy) -> tuple[float, bool]:
    s, total, tripped = env.state(), 0.0, False
    while not env.done:
        s, r, _, o = env.step(policy(s))
        total, tripped = total + r, tripped or o.trip
    return total, tripped


def test_environment_is_deterministic_and_actions_matter():
    a, b = VmcEnv(seed=3, scenario="vibration_fault"), VmcEnv(seed=3, scenario="vibration_fault")
    assert _run(a, lambda s: "continue") == _run(b, lambda s: "continue")
    # a bearing defect left running trips the spindle; maintaining on zone D avoids it
    ret_continue, trip_continue = _run(VmcEnv(seed=3, scenario="vibration_fault"), lambda s: "continue")
    ret_rule, trip_rule = _run(VmcEnv(seed=3, scenario="vibration_fault"), rule_policy)
    assert trip_continue and not trip_rule and ret_rule > ret_continue
    # with no fault, running at full speed beats derating
    assert _run(VmcEnv(seed=4, scenario="none"), lambda s: "continue")[0] > _run(VmcEnv(seed=4, scenario="none"), lambda s: "derate_40")[0]


def test_inspection_and_air_interlock_in_the_environment():
    env = VmcEnv(seed=12, scenario="over_temperature")
    env.inject_at = 0
    env.step("continue")
    s, _, _, _ = env.step("inspect")
    assert s.endswith("|iF")  # the inspection sees the injected coolant fault
    for _ in range(3):
        s, _, _, _ = env.step("derate_20")
    assert s.endswith("|i-")  # results expire after INSPECTION_VALID_DECISIONS
    clean = VmcEnv(seed=13, scenario="none")
    assert clean.step("inspect")[0].endswith("|iO")
    air = VmcEnv(seed=11, scenario="air_pressure_drop")
    air.inject_at = 0
    for _ in range(2):
        s, _, _, o = air.step("continue")
    assert "|d20|a2|" in s and o.min_air_bar < AIR_MIN_BAR  # interlock derates the VMC 25 %
    assert rule_policy(s) == "continue" and rule_inspect_policy("m2|up|AB|ok|t0|d0|a0|i-") == "inspect"
    assert rule_inspect_policy("m2|up|AB|ok|t0|d0|a0|iF") == "maintain"


def test_q_update_and_fallback():
    pol = QPolicy()
    assert pol.best("m3|st|D|ok|t0|d0|a0|i-") == "maintain"  # unseen state → rule baseline
    before, after = pol.update("s", "derate_20", 100.0, "s2", done=True, alpha=0.5)
    assert (before, after) == (0.0, 50.0) and pol.best("s") == "derate_20"
    pol.update("s2", "continue", 300.0, "s3", done=True, alpha=1.0)
    _, after = pol.update("s", "continue", 10.0, "s2", done=False, alpha=1.0)
    assert after == pytest.approx(10.0 + 0.9 * 300.0)


@pytest.mark.skipif(not POLICY_PATH.exists(), reason="run python -m ml.train_rl first")
def test_trained_policy_and_metrics():
    import json
    pol = QPolicy.load()
    assert pol is not None and len(pol.q) > 50 and pol.meta["episodes"] > 0
    res = json.loads(METRICS_PATH.read_text())["results"]
    assert set(res) == {"always_continue", "rule_based", "rule_with_inspection", "rl_policy"}
    assert "air_pressure_drop" in res["rl_policy"]
    assert res["rl_policy"]["all"]["return_inr"] > res["always_continue"]["all"]["return_inr"]
    assert res["rl_policy"]["all"]["trips"] <= res["always_continue"]["all"]["trips"]


def _feed(agent: FeedbackAgent, engine: FactoryEngine, ticks: int) -> None:
    for _ in range(ticks):
        agent.observe(engine.step())


def test_feedback_loop_evaluates_updates_and_never_actuates():
    e = FactoryEngine(seed=42, start=T0)
    e.auto_derate = False  # so any derate change could only come from the agent
    rows = []
    agent = FeedbackAgent(policy=QPolicy(), sink=rows.append)
    _feed(agent, e, 60)
    e.command({"cmd": "inject_fault", "machine_id": "VMC-01", "fault": "coolant_degradation"})
    _feed(agent, e, DECISION_TICKS * 6)
    assert e.plants["VMC-01"].derate_pct == 0.0  # recommendations are never applied
    assert len(rows) >= 5 and rows == list(reversed(list(agent.recent)))[-len(rows):]
    r = rows[-1]
    for k in ("ts_start", "ts_end", "state", "recommended_action", "action_taken", "outcome_adverse", "reward_inr",
              "prediction_correct", "policy_updated", "q_before", "q_after"):
        assert k in r
    assert r["action_taken"] == "continue" and r["policy_updated"] is True and r["mode"] == "SIM"
    assert agent.stats["online_updates"] == len(rows)
    rec = agent.recommendation("VMC-01")
    assert rec["requires_approval"] is True and rec["action"] in ACTIONS and set(rec["q_values"]) == set(ACTIONS)


def test_feedback_loop_detects_maintenance_and_skips_learning_outside_sim():
    e = FactoryEngine(seed=42, start=T0)
    agent = FeedbackAgent(policy=QPolicy())
    _feed(agent, e, DECISION_TICKS + 2)
    e.command({"cmd": "inject_fault", "machine_id": "VMC-01", "fault": "tool_wear_accel"})
    agent.observe(e.command({"cmd": "resolve", "machine_id": "VMC-01"}))
    _feed(agent, e, DECISION_TICKS)
    assert agent.recent[0]["action_taken"] == "maintain"
    # REPLAY / LIVE data: still evaluated, but the policy is not updated
    replay = FeedbackAgent(policy=QPolicy())
    for _ in range(DECISION_TICKS * 2 + 2):
        replay.observe([{**m, "mode": "REPLAY"} if m.get("type") == "snapshot" else m for m in e.step()])
    assert replay.recent and all(not r["policy_updated"] for r in replay.recent) and not replay.policy.q.get("s")
    # an inspection work order is recognised and its (simulated) result enters the next state
    insp = FeedbackAgent(policy=QPolicy())
    e2 = FactoryEngine(seed=42, start=T0)
    _feed(insp, e2, DECISION_TICKS + 2)
    e2.command({"cmd": "inject_fault", "machine_id": "VMC-01", "fault": "coolant_degradation"})
    _feed(insp, e2, 2)
    insp.observe([{"type": "event", "kind": "WORK_ORDER", "machine_id": "VMC-01", "ts": "x",
                   "title": "Work order WO-0001 created: Inspect VMC-01 (RL short stop) (Open)"}])
    _feed(insp, e2, DECISION_TICKS)
    assert insp.recent[0]["action_taken"] == "inspect" and insp.recent[0]["inspection_result"] == "fault"
    assert insp.open["VMC-01"].state.endswith("|iF")
    # a reset drops open decisions instead of evaluating across it
    agent.observe(e.command({"cmd": "reset"}))
    assert agent.open == {}


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("UDYAM_TICKER", "0")
    monkeypatch.setenv("UDYAM_AUTO_DEMO", "0")
    monkeypatch.setenv("UDYAM_OPERATOR_TOKEN", "pin")
    from api.app import app, broadcast_messages, factory_engine, rl_agent
    with TestClient(app) as c:
        async def go(n):
            for _ in range(n):
                await broadcast_messages(factory_engine.step())
        asyncio.run(go(DECISION_TICKS * 2 + 4))
        yield c, factory_engine, rl_agent


def test_rl_api_recommends_and_proposal_needs_approval(client):
    c, eng, agent = client
    st = c.get("/api/rl").json()
    assert "VMC-01" in st["recommendations"] and st["feedback"]["decisions"] >= 1 and st["recent"]
    assert "recommendations only" in st["label"]
    assert c.get("/api/rl/metrics").status_code in (200, 404)
    assert c.post("/api/rl/NOPE-01/propose").status_code == 404
    # force a derate recommendation for the current state, then propose it
    rec = agent.recommendation("VMC-01")
    agent.open["VMC-01"].recommended = "derate_20"
    before = eng.plants["VMC-01"].derate_pct
    r = c.post("/api/rl/VMC-01/propose")
    assert r.status_code == 200, r.text
    p = r.json()["proposal"]
    assert p["status"] == "pending_approval" and p["action"] == "derate" and p["derate_pct"] == 20.0
    assert eng.plants["VMC-01"].derate_pct == before  # nothing applied yet
    assert c.post(f"/api/chat/proposals/{p['proposal_id']}/approve").status_code == 401
    ok = c.post(f"/api/chat/proposals/{p['proposal_id']}/approve", headers={"X-Operator-Token": "pin"})
    assert ok.status_code == 200 and eng.plants["VMC-01"].derate_pct == 20.0
    eng.plants["VMC-01"].derate_pct = 0.0
    agent.open["VMC-01"].recommended = rec["action"]
    # maintenance becomes a work-order proposal
    agent.open["VMC-01"].recommended = "maintain"
    p = c.post("/api/rl/VMC-01/propose").json()["proposal"]
    assert p["action"] == "work_order" and "RL" in p["title"]
    agent.open["VMC-01"].recommended = "inspect"
    p = c.post("/api/rl/VMC-01/propose").json()["proposal"]
    assert p["action"] == "work_order" and p["title"].startswith("Inspect VMC-01")
    assert st["policy"]["actions"] == list(ACTIONS) and "inspect" in ACTIONS
