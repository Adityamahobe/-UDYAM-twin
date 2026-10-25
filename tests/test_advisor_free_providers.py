"""Provider selection, and the local Ollama model via the OpenAI-compatible tool loop.

The Gemini (Google Gen AI SDK) loop is tested in test_advisor_chat.py."""

import asyncio
import json
import urllib.error
from datetime import datetime, timezone

from sim.factory import FactoryEngine
from api.advisor import OPENAI_TOOLS, AdvisorConfig, ChatRequest, TwinAdvisor

T0 = datetime(2026, 10, 26, 10, 0, tzinfo=timezone.utc)
ENV = ("UDYAM_LLM_PROVIDER", "GEMINI_API_KEY", "GOOGLE_API_KEY", "GEMINI_MODEL", "OLLAMA_MODEL",
       "OLLAMA_BASE_URL")


def _clean_env(monkeypatch):
    for k in ENV:
        monkeypatch.delenv(k, raising=False)


def _advisor() -> tuple[FactoryEngine, TwinAdvisor]:
    e = FactoryEngine(seed=42, start=T0)
    a = TwinAdvisor(e, economics=lambda: {"inr_per_part": 450, "downtime_cost_per_hr": 2500})
    for _ in range(160):
        a.observe(e.step())
    e.command({"cmd": "inject_fault", "machine_id": "VMC-01", "fault": "coolant_degradation"})
    for _ in range(120):
        a.observe(e.step())
    return e, a


def test_provider_selection(monkeypatch):
    _clean_env(monkeypatch)
    assert AdvisorConfig.from_env().provider == "offline"
    monkeypatch.setenv("OLLAMA_MODEL", "qwen2.5:7b")
    assert AdvisorConfig.from_env().provider == "ollama"
    monkeypatch.setenv("GEMINI_API_KEY", "g-key")
    assert AdvisorConfig.from_env().provider == "ollama"  # no GEMINI_MODEL: no model is hard-coded
    monkeypatch.setenv("GEMINI_MODEL", "gemini-test-model")
    cfg = AdvisorConfig.from_env()
    assert cfg.provider == "gemini" and cfg.llm_model == "gemini-test-model"
    monkeypatch.setenv("UDYAM_LLM_PROVIDER", "ollama")
    assert AdvisorConfig.from_env().provider == "ollama"
    monkeypatch.setenv("UDYAM_LLM_PROVIDER", "offline")
    assert AdvisorConfig.from_env().provider == "offline"


def test_env_example_placeholders_count_as_unset(monkeypatch):
    _clean_env(monkeypatch)
    monkeypatch.setenv("GEMINI_API_KEY", "your-gemini-api-key-here")
    monkeypatch.setenv("GEMINI_MODEL", "gemini-test-model")
    assert AdvisorConfig.from_env().provider == "offline"
    monkeypatch.setenv("GEMINI_API_KEY", "AIza-real-looking-key")
    assert AdvisorConfig.from_env().provider == "gemini"


def test_openai_tool_schemas_are_portable():
    for t in OPENAI_TOOLS:
        fn = t["function"]
        params = fn.get("parameters")
        if params is not None:
            assert params["properties"], f"{fn['name']} has an empty object schema"
        blob = json.dumps(t)
        assert "additionalProperties" not in blob and '"strict"' not in blob


def test_ollama_not_running_falls_back_offline(monkeypatch):
    _clean_env(monkeypatch)
    monkeypatch.setenv("OLLAMA_MODEL", "qwen2.5:7b")
    _, a = _advisor()

    def refused(url, body, headers, timeout_s):
        assert url == "http://localhost:11434/v1/chat/completions"
        raise urllib.error.URLError(ConnectionRefusedError(61, "Connection refused"))

    monkeypatch.setattr(a, "_post_json", refused)
    r = asyncio.run(a.chat(ChatRequest(message="VMC-01 status?")))
    assert r["mode"] == "offline" and r["fallback_reason"]["error"] == "provider_unreachable"
    assert "ollama serve" in r["fallback_reason"]["message"]


def test_plain_text_answer_is_accepted(monkeypatch):
    """Small local models sometimes answer without submit_answer; that still works (structured=False)."""
    _clean_env(monkeypatch)
    monkeypatch.setenv("OLLAMA_MODEL", "qwen2.5:3b")
    _, a = _advisor()
    monkeypatch.setattr(a, "_post_json", lambda *args: {"choices": [{"finish_reason": "stop", "message": {
        "role": "assistant", "content": "All machines are running."}}]})
    r = asyncio.run(a.chat(ChatRequest(message="hello")))
    assert r["mode"] == "ollama" and r["structured"] is False and r["reply"] == "All machines are running."
    # the next turn continues the same conversation as plain text
    r2 = asyncio.run(a.chat(ChatRequest(message="and now?", conversation_id=r["conversation_id"])))
    assert r2["conversation_id"] == r["conversation_id"]
