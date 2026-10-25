"""UDYAM-TWIN Advisor: a decision-support chatbot grounded in the live twin (POST /api/chat).

The LLM (Google Gemini through the official Google Gen AI SDK, or a local Ollama model) never
invents numbers. It calls tools that read the live twin, the datasets (UDYAM-SIM, AI4I 2020,
CWRU) and runs what-if forecasts with the twin's own calibrated physics, then finishes with
`submit_answer` (MEASURED / INFERRED / SUGGESTED). The server, not the model, builds the evidence
list from the tool calls it actually ran and decides `requires_approval`.

Every tool call goes through `dispatch`: a fixed table of tools, strict argument models (unknown
fields rejected), machine IDs checked against the engine, nothing model-written ever executed.
Gemini's automatic function calling is switched off, so the SDK never runs anything by itself.

The model can only PROPOSE an action. Proposals are stored here with an ID; nothing happens until
an operator approves one through POST /api/chat/proposals/{id}/approve (api/app.py), which checks
the operator's authorization and applies it to the simulated plant only.

Without a key or model, without the SDK, or (by default) when the provider fails, the same tools
power a rule-based offline mode, so the demo never breaks; the response says why.

Environment (server only; never sent to the browser):
  GEMINI_API_KEY               required for Gemini (aistudio.google.com/apikey)
  GEMINI_MODEL                 required for Gemini: a model ID your key can use (nothing is hard-coded)
  GEMINI_TIMEOUT_S             per-request timeout, default 60
  GEMINI_MAX_RETRIES           SDK retries on 408/429/5xx and network errors, default 2
  GEMINI_MAX_OUTPUT_TOKENS     default 8192
  UDYAM_LLM_PROVIDER           auto (default: Gemini if configured, else Ollama, else offline) | gemini | ollama | offline
  OLLAMA_MODEL                 optional local model with tool support, e.g. qwen2.5:7b
  OLLAMA_BASE_URL              default http://localhost:11434/v1
  UDYAM_LLM_TIMEOUT_S          Ollama request timeout, default 60
  UDYAM_CHAT_OFFLINE_FALLBACK  1 (default): answer offline when the model is unavailable; 0: return HTTP errors
  UDYAM_CHAT_MAX_TOOL_ROUNDS   default 6
  UDYAM_OPERATOR_TOKEN         if set, approving a proposal needs this token (X-Operator-Token); if unset,
                               only a browser on the server machine itself can approve
"""

from __future__ import annotations

import json
import os
import re
import asyncio
import threading
import time
import urllib.error
import urllib.request
import uuid
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from config import get_config
from ml.sim_predictor import SimPredictor
from sim.factory import TICK_S
from sim.line import LINE_FLOW

ROOT = Path(__file__).resolve().parent.parent
KNOWLEDGE_PATH = ROOT / "ml" / "sim_knowledge.json"
CWRU_MANIFEST = ROOT / "ml" / "vibration" / "data" / "cwru" / "dataset" / "manifest.csv"
BUFFER_TICKS = 240          # 2 minutes of history per machine
CYCLE_S = dict(LINE_FLOW)  # station cycle time in s (sim/line.py)
MACHINE_ID_RE = re.compile(r"^[A-Z0-9-]{2,20}$")
K_TO_C = 273.15
MAX_CONVERSATIONS = 200
MAX_USER_TURNS = 20
OLLAMA_DEFAULT_URL = "http://localhost:11434/v1"
PROVIDER_LABEL = {"gemini": "Gemini", "ollama": "Local model", "offline": "Offline"}
PROPOSAL_TTL_S = 15 * 60
MAX_PROPOSALS = 500
# Gemini finish reasons that mean the answer was withheld rather than produced
GEMINI_BLOCKED = {"SAFETY", "RECITATION", "BLOCKLIST", "PROHIBITED_CONTENT", "SPII", "LANGUAGE"}
ADVISORY = ("Advisory only. The Advisor cannot control machinery; every action marked requires_operator_approval "
            "must be approved and carried out by the operator.")
PROVENANCE = {
    "SIM": "simulated telemetry from the UDYAM-Twin simulator (not a physical sensor)",
    "REPLAY": "a replayed row of the AI4I 2020 dataset (not live telemetry)",
    "LIVE": "a live device bound through the IoT hub",
}

SYSTEM_PROMPT = """You are UDYAM-TWIN Advisor, the decision-support assistant of a digital twin for a small \
Indian CNC job-shop (bar saw SAW-01 -> CNC lathe LATHE-01 -> CNC mill VMC-01, plus a shared air compressor COMP-01).

Get every fact from a tool: get_line_status or get_machine_state for readings, get_recent_events for alerts and \
history, query_dataset for any dataset statistic, simulate_what_if for any "should I / can I / what if" question \
(compare options), get_economics for rupee assumptions. Each tool result states its kind and provenance; keep \
those labels when you use the values.

Rules:
- Never state a number that is not in a tool result from this conversation. Never guess readings, forecasts, \
probabilities, rupee values, dataset statistics, confidence scores or remaining useful life. If something is \
unavailable, say so plainly.
- Say whether data is SIM (simulated), REPLAY (dataset row) or LIVE (sensor); never present SIM or REPLAY values as \
physical measurements. UDYAM-SIM statistics come from simulated episodes, not real machines.
- simulate_what_if results are projections of the twin's thermal physics under the stated assumptions: INFERRED, \
never measured.
- You cannot change any machine. To recommend a derate, clearing a derate or a work order, call propose_action; \
the operator must click Approve. Never say you have applied anything.
- If calibration is frozen, vibration is in zone C/D, or sensor data quality is bad, say the thermal forecast is \
less trustworthy and recommend inspection, not just derating.
- Vibration zones are configurable demo thresholds referencing ISO 10816-3, not a certified assessment. AI4I \
failure risk is a model vote share, not a calibrated probability.
- Tools report process temperatures in kelvin; show them in °C (°C = K - 273.15), like the dashboard.
- Use cautious wording for actions ("inspect", "check", "consider"). Reply in the user's language (English, Hindi \
or Hinglish). Keep it under 150 words unless asked for detail.

Finish every turn by calling submit_answer:
- reply: the answer for the operator, with **Measured**, **Inferred** and **Suggested** sections where they apply.
- measured: one line per value used from telemetry or records (value, unit, machine, provenance).
- inferred: one line per model output or simulation result (source and assumptions).
- unavailable: anything asked for that the tools could not provide.
- suggested_actions: possible next steps the evidence supports, each with a category; anything that changes how \
a machine runs is category machine_control."""


def _schema(props: dict[str, Any]) -> dict[str, Any]:
    return {"type": "object", "properties": props, "required": list(props), "additionalProperties": False}


WHAT_IF_ACTIONS = ("compare", "continue", "derate_10", "derate_20", "derate_30", "derate_40", "derate_50", "stop")
SIM_FAULTS = ("normal", "coolant_degradation", "bearing_defect", "overload", "tool_wear_accel", "sensor_stuck",
              "pressure_drop", "order_surge")

TOOLS: list[dict[str, Any]] = [
    {
        "name": "get_line_status",
        "description": "Snapshot of every machine on the line: PLC state, key readings, vibration zone, twin "
                       "residual state, forecast, risk and derate, plus line throughput, rupee loss per hour and OEE.",
        "input_schema": _schema({}),
    },
    {
        "name": "get_machine_state",
        "description": "Detailed state of one machine: latest readings (temperature, rpm, vibration, operating "
                       "state, data quality), twin prediction vs observation, calibrated parameters, forecast, rule "
                       "risk, AI4I failure risk, tool RUL estimate, and the UDYAM-SIM ML diagnosis (most likely "
                       "developing fault with probabilities).",
        "input_schema": _schema({"machine_id": {"type": "string", "description": "VMC-01, LATHE-01, SAW-01, COMP-01"}}),
    },
    {
        "name": "get_recent_events",
        "description": "The n most recent alerts and events (calibration, anomalies, forecasts, common cause, work "
                       "orders), oldest first, optionally for one machine.",
        "input_schema": _schema({
            "n": {"type": "integer", "description": "how many events, 1 to 50"},
            "machine_id": {"type": "string", "description": "a machine ID, or empty for all machines"},
        }),
    },
    {
        "name": "query_dataset",
        "description": "Counts and statistics from the datasets on this server. UDYAM-SIM (our simulated "
                       "episodes): 'overview' or a fault (normal, coolant_degradation, bearing_defect, overload, "
                       "tool_wear_accel, sensor_stuck, pressure_drop, order_surge) - how often it reached the "
                       "limit with and without auto-derate, minutes from onset to limit, parts output, model "
                       "accuracy. AI4I 2020: machine_failure, TWF, HDF, PWF, OSF, RNF - row counts, fault rate and "
                       "feature statistics. CWRU bearing data: cwru_normal, ball, inner_race, outer_race - window "
                       "and recording counts.",
        "input_schema": _schema({"fault": {"type": "string"}}),
    },
    {
        "name": "simulate_what_if",
        "description": "Run the twin's calibrated thermal physics forward for a CNC machine (VMC-01 or LATHE-01) "
                       "and compare options against continuing unchanged: minutes until the temperature limit, peak "
                       "temperature, parts produced, rupees lost, kWh and CO2. action: compare (0/20/40 % derate), "
                       "continue, derate_10..derate_50, stop. Thermal behaviour only; results are projections.",
        "input_schema": _schema({
            "machine_id": {"type": "string"},
            "action": {"type": "string", "enum": list(WHAT_IF_ACTIONS)},
            "minutes": {"type": "number", "description": "horizon, 1 to 120"},
            "parts_target": {"type": "integer", "description": "batch size to check, or 0 for none"},
        }),
    },
    {
        "name": "get_economics",
        "description": "Shop economics used for rupee estimates (contribution per part, downtime cost per hour, "
                       "tariff, CO2 grid factor). These are editable factory assumptions.",
        "input_schema": _schema({}),
    },
    {
        "name": "propose_action",
        "description": "Propose an action for the operator to approve in the UI. Does NOT execute it. derate_pct "
                       "(5-50) is used only for 'derate' (else 0); title only for 'work_order' (else empty).",
        "input_schema": _schema({
            "machine_id": {"type": "string"},
            "action": {"type": "string", "enum": ["derate", "work_order", "clear_derate"]},
            "derate_pct": {"type": "number"},
            "title": {"type": "string"},
            "reason": {"type": "string"},
        }),
    },
    {
        "name": "submit_answer",
        "description": "Deliver the final answer to the operator. Call this exactly once, as the last step of every turn.",
        "input_schema": _schema({
            "reply": {"type": "string"},
            "measured": {"type": "array", "items": {"type": "string"}},
            "inferred": {"type": "array", "items": {"type": "string"}},
            "unavailable": {"type": "array", "items": {"type": "string"}},
            "suggested_actions": {"type": "array", "items": _schema({
                "action": {"type": "string"},
                "category": {"type": "string",
                             "enum": ["inspect", "monitor", "maintenance", "machine_control", "information"]},
                "machine_id": {"type": "string", "description": "machine ID, or empty"},
                "rationale": {"type": "string"},
            })},
        }),
    },
]

def _env_secret(*names: str) -> Optional[str]:
    """First non-empty value, treating .env.example placeholders ("your-...-here") as unset."""
    for n in names:
        v = (os.environ.get(n) or "").strip()
        if v and not (v.lower().startswith("your-") or v.lower().endswith("-here")):
            return v
    return None


_OPENAI_SCHEMA_KEYS = {"type", "description", "properties", "required", "items", "enum"}


def _openai_schema(schema: Any) -> Any:
    """Keep the JSON-schema subset Gemini and Ollama accept (no strict / additionalProperties)."""
    if not isinstance(schema, dict):
        return schema
    out = {k: v for k, v in schema.items() if k in _OPENAI_SCHEMA_KEYS}
    if "properties" in out:
        out["properties"] = {k: _openai_schema(v) for k, v in out["properties"].items()}
    if "items" in out:
        out["items"] = _openai_schema(out["items"])
    return out


def _openai_tool(t: dict[str, Any]) -> dict[str, Any]:
    fn: dict[str, Any] = {"name": t["name"], "description": t["description"]}
    params = _openai_schema(t["input_schema"])
    if params.get("properties"):  # no-argument tools omit parameters (Gemini rejects an empty object schema)
        fn["parameters"] = params
    return {"type": "function", "function": fn}


OPENAI_TOOLS: list[dict[str, Any]] = [_openai_tool(t) for t in TOOLS]


def gemini_tools() -> list:
    """The same tools as Gemini function declarations (JSON-schema parameters, Gemini-safe subset)."""
    from google.genai import types

    decls = []
    for t in TOOLS:
        params = _openai_schema(t["input_schema"])
        if params.get("properties"):  # no-argument tools declare no parameters
            decls.append(types.FunctionDeclaration(name=t["name"], description=t["description"],
                                                   parameters_json_schema=params))
        else:
            decls.append(types.FunctionDeclaration(name=t["name"], description=t["description"]))
    return [types.Tool(function_declarations=decls)]

# Ground truth from the simulator (which fault / scenario was injected): the advisor must not peek,
# exactly like on a real machine where nobody tells you what is wrong.
HIDDEN_FIELDS = {"active_fault", "active_scenario"}


# --------------------------------------------------------------------------- strict tool arguments
class _Args(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class NoArgs(_Args):
    pass


class MachineArgs(_Args):
    machine_id: str = Field(min_length=2, max_length=20)


class EventsArgs(_Args):
    n: int = Field(default=10, ge=1, le=50)
    machine_id: str = Field(default="", max_length=20)


class DatasetArgs(_Args):
    fault: str = Field(default="overview", min_length=1, max_length=40)


class WhatIfArgs(_Args):
    machine_id: str = Field(min_length=2, max_length=20)
    action: Literal[WHAT_IF_ACTIONS] = "compare"  # type: ignore[valid-type]
    minutes: float = Field(default=30.0, ge=1, le=120)
    parts_target: int = Field(default=0, ge=0, le=100_000)


class ProposeArgs(_Args):
    machine_id: str = Field(min_length=2, max_length=20)
    action: Literal["derate", "work_order", "clear_derate"]
    reason: str = Field(min_length=3, max_length=500)
    derate_pct: float = Field(default=0.0, ge=0, le=50)
    title: str = Field(default="", max_length=120)

    @model_validator(mode="after")
    def _derate_needs_pct(self):
        if self.action == "derate" and not 5 <= self.derate_pct <= 50:
            raise ValueError("derate_pct must be between 5 and 50 for a derate")
        return self


class AnswerAction(_Args):
    action: str = Field(min_length=1, max_length=300)
    category: Literal["inspect", "monitor", "maintenance", "machine_control", "information"]
    machine_id: str = Field(default="", max_length=20)
    rationale: str = Field(default="", max_length=600)


class AnswerArgs(_Args):
    reply: str = Field(min_length=1, max_length=6000)
    measured: list[str] = Field(default_factory=list, max_length=40)
    inferred: list[str] = Field(default_factory=list, max_length=40)
    unavailable: list[str] = Field(default_factory=list, max_length=20)
    suggested_actions: list[AnswerAction] = Field(default_factory=list, max_length=10)


class ToolError(Exception):
    """A tool could not run with these arguments; the message goes back to the model."""


# --------------------------------------------------------------------------- configuration, errors, requests
@dataclass
class AdvisorConfig:
    gemini_key: Optional[str]
    gemini_model: Optional[str]
    gemini_sdk: bool
    gemini_timeout_s: float = 60.0
    gemini_max_retries: int = 2
    gemini_max_output_tokens: int = 8192
    max_rounds: int = 6
    offline_fallback: bool = True
    provider_pref: str = "auto"
    # Optional local model through Ollama's OpenAI-compatible endpoint
    ollama_model: Optional[str] = None
    ollama_url: str = OLLAMA_DEFAULT_URL
    llm_timeout_s: float = 60.0

    @classmethod
    def from_env(cls) -> "AdvisorConfig":
        try:
            from google import genai  # noqa: F401
            sdk = True
        except Exception:
            sdk = False
        pref = (os.environ.get("UDYAM_LLM_PROVIDER") or "auto").strip().lower()
        return cls(
            gemini_key=_env_secret("GEMINI_API_KEY"),
            gemini_model=(os.environ.get("GEMINI_MODEL") or "").strip() or None,
            gemini_sdk=sdk,
            gemini_timeout_s=float(os.environ.get("GEMINI_TIMEOUT_S", "60")),
            gemini_max_retries=int(os.environ.get("GEMINI_MAX_RETRIES", "2")),
            gemini_max_output_tokens=int(os.environ.get("GEMINI_MAX_OUTPUT_TOKENS", "8192")),
            max_rounds=int(os.environ.get("UDYAM_CHAT_MAX_TOOL_ROUNDS", "6")),
            offline_fallback=(os.environ.get("UDYAM_CHAT_OFFLINE_FALLBACK") or "1").strip() != "0",
            provider_pref=pref if pref in ("auto", "gemini", "ollama", "offline") else "auto",
            ollama_model=(os.environ.get("OLLAMA_MODEL") or "").strip() or None,
            ollama_url=((os.environ.get("OLLAMA_BASE_URL") or "").strip() or OLLAMA_DEFAULT_URL).rstrip("/"),
            llm_timeout_s=float(os.environ.get("UDYAM_LLM_TIMEOUT_S", "60")),
        )

    @property
    def gemini_ready(self) -> bool:
        return bool(self.gemini_key and self.gemini_model and self.gemini_sdk)

    def _ready(self, name: str) -> bool:
        return {"gemini": self.gemini_ready, "ollama": bool(self.ollama_model)}.get(name, False)

    @property
    def provider(self) -> str:
        """The provider this turn will use: gemini, ollama, or offline."""
        pref = self.provider_pref
        if pref in ("gemini", "ollama"):
            return pref if self._ready(pref) else "offline"
        if pref == "offline":
            return "offline"
        return next((p for p in ("gemini", "ollama") if self._ready(p)), "offline")

    @property
    def llm_ready(self) -> bool:
        return self.provider != "offline"

    @property
    def llm_model(self) -> Optional[str]:
        return {"gemini": self.gemini_model, "ollama": self.ollama_model}.get(self.provider)

    def gemini_not_ready_reason(self) -> str:
        if not self.gemini_key:
            return "GEMINI_API_KEY is not set on the server (key: aistudio.google.com/apikey)."
        if not self.gemini_model:
            return "GEMINI_MODEL is not set on the server (a model ID your API key can use)."
        return "The Google Gen AI SDK is not installed (pip install google-genai)."

    def llm_not_ready_reason(self) -> str:
        pref = self.provider_pref
        if pref == "offline":
            return "UDYAM_LLM_PROVIDER=offline: rule-based answers only."
        if pref == "ollama":
            return "OLLAMA_MODEL is not set on the server (e.g. qwen2.5:7b after `ollama pull qwen2.5:7b`)."
        if pref == "gemini" or self.gemini_key or self.gemini_model:
            return self.gemini_not_ready_reason()
        return "No chat model configured: set GEMINI_API_KEY and GEMINI_MODEL (or OLLAMA_MODEL for a local model)."


class ChatError(Exception):
    def __init__(self, status: int, code: str, message: str, **extra: Any):
        super().__init__(message)
        self.status, self.code, self.message, self.extra = status, code, message, extra

    def detail(self) -> dict[str, Any]:
        return {"error": self.code, "message": self.message, **self.extra}


class ChatRequest(BaseModel):
    message: str = Field(max_length=4000)
    machine_id: Optional[str] = Field(default=None, max_length=20)
    conversation_id: Optional[str] = Field(default=None, pattern=r"^[0-9a-f]{32}$")
    # Older clients send the transcript; used only to seed a new conversation.
    history: Optional[list[dict[str, Any]]] = Field(default=None, max_length=40)

    @field_validator("message")
    @classmethod
    def _not_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("message must not be empty")
        return v.strip()


@dataclass
class Conversation:
    messages: list = field(default_factory=list)
    user_turns: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock)


class ConversationStore:
    """In-memory and bounded. Turns are stored as plain text, so the provider can change between turns."""

    def __init__(self, max_conversations: int = MAX_CONVERSATIONS):
        self._items: "OrderedDict[str, Conversation]" = OrderedDict()
        self._lock = threading.Lock()
        self.max = max_conversations

    def get_or_create(self, conversation_id: Optional[str]) -> tuple[str, Conversation, bool]:
        with self._lock:
            if conversation_id is None:
                cid = uuid.uuid4().hex
                self._items[cid] = Conversation()
                while len(self._items) > self.max:
                    self._items.popitem(last=False)
                return cid, self._items[cid], True
            conv = self._items.get(conversation_id)
            if conv is None:
                raise ChatError(404, "unknown_conversation", "Conversation not found (it may have expired). Start a new one.")
            self._items.move_to_end(conversation_id)
            return conversation_id, conv, False


def make_client(cfg: AdvisorConfig):
    """The Gemini client (tests replace this). The key is passed explicitly and never logged or returned."""
    from google import genai
    from google.genai import types

    return genai.Client(api_key=cfg.gemini_key, http_options=types.HttpOptions(
        timeout=int(cfg.gemini_timeout_s * 1000),  # milliseconds
        retry_options=types.HttpRetryOptions(attempts=cfg.gemini_max_retries + 1),
    ))


def _retry_after_s(details: Any) -> Optional[int]:
    """Seconds from a google.rpc.RetryInfo detail (e.g. "retryDelay": "7s"), if Gemini sent one."""
    try:
        for d in (details or {}).get("error", {}).get("details", []) or []:
            delay = d.get("retryDelay") if isinstance(d, dict) else None
            if isinstance(delay, str) and delay.endswith("s"):
                return max(1, int(float(delay[:-1])))
    except (AttributeError, TypeError, ValueError):
        pass
    return None


# --------------------------------------------------------------------------- number guard
_ID_RE = re.compile(r"\b[A-Z]{2,6}-\d{1,3}\b")
_NUM_RE = re.compile(r"(?<![\w.])-?\d+(?:\.\d+)?")


def _numbers_in(obj: Any, out: set[float]) -> None:
    if isinstance(obj, bool):
        return
    if isinstance(obj, (int, float)):
        out.add(float(obj))
    elif isinstance(obj, str):
        out.update(float(m) for m in _NUM_RE.findall(_ID_RE.sub(" ", obj)))
    elif isinstance(obj, dict):
        for v in obj.values():
            _numbers_in(v, out)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            _numbers_in(v, out)


def unverified_numbers(text: str, evidence: list[dict[str, Any]]) -> list[str]:
    """Numbers in the reply that match no value the tools returned, at the precision written.

    Kelvin values are also matched as °C, fractions as percentages; integers 0-10 are ignored (counts, ordinals).
    """
    known: set[float] = set()
    for e in evidence:
        _numbers_in(e.get("result"), known)
    known |= {v * 100 for v in known} | {v - K_TO_C for v in known if v > 200}
    flagged: list[str] = []
    for tok in _NUM_RE.findall(_ID_RE.sub(" ", text)):
        x = float(tok)
        if x.is_integer() and 0 <= x <= 10:
            continue
        nd = len(tok.split(".")[1]) if "." in tok else 0
        tol = 0.5 * 10 ** (-nd) + 1e-9
        if not any(abs(v - x) <= tol for v in known) and tok not in flagged:
            flagged.append(tok)
    return flagged


# --------------------------------------------------------------------------- datasets
AI4I_ALIASES = {
    "machine_failure": "machine_failure", "failure": "machine_failure", "ai4i": "machine_failure",
    "twf": "TWF", "tool_wear_failure": "TWF", "hdf": "HDF", "heat_dissipation": "HDF", "heat_dissipation_failure": "HDF",
    "pwf": "PWF", "power_failure": "PWF", "osf": "OSF", "overstrain": "OSF", "overstrain_failure": "OSF",
    "rnf": "RNF", "random_failure": "RNF",
}
CWRU_ALIASES = {"cwru_normal": "normal", "ball": "ball", "cwru_ball": "ball", "inner_race": "inner_race",
                "cwru_inner_race": "inner_race", "outer_race": "outer_race", "cwru_outer_race": "outer_race"}


@lru_cache(maxsize=1)
def _ai4i():
    from ml.ai4i import load_dataset
    return load_dataset()


def _ai4i_stats(frame) -> dict[str, Any]:
    from ml.ai4i import FEATURES
    out = {}
    for f in FEATURES:
        col = frame[f] - K_TO_C if f.endswith("_k") else frame[f]
        name = f.removesuffix("_k") + "_c" if f.endswith("_k") else f
        out[name] = {s: round(float(getattr(col, s)()), 2) for s in ("mean", "std", "min", "median", "max")}
    return out


def _query_ai4i(target: str) -> dict[str, Any]:
    from ml.ai4i import MODE_INFO, SOURCE_URL, DatasetError
    try:
        df = _ai4i()
    except DatasetError as err:
        raise ToolError(f"AI4I 2020 dataset unavailable: {err}")
    pos, neg = df[df[target] == 1], df[df[target] == 0]
    return {
        "kind": "dataset",
        "dataset": "AI4I 2020 Predictive Maintenance Dataset (UCI, CC BY 4.0; itself synthetic, modelled on a milling process)",
        "source": SOURCE_URL,
        "fault": target,
        "fault_label": "Machine failure" if target == "machine_failure" else MODE_INFO[target],
        "rows_total": int(len(df)),
        "rows_with_fault": int(len(pos)),
        "fault_rate": round(len(pos) / len(df), 4),
        "by_product_variant": {v: {"rows": int((df.variant_type == v).sum()), "with_fault": int((pos.variant_type == v).sum())}
                               for v in sorted(df.variant_type.unique())},
        "feature_stats_with_fault": _ai4i_stats(pos) if len(pos) else None,
        "feature_stats_without_fault": _ai4i_stats(neg),
        "units": {"air_temp_c": "°C", "process_temp_c": "°C", "rpm": "rpm", "torque_nm": "N·m", "tool_wear_min": "min"},
    }


def _query_cwru(label: str) -> dict[str, Any]:
    import pandas as pd
    if not CWRU_MANIFEST.exists():
        raise ToolError("The CWRU bearing dataset is not present on this server.")
    m = pd.read_csv(CWRU_MANIFEST)
    sub = m[m["label"] == label]
    return {
        "kind": "dataset",
        "dataset": "CWRU Bearing Data Center, 12k drive-end subset (one laboratory test rig)",
        "fault": label,
        "windows_total": int(len(m)),
        "windows_with_label": int(len(sub)),
        "label_share": round(len(sub) / len(m), 4) if len(m) else None,
        "recordings": int(sub["source_file"].nunique()),
        "by_load": {str(k): int(v) for k, v in sub["group_id"].value_counts().sort_index().items()},
        "sampling_rate_hz": sorted(int(x) for x in sub["sampling_rate_hz"].unique()),
    }


# --------------------------------------------------------------------------- the advisor
class TwinAdvisor:
    def __init__(
        self,
        engine,
        economics: Callable[[], dict] | None = None,
        events: Callable[[int], list[dict]] | None = None,
        analytics: Callable[[], dict] | None = None,
        telemetry_mode: Callable[[], str] | None = None,
        client_factory: Callable[[AdvisorConfig], Any] = make_client,
    ):
        self.engine = engine
        self._economics = economics or (lambda: {})
        self._analytics = analytics
        self._events_db = events
        self._telemetry_mode = telemetry_mode or (lambda: getattr(engine, "mode", "SIM"))
        self.client_factory = client_factory
        self.buffers: dict[str, deque] = {}
        self.h_hist: dict[str, deque] = {}
        self.last_line: dict = {}
        self.events: deque = deque(maxlen=60)
        self.predictor = SimPredictor()
        self._pressure = 7.0
        self._knowledge: dict | None = None
        self.store = ConversationStore()
        # Proposals waiting for an operator (proposal_id -> proposal); executed only via approve_proposal
        self.proposals: "OrderedDict[str, dict]" = OrderedDict()
        self._proposal_lock = threading.Lock()
        self._tools: dict[str, tuple[type[_Args], Callable[[Any], dict]]] = {
            "get_line_status": (NoArgs, lambda a: self.tool_get_line_status()),
            "get_machine_state": (MachineArgs, lambda a: self.tool_get_machine_state(a.machine_id)),
            "get_recent_events": (EventsArgs, lambda a: self.tool_get_recent_events(a.n, a.machine_id or None)),
            "query_dataset": (DatasetArgs, lambda a: self.tool_query_dataset(a.fault)),
            "simulate_what_if": (WhatIfArgs, lambda a: self.tool_simulate_what_if(a.machine_id, a.action, a.minutes,
                                                                                  a.parts_target or None)),
            "get_economics": (NoArgs, lambda a: self.tool_get_economics()),
            "propose_action": (ProposeArgs, lambda a: self.tool_propose_action(
                a.machine_id, a.action, a.reason, a.derate_pct if a.action == "derate" else None, a.title or None)),
        }

    # ------------------------------------------------------------------ live feed
    def observe(self, msgs: list[dict]) -> None:
        """Call with every batch of messages the engine emits."""
        pending_obs: dict[str, dict] = {}
        for m in msgs:
            t = m.get("type")
            if t == "observed":
                mid = m["machine_id"]
                if m.get("pressure_bar") is not None:
                    self._pressure = float(m["pressure_bar"])
                pending_obs[mid] = m
            elif t == "twin":
                mid = m["machine_id"]
                obs = pending_obs.pop(mid, None)
                if obs is None:
                    continue
                buf = self.buffers.setdefault(mid, deque(maxlen=BUFFER_TICKS))
                buf.append({"obs": obs, "twin": m, "pressure_bar": self._pressure})
                h = (m.get("params") or {}).get("values", {}).get("h_cool")
                if h is not None:
                    self.h_hist.setdefault(mid, deque(maxlen=BUFFER_TICKS)).append((self.engine.t, float(h)))
            elif t == "line":
                self.last_line = m
            elif t == "event":
                self.events.append(m)

    # ------------------------------------------------------------------ limits (config/machines.yaml)
    def _temp_limit(self, mid: str) -> float | None:
        plant = self.engine.plants.get(mid)
        v = (plant.limits if plant else {}).get("process_temp_limit_k")
        return float(v) if v is not None else None

    def _zones(self, mid: str) -> dict | None:
        plant = self.engine.plants.get(mid)
        if plant is None:
            return None
        z = get_config().risk_zones(mid, plant.template)
        return {"C_starts_mm_s": z[0], "D_starts_mm_s": z[1],
                "basis": "configurable demo thresholds referencing ISO 10816-3"} if z else None

    def _snap(self, mid: str) -> dict:
        snap = (getattr(self.engine, "snapshots", None) or {}).get(mid) or {}
        keep = ("mode", "state", "temp_c", "load_pct", "energy_kwh", "severity", "rul")
        return {k: snap.get(k) for k in keep if snap.get(k) is not None}

    def _check_machine(self, machine_id: str) -> None:
        if not MACHINE_ID_RE.match(machine_id) or machine_id not in self.engine.plants:
            raise ToolError(f"Unknown machine '{machine_id}'. Machines on this line: {', '.join(self.engine.plants)}.")

    # ------------------------------------------------------------------ tools
    def _latest(self, mid: str) -> dict | None:
        buf = self.buffers.get(mid)
        return buf[-1] if buf else None

    @staticmethod
    def _clean_obs(obs: dict) -> dict:
        return {k: v for k, v in obs.items() if k not in HIDDEN_FIELDS and k not in ("type", "schema_version")}

    def tool_get_line_status(self) -> dict:
        machines = []
        oee = {}
        if self._analytics:
            try:
                oee = {m: (v or {}).get("oee") for m, v in (self._analytics() or {}).get("machines", {}).items()}
            except Exception:
                oee = {}
        for mid in self.engine.plants:
            f = self._latest(mid)
            if not f:
                continue
            o, tw = f["obs"], f["twin"]
            sev = self._snap(mid).get("severity") or {}
            machines.append({
                "machine_id": mid,
                "source_mode": o.get("source_mode"),
                "plc_state": o.get("plc_state"),
                "process_temp_k": o.get("process_temp_k"),
                "vibration_rms_mm_s": o.get("vibration_rms_mm_s"),
                "pressure_bar": o.get("pressure_bar"),
                "derate_pct": o.get("derate_pct"),
                "vibration_zone": sev.get("zone"),
                "residual_state": (tw.get("residual") or {}).get("state"),
                "forecast": tw.get("forecast"),
                "risk_rules": (tw.get("risk") or {}).get("rules"),
                "oee": (oee.get(mid) or {}).get("oee"),
            })
        line = {k: self.last_line.get(k) for k in
                ("throughput_parts_per_hr", "line_loss_inr_per_hr", "bottleneck", "air_supply_ok")}
        return {"kind": "measured", "telemetry_mode": self._telemetry_mode(),
                "provenance": PROVENANCE.get(self._telemetry_mode(), "telemetry"),
                "machines": machines, "line": line,
                "oee_note": "OEE = availability x performance x quality since the last reset (rule-based quality)"}

    def tool_get_machine_state(self, machine_id: str) -> dict:
        self._check_machine(machine_id)
        f = self._latest(machine_id)
        if not f:
            raise ToolError(f"No data yet for {machine_id}")
        tw = f["twin"]
        risk = dict(tw.get("risk") or {})
        ml = risk.pop("ml", None) or {}
        risk["ai4i"] = {k: ml.get(k) for k in ("status", "failure_risk_pct", "top_mode", "note") if ml.get(k) is not None}
        src = f["obs"].get("source_mode")
        out = {
            "kind": "measured",
            "provenance": PROVENANCE.get(src, "telemetry"),
            "observed": self._clean_obs(f["obs"]),
            "twin": {"kind": "inferred", **{k: tw.get(k) for k in ("predicted", "residual", "params", "forecast", "rul")},
                     "risk": risk},
            "dashboard": self._snap(machine_id),
            "temp_limit_k": self._temp_limit(machine_id),
            "vibration_zones": self._zones(machine_id),
        }
        if machine_id in ("VMC-01", "LATHE-01"):
            diag = self.predictor.predict(list(self.buffers[machine_id]))
            if diag:
                if machine_id != "VMC-01":
                    diag["note"] = "Model was trained on the VMC; treat lathe diagnosis as lower confidence."
                out["ml_diagnosis"] = {"kind": "inferred", "source": "UDYAM-SIM model (ml/sim_predictor.py)", **diag}
            else:
                out["ml_diagnosis"] = {"unavailable": self.predictor.error or "need 60 s of data"}
        return out

    def tool_get_recent_events(self, n: int = 10, machine_id: str | None = None) -> dict:
        if machine_id:
            self._check_machine(machine_id)
        evs = list(self.events)
        if not evs and self._events_db:
            try:
                evs = list(reversed(self._events_db(n * 2)))
            except Exception:
                evs = []
        if machine_id:
            evs = [e for e in evs if e.get("machine_id") == machine_id]
        keep = ("ts", "machine_id", "kind", "severity", "title", "measured", "inferred", "suggested")
        evs = [e for e in evs if e.get("kind") != "FAULT_INJECTED"]
        return {"kind": "measured", "source": "UDYAM-Twin event stream",
                "events": [{k: e.get(k) for k in keep if e.get(k) is not None} for e in evs[-n:]]}

    def knowledge(self) -> dict:
        if self._knowledge is None:
            try:
                self._knowledge = json.loads(KNOWLEDGE_PATH.read_text())
            except Exception:
                self._knowledge = {}
        return self._knowledge

    def tool_query_dataset(self, fault: str = "overview") -> dict:
        key = fault.strip().lower().replace(" ", "_").replace("-", "_")
        if key in ("overview", *SIM_FAULTS):
            k = self.knowledge()
            if not k:
                raise ToolError("UDYAM-SIM knowledge not found. Run: python -m tools.generate_dataset && python -m ml.train_sim")
            base = {"kind": "dataset", "dataset": k.get("dataset"), "data_kind": "simulated episodes (synthetic)",
                    "episodes": k.get("episodes_in_distribution"), "minutes_per_episode": k.get("minutes_per_episode")}
            if key == "overview":
                return {**base, "faults": k.get("faults"), "model_accuracy": k.get("model_summary"),
                        "top_features": k.get("top_features")}
            stats = (k.get("faults") or {}).get(key)
            if stats is None:
                raise ToolError(f"UDYAM-SIM has no statistics for '{key}'.")
            return {**base, "fault": key, "stats": stats, "model_accuracy": k.get("model_summary")}
        if key in AI4I_ALIASES:
            return _query_ai4i(AI4I_ALIASES[key])
        if key in CWRU_ALIASES:
            return _query_cwru(CWRU_ALIASES[key])
        raise ToolError(f"Unknown fault '{fault}'. UDYAM-SIM: overview, {', '.join(SIM_FAULTS)}; AI4I 2020: "
                        "machine_failure, TWF, HDF, PWF, OSF, RNF; CWRU: cwru_normal, ball, inner_race, outer_race.")

    def tool_get_economics(self) -> dict:
        try:
            return {"kind": "assumption", "note": "editable factory assumptions, not measurements", **(self._economics() or {})}
        except Exception as err:
            raise ToolError(f"Economics unavailable: {err}")

    def _h_rate(self, mid: str) -> float:
        """Recent drift of the calibrated cooling coefficient (per second, <= 0)."""
        hist = list(self.h_hist.get(mid, []))
        if len(hist) < 20:
            return 0.0
        t0, h0 = hist[0]
        t1, h1 = hist[-1]
        if t1 - t0 <= 0:
            return 0.0
        return min(0.0, (h1 - h0) / (t1 - t0))

    @staticmethod
    def _what_if_options(action: str) -> list[float]:
        if action == "compare":
            return [0.0, 20.0, 40.0]
        if action == "continue":
            return [0.0]
        if action == "stop":
            return [0.0, 100.0]
        return [0.0, float(action.split("_")[1])]

    def tool_simulate_what_if(self, machine_id: str, action: str = "compare", minutes: float = 30.0,
                              parts_target: int | None = None) -> dict:
        self._check_machine(machine_id)
        if machine_id not in ("VMC-01", "LATHE-01") and self.engine.plants[machine_id].template not in ("cnc_mill", "cnc_lathe"):
            raise ToolError(f"The what-if model covers CNC thermal behaviour only; {machine_id} is a "
                            f"{self.engine.plants[machine_id].template}. No simulation is available for it.")
        if getattr(self.engine, "replay_machine", None) == machine_id:
            raise ToolError(f"{machine_id} is in REPLAY mode (AI4I rows are independent samples, not a process the "
                            "twin can project). Switch to SIM to run a what-if.")
        f = self._latest(machine_id)
        if not f:
            raise ToolError(f"No data yet for {machine_id}")
        o, tw = f["obs"], f["twin"]
        options = self._what_if_options(action)
        minutes = float(minutes or 30.0)
        econ = self._economics() or {}
        inr_part = float(econ.get("inr_per_part", 450.0))
        down_hr = float(econ.get("downtime_cost_per_hr", 2500.0))
        tariff = float(econ.get("electricity_tariff_per_kwh", 8.5))
        co2f = float(econ.get("cea_co2_factor", 0.716))

        params = (tw.get("params") or {})
        h0 = float(params.get("values", {}).get("h_cool", 1.0))
        frozen = bool(params.get("frozen"))
        h_rate = 0.0 if frozen else self._h_rate(machine_id)
        temp0 = (tw.get("predicted") or {}).get("process_temp_k") or o.get("process_temp_k") or 311.5
        air = float(o.get("air_temp_k") or 300.0)
        d_now = float(o.get("derate_pct") or 0.0)
        p_now = float(o.get("power_kw") or 0.0)
        p_full = p_now / max(0.05, 1.0 - d_now / 100.0)
        cycle = CYCLE_S.get(machine_id, 30.0)
        dt = TICK_S
        temp_limit = self._temp_limit(machine_id) or 315.5

        results = []
        for d in options:
            d = max(0.0, min(100.0, float(d)))
            speed = 1.0 - d / 100.0
            power = p_full * speed
            T, h, peak, t_limit = float(temp0), h0, float(temp0), None
            steps = int(minutes * 60 / dt)
            for k in range(steps):
                h = max(0.55, h + h_rate * dt)
                target = air + 8.0 + 0.7 * power / h
                T += (target - T) * dt / 20.0
                peak = max(peak, T)
                if t_limit is None and T >= temp_limit:
                    t_limit = (k + 1) * dt / 60.0
            run_min = t_limit if t_limit is not None else minutes
            parts = int(run_min * 60 / cycle * speed)
            stopped_min = minutes - run_min
            kwh = power * run_min / 60.0
            r = {
                "derate_pct": d,
                "minutes_to_temp_limit": round(t_limit, 1) if t_limit is not None else None,
                "peak_temp_k": round(peak, 2),
                "parts_in_window": parts,
                "machine_stops_on_limit": t_limit is not None,
                "kwh": round(kwh, 2),
                "energy_inr": round(kwh * tariff, 0),
                "co2_kg": round(kwh * co2f, 2),
                "downtime_inr": round(stopped_min / 60.0 * down_hr, 0),
            }
            if parts_target:
                r["reaches_parts_target"] = parts >= parts_target
                need = parts_target * cycle / max(0.01, speed) / 60.0
                r["minutes_needed_for_target"] = round(need, 1) if speed > 0 else None
            results.append(r)
        best_parts = max(r["parts_in_window"] for r in results)
        for r in results:
            r["parts_lost_vs_best"] = best_parts - r["parts_in_window"]
            r["total_inr_impact"] = round(r["parts_lost_vs_best"] * inr_part + r["downtime_inr"], 0)
        caveats = []
        vib = float(o.get("vibration_rms_mm_s") or 0.0)
        zones = self._zones(machine_id)
        if zones and vib >= zones["C_starts_mm_s"]:
            caveats.append(f"Vibration {vib} mm/s is in zone C/D (ISO-referenced demo thresholds): likely "
                           "mechanical. Derating does not fix a bearing; inspect before continuing.")
        if frozen:
            caveats.append(f"Calibration is frozen ({params.get('freeze_reason')}); forecast is less trustworthy.")
        if o.get("dq_flag") not in (None, "OK"):
            caveats.append(f"Temperature sensor data quality is {o.get('dq_flag')}.")
        return {
            "kind": "inferred",
            "machine_id": machine_id,
            "action": action,
            "source_mode": o.get("source_mode"),
            "method": "Twin's calibrated thermal physics run forward (h_cool trend extrapolated). Thermal only. "
                      "If the limit is reached the machine is assumed stopped for the rest of the window.",
            "start": {"temp_k": round(float(temp0), 2), "h_cool": round(h0, 3),
                      "h_cool_trend_per_min": round(h_rate * 60, 4), "current_derate_pct": d_now},
            "horizon_min": minutes,
            "temp_limit_k": temp_limit,
            "options": results,
            "assumptions": {"inr_per_part": inr_part, "downtime_cost_per_hr": down_hr,
                            "tariff_inr_per_kwh": tariff, "co2_kg_per_kwh": co2f,
                            "cycle_time_s": cycle},
            "caveats": caveats,
            "limitations": ["Thermal behaviour only: vibration, tool wear and air supply are not projected."],
        }

    def tool_propose_action(self, machine_id: str, action: str, reason: str,
                            derate_pct: float | None = None, title: str | None = None) -> dict:
        self._check_machine(machine_id)
        now = time.time()
        p = {"kind": "proposal", "proposal_id": uuid.uuid4().hex, "machine_id": machine_id, "action": action,
             "reason": reason, "requires_approval": True, "executed": False, "status": "pending_approval",
             "expires_in_s": PROPOSAL_TTL_S,
             "approval": "An operator must approve this in the Advisor panel; nothing has been applied."}
        if action == "derate":
            p["derate_pct"] = float(derate_pct if derate_pct is not None else 20.0)
        if action == "work_order":
            p["title"] = title or f"Inspect {machine_id}"
        with self._proposal_lock:
            self.proposals[p["proposal_id"]] = {**p, "_created": now}
            while len(self.proposals) > MAX_PROPOSALS:
                self.proposals.popitem(last=False)
        return p

    # ------------------------------------------------------------------ operator approval (see api/app.py)
    def claim_proposal(self, proposal_id: str, decision: str, operator: str) -> dict:
        """Move a pending proposal to approved/dismissed exactly once. Raises ChatError otherwise."""
        with self._proposal_lock:
            p = self.proposals.get(proposal_id)
            if p is None:
                raise ChatError(404, "unknown_proposal", "Proposal not found (it may have expired or the server restarted).")
            if p["status"] != "pending_approval":
                raise ChatError(409, "proposal_already_decided", f"This proposal was already {p['status']}.")
            if time.time() - p["_created"] > PROPOSAL_TTL_S:
                p["status"] = "expired"
                raise ChatError(410, "proposal_expired", "This proposal expired; ask the Advisor again for current data.")
            if p["machine_id"] not in self.engine.plants:
                raise ChatError(409, "machine_gone", f"{p['machine_id']} is no longer on the line.")
            p.update(status=decision, decided_by=operator, decided_at=time.time())
            return {k: v for k, v in p.items() if not k.startswith("_")}

    def record_execution(self, proposal_id: str, result: dict) -> None:
        with self._proposal_lock:
            p = self.proposals.get(proposal_id)
            if p is not None:
                p.update(executed=True, status="executed", result=result)

    # ------------------------------------------------------------------ dispatcher
    def dispatch(self, name: str, raw_input: Any) -> tuple[bool, dict[str, Any]]:
        """Run one tool call from the fixed table. Returns (ok, payload); never raises for bad input."""
        entry = self._tools.get(name)
        if entry is None:
            return False, {"error": f"Unknown tool '{name}'. Available tools: {', '.join(self._tools)}."}
        if not isinstance(raw_input, dict):
            return False, {"error": "Tool input must be a JSON object."}
        model, fn = entry
        try:
            args = model.model_validate(raw_input)
        except ValidationError as err:
            problems = "; ".join(f"{'.'.join(str(p) for p in e['loc']) or 'input'}: {e['msg']}" for e in err.errors())
            return False, {"error": f"Invalid arguments for {name}: {problems}"}
        try:
            return True, fn(args)
        except ToolError as err:
            return False, {"error": str(err)}

    # ------------------------------------------------------------------ status
    def status(self) -> dict:
        """What the chat panel needs to know. Never includes a key."""
        cfg = AdvisorConfig.from_env()
        provider = cfg.provider
        return {
            "provider": provider,
            "provider_label": PROVIDER_LABEL[provider],
            "llm_ready": cfg.llm_ready,
            "llm_model": cfg.llm_model,
            "llm_not_ready_reason": None if cfg.llm_ready else cfg.llm_not_ready_reason(),
            "gemini_ready": cfg.gemini_ready,
            "gemini_key_set": bool(cfg.gemini_key),
            "gemini_model": cfg.gemini_model,
            "gemini_sdk_installed": cfg.gemini_sdk,
            "ollama_model": cfg.ollama_model,
            "offline_fallback": cfg.offline_fallback,
            "approval_requires_token": bool(_env_secret("UDYAM_OPERATOR_TOKEN")),
            "sim_model_loaded": self.predictor.available,
            "sim_knowledge_loaded": bool(self.knowledge()),
        }

    # ------------------------------------------------------------------ chat
    async def chat(self, req: ChatRequest) -> dict:
        """One operator turn. Raises ChatError for bad input (and for provider failures if offline fallback is off)."""
        t0 = time.time()
        cfg = AdvisorConfig.from_env()
        if req.machine_id is not None:
            try:
                self._check_machine(req.machine_id)
            except ToolError:
                raise ChatError(404, "unknown_machine", f"Unknown machine '{req.machine_id}'.",
                                valid_machine_ids=list(self.engine.plants))
        cid, conv, new = self.store.get_or_create(req.conversation_id)
        if not conv.lock.acquire(blocking=False):
            raise ChatError(409, "conversation_busy", "This conversation is still answering the previous message.")
        try:
            if conv.user_turns >= MAX_USER_TURNS:
                raise ChatError(409, "conversation_too_long",
                                f"This conversation reached {MAX_USER_TURNS} messages. Start a new one.")
            if new and req.history:
                conv.messages = _seed_history(req.history)
            text = req.message if req.machine_id is None else f"[Operator is viewing {req.machine_id}]\n{req.message}"
            result, reason = None, None
            provider = cfg.provider
            if provider != "offline":
                try:
                    if provider == "gemini":
                        result = await self._chat_gemini(cfg, conv.messages, text)
                    else:
                        result = await self._chat_openai(cfg, provider, conv.messages, text)
                except ChatError as err:
                    if not cfg.offline_fallback:
                        raise
                    reason = {**err.detail(), "provider": provider}
            else:
                reason = {"error": "chat_not_configured", "message": cfg.llm_not_ready_reason()}
                if not cfg.offline_fallback:
                    raise ChatError(503, reason["error"], reason["message"])
            if result is None:
                result = self._chat_offline(req.message, req.machine_id)
                result["messages"] = conv.messages + [{"role": "user", "content": text},
                                                      {"role": "assistant", "content": result["reply"]}]
            conv.messages = result.pop("messages")
            conv.user_turns += 1
        finally:
            conv.lock.release()
        return self._response(cid, req, result, reason, time.time() - t0)

    def _response(self, cid: str, req: ChatRequest, r: dict, reason: Optional[dict], elapsed: float) -> dict:
        actions = []
        for p in r["proposals"]:
            what = {"derate": f"Derate {p['machine_id']} to {p.get('derate_pct', 0):.0f} %",
                    "clear_derate": f"Clear the derate on {p['machine_id']}",
                    "work_order": f"Work order: {p.get('title') or p['machine_id']}"}[p["action"]]
            actions.append({"action": what, "category": "maintenance" if p["action"] == "work_order" else "machine_control",
                            "machine_id": p["machine_id"], "rationale": p.get("reason", ""),
                            "requires_operator_approval": True, "executed": False, "proposal": p})
        for a in r.get("suggested", []):
            actions.append({**a, "machine_id": a.get("machine_id") or None,
                            "requires_operator_approval": a.get("category") == "machine_control", "executed": False})
        notice = None
        if reason:
            label = PROVIDER_LABEL.get(reason.get("provider", "gemini"), "The chat model")
            notice = (f"Offline mode: {reason['message']}" if reason["error"] == "chat_not_configured"
                      else f"{label} unavailable ({reason['error']}: {reason.get('message', '')}); "
                           "answered offline from the twin.")
        tools_used = [e["tool"] for e in r["evidence"]]
        return {
            "reply": r["reply"],
            "conversation_id": cid,
            "mode": r["mode"],
            "telemetry_mode": self._telemetry_mode(),
            "machine_id": req.machine_id,
            "measured": r.get("measured", []),
            "inferred": r.get("inferred", []),
            "unavailable": r.get("unavailable", []),
            "evidence": r["evidence"],
            "suggested_actions": actions,
            "requires_approval": any(a["requires_operator_approval"] for a in actions),
            "proposals": r["proposals"],
            "tools_used": tools_used,
            "structured": r.get("structured", r["mode"] == "offline"),
            "checks": {"unverified_numbers": unverified_numbers(r["reply"], r["evidence"])},
            "fallback_reason": reason,
            "notice": notice,
            "advisory_notice": ADVISORY,
            "model": r.get("model"),
            "stop_reason": r.get("stop_reason"),
            "tool_rounds": r.get("tool_rounds", 0),
            "latency_s": round(elapsed, 1),
        }

    async def _call_gemini(self, client: Any, cfg: AdvisorConfig, contents: list, config: Any) -> Any:
        from google.genai import errors

        try:
            import httpx
            transient: tuple = (httpx.TransportError,)
            timeouts: tuple = (httpx.TimeoutException,)
        except ImportError:  # pragma: no cover - httpx is a google-genai dependency
            transient, timeouts = (), ()
        try:
            import httpx2
            transient += (httpx2.TransportError,)
            timeouts += (httpx2.TimeoutException,)
        except ImportError:
            pass
        try:
            return await client.aio.models.generate_content(model=cfg.gemini_model, contents=contents, config=config)
        except errors.APIError as err:
            code, status, msg = err.code or 0, (err.status or "").upper(), err.message or ""
            if code == 429 or status == "RESOURCE_EXHAUSTED":
                raise ChatError(429, "provider_rate_limited", "Gemini quota or rate limit reached. Try again shortly.",
                                retry_after_s=_retry_after_s(err.details))
            if code in (401, 403) or status in ("UNAUTHENTICATED", "PERMISSION_DENIED") or (
                    code == 400 and "api key" in msg.lower()):
                raise ChatError(502, "provider_auth_failed", "Gemini rejected the server's API key (GEMINI_API_KEY).")
            if code == 404 or status == "NOT_FOUND":
                raise ChatError(502, "model_unavailable", f"The model '{cfg.gemini_model}' is not available to this API "
                                                          "key. Check GEMINI_MODEL.")
            if code == 504 or status == "DEADLINE_EXCEEDED":
                raise ChatError(504, "provider_timeout", f"Gemini did not answer within {cfg.gemini_timeout_s:g} s.")
            if 400 <= code < 500:
                raise ChatError(502, "provider_bad_request", f"Gemini rejected the request: {msg[:300]}")
            raise ChatError(502, "provider_error", f"Gemini returned HTTP {code}.")
        except errors.UnknownApiResponseError:
            raise ChatError(502, "malformed_provider_response", "Gemini returned a response the SDK could not read.")
        except timeouts:
            raise ChatError(504, "provider_timeout", f"Gemini did not answer within {cfg.gemini_timeout_s:g} s.")
        except transient:
            raise ChatError(503, "provider_unreachable", "Could not reach the Gemini API. Check the network.")

    async def _chat_gemini(self, cfg: AdvisorConfig, history: list, text: str) -> dict:
        """Gemini function-calling loop: same tools, dispatcher, evidence and submit_answer as every provider."""
        from google.genai import types

        client = self.client_factory(cfg)
        prior = [{"role": m["role"], "content": m["content"]} for m in history
                 if m.get("role") in ("user", "assistant") and isinstance(m.get("content"), str)]
        contents = [types.Content(role="model" if m["role"] == "assistant" else "user",
                                  parts=[types.Part.from_text(text=m["content"])]) for m in prior]
        contents.append(types.Content(role="user", parts=[types.Part.from_text(text=text)]))
        config = types.GenerateContentConfig(
            system_instruction=SYSTEM_PROMPT,
            tools=gemini_tools(),
            tool_config=types.ToolConfig(function_calling_config=types.FunctionCallingConfig(mode="AUTO")),
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),  # the server runs tools
            max_output_tokens=cfg.gemini_max_output_tokens,
        )
        evidence: list[dict] = []
        proposals: list[dict] = []
        answer: Optional[AnswerArgs] = None
        final_text, finish, rounds, model_used = "", None, 0, cfg.gemini_model
        while answer is None:
            if rounds >= cfg.max_rounds:
                raise ChatError(502, "tool_round_limit", f"Gemini did not finish within {cfg.max_rounds} tool rounds.")
            rounds += 1
            resp = await self._call_gemini(client, cfg, contents, config)
            model_used = getattr(resp, "model_version", None) or model_used
            candidates = getattr(resp, "candidates", None)
            if not candidates:
                block = getattr(getattr(resp, "prompt_feedback", None), "block_reason", None)
                if block:
                    final_text, finish = "Gemini declined to answer this request.", f"BLOCKED_{getattr(block, 'name', block)}"
                    break
                raise ChatError(502, "malformed_provider_response", "Gemini returned no candidates.")
            cand = candidates[0]
            fr = getattr(cand, "finish_reason", None)
            finish = getattr(fr, "name", None) or (str(fr) if fr is not None else None)
            content = getattr(cand, "content", None)
            parts = list(getattr(content, "parts", None) or [])
            if finish in GEMINI_BLOCKED and not parts:
                final_text = "Gemini declined to answer this request."
                break
            if finish == "MAX_TOKENS":
                raise ChatError(502, "provider_truncated", "Gemini's answer was cut off (max output tokens).")
            if finish == "MALFORMED_FUNCTION_CALL" and not parts:
                # Ask once more for a well-formed call (this costs a round).
                contents.append(types.Content(role="user", parts=[types.Part.from_text(
                    text="Your last function call was malformed. Call the tool again with valid arguments.")]))
                continue
            if not parts:
                raise ChatError(502, "malformed_provider_response", "Gemini returned an empty answer.")
            contents.append(content)  # exactly as returned, so thought signatures go back unchanged
            calls = [p.function_call for p in parts if getattr(p, "function_call", None) is not None]
            if not calls:
                final_text = "\n".join(p.text for p in parts if isinstance(getattr(p, "text", None), str)
                                       and not getattr(p, "thought", False)).strip()
                if not final_text:
                    raise ChatError(502, "malformed_provider_response", "Gemini returned an empty answer.")
                break
            responses = []
            for call in calls:
                name, args = getattr(call, "name", None), getattr(call, "args", None)
                if not isinstance(name, str) or not name:
                    raise ChatError(502, "malformed_provider_response", "Gemini returned a malformed function call.")
                args = dict(args) if isinstance(args, dict) else ({} if args is None else args)
                if name == "submit_answer":
                    try:
                        answer = AnswerArgs.model_validate(args if isinstance(args, dict) else {})
                        payload: dict = {"output": "Answer delivered."}
                    except ValidationError as err:
                        payload = {"error": f"Invalid submit_answer: {err.errors()[0]['msg']}"}
                else:
                    ok, result = self.dispatch(name, args)
                    evidence.append({"tool": name, "input": args if isinstance(args, dict) else None,
                                     "ok": ok, "kind": result.get("kind") if ok else None, "result": result})
                    if ok and name == "propose_action":
                        proposals.append(result)
                    payload = {"output": json.loads(json.dumps(result, default=str))} if ok else result
                responses.append(types.Part(function_response=types.FunctionResponse(
                    id=getattr(call, "id", None), name=name, response=payload)))
            contents.append(types.Content(role="user", parts=responses))  # every call answered, in one turn
        a = answer.model_dump() if answer else {}
        reply = a.get("reply") or final_text
        return {"reply": reply, "mode": "gemini", "model": model_used, "stop_reason": finish,
                "tool_rounds": rounds, "evidence": evidence, "proposals": proposals, "structured": answer is not None,
                "measured": a.get("measured", []), "inferred": a.get("inferred", []),
                "unavailable": a.get("unavailable", []), "suggested": a.get("suggested_actions", []),
                "messages": prior + [{"role": "user", "content": text}, {"role": "assistant", "content": reply}]}

    # ------------------------------------------------------------------ Ollama (OpenAI-compatible, local)
    def _post_json(self, url: str, body: dict, headers: dict, timeout_s: float) -> dict:
        """One HTTPS/HTTP POST with a JSON body (tests replace this)."""
        data = json.dumps(body).encode()
        req = urllib.request.Request(url, data=data, method="POST",
                                     headers={"Content-Type": "application/json", **headers})
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:  # noqa: S310 - fixed provider URLs
            return json.loads(resp.read().decode())

    async def _call_openai(self, cfg: AdvisorConfig, provider: str, messages: list) -> dict:
        label = PROVIDER_LABEL[provider]
        url, headers, model = f"{cfg.ollama_url}/chat/completions", {"Authorization": "Bearer ollama"}, cfg.ollama_model
        body = {"model": model, "messages": messages, "tools": OPENAI_TOOLS, "tool_choice": "auto"}
        try:
            return await asyncio.to_thread(self._post_json, url, body, headers, cfg.llm_timeout_s)
        except urllib.error.HTTPError as err:
            try:
                detail = err.read().decode(errors="replace")[:300]
            except Exception:
                detail = ""
            if err.code == 429:
                raise ChatError(429, "provider_rate_limited", f"{label} rate limit reached.")
            if err.code in (401, 403):
                raise ChatError(502, "provider_auth_failed", f"{label} rejected the API key.")
            if err.code == 404:
                raise ChatError(502, "model_unavailable", f"The model '{model}' is not available on {label}."
                                + (" Run `ollama pull " + str(model) + "`." if provider == "ollama" else ""))
            if err.code == 400:
                raise ChatError(502, "provider_bad_request", f"{label} rejected the request: {detail}")
            raise ChatError(502, "provider_error", f"{label} returned HTTP {err.code}.")
        except (TimeoutError, OSError) as err:  # URLError is an OSError; socket timeouts too
            if "timed out" in str(err).lower():
                raise ChatError(504, "provider_timeout", f"{label} did not answer within {cfg.llm_timeout_s:g} s.")
            hint = " Is `ollama serve` running?" if provider == "ollama" else " Check the internet connection."
            raise ChatError(503, "provider_unreachable", f"Could not reach {label}.{hint}")
        except json.JSONDecodeError:
            raise ChatError(502, "malformed_provider_response", f"{label} returned a response that is not JSON.")

    async def _chat_openai(self, cfg: AdvisorConfig, provider: str, history: list, text: str) -> dict:
        """Tool-use loop for a local Ollama model. Same tools, dispatcher, evidence and submit_answer as Gemini."""
        label = PROVIDER_LABEL[provider]
        # Only plain-text turns carry over.
        prior = [{"role": m["role"], "content": m["content"]} for m in history
                 if m.get("role") in ("user", "assistant") and isinstance(m.get("content"), str)]
        messages: list[dict] = [{"role": "system", "content": SYSTEM_PROMPT}] + prior + [{"role": "user", "content": text}]
        evidence: list[dict] = []
        proposals: list[dict] = []
        answer: Optional[AnswerArgs] = None
        final_text, finish, rounds = "", None, 0
        model_used = cfg.llm_model
        while answer is None:
            if rounds >= cfg.max_rounds:
                raise ChatError(502, "tool_round_limit", f"{label} did not finish within {cfg.max_rounds} tool rounds.")
            rounds += 1
            resp = await self._call_openai(cfg, provider, messages)
            try:
                choice = resp["choices"][0]
                msg = choice["message"]
            except (KeyError, IndexError, TypeError):
                raise ChatError(502, "malformed_provider_response", f"{label} returned a response without a message.")
            finish = choice.get("finish_reason")
            model_used = resp.get("model") or model_used
            if finish == "length":
                raise ChatError(502, "provider_truncated", f"{label}'s answer was cut off (max tokens).")
            # Keep the assistant message exactly as returned.
            messages.append({k: v for k, v in msg.items() if v is not None})
            calls = msg.get("tool_calls") or []
            if not calls:
                final_text = (msg.get("content") or "").strip() if isinstance(msg.get("content"), str) else ""
                if not final_text:
                    raise ChatError(502, "malformed_provider_response", f"{label} returned an empty answer.")
                break
            for i, call in enumerate(calls):
                fn = (call or {}).get("function") or {}
                name, call_id = fn.get("name"), call.get("id") or f"call_{rounds}_{i}"
                if not isinstance(name, str):
                    raise ChatError(502, "malformed_provider_response", f"{label} returned a malformed tool call.")
                raw = fn.get("arguments")
                try:
                    args = json.loads(raw) if isinstance(raw, str) and raw.strip() else (raw if isinstance(raw, dict) else {})
                except json.JSONDecodeError:
                    args = None
                if name == "submit_answer":
                    try:
                        answer = AnswerArgs.model_validate(args if isinstance(args, dict) else {})
                        content = "Answer delivered."
                    except ValidationError as err:
                        content = json.dumps({"error": f"Invalid submit_answer: {err.errors()[0]['msg']}"})
                else:
                    ok, payload = (self.dispatch(name, args) if args is not None
                                   else (False, {"error": "Tool arguments were not valid JSON."}))
                    evidence.append({"tool": name, "input": args if isinstance(args, dict) else None,
                                     "ok": ok, "kind": payload.get("kind") if ok else None, "result": payload})
                    if ok and name == "propose_action":
                        proposals.append(payload)
                    content = json.dumps(payload, default=str)
                messages.append({"role": "tool", "tool_call_id": call_id, "name": name, "content": content})
        a = answer.model_dump() if answer else {}
        reply = a.get("reply") or final_text
        return {"reply": reply, "mode": provider, "model": model_used, "stop_reason": finish,
                "tool_rounds": rounds, "evidence": evidence, "proposals": proposals, "structured": answer is not None,
                "measured": a.get("measured", []), "inferred": a.get("inferred", []),
                "unavailable": a.get("unavailable", []), "suggested": a.get("suggested_actions", []),
                # The stored history keeps plain text turns only, so the provider can change between turns.
                "messages": prior + [{"role": "user", "content": text}, {"role": "assistant", "content": reply}]}

    # ------------------------------------------------------------------ offline
    def _chat_offline(self, message: str, machine_hint: str | None = None) -> dict:
        q = message.lower()
        machine_ids = list(self.engine.plants)
        evidence: list[dict] = []
        proposals: list[dict] = []

        def use(name, **kw):
            ok, payload = self.dispatch(name, kw)
            evidence.append({"tool": name, "input": kw, "ok": ok, "kind": payload.get("kind") if ok else None,
                             "result": payload})
            return payload

        def done(reply, measured=(), inferred=(), suggested=()):
            return {"reply": reply, "mode": "offline", "evidence": evidence, "proposals": proposals,
                    "measured": list(measured), "inferred": list(inferred), "suggested": list(suggested)}

        mid = machine_hint or next((m for m in machine_ids if m.lower() in q), None)
        named = mid is not None or re.search(r"lathe|saw|compressor|mill|vmc", q)
        line_summary = ""
        if not named and re.search(r"line|happening|overview|status|all machines|factory|floor", q):
            ls = use("get_line_status")
            parts = [f"{m['machine_id']} {m['plc_state']}" + (f", derate {m['derate_pct']:.0f}%" if m.get("derate_pct") else "")
                     + (f", OEE {m['oee'] * 100:.0f}%" if isinstance(m.get("oee"), (int, float)) else "")
                     + (f", residual {m['residual_state']}" if m.get("residual_state") not in (None, "IN_BAND") else "")
                     for m in ls.get("machines", [])]
            ln = ls.get("line", {})
            line_summary = (f"**Line:** {ln.get('throughput_parts_per_hr')} parts/h, loss ₹{ln.get('line_loss_inr_per_hr')}/h, "
                            f"bottleneck {ln.get('bottleneck')}, air {'OK' if ln.get('air_supply_ok') else 'LOW'} · " + "; ".join(parts) + "\n\n")
        if mid is None:
            mid = ("LATHE-01" if "lathe" in q else "SAW-01" if "saw" in q
                   else "COMP-01" if ("compressor" in q or "air" in q) else "VMC-01")

        decision = re.search(r"what if|should i|can i|finish|batch|derate|slow|continue|run|stop|kya|karu|chala", q)
        knowledge_q = re.search(r"dataset|history|usually|how often|trained|accuracy|sim data|episodes", q)
        target = re.search(r"(\d+)\s*(parts|pcs|pieces|components)", q)

        if knowledge_q:
            k = use("query_dataset", fault="overview")
            if "error" in k:
                return done(k["error"])
            acc = (k.get("model_accuracy") or {}).get("test_in_distribution", {})
            lines = [f"**UDYAM-SIM** ({k.get('episodes')} simulated episodes, synthetic data)"]
            for fault, s in (k.get("faults") or {}).items():
                na = s.get("no_action") or {}
                ad = s.get("auto_derate") or {}
                lines.append(f"- {fault}: reached limit {na.get('pct_reached_limit', '–')}% without action, "
                             f"{ad.get('pct_reached_limit', '–')}% with auto-derate")
            if acc:
                lines.append(f"Model macro-F1 on unseen episodes: {acc.get('rf_macro_f1')} "
                             f"(rules: {acc.get('rules_macro_f1')}).")
            return done("\n".join(lines), inferred=["UDYAM-SIM statistics come from simulated episodes, not real machines."])

        state = use("get_machine_state", machine_id=mid)
        if "error" in state:
            return done(state["error"])
        o, tw = state["observed"], state["twin"]
        diag = state.get("ml_diagnosis") or {}
        top = (diag.get("top_faults") or [{}])[0]
        tl = state.get("temp_limit_k")
        tk = o.get("process_temp_k")
        measured = (f"{o.get('source_mode')} data · temp "
                    + (f"{tk - 273.15:.1f} °C" if isinstance(tk, (int, float)) else "n/a")
                    + (f" (limit {tl - 273.15:.1f} °C)" if tl else "") + " · "
                    f"vibration {o.get('vibration_rms_mm_s')} mm/s · {o.get('rpm', '–')} rpm · "
                    f"derate {o.get('derate_pct')}%")
        res = tw.get("residual") or {}
        fc = tw.get("forecast") or {}
        inferred = [f"twin residual {res.get('state', '–')}, h_cool {((tw.get('params') or {}).get('values') or {}).get('h_cool', '–')}"]
        if fc.get("minutes_to_warning") is not None:
            inferred.append(f"physics forecast: {fc.get('warning')} in ~{fc['minutes_to_warning']:.0f} min")
        if top.get("fault"):
            inferred.append(f"ML (UDYAM-SIM): most likely **{top['fault']}** ({top['probability'] * 100:.0f}%)")
        zone = ((state.get("dashboard") or {}).get("severity") or {}).get("zone")
        if zone:
            inferred.append(f"vibration zone {zone}")
        ai4i = ((tw.get("risk") or {}).get("ai4i") or {}).get("failure_risk_pct")
        if ai4i is not None:
            inferred.append(f"AI4I failure risk {ai4i:.0f}% (model vote share)")
        rul = tw.get("rul") or {}
        if rul.get("status") == "OK" and rul.get("rul_minutes") is not None:
            inferred.append(f"tool RUL ≈ {rul['rul_minutes']:.0f} min (estimate)")

        suggested = "Keep running and watch the residual."
        advisory: list[dict] = []
        if decision and mid in ("VMC-01", "LATHE-01"):
            wi = use("simulate_what_if", machine_id=mid, action="compare", minutes=30,
                     parts_target=int(target.group(1)) if target else 0)
            opts = wi.get("options", [])
            parts_lines = []
            for r in opts:
                lim = r["minutes_to_temp_limit"]
                parts_lines.append(f"{r['derate_pct']:.0f}% derate: {'limit in ' + str(lim) + ' min' if lim else 'no limit in 30 min'}, "
                                   f"{r['parts_in_window']} parts, ₹{r['total_inr_impact']:.0f} impact")
            if opts:
                safe = [r for r in opts if not r["machine_stops_on_limit"]]
                best = min(safe or opts, key=lambda r: r["total_inr_impact"])
                if best["derate_pct"] > 0:
                    p = use("propose_action", machine_id=mid, action="derate", derate_pct=best["derate_pct"],
                            reason=f"Twin what-if: no temperature limit in 30 min, {best['parts_in_window']} parts "
                                   f"vs {opts[0]['parts_in_window']} at full speed")
                    if "error" not in p:
                        proposals.append(p)
                    suggested = f"Derate to {best['derate_pct']:.0f}% (needs your approval). " + " ".join(wi.get("caveats", []))
                else:
                    suggested = "No derate needed in the next 30 min. " + " ".join(wi.get("caveats", []))
                inferred.append("What-if (30 min, thermal physics): " + "; ".join(parts_lines))
        elif top.get("fault") not in (None, "normal") or (
                state.get("vibration_zones") and (o.get("vibration_rms_mm_s") or 0) >= state["vibration_zones"]["C_starts_mm_s"]):
            fault = top.get("fault") or "bearing_defect"
            advice = {
                "coolant_degradation": "Check coolant flow, level and pump filter; consider a 20% derate.",
                "bearing_defect": "Stop at the next safe point and inspect the spindle bearing.",
                "overload": "Check tool sharpness, feed and depth of cut.",
                "tool_wear_accel": "Inspect the cutting tool; plan a tool change.",
                "sensor_stuck": "Check the temperature sensor wiring; don't trust the thermal forecast.",
                "pressure_drop": "Root cause is the compressor: check intake filter and leaks.",
                "order_surge": "Spindle is pushed above nominal for the rush order; watch temperature.",
            }.get(fault, "Inspect the machine.")
            suggested = advice
            p = use("propose_action", machine_id=mid, action="work_order",
                    title=f"{mid}: check {fault.replace('_', ' ')}", reason=advice)
            if "error" not in p:
                proposals.append(p)
        else:
            advisory.append({"action": suggested, "category": "monitor", "machine_id": mid, "rationale": "No developing fault detected."})

        reply = (line_summary + f"**Measured:** {measured}\n\n**Inferred:** " + "; ".join(inferred) +
                 f"\n\n**Suggested:** {suggested}")
        return done(reply, measured=[measured], inferred=inferred, suggested=advisory)


def _seed_history(history: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Plain-text transcript from an older client, cleaned into alternating user/assistant turns."""
    msgs: list[dict] = []
    for h in history[-8:]:
        if h.get("role") in ("user", "assistant") and isinstance(h.get("content"), str) and h["content"].strip():
            if msgs and msgs[-1]["role"] == h["role"]:
                continue
            msgs.append({"role": h["role"], "content": h["content"][:4000]})
    if msgs and msgs[0]["role"] != "user":
        msgs = msgs[1:]
    if msgs and msgs[-1]["role"] == "user":
        msgs = msgs[:-1]
    return msgs
