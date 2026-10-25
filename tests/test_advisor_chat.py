"""POST /api/chat with Gemini (Google Gen AI SDK): function-calling loop, contract, dispatcher, validation,
error handling, and the operator approval workflow.

Gemini is always mocked: the fake client returns real google.genai.types responses and raises real
google.genai.errors, so no test touches the network.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from datetime import datetime, timezone
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient
from google.genai import errors, types

from api.advisor import AdvisorConfig, ChatError, ChatRequest, TwinAdvisor, make_client, unverified_numbers
from sim.factory import FactoryEngine

T0 = datetime(2026, 10, 26, 10, 0, tzinfo=timezone.utc)
KEY = "AIza-test-key-never-shown-0123456789"
MODEL = "gemini-test-model"


# ----------------------------------------------------------------- fake Gemini (real SDK types)
def call(i, name, **args):
    return types.Part(function_call=types.FunctionCall(id=f"c{i}", name=name, args=args))


def gresp(*parts, finish="STOP", model=MODEL):
    return types.GenerateContentResponse(model_version=model, candidates=[types.Candidate(
        content=types.Content(role="model", parts=list(parts)) if parts else None,
        finish_reason=types.FinishReason(finish))])


def answer(i, reply="**Measured:** see evidence", **kw):
    return gresp(call(i, "submit_answer", reply=reply, measured=kw.get("measured", []), inferred=kw.get("inferred", []),
                      unavailable=kw.get("unavailable", []), suggested_actions=kw.get("suggested_actions", [])))


def api_error(cls, code, status, message, details=None):
    return cls(code, {"error": {"code": code, "status": status, "message": message, "details": details or []}})


class FakeGemini:
    """Stands in for genai.Client: `aio.models.generate_content` pops scripted responses or raises."""

    def __init__(self, *script):
        self.script, self.calls = list(script), []
        self.aio = SimpleNamespace(models=SimpleNamespace(generate_content=self._generate))

    async def _generate(self, *, model, contents, config):
        self.calls.append({"model": model, "contents": list(contents), "config": config})
        item = self.script.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


@pytest.fixture
def env(monkeypatch):
    for k in ("UDYAM_LLM_PROVIDER", "OLLAMA_MODEL", "UDYAM_CHAT_OFFLINE_FALLBACK", "UDYAM_OPERATOR_TOKEN",
              "UDYAM_CHAT_MAX_TOOL_ROUNDS"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("GEMINI_API_KEY", KEY)
    monkeypatch.setenv("GEMINI_MODEL", MODEL)
    return monkeypatch


def advisor(fault: str | None = "coolant_degradation", ticks: int = 120) -> TwinAdvisor:
    e = FactoryEngine(seed=42, start=T0)
    a = TwinAdvisor(e, economics=lambda: {"inr_per_part": 450, "downtime_cost_per_hr": 2500})
    for _ in range(140):
        a.observe(e.step())
    if fault:
        e.command({"cmd": "inject_fault", "machine_id": "VMC-01", "fault": fault})
    for _ in range(ticks):
        a.observe(e.step())
    return a


def chat(a: TwinAdvisor, fake: FakeGemini | None, message="What is VMC-01 doing?", **kw) -> dict:
    if fake is not None:
        a.client_factory = lambda cfg: fake
    return asyncio.run(a.chat(ChatRequest(message=message, **kw)))


# ----------------------------------------------------------------- configuration
def test_gemini_needs_key_and_model_and_nothing_is_hard_coded(env):
    cfg = AdvisorConfig.from_env()
    assert cfg.provider == "gemini" and cfg.llm_model == MODEL
    env.delenv("GEMINI_MODEL")
    cfg = AdvisorConfig.from_env()
    assert cfg.provider == "offline" and cfg.gemini_model is None and "GEMINI_MODEL" in cfg.llm_not_ready_reason()
    env.setenv("GEMINI_MODEL", MODEL)
    env.setenv("GEMINI_API_KEY", "your-gemini-api-key-here")  # the .env.example placeholder
    assert AdvisorConfig.from_env().provider == "offline"


def test_client_uses_sdk_with_timeout_and_retries(env):
    env.setenv("GEMINI_TIMEOUT_S", "12")
    env.setenv("GEMINI_MAX_RETRIES", "3")
    from google import genai
    client = make_client(AdvisorConfig.from_env())
    assert isinstance(client, genai.Client)
    opts = client._api_client._http_options
    assert opts.timeout == 12000 and opts.retry_options.attempts == 4


# ----------------------------------------------------------------- the function-calling cycle
def test_full_cycle_contract_and_server_built_evidence(env):
    a = advisor()
    thought = types.Part(text="", thought=True, thought_signature=b"sig-123")
    fake = FakeGemini(
        gresp(thought, call(1, "get_machine_state", machine_id="VMC-01"), call(2, "query_dataset", fault="coolant_degradation")),
        answer(3, reply="**Measured:** VMC-01 temperature from SIM telemetry.",
               measured=["VMC-01 process temperature (SIM)"], inferred=["UDYAM-SIM statistics"],
               suggested_actions=[{"action": "Check coolant level", "category": "inspect", "machine_id": "VMC-01",
                                   "rationale": "cooling drift"}]),
    )
    r = chat(a, fake)
    for k in ("reply", "conversation_id", "evidence", "suggested_actions", "requires_approval", "mode"):
        assert k in r
    assert r["mode"] == "gemini" and r["structured"] and r["model"] == MODEL and r["telemetry_mode"] == "SIM"
    # evidence = what the server ran, with the real tool output
    assert [e["tool"] for e in r["evidence"]] == ["get_machine_state", "query_dataset"]
    assert r["evidence"][0]["result"] == a.dispatch("get_machine_state", {"machine_id": "VMC-01"})[1]
    assert r["evidence"][1]["result"]["stats"]["episodes"] == a.knowledge()["faults"]["coolant_degradation"]["episodes"]
    assert r["suggested_actions"][0]["requires_operator_approval"] is False and r["requires_approval"] is False
    # the request: configured model, declarations, no automatic function calling, the evidence rules
    first = fake.calls[0]
    assert first["model"] == MODEL
    cfg = first["config"]
    assert cfg.automatic_function_calling.disable is True
    assert cfg.tool_config.function_calling_config.mode == types.FunctionCallingConfigMode.AUTO
    names = {d.name for d in cfg.tools[0].function_declarations}
    assert {"get_machine_state", "get_recent_events", "query_dataset", "simulate_what_if", "propose_action",
            "submit_answer"} <= names
    assert "Never state a number that is not in a tool result" in cfg.system_instruction
    assert first["contents"][-1].parts[0].text == "What is VMC-01 doing?"
    # round 2: Gemini's own content (thought signature included) went back unchanged, then one user turn
    # answering both calls by id
    second = fake.calls[1]["contents"]
    assert second[-2].parts[0].thought_signature == b"sig-123"
    responses = second[-1].parts
    assert second[-1].role == "user" and [p.function_response.id for p in responses] == ["c1", "c2"]
    assert "output" in responses[0].function_response.response


def test_conversation_id_keeps_history(env):
    a = advisor(None, 10)
    fake = FakeGemini(answer(1, "first answer"), answer(2, "second answer"))
    r1 = chat(a, fake, "hello")
    r2 = chat(a, fake, "and now?", conversation_id=r1["conversation_id"])
    assert r2["conversation_id"] == r1["conversation_id"]
    texts = [(c.role, c.parts[0].text) for c in fake.calls[1]["contents"]]
    assert texts == [("user", "hello"), ("model", "first answer"), ("user", "and now?")]


def test_proposals_are_never_applied_by_the_chat(env):
    a = advisor()
    before = a.engine.plants["VMC-01"].derate_pct
    fake = FakeGemini(
        gresp(call(1, "propose_action", machine_id="VMC-01", action="derate", derate_pct=20, title="", reason="cooling")),
        answer(2, suggested_actions=[{"action": "Stop the spindle", "category": "machine_control",
                                      "machine_id": "VMC-01", "rationale": "x"}]),
    )
    r = chat(a, fake)
    assert r["requires_approval"] is True
    assert [s["requires_operator_approval"] for s in r["suggested_actions"]] == [True, True]
    p = r["proposals"][0]
    assert p["status"] == "pending_approval" and len(p["proposal_id"]) == 32 and p["executed"] is False
    assert a.engine.plants["VMC-01"].derate_pct == before


@pytest.mark.parametrize("name,args,needle", [
    ("get_machine_state", {"machine_id": "XYZ-99"}, "Unknown machine"),
    ("get_machine_state", {"machine_id": "VMC-01", "extra": 1}, "Extra inputs"),
    ("get_recent_events", {"n": 0, "machine_id": ""}, "greater than or equal to 1"),
    ("query_dataset", {"fault": "spaghetti"}, "Unknown fault"),
    ("simulate_what_if", {"machine_id": "VMC-01", "action": "explode", "minutes": 5, "parts_target": 0}, "action"),
    ("propose_action", {"machine_id": "VMC-01", "action": "derate", "derate_pct": 80, "title": "", "reason": "hot!"}, "derate_pct"),
    ("run_shell", {"cmd": "rm -rf /"}, "Unknown tool"),
])
def test_invalid_tool_calls_go_back_as_errors(env, name, args, needle):
    a = advisor(None, 10)
    fake = FakeGemini(gresp(call(1, name, **args)), answer(2))
    r = chat(a, fake)
    [part] = fake.calls[1]["contents"][-1].parts
    assert needle in part.function_response.response["error"]
    assert r["evidence"][0]["ok"] is False


def test_unavailable_data_is_reported_not_invented(env):
    a = advisor(None, 10)
    ok, r = a.dispatch("simulate_what_if", {"machine_id": "SAW-01", "action": "continue", "minutes": 10, "parts_target": 0})
    assert not ok and "No simulation is available" in r["error"]
    a.engine.command({"cmd": "set_mode", "mode": "REPLAY"})
    ok, r = a.dispatch("simulate_what_if", {"machine_id": "VMC-01", "action": "continue", "minutes": 10, "parts_target": 0})
    assert not ok and "REPLAY" in r["error"]


def test_dispatcher_returns_real_numbers():
    a = advisor()
    ok, twf = a.dispatch("query_dataset", {"fault": "TWF"})
    assert ok and (twf["rows_with_fault"], twf["rows_total"]) == (46, 10000)
    ok, wi = a.dispatch("simulate_what_if", {"machine_id": "VMC-01", "action": "derate_20", "minutes": 20, "parts_target": 40})
    assert ok and wi["kind"] == "inferred" and [o["derate_pct"] for o in wi["options"]] == [0.0, 20.0]


def test_unverified_number_guard():
    ev = [{"result": {"process_temp_k": 314.65, "vibration_rms_mm_s": 1.07, "fault_rate": 0.0046}}]
    assert unverified_numbers("VMC-01 is at 41.5 °C, vibration 1.07 mm/s, rate 0.46 %, 2 alerts", ev) == []
    assert unverified_numbers("Failure probability is 87.3 %", ev) == ["87.3"]


# ----------------------------------------------------------------- errors
PROVIDER_ERRORS = [
    (api_error(errors.ClientError, 429, "RESOURCE_EXHAUSTED", "Resource has been exhausted",
               [{"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "7s"}]), 429, "provider_rate_limited"),
    (api_error(errors.ClientError, 400, "INVALID_ARGUMENT", "API key not valid. Please pass a valid API key."),
     502, "provider_auth_failed"),
    (api_error(errors.ClientError, 403, "PERMISSION_DENIED", "Permission denied"), 502, "provider_auth_failed"),
    (api_error(errors.ClientError, 404, "NOT_FOUND", "models/gemini-test-model is not found"), 502, "model_unavailable"),
    (api_error(errors.ClientError, 400, "INVALID_ARGUMENT", "Request contains an invalid argument."), 502, "provider_bad_request"),
    (api_error(errors.ServerError, 503, "UNAVAILABLE", "The model is overloaded."), 502, "provider_error"),
    (api_error(errors.ServerError, 504, "DEADLINE_EXCEEDED", "Deadline exceeded"), 504, "provider_timeout"),
    (httpx.ReadTimeout("timed out"), 504, "provider_timeout"),
    (httpx.ConnectError("connection refused"), 503, "provider_unreachable"),
]


@pytest.mark.parametrize("exc,status,code", PROVIDER_ERRORS)
def test_provider_errors_fall_back_offline_or_return_codes(env, exc, status, code):
    a = advisor(None, 10)
    r = chat(a, FakeGemini(exc))
    assert r["mode"] == "offline" and r["fallback_reason"]["error"] == code and r["reply"]
    assert KEY not in json.dumps(r)
    env.setenv("UDYAM_CHAT_OFFLINE_FALLBACK", "0")
    with pytest.raises(ChatError) as err:
        chat(a, FakeGemini(exc))
    assert (err.value.status, err.value.code) == (status, code)
    assert KEY not in json.dumps(err.value.detail())
    if code == "provider_rate_limited":
        assert err.value.extra["retry_after_s"] == 7


@pytest.mark.parametrize("bad,code", [
    (types.GenerateContentResponse(candidates=[]), "malformed_provider_response"),
    (gresp(finish="STOP"), "malformed_provider_response"),
    (gresp(types.Part(text="   "), finish="STOP"), "malformed_provider_response"),
    (gresp(types.Part(function_call=types.FunctionCall(id="c1", name=None, args={}))), "malformed_provider_response"),
    (gresp(types.Part(text="partial"), finish="MAX_TOKENS"), "provider_truncated"),
])
def test_malformed_provider_responses(env, bad, code):
    env.setenv("UDYAM_CHAT_OFFLINE_FALLBACK", "0")
    with pytest.raises(ChatError) as err:
        chat(advisor(None, 10), FakeGemini(bad))
    assert (err.value.status, err.value.code) == (502, code)


def test_malformed_function_call_is_retried_once(env):
    fake = FakeGemini(gresp(finish="MALFORMED_FUNCTION_CALL"), answer(1, "ok after retry"))
    r = chat(advisor(None, 10), fake)
    assert r["reply"] == "ok after retry" and r["tool_rounds"] == 2
    assert "malformed" in fake.calls[1]["contents"][-1].parts[0].text


def test_tool_round_limit(env):
    env.setenv("UDYAM_CHAT_OFFLINE_FALLBACK", "0")
    env.setenv("UDYAM_CHAT_MAX_TOOL_ROUNDS", "2")
    with pytest.raises(ChatError) as err:
        chat(advisor(None, 10), FakeGemini(*[gresp(call(i, "get_line_status")) for i in range(3)]))
    assert err.value.code == "tool_round_limit"


def test_blocked_prompt_is_reported(env):
    blocked = types.GenerateContentResponse(
        prompt_feedback=types.GenerateContentResponsePromptFeedback(block_reason=types.BlockedReason.SAFETY))
    r = chat(advisor(None, 10), FakeGemini(blocked))
    assert r["mode"] == "gemini" and "declined" in r["reply"] and r["structured"] is False


def test_failed_turn_leaves_history_unchanged(env):
    env.setenv("UDYAM_CHAT_OFFLINE_FALLBACK", "0")
    a = advisor(None, 10)
    r = chat(a, FakeGemini(answer(1)))
    conv = a.store._items[r["conversation_id"]]
    before = list(conv.messages)
    with pytest.raises(ChatError):
        chat(a, FakeGemini(gresp(call(2, "get_line_status")), httpx.ReadTimeout("timed out")),
             conversation_id=r["conversation_id"])
    assert conv.messages == before and conv.user_turns == 1


def test_missing_credentials_answer_offline_or_503(env):
    env.delenv("GEMINI_API_KEY")
    a = advisor(None, 10)
    r = chat(a, None, "VMC-01 status")
    assert r["mode"] == "offline" and r["fallback_reason"]["error"] == "chat_not_configured"
    assert "GEMINI_API_KEY" in r["notice"] and r["evidence"]
    env.setenv("UDYAM_CHAT_OFFLINE_FALLBACK", "0")
    with pytest.raises(ChatError) as err:
        chat(a, None, "VMC-01 status")
    assert (err.value.status, err.value.code) == (503, "chat_not_configured")


# ----------------------------------------------------------------- HTTP: chat + approval workflow
@pytest.fixture
def client(env):
    env.setenv("UDYAM_TICKER", "0")
    env.setenv("UDYAM_AUTO_DEMO", "0")
    from api.app import advisor as app_advisor, app, broadcast_messages, factory_engine
    with TestClient(app) as c:
        async def go():
            for _ in range(20):
                await broadcast_messages(factory_engine.step())
        asyncio.run(go())
        yield c, app_advisor, factory_engine


def _propose(c, adv, action="derate", **extra):
    args = {"machine_id": "VMC-01", "action": action, "derate_pct": 20 if action == "derate" else 0,
            "title": "Check coolant pump" if action == "work_order" else "", "reason": "twin what-if", **extra}
    adv.client_factory = lambda cfg: FakeGemini(gresp(call(1, "propose_action", **args)), answer(2))
    r = c.post("/api/chat", json={"message": "what should I do?"})
    assert r.status_code == 200
    return r.json()["proposals"][0]["proposal_id"]


def test_http_validation_status_and_no_key_leak(client):
    c, adv, _ = client
    assert c.post("/api/chat", json={"message": ""}).status_code == 422
    assert c.post("/api/chat", json={"message": "   "}).status_code == 422
    assert c.post("/api/chat", json={"message": "hi", "conversation_id": "nope"}).status_code == 422
    r = c.post("/api/chat", json={"message": "hi", "machine_id": "CONV-01"})
    assert r.status_code == 404 and r.json()["detail"]["error"] == "unknown_machine"
    st = c.get("/api/chat/status")
    assert st.json()["provider"] == "gemini" and st.json()["llm_model"] == MODEL and KEY not in st.text
    adv.client_factory = lambda cfg: FakeGemini(gresp(call(1, "get_machine_state", machine_id="VMC-01")), answer(2))
    r = c.post("/api/chat", json={"message": "VMC-01?", "machine_id": "VMC-01"})
    assert r.status_code == 200 and r.json()["mode"] == "gemini" and KEY not in r.text


def test_approval_needs_operator_token_when_configured(client, env):
    c, adv, eng = client
    env.setenv("UDYAM_OPERATOR_TOKEN", "pin-4711")
    pid = _propose(c, adv)
    before = eng.plants["VMC-01"].derate_pct
    assert c.post(f"/api/chat/proposals/{pid}/approve").status_code == 401
    assert c.post(f"/api/chat/proposals/{pid}/approve", headers={"X-Operator-Token": "wrong"}).status_code == 401
    assert eng.plants["VMC-01"].derate_pct == before  # nothing happened
    r = c.post(f"/api/chat/proposals/{pid}/approve", headers={"X-Operator-Token": "pin-4711"},
               json={"operator": "Asha"})
    assert r.status_code == 200 and r.json()["status"] == "executed" and r.json()["approved_by"] == "Asha"
    assert r.json()["result"] == {"applied_to": "simulator", "machine_id": "VMC-01", "derate_pct": 20.0}
    assert eng.plants["VMC-01"].derate_pct == 20.0
    again = c.post(f"/api/chat/proposals/{pid}/approve", headers={"X-Operator-Token": "pin-4711"})
    assert again.status_code == 409 and again.json()["detail"]["error"] == "proposal_already_decided"
    eng.plants["VMC-01"].derate_pct = 0.0


def test_approval_without_token_is_local_only(client):
    c, adv, eng = client
    pid = _propose(c, adv)
    r = c.post(f"/api/chat/proposals/{pid}/approve")  # TestClient is not a loopback client
    assert r.status_code == 403 and r.json()["detail"]["error"] == "approval_requires_local_operator"
    from api.app import _authorize_operator
    local = SimpleNamespace(client=SimpleNamespace(host="127.0.0.1"), headers={"host": "localhost:8000"})
    _authorize_operator(local, None)  # allowed
    cross = SimpleNamespace(client=SimpleNamespace(host="127.0.0.1"),
                            headers={"host": "localhost:8000", "origin": "https://evil.example"})
    with pytest.raises(Exception) as err:
        _authorize_operator(cross, None)
    assert err.value.status_code == 403


def test_work_order_proposal_dismiss_expiry_and_unknown(client, env):
    c, adv, _ = client
    env.setenv("UDYAM_OPERATOR_TOKEN", "pin")
    h = {"X-Operator-Token": "pin"}
    pid = _propose(c, adv, action="work_order")
    r = c.post(f"/api/chat/proposals/{pid}/approve", headers=h)
    wo_id = r.json()["result"]["work_order"]
    wo = c.get(f"/api/work-orders/{wo_id}").json()
    assert wo["title"] == "Check coolant pump" and wo["evidence"] == "twin what-if" and pid in wo["operator_notes"]
    pid2 = _propose(c, adv)
    assert c.post(f"/api/chat/proposals/{pid2}/dismiss", headers=h).json()["status"] == "dismissed"
    assert c.post(f"/api/chat/proposals/{pid2}/approve", headers=h).status_code == 409
    pid3 = _propose(c, adv)
    adv.proposals[pid3]["_created"] = time.time() - 3600
    r = c.post(f"/api/chat/proposals/{pid3}/approve", headers=h)
    assert r.status_code == 410 and r.json()["detail"]["error"] == "proposal_expired"
    assert c.post(f"/api/chat/proposals/{'0' * 32}/approve", headers=h).status_code == 404
    assert c.post("/api/chat/proposals/not-an-id/approve", headers=h).status_code == 404


def test_no_api_key_in_the_frontend():
    root = os.path.join(os.path.dirname(__file__), "..", "web")
    for name in os.listdir(root):
        if name.endswith((".js", ".html")):
            text = open(os.path.join(root, name), encoding="utf-8").read()
            assert "GEMINI_API_KEY" not in text and "AIza" not in text and "generativelanguage" not in text
