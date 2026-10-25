"""UDYAM-SIM dataset generator, sim model and the Advisor chatbot (offline + mocked Gemini)."""

import asyncio
import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from sim.factory import FactoryEngine
from tools.generate_dataset import FEATURES, run_episode
from api.advisor import ChatRequest, TwinAdvisor
from ml.sim_predictor import SimPredictor


T0 = datetime(2026, 10, 26, 10, 0, tzinfo=timezone.utc)  # simulated clock: deterministic frames


def _advisor_with_coolant_fault(ticks_after: int = 120) -> tuple[FactoryEngine, TwinAdvisor]:
    e = FactoryEngine(seed=42, start=T0)
    a = TwinAdvisor(e, economics=lambda: {"inr_per_part": 450, "downtime_cost_per_hr": 2500})
    for _ in range(160):
        a.observe(e.step())
    e.command({"cmd": "inject_fault", "machine_id": "VMC-01", "fault": "coolant_degradation"})
    for _ in range(ticks_after):
        a.observe(e.step())
    return e, a


def test_default_knobs_keep_demo_unchanged():
    """Variation knobs are neutral by default: same seed gives identical frames."""
    a, b = FactoryEngine(seed=42, start=T0), FactoryEngine(seed=42, start=T0)
    b.plants["VMC-01"].fault_severity = 1.0
    b.plants["VMC-01"].load_scale = 1.0
    for _ in range(300):
        assert a.step() == b.step()


def test_episode_labels_come_from_injected_fault():
    import twin.model as tm

    before = tm.ai4i_predictor
    rows, meta = run_episode(3, minutes=6, base_seed=11)
    assert tm.ai4i_predictor is before  # AI4I scoring restored for the live twin
    assert rows and set(FEATURES) <= set(rows[0])
    labels = {r["label_fault"] for r in rows}
    assert labels <= {"normal", meta["fault"]}
    for r in rows:
        assert 0.0 <= r["minutes_to_limit"] <= 30.0
        assert "active_fault" not in r  # ground truth never leaks into features


def test_order_surge_raises_spindle_speed():
    e = FactoryEngine(seed=5, start=T0)
    rpm = lambda: next(m for m in e.step() if m.get("type") == "observed" and m["machine_id"] == "VMC-01")["rpm"]
    base = sum(rpm() for _ in range(20)) / 20
    e.command({"cmd": "inject_fault", "machine_id": "VMC-01", "fault": "order_surge"})
    surge = sum(rpm() for _ in range(20)) / 20
    assert surge > base * 1.1


def test_what_if_derate_delays_overtemp():
    _, a = _advisor_with_coolant_fault()
    wi = a.tool_simulate_what_if("VMC-01", "compare", minutes=30)  # 0, 20 and 40 % derate
    opts = {o["derate_pct"]: o for o in wi["options"]}
    assert opts[0.0]["peak_temp_k"] > opts[20.0]["peak_temp_k"] > opts[40.0]["peak_temp_k"]
    t0 = opts[0.0]["minutes_to_temp_limit"]
    t20 = opts[20.0]["minutes_to_temp_limit"]
    assert t0 is not None and (t20 is None or t20 > t0)


def test_advisor_hides_ground_truth_fault():
    _, a = _advisor_with_coolant_fault(40)
    st = a.tool_get_machine_state("VMC-01")
    assert "active_fault" not in st["observed"]
    evs = a.tool_get_recent_events(n=30)["events"]
    assert all(ev.get("kind") != "FAULT_INJECTED" for ev in evs)


def test_offline_chat_proposes_but_never_applies(monkeypatch):
    for k in ("GEMINI_API_KEY", "GEMINI_MODEL", "OLLAMA_MODEL"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("UDYAM_LLM_PROVIDER", "offline")  # never call a real Gemini/Ollama from tests
    e, a = _advisor_with_coolant_fault()
    before = e.plants["VMC-01"].derate_pct
    r = asyncio.run(a.chat(ChatRequest(message="VMC-01 is heating up, can I finish a batch of 40 parts?")))
    assert r["mode"] == "offline" and "Measured" in r["reply"]
    assert "simulate_what_if" in r["tools_used"]
    assert all(p["requires_approval"] for p in r["proposals"])
    assert e.plants["VMC-01"].derate_pct == before  # advisor did not touch the machine


@pytest.mark.skipif(not SimPredictor().available, reason="run python -m ml.train_sim first")
def test_sim_model_spots_coolant_drift():
    _, a = _advisor_with_coolant_fault(160)
    diag = a.tool_get_machine_state("VMC-01")["ml_diagnosis"]
    assert diag["top_faults"][0]["fault"] == "coolant_degradation"


def test_gemini_tool_loop_with_mocked_sdk(monkeypatch):
    from google.genai import types
    calls = []

    def fc(cid, name, **args):
        return types.Part(function_call=types.FunctionCall(id=cid, name=name, args=args))

    def resp(*parts):
        return types.GenerateContentResponse(candidates=[types.Candidate(
            content=types.Content(role="model", parts=list(parts)), finish_reason=types.FinishReason.STOP)])

    async def generate_content(*, model, contents, config):
        calls.append(list(contents))
        if len(calls) == 1:
            return resp(fc("t1", "simulate_what_if", machine_id="VMC-01", action="derate_20", minutes=30, parts_target=0),
                        fc("t2", "propose_action", machine_id="VMC-01", action="derate", derate_pct=20, title="",
                           reason="x-y"))
        results = [p.function_response for p in contents[-1].parts]
        assert [r.id for r in results] == ["t1", "t2"]
        assert len(results[0].response["output"]["options"]) == 2
        return resp(fc("t3", "submit_answer", reply="**Suggested:** derate 20%", measured=[], inferred=[],
                       unavailable=[], suggested_actions=[]))

    monkeypatch.setenv("GEMINI_API_KEY", "AIza-test")
    monkeypatch.setenv("GEMINI_MODEL", "gemini-test-model")
    monkeypatch.setenv("UDYAM_LLM_PROVIDER", "gemini")
    _, a = _advisor_with_coolant_fault(40)
    a.client_factory = lambda cfg: SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(
        generate_content=generate_content)))
    r = asyncio.run(a.chat(ChatRequest(message="what should I do?")))
    assert r["mode"] == "gemini"
    assert r["tools_used"] == ["simulate_what_if", "propose_action"]
    assert r["proposals"][0]["derate_pct"] == 20.0
    assert r["requires_approval"] is True
