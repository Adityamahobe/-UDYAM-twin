"""Production FastAPI application for UDYAM-TWIN.

Provides:
- Bidirectional WebSocket /ws/telemetry with schema v1.0 validation
- REST endpoints for faults, alerts, work orders, onboarding, parameters, and economics
- Static serving of web/ dashboard and /operator mobile view
- SQLite persistence for events, work orders, and calibration logs
"""

import asyncio
import hmac
import csv
import io
import json
import os
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, Header, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from api import alerts as alert_store
from api.advisor import ChatError, ChatRequest, TwinAdvisor, _env_secret
from api.demo import AutoDemo
from api import export as exporter
from api.operating_mode import MODES as OPERATING_MODES, REAL_DATA, OperatingMode, ReadOnlyError, connection_state
from api.db import (
    DB_PATH,
    LifecycleError,
    WO_PRIORITIES,
    acknowledge_alert,
    create_work_order,
    delete_custom_machine,
    get_alert,
    get_work_order,
    link_alert_work_order,
    list_alerts,
    list_custom_machines,
    resolve_alerts,
    save_custom_machine,
    get_economics,
    get_events,
    max_event_seq,
    get_parameter_history,
    init_db,
    list_work_orders,
    log_event,
    log_parameter_change,
    log_rl_decision,
    log_telemetry,
    prune_telemetry,
    query_events,
    query_rl_decisions,
    query_telemetry,
    update_economics,
    update_work_order_status,
)
from api.iot import build_router
from config import ConfigError, get_config
from ml.vibration.severity import SeverityAssessor
from api.vibration import build_router as build_vibration_router
from api.synthetic import build_router as build_synthetic_router
from ml.synthetic import SyntheticStore
from ml.ai4i import FEATURES as FEATURES_AI4I, MODEL_PATH as AI4I_MODEL_PATH
from ml.candidate import GATE as MODEL_GATE, CandidateError, TrainingJob
from ml.model_registry import ModelRegistry, RegistryError
from ml.adaptive import (DriftMonitor, LabelError, LabelStore, TrainingReference, ai4i_training_inputs,
                         load_reference, MAX_LABEL_WINDOW)
from twin.model import use_model_path
from hub import IoTHub
from hub.mqtt_bridge import MqttBridge
from hub.wokwi import SOURCE as WOKWI_SOURCE, VirtualDeviceStore, WokwiBridge
from ml.maintenance_rl import ACTION_DERATE, METRICS_PATH as RL_METRICS_PATH, FeedbackAgent
from ml.vibration.monitor import VibrationMonitor
from sim.analytics import Analytics
from sim.controls import ParamError
from sim import whatif
from sim import factory_run
from ml import factory_dataset, factory_learning
from sim.factory import FactoryEngine, TICK_S
from sim.scenarios import SCENARIOS

ROOT = Path(__file__).resolve().parent.parent
WEB_DIR = ROOT / "web"
DOCS_DIR = ROOT / "docs"

# Shared factory engine instance
factory_engine = FactoryEngine(seed=42, machines=4)
connected_clients: set = set()

# IoT hub: devices bound to a machine replace its simulated sensor signals (source_mode LIVE)
iot_hub = IoTHub(
    db_path=DB_PATH,
    template_of=lambda mid: factory_engine.plants[mid].template if mid in factory_engine.plants else None,
)
factory_engine.live_overlay = iot_hub.overlay
mqtt_ref: Dict[str, Optional[MqttBridge]] = {"mqtt": None}


def _machine_info(mid: str) -> Optional[Dict[str, Any]]:
    plant = factory_engine.plants.get(mid)
    if plant is None:
        return None
    return {
        "template": plant.template,
        "name": plant.name,
        "rated_kw": plant.nameplate.get("rated_kw"),
        "stream_band_hz": get_config().stream_band_hz(mid, plant.template),
        "bearing_model": get_config().ml(mid, plant.template)["bearing_model"],
    }


# Vibration health (ml/vibration): fed from the stream each tick and from waveform uploads
vibration_monitor = VibrationMonitor(machine_info=_machine_info)
# OEE / energy / trends, fed from the same broadcast stream as every panel
analytics = Analytics()
# Decision-support chatbot grounded in the live twin (Gemini via the Google Gen AI SDK, with an offline fallback)
advisor = TwinAdvisor(factory_engine, economics=get_economics, events=lambda n: get_events(limit=n),
                      analytics=analytics.summary, telemetry_mode=lambda: mode_status()["mode"])
# Reinforcement-learning maintenance policy: recommends, then learns from the simulated outcome (SIM only)
rl_agent = FeedbackAgent(sink=log_rl_decision)
# TESTING (simulation, every control) or REAL DATA (read-only monitoring of real devices); see api/operating_mode.py
operating_mode = OperatingMode()
# Telemetry history (every 2 s per machine) for time-range CSV / Excel exports
telemetry_recorder = exporter.TelemetryRecorder(write=log_telemetry, prune=prune_telemetry)


class FaultInjectRequest(BaseModel):
    machine_id: str
    fault: str


class FaultClearRequest(BaseModel):
    machine_id: Optional[str] = None


class DerateRequest(BaseModel):
    machine_id: str
    pct: float


ADDABLE_TEMPLATES = ("cnc_mill", "cnc_lathe", "bar_saw", "air_compressor")


class ZoneBoundaries(BaseModel):
    A_B: float = Field(gt=0, le=50)
    B_C: float = Field(gt=0, le=50)
    C_D: float = Field(gt=0, le=50)


class AddMachineRequest(BaseModel):
    """Add Your Machine. Everything except the template has a template default."""
    template: str = Field(pattern="^(" + "|".join(ADDABLE_TEMPLATES) + ")$")
    name: Optional[str] = Field(default=None, min_length=2, max_length=40)
    rated_kw: Optional[float] = Field(default=None, gt=0, le=500)
    rated_rpm: Optional[float] = Field(default=None, ge=60, le=24000, description="normal operating speed")
    foundation: Optional[str] = Field(default=None, pattern="^(rigid|flexible)$")
    iso_class: Optional[str] = Field(default=None, max_length=60, description="e.g. 'ISO 20816-3 Group 2, rigid'")
    zone_boundaries: Optional[ZoneBoundaries] = Field(default=None, description="velocity RMS mm/s; custom profile")
    thresholds: Optional[Dict[str, float]] = None
    x: Optional[float] = Field(default=None, ge=-50, le=50)
    y: Optional[float] = Field(default=None, ge=-50, le=50)
    nameplate: Optional[Dict[str, Any]] = None  # legacy dashboard form: {rated_kw, max_rpm}


class WorkOrderCreateRequest(BaseModel):
    machine_id: str
    title: Optional[str] = None
    priority: Optional[str] = "Medium"
    notes: Optional[str] = ""
    cause: Optional[str] = None
    evidence: Optional[str] = None
    recommended_action: Optional[str] = None
    alert_id: Optional[str] = None


class AlertAckRequest(BaseModel):
    by: Optional[str] = None


class AlertResolveRequest(BaseModel):
    resolution: Optional[str] = "Resolved by operator"


class WorkOrderUpdateRequest(BaseModel):
    status: str
    notes: Optional[str] = None
    tool_wear_measured: Optional[float] = None


class ToolWearEntryRequest(BaseModel):
    machine_id: str
    measured_wear_min: float
    wo_id: Optional[str] = None


class EconomicsUpdateRequest(BaseModel):
    inr_per_part: Optional[float] = Field(default=None, gt=0)
    downtime_cost_per_hr: Optional[float] = Field(default=None, gt=0)
    electricity_tariff_per_kwh: Optional[float] = Field(default=None, gt=0)
    cea_co2_factor: Optional[float] = Field(default=None, gt=0)
    sensor_node_cost_inr: Optional[float] = Field(default=None, gt=0)
    operating_hours_per_month: Optional[float] = Field(default=None, gt=0, le=744)
    baseline_downtime_h_per_month: Optional[float] = Field(default=None, ge=0)
    assumed_downtime_reduction_pct: Optional[float] = Field(default=None, ge=0, le=100)
    sensor_nodes: Optional[float] = Field(default=None, ge=1)


def _testing_only(action: str) -> None:
    """Simulator controls are refused in REAL DATA mode (read-only monitoring)."""
    try:
        operating_mode.guard(action)
    except ReadOnlyError as err:
        raise HTTPException(status_code=409, detail=err.detail())


async def broadcast_messages(messages: List[Dict[str, Any]]) -> None:
    """Broadcast JSON payloads to all connected WebSocket clients and log events to SQLite.

    Alert-worthy events also update the alert store, whose `alert` frames go out in the same batch.
    """
    if not messages:
        return
    vibration_monitor.observe(messages)  # sees every frame, including RESET from a manual reset
    analytics.observe(messages)
    for name, observer in (("advisor", advisor), ("rl", rl_agent), ("telemetry", telemetry_recorder),
                           ("drift", drift_monitor), ("factory models", live_factory_models)):
        try:
            observer.observe(messages)
        except Exception as err:  # decision support and history must never break telemetry
            print(f"[{name} observe error]: {err}")
    for ev in drift_monitor.take_events():  # information only: DATA_DRIFT never becomes an alert
        factory_engine._emit_event(ev["machine_id"], "DATA_DRIFT", ev["severity"], ev["title"], data=ev["data"])
    messages = list(messages) + factory_engine.flush_events()
    messages = messages + alert_store.ingest(messages)

    # Persist events and parameter records to SQLite
    for m in messages:
        if m.get("type") == "event":
            log_event(m)
            if m.get("kind") == "CALIBRATION" and m.get("data"):
                data = m["data"]
                log_parameter_change(
                    machine_id=m.get("machine_id", "VMC-01"),
                    param=data.get("param", "h_cool"),
                    from_val=data.get("from", 1.0),
                    to_val=data.get("to", 1.0),
                    version=data.get("version", "v1.0"),
                    residual_ewma=data.get("residual_ewma", 0.0),
                    ts=m.get("ts"),
                )

    payloads = [json.dumps(m, ensure_ascii=False) for m in messages]
    dead_clients = set()
    for ws in list(connected_clients):
        try:
            for p in payloads:
                await ws.send_text(p)
        except Exception:
            dead_clients.add(ws)
    connected_clients.difference_update(dead_clients)


# -----------------------------------------------------------------------------
# Fault lab, resolution, clean reset and the Auto Demo director
# -----------------------------------------------------------------------------
async def clean_reset() -> None:
    """Fresh seeded engine; the RESET event resolves every alert and closes scenario work orders."""
    await broadcast_messages(factory_engine.command({"cmd": "reset"}))


async def start_scenario(scenario_id: str) -> None:
    await broadcast_messages(factory_engine.command({"cmd": "start_scenario", "scenario": scenario_id}))


async def resolve_machine(machine_id: Optional[str]) -> None:
    await broadcast_messages(factory_engine.command({"cmd": "resolve", "machine_id": machine_id}))


async def work_order_for_rule(rule: str) -> Optional[Dict[str, Any]]:
    alert = next((a for a in list_alerts() if a["rule"] == rule), None)
    return await _work_order_from_alert(alert) if alert else None


async def set_work_order_status(wo_id: str, status: str) -> None:
    wo = update_work_order_status(wo_id=wo_id, status=status, notes=f"{status} (Auto Demo)")
    if wo:
        await _broadcast_work_order("updated", wo)


async def narrate(title: str, data: Dict[str, Any]) -> None:
    factory_engine._emit_event(None, "DEMO", "INFO", title, data=data)
    await broadcast_messages(factory_engine.flush_events())


class _DemoActions:
    clean_reset = staticmethod(clean_reset)
    start_scenario = staticmethod(start_scenario)
    resolve = staticmethod(resolve_machine)
    work_order_for_rule = staticmethod(work_order_for_rule)
    set_work_order_status = staticmethod(set_work_order_status)
    narrate = staticmethod(narrate)


auto_demo = AutoDemo(_DemoActions())


async def engine_ticker_loop() -> None:
    """Background ticker: Auto Demo step (if on), then one engine tick, broadcast every TICK_S.

    Fixed-rate: each tick is scheduled TICK_S after the previous one, so processing time does not
    stretch the clock (if the server falls behind by more than a tick, it skips ahead instead of bursting).
    """
    loop = asyncio.get_running_loop()
    next_at = loop.time()
    while True:
        try:
            if factory_engine.running:  # paused or stopped: the Auto Demo clock (and a live factory run) waits too
                await auto_demo.tick()
                await factory_playback.tick()
            await broadcast_messages(factory_engine.step())
        except Exception as err:
            print(f"[ticker error]: {err}")
        next_at += TICK_S
        delay = next_at - loop.time()
        if delay < -TICK_S:
            next_at, delay = loop.time(), 0.0
        await asyncio.sleep(max(0.0, delay))


@asynccontextmanager
async def app_lifespan(app: FastAPI):
    # Startup: initialize database and launch ticker task
    init_db()
    label_store.init()
    wokwi_store.init()
    factory_engine.event_seq = max(factory_engine.event_seq, max_event_seq())  # keep event ids unique across restarts
    _restore_custom_machines()
    # Simulation state does not survive a restart, so alerts left open by a previous run are stale.
    resolve_alerts("Server restart: simulation state reset")
    bridge = MqttBridge.from_env(iot_hub)
    if bridge:
        bridge.start()
        mqtt_ref["mqtt"] = bridge
    wokwi = WokwiBridge.from_env(wokwi_store)
    if wokwi:
        wokwi.start()
        wokwi_ref["bridge"] = wokwi
    if os.environ.get("UDYAM_AUTO_DEMO", "1") != "0":
        auto_demo.on = True
    # UDYAM_TICKER=0 (tests) leaves ticking to the caller.
    task = asyncio.create_task(engine_ticker_loop()) if os.environ.get("UDYAM_TICKER", "1") != "0" else None
    yield
    # Shutdown: cancel background ticker and MQTT bridge
    if task:
        task.cancel()
    if bridge:
        bridge.stop()
    if wokwi:
        wokwi.stop()


app = FastAPI(
    title="UDYAM-TWIN Industrial Digital Twin API",
    description="Zero-licence self-calibrating digital twin backend for MSME machine shops",
    version="1.0.0",
    lifespan=app_lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def revalidate_ui_files(request: Request, call_next):
    """Dashboard pages and scripts: the browser must revalidate (304 if unchanged), so an update never
    mixes a new index.html with a stale cached app.js."""
    response = await call_next(request)
    path = request.url.path
    if path in ("/", "/operator", "/iot") or path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-cache"
    return response


app.include_router(build_router(iot_hub, factory_engine, mqtt_ref))
app.include_router(build_vibration_router(iot_hub, vibration_monitor, lambda: list(factory_engine.plants)))
synthetic_store = SyntheticStore()
app.include_router(build_synthetic_router(synthetic_store))
model_registry = ModelRegistry()
label_store = LabelStore(DB_PATH)
# Wokwi virtual devices (hub/wokwi.py): own store, own MQTT connection; never fed into the simulation engine.
wokwi_store = VirtualDeviceStore(DB_PATH)
wokwi_ref: Dict[str, Any] = {}
training_job = TrainingJob(model_registry, synthetic_store, label_store)
_references: Dict[str, TrainingReference] = {}


def production_reference() -> TrainingReference:
    """Training inputs of the deployed model, for the drift monitor (cached per version)."""
    version = model_registry.production() if model_registry.index_path.is_file() else "shipped"
    if version not in _references:
        _references[version] = (load_reference(model_registry, version) if version != "shipped"
                                else TrainingReference(ai4i_training_inputs(), "v001 (shipped)"))
    return _references[version]


drift_monitor = DriftMonitor(production_reference)
whatif_jobs = whatif.WhatIfJobs()
factory_run_jobs = factory_run.RunJobs()
factory_datasets = factory_dataset.FactoryDatasetStore()
factory_registries = factory_learning.registries()
factory_training = factory_learning.FactoryTrainingJob(factory_registries, factory_datasets)
live_factory_models = factory_learning.LiveFactoryModels(factory_registries)


class FactoryPlayback:
    """Plays a factory run's event list on the live simulation in real time (Testing mode only)."""

    def __init__(self):
        self.state = "idle"
        self.spec: Optional[factory_run.RunSpec] = None
        self.events: List[Dict[str, Any]] = []
        self.idx = 0
        self.start_t = 0.0
        self.applied: List[Dict[str, Any]] = []

    def status(self) -> Dict[str, Any]:
        elapsed = (factory_engine.t - self.start_t) / 60 if self.state == "running" else None
        nxt = self.events[self.idx] if self.state == "running" and self.idx < len(self.events) else None
        return {"state": self.state, "name": self.spec.name if self.spec else None,
                "minutes": self.spec.minutes if self.spec else None,
                "elapsed_min": round(elapsed, 2) if elapsed is not None else None,
                "next_event": {**nxt, "text": factory_run.describe(nxt)} if nxt else None,
                "applied": self.applied[-20:]}

    async def start(self, spec: factory_run.RunSpec) -> None:
        _testing_only("Playing a factory run on the live simulation")
        events = factory_run.validate(spec, factory_run.machines_of(factory_engine.custom_machines))
        if auto_demo.on:
            await auto_demo.set(False)  # its own story would fight the run; also resets the plant
        else:
            await clean_reset()
        await broadcast_messages(factory_engine.reset_params())
        await broadcast_messages(factory_engine.run_control("start"))
        factory_engine.auto_derate = spec.self_healing
        self.spec, self.events, self.idx, self.applied = spec, events, 0, []
        self.start_t, self.state = factory_engine.t, "running"
        factory_engine._emit_event(None, "SIM_RUN", "INFO", f"Factory run '{spec.name}' started on the live simulation "
                                   f"({spec.minutes:g} min, {len(events)} events)", data={"factory_run": spec.name})
        await broadcast_messages(factory_engine.flush_events())

    async def stop(self, why: str = "stopped") -> None:
        if self.state != "running":
            return
        self.state = why
        factory_engine.auto_derate = True  # back to the default
        factory_engine._emit_event(None, "SIM_RUN", "INFO", f"Factory run '{self.spec.name}' {why}",
                                   data={"factory_run": self.spec.name})
        await broadcast_messages(factory_engine.flush_events())

    async def tick(self) -> None:
        if self.state != "running":
            return
        elapsed = (factory_engine.t - self.start_t) / 60
        while self.idx < len(self.events) and self.events[self.idx]["at_min"] <= elapsed + 1e-9:
            ev = self.events[self.idx]
            self.idx += 1
            msgs = factory_run.apply_event(factory_engine, ev)
            text = factory_run.describe(ev)
            self.applied.append({"at_min": ev["at_min"], "text": text})
            factory_engine._emit_event(ev.get("machine_id"), "SIM_RUN", "INFO", f"Run event at {ev['at_min']:g} min: {text}",
                                       data={"factory_run": self.spec.name, "event": ev})
            await broadcast_messages(msgs + factory_engine.flush_events())
        if elapsed >= self.spec.minutes:
            await self.stop("finished")


factory_playback = FactoryPlayback()


def _deployed_model_path() -> Path:
    """The model the twin uses: the registry's production version, or the shipped file before the registry exists."""
    return model_registry.production_model_path() if model_registry.index_path.is_file() else AI4I_MODEL_PATH


use_model_path(_deployed_model_path)


# -----------------------------------------------------------------------------
# REST Endpoints
# -----------------------------------------------------------------------------
@app.get("/api/health")
def get_health():
    return {
        "status": "ok",
        "service": "udyam_twin_core",
        "mode": mode_status()["mode"],
        "auto_demo": auto_demo.on,
        "schema_version": "1.0",
        "clients_connected": len(connected_clients),
        "live_machines": sorted(iot_hub.bound_machines()),
    }


@app.get("/api/config")
def get_machine_config():
    """Resolved machine classes, thresholds and site constants from config/machines.yaml."""
    return get_config().public()


@app.get("/schema.json")
def get_schema():
    schema_path = DOCS_DIR / "schema.json"
    if schema_path.exists():
        return FileResponse(schema_path, media_type="application/json")
    raise HTTPException(status_code=404, detail="schema.json not found")


@app.get("/api/snapshots")
def get_snapshots():
    """Latest `snapshot` per machine (same objects the WebSocket pushes every tick)."""
    return list(factory_engine.snapshots.values())


@app.get("/api/registry")
def get_registry():
    return factory_engine.registry()


@app.post("/api/faults/inject")
async def post_inject_fault(req: FaultInjectRequest):
    _testing_only("Fault injection")
    msgs = factory_engine.command({
        "cmd": "inject_fault",
        "machine_id": req.machine_id,
        "fault": req.fault,
    })
    await broadcast_messages(msgs)
    return {"status": "injected", "machine_id": req.machine_id, "fault": req.fault}


@app.post("/api/faults/clear")
async def post_clear_faults(req: FaultClearRequest):
    _testing_only("Clearing simulated faults")
    msgs = factory_engine.command({
        "cmd": "clear_faults",
        "machine_id": req.machine_id,
    })
    await broadcast_messages(msgs)
    return {"status": "cleared", "machine_id": req.machine_id}


@app.post("/api/derate")
async def post_set_derate(req: DerateRequest):
    _testing_only("A simulator derate")
    msgs = factory_engine.command({
        "cmd": "set_derate",
        "machine_id": req.machine_id,
        "pct": req.pct,
    })
    await broadcast_messages(msgs)
    return {"status": "derate_applied", "machine_id": req.machine_id, "pct": req.pct}


# -----------------------------------------------------------------------------
# Testing-mode machine parameters and run control (simulation only; refused in REAL DATA mode)
# -----------------------------------------------------------------------------
class SimParamsRequest(BaseModel):
    machine_id: str
    params: Dict[str, Any]


class SimRunRequest(BaseModel):
    action: str = Field(pattern="^(pause|resume|stop|start|run_for)$")
    minutes: Optional[float] = None


def sim_controls_status() -> Dict[str, Any]:
    return {"run": factory_engine.run_status(), "auto_demo": auto_demo.on, "simulation_only": True,
            "machines": [factory_engine.machine_params(mid) for mid in factory_engine.plants]}


@app.get("/api/sim/controls")
def get_sim_controls():
    """Run state and each simulated machine's parameters with their engineering limits."""
    return sim_controls_status()


@app.post("/api/sim/params")
async def post_sim_params(req: SimParamsRequest):
    """Change parameters of one simulated machine. Values outside the limits are refused (422), not clamped.
    Only the simulation changes: nothing is sent to any device. The Auto Demo stops (its story assumes defaults)."""
    _testing_only("Changing simulation parameters")
    if req.machine_id not in factory_engine.plants:
        raise HTTPException(status_code=404, detail=f"Unknown machine {req.machine_id}")
    try:
        msgs = factory_engine.set_params(req.machine_id, req.params)
    except ParamError as err:
        raise HTTPException(status_code=422, detail={"error": "parameter_out_of_limits", "message": str(err)})
    if auto_demo.on:
        await auto_demo.set(False)
    await broadcast_messages(msgs)
    return sim_controls_status()


@app.post("/api/sim/params/reset")
async def post_sim_params_reset(machine_id: Optional[str] = Query(default=None)):
    """Back to the default parameters (one machine, or all)."""
    _testing_only("Resetting simulation parameters")
    await broadcast_messages(factory_engine.reset_params(machine_id))
    return sim_controls_status()


@app.post("/api/sim/run")
async def post_sim_run(req: SimRunRequest):
    """pause / resume / stop (reset to baseline) / start / run_for N minutes, then pause."""
    _testing_only("Simulation run control")
    if req.action == "stop" and auto_demo.on:
        auto_demo.on = False  # stop resets the engine itself
    try:
        msgs = factory_engine.run_control(req.action, req.minutes)
    except ParamError as err:
        raise HTTPException(status_code=422, detail={"error": "invalid_run_request", "message": str(err)})
    await broadcast_messages(msgs)
    return sim_controls_status()


def _machine_spec(req: AddMachineRequest, machine_id: str) -> tuple[Dict[str, Any], Dict[str, Any], Optional[Dict[str, Any]]]:
    """(engine spec, config entry, custom vibration profile) for a validated Add-Machine request."""
    cfg = get_config()
    tpl = cfg.template(req.template)
    nameplate = {**tpl.get("nameplate", {}), **{k: v for k, v in (req.nameplate or {}).items()
                                                 if k in ("rated_kw", "max_rpm", "rated_pressure_bar")}}
    if req.template == "air_compressor":
        nameplate.pop("max_rpm", None)  # the legacy form sends rated pressure in this field
    if req.rated_kw is not None:
        nameplate["rated_kw"] = req.rated_kw
    if req.rated_rpm is not None:
        if req.template == "air_compressor":
            raise ConfigError("rated_rpm does not apply to an air compressor")
        nameplate["rpm_nominal"] = req.rated_rpm
    if nameplate.get("max_rpm") and nameplate.get("rpm_nominal") and nameplate["rpm_nominal"] > nameplate["max_rpm"]:
        raise ConfigError(f"rated_rpm {nameplate['rpm_nominal']:g} exceeds max_rpm {nameplate['max_rpm']:g}")
    entry: Dict[str, Any] = {"template": req.template, "nameplate": nameplate}
    if req.thresholds:
        entry["thresholds"] = dict(req.thresholds)
    vib = dict(tpl.get("vibration") or {})
    if req.foundation:
        vib["foundation"] = req.foundation
        vib["profile"] = f"demo-iso20816-3-g2-{req.foundation}"
    profile = None
    if req.zone_boundaries is not None:
        profile = {
            "profile_id": f"custom-{machine_id.lower()}",
            "standard": "Operator-entered thresholds" + (f" ({req.iso_class})" if req.iso_class else ""),
            "edition": None, "machine_group": req.iso_class or "not stated", "quantity": "velocity_rms_mm_s",
            "band_hz": [10, 1000], "measurement_location": vib.get("measurement_location", "bearing_housing"),
            "mounting": vib.get("foundation", "rigid"), "zone_boundaries": req.zone_boundaries.model_dump(),
            "label": "Operator-entered thresholds — not a certified assessment", "verified": False,
        }
        vib["profile"] = profile["profile_id"]
    entry["vibration"] = vib
    if req.iso_class:
        entry["iso_class"] = req.iso_class
    spec = {"machine_id": machine_id, "template": req.template,
            "name": req.name or f"{req.template.replace('_', ' ').title()} {machine_id}",
            "nameplate": nameplate, "x": req.x if req.x is not None else 4.0 * len(factory_engine.plants) - 12.0,
            "y": req.y if req.y is not None else 4.0, "config": entry, "profile": profile}
    return spec, entry, profile


def _refresh_assessors() -> None:
    """Severity profiles changed (machine added/removed): rebuild both assessors from config."""
    factory_engine.assessor = SeverityAssessor()
    vibration_monitor.assessor = SeverityAssessor()


def _restore_custom_machines() -> None:
    """Re-create machines added in earlier runs (persisted in SQLite)."""
    for spec in list_custom_machines():
        try:
            get_config().register_machine(spec["machine_id"], spec["config"], spec.get("profile"))
            factory_engine.add_custom_machine(spec, announce=False)
        except (ConfigError, KeyError) as err:
            print(f"[custom machine {spec.get('machine_id')} skipped]: {err}")
    _refresh_assessors()
    factory_engine.registry_dirty = True


@app.post("/api/machines")
async def post_add_machine(req: AddMachineRequest):
    """Add Your Machine: validated against config rules, live in every panel at once, kept across resets."""
    machine_id = factory_engine.next_machine_id(req.template)
    try:
        spec, entry, profile = _machine_spec(req, machine_id)
        get_config().register_machine(machine_id, entry, profile)
    except ConfigError as err:
        raise HTTPException(status_code=422, detail=str(err))
    save_custom_machine(machine_id, spec)
    _refresh_assessors()
    await broadcast_messages(factory_engine.add_custom_machine(spec))
    return {"status": "machine_onboarded", "machine_id": machine_id, "template": req.template, "name": spec["name"],
            "config": get_config().machine(machine_id)}


@app.delete("/api/machines/{machine_id}")
async def delete_machine(machine_id: str):
    """Remove a machine added with Add Your Machine (built-in machines cannot be removed)."""
    if machine_id not in get_config().runtime:
        raise HTTPException(status_code=409 if machine_id in factory_engine.plants else 404,
                            detail=f"{machine_id} is not a machine added at runtime")
    get_config().unregister_machine(machine_id)
    delete_custom_machine(machine_id)
    _refresh_assessors()
    await broadcast_messages(factory_engine.remove_custom_machine(machine_id))
    return {"status": "removed", "machine_id": machine_id}


@app.post("/api/reset")
async def post_reset(auto_demo_on: Optional[bool] = Query(default=None, alias="auto_demo")):
    """Clean baseline: fresh seeded engine, every alert resolved, scenario work orders closed, charts cleared.

    If the Auto Demo is on it restarts from 0 s; `?auto_demo=false|true` also switches it.
    """
    _testing_only("Resetting the simulation")
    if auto_demo_on is False and auto_demo.on:
        await auto_demo.set(False)  # also resets
    else:
        await clean_reset()
        if auto_demo_on:
            await auto_demo.set(True)
        auto_demo.restart_cycle()
    return {"status": "reset", "auto_demo": auto_demo.on, "open_alerts": len(list_alerts())}


@app.get("/api/auto_demo")
def get_auto_demo():
    return auto_demo.status()


@app.post("/api/auto_demo")
async def post_auto_demo(on: bool):
    """Toggle the 90-second Auto Demo. Turning it off leaves a clean baseline."""
    if on:
        _testing_only("The Auto Demo")
        await broadcast_messages(factory_engine.run_control("start"))  # a paused or stopped simulation runs again
    return await auto_demo.set(on)


@app.get("/api/scenarios")
def get_scenarios():
    """Fault-injection lab catalogue (demo simulation only) and which scenarios are running."""
    active = factory_engine.active_scenarios
    return [{**sc.public(), "active": active.get(sc.machine_id) == sid} for sid, sc in SCENARIOS.items()]


@app.post("/api/scenarios/{scenario_id}/start")
async def post_start_scenario(scenario_id: str):
    _testing_only("Starting a fault scenario")
    sc = SCENARIOS.get(scenario_id)
    if sc is None:
        raise HTTPException(status_code=404, detail=f"Unknown scenario {scenario_id}")
    if sc.machine_id not in factory_engine.plants:
        raise HTTPException(status_code=409, detail=f"{sc.machine_id} is not on the floor")
    await start_scenario(scenario_id)
    return {"status": "started", **sc.public()}


@app.post("/api/scenarios/resolve")
async def post_resolve(machine_id: Optional[str] = None):
    """Simulate resolution: clear faults, release SIM derates, resolve alerts, close their work orders."""
    _testing_only("Simulated resolution")
    if machine_id is not None and machine_id not in factory_engine.plants:
        raise HTTPException(status_code=404, detail=f"Machine {machine_id} not found")
    await resolve_machine(machine_id)
    return {"status": "resolved", "machine_id": machine_id, "open_alerts": len(list_alerts())}


@app.get("/api/events")
def api_get_events(limit: int = 50):
    return get_events(limit=limit)


async def _broadcast_work_order(action: str, wo: Dict[str, Any]) -> None:
    """`work_order` frame plus the v1.0 WORK_ORDER event the dashboard already listens for."""
    await broadcast_messages(alert_store.work_order_frames(action, wo))


@app.get("/api/work-orders")
def api_get_work_orders(status: Optional[str] = None):
    return list_work_orders(status=status)


@app.get("/api/work-orders/{wo_id}")
def api_get_work_order(wo_id: str):
    wo = get_work_order(wo_id)
    if not wo:
        raise HTTPException(status_code=404, detail="Work order not found")
    return wo


@app.post("/api/work-orders")
async def api_create_work_order(req: WorkOrderCreateRequest):
    title = req.title or req.cause
    if not title:
        raise HTTPException(status_code=422, detail="title or cause is required")
    try:
        wo = create_work_order(
            machine_id=req.machine_id,
            title=title,
            priority=req.priority or "Medium",
            notes=req.notes or "",
            cause=req.cause,
            evidence=req.evidence,
            recommended_action=req.recommended_action,
            alert_id=req.alert_id,
        )
    except LifecycleError as err:
        raise HTTPException(status_code=422, detail=str(err))
    if req.alert_id and get_alert(req.alert_id):
        link_alert_work_order(req.alert_id, wo["wo_id"])
    await _broadcast_work_order("created", wo)
    return wo


@app.patch("/api/work-orders/{wo_id}")
async def api_update_work_order(wo_id: str, req: WorkOrderUpdateRequest):
    try:
        wo = update_work_order_status(
            wo_id=wo_id,
            status=req.status,
            notes=req.notes,
            tool_wear_measured=req.tool_wear_measured,
        )
    except LifecycleError as err:
        raise HTTPException(status_code=409, detail=str(err))
    if not wo:
        raise HTTPException(status_code=404, detail="Work order not found")
    out = [alert_store.message("resolved", a) for a in
           (resolve_alerts(f"Work order {wo_id} closed", wo_id=wo_id) if wo["status"] == "Closed" else [])]
    await _broadcast_work_order("updated", wo)
    await broadcast_messages(out)
    return wo


@app.get("/api/alerts")
def api_get_alerts(include_resolved: bool = False, limit: int = 200):
    """Open alerts (ACTIVE, ACKNOWLEDGED), newest first; `include_resolved=true` adds history."""
    return list_alerts(include_resolved=include_resolved, limit=limit)


@app.get("/api/alerts/{alert_id}")
def api_get_alert(alert_id: str):
    a = get_alert(alert_id)
    if not a:
        raise HTTPException(status_code=404, detail="Alert not found")
    return a


@app.post("/api/alerts/{alert_id}/acknowledge")
async def api_acknowledge_alert(alert_id: str, req: Optional[AlertAckRequest] = None):
    try:
        a = acknowledge_alert(alert_id, by=req.by if req else None)
    except LifecycleError as err:
        raise HTTPException(status_code=409, detail=str(err))
    if not a:
        raise HTTPException(status_code=404, detail="Alert not found")
    await broadcast_messages([alert_store.message("acknowledged", a)])
    return a


@app.post("/api/alerts/{alert_id}/resolve")
async def api_resolve_alert(alert_id: str, req: Optional[AlertResolveRequest] = None):
    if not get_alert(alert_id):
        raise HTTPException(status_code=404, detail="Alert not found")
    resolved = resolve_alerts((req.resolution if req else None) or "Resolved by operator", alert_id=alert_id)
    await broadcast_messages([alert_store.message("resolved", a) for a in resolved])
    return get_alert(alert_id)


@app.post("/api/alerts/{alert_id}/work-order")
async def api_alert_work_order(alert_id: str):
    """Generate Work Order from an alert: machine, cause, evidence, recommended action, priority.

    One work order per alert; asking again returns the existing one.
    """
    a = get_alert(alert_id)
    if not a:
        raise HTTPException(status_code=404, detail="Alert not found")
    return await _work_order_from_alert(a)


async def _work_order_from_alert(a: Dict[str, Any]) -> Dict[str, Any]:
    alert_id = a["alert_id"]
    if a.get("wo_id") and (existing := get_work_order(a["wo_id"])):
        return {**existing, "existing": True}
    d = alert_store.work_order_draft(a)
    wo = create_work_order(
        machine_id=d["machine_id"],
        title=d["cause"],
        priority=d["priority"] if d["priority"] in WO_PRIORITIES else "Medium",
        notes=f"Generated from alert {alert_id}",
        cause=d["cause"],
        evidence=d["evidence"],
        recommended_action=d["recommended_action"],
        alert_id=alert_id,
    )
    link_alert_work_order(alert_id, wo["wo_id"])
    await _broadcast_work_order("created", wo)
    await broadcast_messages([alert_store.message("updated", get_alert(alert_id))])
    return {**wo, "existing": False}


@app.post("/api/tool-wear")
async def post_tool_wear_entry(req: ToolWearEntryRequest):
    """Human-in-the-loop tool wear update: operator measures edge wear at tool change."""
    twin = factory_engine.twins.get(req.machine_id)
    if not twin:
        raise HTTPException(status_code=404, detail=f"Machine {req.machine_id} not found")

    cal_rec = twin.calibrator.update_tool_wear_operator(
        measured_wear_min=req.measured_wear_min,
        predicted_wear_min=twin.pred_wear,
    )

    ev = {
        "type": "event",
        "schema_version": "1.0",
        "ts": factory_engine._ts(),
        "event_id": f"EV_WEAR_UPDATE_{req.machine_id}",
        "machine_id": req.machine_id,
        "kind": "CALIBRATION",
        "severity": "INFO",
        "title": f"Operator feedback: tool-wear rate k_wear recalibrated to {twin.calibrator.k_wear:.2f}",
        "inferred": f"Measured wear {req.measured_wear_min:.1f} min vs predicted {twin.pred_wear:.1f} min",
        "suggested": "Calibration updated based on verified operator measurement",
        "data": {
            "param": "k_wear",
            "k_wear": round(twin.calibrator.k_wear, 3),
            "measured_wear_min": req.measured_wear_min,
        },
    }
    await broadcast_messages([ev])
    return {"status": "calibrated", "k_wear": twin.calibrator.k_wear}


@app.get("/api/parameters/history")
def api_get_parameter_history(machine_id: Optional[str] = None):
    return get_parameter_history(machine_id=machine_id)


@app.get("/api/economics")
def api_get_economics():
    """Retrieve MSME economics parameters, calculated metrics, and editable assumptions."""
    config = get_economics()
    return {
        "assumptions": config,
        "formulas": {
            "downtime_loss_inr": "breakdown_hours * downtime_cost_per_hr",
            "line_loss_inr_per_hr": "max(0, nominal_rate - current_rate) * inr_per_part",
            "energy_cost_inr": "sum(power_kw * hours) * electricity_tariff_per_kwh",
            "co2_emissions_kg": "kwh_consumed * cea_co2_factor (CEA Baseline v20 2024-25)",
            "payback_months": "(total_sensor_nodes * sensor_node_cost_inr) / monthly_avoided_breakdown_value",
        },
    }


@app.post("/api/economics")
def api_update_economics(req: EconomicsUpdateRequest):
    updates = {}
    for k, v in req.model_dump(exclude_unset=True).items():
        if v is not None:
            updates[k] = v
    new_config = update_economics(updates)
    return {"status": "updated", "assumptions": new_config}


def analytics_report(window: str = "window") -> Dict[str, Any]:
    """OEE, energy, CO₂ and cost from tick data, plus clearly labelled illustrative ROI."""
    econ = get_economics()
    site = get_config().site
    tariff, co2 = econ["electricity_tariff_per_kwh"], econ["cea_co2_factor"]
    summ = analytics.summary(window)
    kwh = summ["site_energy_kwh"]
    hours = summ["elapsed_s"] / 3600.0
    mean_kw = kwh / hours if hours else None
    for m in summ["machines"].values():
        m["co2_kg"] = round(m["energy_kwh"] * co2, 6)
        m["cost_inr"] = round(m["energy_kwh"] * tariff, 4)
    vmc = (summ["machines"].get("VMC-01") or {}).get("oee")
    month_h = econ["operating_hours_per_month"]
    avoided_h = econ["baseline_downtime_h_per_month"] * econ["assumed_downtime_reduction_pct"] / 100.0
    avoided_inr = avoided_h * econ["downtime_cost_per_hr"]
    node_cost = econ["sensor_nodes"] * econ["sensor_node_cost_inr"]
    return {
        **summ,
        "window": window,
        "site": {
            "energy_kwh": kwh,
            "co2_kg": round(kwh * co2, 6),
            "cost_inr": round(kwh * tariff, 4),
            "mean_power_kw": round(mean_kw, 3) if mean_kw is not None else None,
            "line_oee": vmc,
            "line_oee_basis": "OEE of the last station (VMC-01), which paces the line",
        },
        "factors": {
            "grid_co2_kg_per_kwh": co2, "grid_co2_source": site.get("grid_co2_source"),
            "tariff_inr_per_kwh": tariff, "tariff_source": site.get("tariff_source"),
            "editable_at": "POST /api/economics",
        },
        "projection": {
            "label": "illustrative estimate",
            "assumption": f"current mean site power × {month_h:g} operating h/month",
            "energy_kwh_per_month": round(mean_kw * month_h, 1) if mean_kw is not None else None,
            "cost_inr_per_month": round(mean_kw * month_h * tariff, 0) if mean_kw is not None else None,
            "co2_kg_per_month": round(mean_kw * month_h * co2, 0) if mean_kw is not None else None,
        },
        "roi": {
            "label": "illustrative estimate",
            "assumptions": {
                "baseline_downtime_h_per_month": econ["baseline_downtime_h_per_month"],
                "assumed_downtime_reduction_pct": econ["assumed_downtime_reduction_pct"],
                "downtime_cost_per_hr_inr": econ["downtime_cost_per_hr"],
                "sensor_nodes": econ["sensor_nodes"],
                "sensor_node_cost_inr": econ["sensor_node_cost_inr"],
            },
            "avoided_downtime_h_per_month": round(avoided_h, 2),
            "avoided_downtime_inr_per_month": round(avoided_inr, 0),
            "sensor_hardware_inr": round(node_cost, 0),
            "payback_months": round(node_cost / avoided_inr, 2) if avoided_inr > 0 else None,
            "note": "Downtime reduction is an input assumption, not a measured or guaranteed saving.",
        },
        "method": {
            "oee": "Availability × Performance × Quality per machine from 500 ms ticks (see sim/analytics.py)",
            "quality": "Rule-based: parts completed while rule risk is HIGH or a tool-wear warning is active are "
                       "counted as quality hold; no inspection data in SIM",
            "energy": "∑ power_kw × 0.5 s per machine; site total = sum over machines",
            "co2": "kWh × grid factor", "cost": "kWh × tariff",
        },
    }


class ModeRequest(BaseModel):
    mode: str = Field(pattern="^(SIM|REPLAY|LIVE)$")
    machine_id: Optional[str] = "VMC-01"
    start_row: Optional[int] = Field(default=0, ge=0, lt=10000)


def _live_devices() -> List[Dict[str, Any]]:
    """Real (non-simulated) devices that are online and bound to a machine."""
    return [d for d in iot_hub.list() if not d.get("simulated") and d.get("status") == "ONLINE" and d.get("machine_id")]


def mode_status() -> Dict[str, Any]:
    live = _live_devices()
    sources = {mid: s.get("mode") for mid, s in factory_engine.snapshots.items()}
    mode = "LIVE" if factory_engine.mode == "SIM" and live else factory_engine.mode
    return {
        "mode": mode,
        "sources": sources,
        "replay": {**factory_engine.replay.status(), "machine_id": factory_engine.replay_machine}
        if factory_engine.replay else None,
        "live": {"devices_online": len(live), "machines": sorted({d["machine_id"] for d in live}),
                 "message": None if live else "No live device connected"},
    }


@app.get("/api/mode")
def get_mode():
    """Current telemetry mode. Each machine's own source is in `sources` (and in every snapshot)."""
    return mode_status()


@app.post("/api/mode")
async def post_mode(req: ModeRequest):
    """SIM, REPLAY (AI4I rows into a CNC mill), or LIVE (only if a real device is online; never faked)."""
    if req.mode == "REPLAY":
        _testing_only("REPLAY of the AI4I dataset")
    if req.mode == "LIVE":
        if not _live_devices():
            raise HTTPException(status_code=409, detail="No live device connected")
        target = "SIM"  # LIVE readings come from the device overlay on the machines they are bound to
    else:
        target = req.mode
    if req.mode != "SIM" and auto_demo.on:
        auto_demo.on = False  # the Auto Demo is a SIM story; its cycle reset would undo REPLAY
    try:
        msgs = factory_engine.command({"cmd": "set_mode", "mode": target, "machine_id": req.machine_id,
                                       "start_row": req.start_row or 0})
    except ValueError as err:
        raise HTTPException(status_code=409, detail=str(err))
    except Exception as err:  # e.g. the AI4I file is missing
        raise HTTPException(status_code=409, detail=f"REPLAY unavailable: {err}")
    await broadcast_messages(msgs)
    return mode_status()


class OperatingModeRequest(BaseModel):
    mode: str = Field(pattern="^(TESTING|REAL_DATA)$")


def real_connection() -> Dict[str, Any]:
    bridge = mqtt_ref.get("mqtt")
    return connection_state(bridge.status() if bridge else None, iot_hub.list())


def wokwi_connection() -> Dict[str, Any]:
    """Wokwi virtual devices, reported apart from real devices (they never count as real data)."""
    bridge = wokwi_ref.get("bridge")
    devices = wokwi_store.snapshot(history=False)
    online = [d["machine_id"] for d in devices if d["state"] == "ONLINE"]
    if bridge is None:
        state, msg = "NOT_CONFIGURED", "No Wokwi broker configured (UDYAM_WOKWI_BROKER)"
    elif online:
        state, msg = "CONNECTED", f"{len(online)} Wokwi virtual device(s) sending data"
    elif devices:
        state, msg = "STALE_DATA", "No new messages from the Wokwi virtual device(s); values are not current"
    elif bridge.state == "CONNECTED":
        state, msg = "CONNECTED", "Broker connected; no Wokwi virtual device has sent data yet"
    else:
        state, msg = bridge.state, bridge.last_error or "Connecting to the broker…"
    return {"state": state, "message": msg, "source": WOKWI_SOURCE, "virtual": True,
            "broker": bridge.status() if bridge else None, "online_machines": online,
            "devices": [{k: d[k] for k in ("machine_id", "state", "age_s", "last_message_at")} for d in devices],
            "counters": wokwi_store.counters()}


def operating_mode_status() -> Dict[str, Any]:
    return {**operating_mode.public(), "connection": real_connection(), "virtual": wokwi_connection(),
            "telemetry_source": mode_status()["mode"], "modes": list(OPERATING_MODES)}


def _wokwi_monitoring(d: Dict[str, Any]) -> Dict[str, Any]:
    """Run the existing monitoring models only where the virtual telemetry fits their inputs; otherwise say why not."""
    last = d["last"]
    vib = factory_engine.assessor.assess(d["machine_id"], {"velocity_rms_mm_s": last.get("vibration_rms_mm_s")},
                                         template="cnc_mill", rpm=last.get("rpm"), operating_state="RUN")
    return {
        "vibration_severity": {**vib.as_dict(), "profile": None,
                               "note": "ISO 20816-3 zones need velocity RMS measured over a known band (10–1000 Hz); the "
                                       "Wokwi value is a software model, so no zone is given."},
        "failure_risk": {"status": "NOT_APPLICABLE",
                         "detail": "The AI4I failure-risk model needs air temperature, process temperature, rpm, torque "
                                   "and tool wear; the Wokwi device sends rpm, temperature, vibration and current."},
        "labels": "scenario and simulated_fault are the simulated condition chosen in Wokwi, not verified faults",
    }


WOKWI_DIAGRAM = ROOT / "wokwi" / "udyam-virtual-machine" / "diagram.json"


@app.get("/api/wokwi/diagram")
def get_wokwi_diagram():
    """The Wokwi project's circuit (parts, positions, wires), so the dashboard draws the same circuit."""
    try:
        d = json.loads(WOKWI_DIAGRAM.read_text(encoding="utf-8"))
    except (OSError, ValueError) as err:
        raise HTTPException(status_code=404, detail=f"Wokwi diagram unavailable: {err}")
    return {"parts": d.get("parts", []), "connections": d.get("connections", []), "source": "wokwi/udyam-virtual-machine/diagram.json"}


@app.get("/api/wokwi")
def get_wokwi():
    """Wokwi virtual devices: connection, latest values, short history and what the monitoring models can use."""
    devices = wokwi_store.snapshot()
    for d in devices:
        d["monitoring"] = _wokwi_monitoring(d)
    return {**wokwi_connection(), "machines": devices,
            "notice": "VIRTUAL data from the Wokwi simulator (simulated sensors and software models). It is not a "
                      "measurement of a real machine and never enters the Testing-mode simulation."}


@app.get("/api/operating-mode")
def get_operating_mode():
    """TESTING or REAL DATA, plus the real-data connection state (MQTT broker, devices, last message)."""
    return operating_mode_status()


@app.post("/api/operating-mode")
async def post_operating_mode(req: OperatingModeRequest):
    """Switch between TESTING and REAL DATA. Entering REAL DATA stops the Auto Demo and REPLAY and resets the
    background simulation to a clean healthy baseline, so earlier test faults cannot raise alerts that look real."""
    if not operating_mode.set(req.mode):
        return operating_mode_status()
    if req.mode == REAL_DATA:
        await factory_playback.stop("stopped: switched to Real Data mode")
        # Real readings reach the dashboard through the engine tick (IoT overlay), so it must be running.
        await broadcast_messages(factory_engine.run_control("start"))
        if factory_engine.mode == "REPLAY":
            await broadcast_messages(factory_engine.command({"cmd": "set_mode", "mode": "SIM"}))
        if auto_demo.on:
            await auto_demo.set(False)  # also resets to a clean baseline
        else:
            await clean_reset()
    factory_engine._emit_event(None, "MODE", "INFO", f"Operating mode: {operating_mode.public()['label']}",
                               data={"operating_mode": req.mode})
    await broadcast_messages(factory_engine.flush_events())
    return operating_mode_status()


@app.get("/api/analytics")
def api_get_analytics(window: str = Query(default="window", pattern="^(window|session)$")):
    """`window` = since the last reset; `session` = since the server started."""
    return analytics_report(window)


REPORT_SECTIONS = ("summary", "energy", "oee", "trends", "alerts", "work_orders")


def build_report(trend_minutes: float = 10.0) -> Dict[str, Any]:
    """Everything the Reports page shows, from the same stores the dashboard uses."""
    n = int(trend_minutes * 60 / 2)  # one trend point every 2 s
    window, session = analytics_report("window"), analytics_report("session")
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "mode": mode_status()["mode"],
        "label": "Simulation data unless a reading is tagged LIVE",
        "window": window,
        "session": session,
        "trends": {mid: list(pts)[-n:] for mid, pts in analytics.trends.items()},
        "trend_minutes": trend_minutes,
        "alerts": list_alerts(include_resolved=True, limit=500),
        "work_orders": list_work_orders(),
    }


def _csv(rows: List[Dict[str, Any]], columns: List[str]) -> str:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=columns, extrasaction="ignore")
    w.writeheader()
    for r in rows:
        w.writerow({k: ("" if r.get(k) is None else r.get(k)) for k in columns})
    return buf.getvalue()


def report_csv(section: str, rep: Dict[str, Any]) -> str:
    if section == "trends":
        rows = [{"machine_id": mid, **p} for mid, pts in rep["trends"].items() for p in pts]
        return _csv(rows, ["machine_id", "ts", "mode", "state", "rpm", "vibration_rms_mm_s", "temp_c", "power_kw",
                           "load_pct", "zone", "risk"])
    if section == "alerts":
        return _csv(rep["alerts"], ["alert_id", "machine_id", "severity", "status", "title", "measured", "inferred",
                                    "suggested", "raised_at", "acknowledged_at", "resolved_at", "resolution",
                                    "occurrences", "wo_id"])
    if section == "work_orders":
        return _csv(rep["work_orders"], ["wo_id", "machine_id", "title", "status", "priority", "cause", "evidence",
                                         "recommended_action", "alert_id", "created_at", "acknowledged_at",
                                         "started_at", "closed_at", "operator_notes"])
    if section in ("energy", "oee"):
        rows = []
        for name, r in (("since_reset", rep["window"]), ("session", rep["session"])):
            for mid, m in r["machines"].items():
                o = m.get("oee") or {}
                rows.append({"window": name, "since": r["since"], "machine_id": mid, "template": m["template"],
                             "energy_kwh": m["energy_kwh"], "co2_kg": m["co2_kg"], "cost_inr": m["cost_inr"],
                             "mean_power_kw": m["mean_power_kw"], **{k: o.get(k) for k in
                             ("availability", "performance", "quality", "oee", "parts", "parts_quality_hold")}})
        return _csv(rows, ["window", "since", "machine_id", "template", "energy_kwh", "co2_kg", "cost_inr",
                           "mean_power_kw", "availability", "performance", "quality", "oee", "parts",
                           "parts_quality_hold"])
    # summary: one row per headline figure, with its unit and basis
    w, f = rep["window"], rep["window"]["factors"]
    line = w["site"]["line_oee"] or {}
    rows = [
        {"metric": "generated_at", "value": rep["generated_at"], "unit": "UTC", "basis": rep["label"]},
        {"metric": "mode", "value": rep["mode"], "unit": "", "basis": ""},
        {"metric": "window_since", "value": w["since"], "unit": "UTC", "basis": "since last reset"},
        {"metric": "site_energy", "value": w["site"]["energy_kwh"], "unit": "kWh", "basis": w["method"]["energy"]},
        {"metric": "site_co2", "value": w["site"]["co2_kg"], "unit": "kg", "basis": f"{f['grid_co2_kg_per_kwh']} kg/kWh ({f['grid_co2_source']})"},
        {"metric": "site_energy_cost", "value": w["site"]["cost_inr"], "unit": "INR", "basis": f"{f['tariff_inr_per_kwh']} INR/kWh"},
        {"metric": "line_oee", "value": line.get("oee"), "unit": "fraction", "basis": w["site"]["line_oee_basis"]},
        {"metric": "open_alerts", "value": sum(a["status"] != "RESOLVED" for a in rep["alerts"]), "unit": "", "basis": ""},
        {"metric": "open_work_orders", "value": sum(o["status"] != "Closed" for o in rep["work_orders"]), "unit": "", "basis": ""},
        {"metric": "payback_months", "value": w["roi"]["payback_months"], "unit": "months", "basis": "illustrative estimate: " + w["roi"]["note"]},
    ]
    return _csv(rows, ["metric", "value", "unit", "basis"])


@app.get("/api/reports")
def api_get_report(trend_minutes: float = Query(default=10.0, gt=0, le=30)):
    """Reports page data: short trends, alert history, work-order log, energy / CO₂ / OEE summary."""
    return build_report(trend_minutes)


RANGE_SECTIONS = ("telemetry", "real_telemetry", "virtual_telemetry", "events", "rl_decisions")
VIRTUAL_COLS = ["ts", "device_ts", "machine_id", "source", "data_label", "seq", "scenario", "simulated_fault", "rpm",
                "temp_c", "vibration_rms_mm_s", "current_a", "ts_source", "value_sources"]
TELEMETRY_COLS = ["ts", "machine_id", "mode", "state", "rpm", "derate_pct", "vibration_rms_mm_s", "zone", "temp_c",
                  "power_kw", "load_pct", "energy_kwh", "tool_wear_min", "risk", "failure_risk_pct", "active_scenario"]
EVENT_COLS = ["ts", "event_id", "machine_id", "kind", "severity", "title", "measured", "inferred", "suggested"]
ALERT_COLS = ["alert_id", "machine_id", "severity", "status", "title", "measured", "inferred", "suggested",
              "raised_at", "acknowledged_at", "resolved_at", "resolution", "occurrences", "wo_id"]
WO_COLS = ["wo_id", "machine_id", "title", "status", "priority", "cause", "evidence", "recommended_action", "alert_id",
           "created_at", "acknowledged_at", "started_at", "closed_at", "operator_notes"]
RL_COLS = ["ts_start", "ts_end", "machine_id", "mode", "state", "risk_rules", "failure_risk_pct", "adverse_expected",
           "recommended_action", "action_taken", "followed", "outcome_parts", "outcome_over_temp_s", "outcome_zone_c_s",
           "outcome_zone_d_s", "outcome_overload_s", "outcome_worn_tool_s", "outcome_trip", "outcome_max_temp_c",
           "outcome_max_vib_mm_s", "outcome_adverse", "parts_done_delta", "reward_inr", "prediction_correct",
           "policy_updated", "q_before", "q_after"]


def _export_range(start: Optional[str], end: Optional[str], minutes: Optional[float]) -> tuple[str, str]:
    try:
        s, e = exporter.parse_range(start, end, minutes)
    except exporter.RangeError as err:
        raise HTTPException(status_code=422, detail={"error": "invalid_range", "message": str(err)})
    return exporter.iso_ms(s), exporter.iso_ms(e)


def range_rows(section: str, start: str, end: str) -> tuple[List[Dict[str, Any]], List[str]]:
    """Rows of one time-filtered section (UTC ISO bounds)."""
    if section == "telemetry":
        return query_telemetry(start, end), TELEMETRY_COLS
    if section == "real_telemetry":  # readings from real devices only (source LIVE)
        return query_telemetry(start, end, source="real"), TELEMETRY_COLS
    if section == "virtual_telemetry":  # Wokwi simulator devices: virtual data, never real measurements
        return wokwi_store.query(start, end), VIRTUAL_COLS
    if section == "events":
        return query_events(start, end), EVENT_COLS
    if section == "rl_decisions":
        return query_rl_decisions(start, end), RL_COLS
    if section == "alerts":
        rows = [a for a in list_alerts(include_resolved=True, limit=100_000)
                if any(exporter.in_range(a.get(k), start, end) for k in ("raised_at", "acknowledged_at", "resolved_at"))]
        return rows, ALERT_COLS
    rows = [w for w in list_work_orders()
            if any(exporter.in_range(w.get(k), start, end) for k in ("created_at", "updated_at", "closed_at"))]
    return rows, WO_COLS


def _filename(stem: str, start: str, end: str, ext: str) -> str:
    fmt = lambda t: t[:16].replace("-", "").replace(":", "")  # 20261025T1000
    return f"udyam-twin-{stem}-{fmt(start)}Z-to-{fmt(end)}Z.{ext}"


RECENT_KINDS = ("telemetry", "events", "alerts", "work_orders")


def _telemetry_summary(r: Dict[str, Any]) -> str:
    parts = [f"{r['rpm']:.0f} rpm" if isinstance(r.get("rpm"), (int, float)) else None,
             f"{r['temp_c']:.1f} °C" if isinstance(r.get("temp_c"), (int, float)) else None,
             f"{r['vibration_rms_mm_s']:.2f} mm/s" if isinstance(r.get("vibration_rms_mm_s"), (int, float)) else None,
             f"zone {r['zone']}" if r.get("zone") else None,
             f"{r['power_kw']:.2f} kW" if isinstance(r.get("power_kw"), (int, float)) else None,
             r.get("state")]
    return " · ".join(p for p in parts if p)


@app.get("/api/reports/recent")
def get_recent_entries(limit: int = Query(default=60, ge=1, le=300),
                       kind: str = Query(default="all", pattern="^(all|telemetry|events|alerts|work_orders)$")):
    """The newest records as stored in the database (what the exports contain), newest first: telemetry rows
    (simulated and real, each tagged with its source), events, alerts and work orders."""
    kinds = RECENT_KINDS if kind == "all" else (kind,)
    out: List[Dict[str, Any]] = []
    if "telemetry" in kinds:
        now = datetime.now(timezone.utc)
        start, end = exporter.iso_ms(now - timedelta(minutes=2)), exporter.iso_ms(now + timedelta(seconds=5))
        for source in ("sim", "real"):
            for r in query_telemetry(start, end, source=source)[-limit:]:
                out.append({"ts": r["ts"], "kind": "telemetry", "machine_id": r["machine_id"], "source": r.get("mode") or "SIM",
                            "severity": None, "summary": _telemetry_summary(r)})
        for r in wokwi_store.query(start, end)[-limit:]:
            out.append({"ts": r["ts"], "kind": "telemetry", "machine_id": r["machine_id"], "source": "WOKWI",
                        "severity": None, "summary": f"{_telemetry_summary(r)} · {r['current_a']:.2f} A · scenario {r['scenario']}"})
    if "events" in kinds:
        for e in get_events(limit=limit):
            out.append({"ts": e["ts"], "kind": "event", "machine_id": e.get("machine_id"), "source": None,
                        "severity": e.get("severity"), "detail": e.get("kind"), "summary": e.get("title")})
    if "alerts" in kinds:
        for a in list_alerts(include_resolved=True, limit=limit):
            out.append({"ts": a["last_seen_at"], "kind": "alert", "machine_id": a.get("machine_id"), "source": None,
                        "severity": a.get("severity"), "detail": a.get("status"), "summary": a.get("title")})
    if "work_orders" in kinds:
        for w in list_work_orders()[:limit]:
            out.append({"ts": w["created_at"], "kind": "work_order", "machine_id": w.get("machine_id"), "source": None,
                        "severity": None, "detail": w.get("status"),
                        "summary": f"{w['wo_id']} · {w.get('title')} · {w.get('priority')}"})
    out.sort(key=lambda x: x["ts"] or "", reverse=True)
    return {"entries": out[:limit], "generated_at": exporter.iso_ms(datetime.now(timezone.utc)),
            "note": "Records as stored (what the exports contain). Telemetry is recorded every 2 s per machine; "
                    "LIVE rows come from real devices, SIM and REPLAY rows from the simulation."}


@app.get("/api/reports/export.csv")
def api_export_report(section: str = Query(default="summary"), trend_minutes: float = Query(default=10.0, gt=0, le=30),
                      start: Optional[str] = None, end: Optional[str] = None,
                      minutes: Optional[float] = Query(default=None, gt=0)):
    """One section as CSV. telemetry, events, rl_decisions, alerts and work_orders honour the time range
    (`start`/`end` ISO 8601, or `minutes` back from now; default the last 60 min)."""
    sections = REPORT_SECTIONS + RANGE_SECTIONS
    if section not in sections:
        raise HTTPException(status_code=422, detail=f"section must be one of {sections}")
    ranged = section in RANGE_SECTIONS + ("alerts", "work_orders")
    if ranged:
        s, e = _export_range(start, end, minutes)
        rows, cols = range_rows(section, s, e)
        body, name = exporter.csv_text(rows, cols), _filename(section, s, e, "csv")
    else:
        body = report_csv(section, build_report(trend_minutes))
        name = f"udyam-twin-{section}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%MZ')}.csv"
    return Response(content=body, media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})


def _csv_rows(text: str) -> tuple[List[Dict[str, Any]], List[str]]:
    reader = csv.DictReader(io.StringIO(text))
    return list(reader), list(reader.fieldnames or [])


def _cmapss_sheets() -> List[Dict[str, Any]]:
    """C-MAPSS RUL results for the workbook (empty until python -m ml.cmapss has run)."""
    if not CMAPSS_METRICS.exists():
        return []
    m = json.loads(CMAPSS_METRICS.read_text(encoding="utf-8"))
    rows = [{"subset": sub["subset"], "description": sub["description"], "train_engines": sub["train_engines"],
             "test_engines": sub["test_engines"], "model": name, **scores,
             "cv_rmse_capped_target": sub["cv_rmse_capped_target"]["mean"] if name == "gradient_boosting" else None}
            for sub in m["subsets"] for name, scores in sub["test"].items()]
    cols = ["subset", "description", "train_engines", "test_engines", "model", "rmse", "rmse_true_capped_125",
            "nasa_score", "mae", "cv_rmse_capped_target"]
    return [{"name": "C-MAPSS RUL", "columns": cols, "rows": rows,
             "note": f"{m['label']}. Official test engines at their last cycle; RMSE in cycles."}]


def _ims_sheets() -> List[Dict[str, Any]]:
    """IMS run-to-failure results for the workbook (empty when the dataset has not been processed)."""
    if not IMS_METRICS.exists():
        return []
    m = json.loads(IMS_METRICS.read_text(encoding="utf-8"))
    zones = [{**{k: z[k] for k in ("test", "bearing", "failed", "failure_mode", "duration_h", "baseline_vel_mm_s",
                                    "max_vel_mm_s", "final_vel_mm_s", "final_zone")},
              **{f"hours_in_{k}": v for k, v in z["hours_in_zone"].items()},
              **{f"first_zone_{k}_h": v for k, v in z["first_reached_h"].items()}} for z in m["zone_progression"]]
    ew = m["early_warning"]
    methods = [("random_forest", ew["random_forest"])] + list(ew["rules"].items())
    warn = [{"method": name, **r["overall"],
             "first_alarm_h_before_failure": " · ".join("none" if l["first_alarm_hours_before_end"] is None
                                                       else str(l["first_alarm_hours_before_end"])
                                                       for l in r["lead_times"] if l["failed"])} for name, r in methods]
    note = f"{m['label']}. Zones: {m['dataset'].get('zone_profile')} ({m['dataset'].get('zone_note')})"
    zone_cols = list(zones[0]) if zones else []
    warn_cols = ["method", "precision", "recall", "f1", "false_alarm_rate_healthy_bearings", "positives", "snapshots",
                 "first_alarm_h_before_failure"]
    return [
        {"name": "IMS zone progression", "columns": zone_cols, "rows": zones, "note": note},
        {"name": "IMS early warning", "columns": warn_cols, "rows": warn,
         "note": f"Fails within {ew['horizon_h']:g} h; {ew['validation']}. Lead times per failed bearing (T1-B3, T1-B4, T2-B1, T3-B3)."},
    ]


@app.get("/api/reports/export.xlsx")
def api_export_workbook(start: Optional[str] = None, end: Optional[str] = None,
                        minutes: Optional[float] = Query(default=None, gt=0), tz: str = "Asia/Kolkata"):
    """Excel workbook for a time range: About, Summary, Telemetry, Energy & OEE, Alerts, Work orders, Events,
    RL decisions and RL policy evaluation. Times are UTC, with a local-time column in `tz`."""
    s, e = _export_range(start, end, minutes)
    try:
        zone = exporter.zone(tz)
    except exporter.RangeError as err:
        raise HTTPException(status_code=422, detail={"error": "invalid_timezone", "message": str(err)})
    rep = build_report()
    summary_rows, summary_cols = _csv_rows(report_csv("summary", rep))
    oee_rows, oee_cols = _csv_rows(report_csv("oee", rep))
    sections = {name: range_rows(name, s, e) for name in ("telemetry", "real_telemetry", "virtual_telemetry", "alerts", "work_orders",
                                                          "events", "rl_decisions")}
    rl_eval: List[Dict[str, Any]] = []
    if RL_METRICS_PATH.exists():
        results = json.loads(RL_METRICS_PATH.read_text(encoding="utf-8")).get("results", {})
        rl_eval = [{"policy": pol, "scenario": sc, **vals} for pol, by_sc in results.items() for sc, vals in by_sc.items()]
    counts = {k: len(v[0]) for k, v in sections.items()}
    about = [
        {"field": "title", "value": "UDYAM-TWIN export"},
        {"field": "range_start_utc", "value": s}, {"field": "range_end_utc", "value": e},
        {"field": "local_time_zone", "value": tz},
        {"field": "generated_at_utc", "value": rep["generated_at"]},
        {"field": "telemetry_mode_now", "value": rep["mode"]},
        {"field": "data", "value": rep["label"]},
        {"field": "telemetry_resolution", "value": "one row per machine every 2 s"},
        {"field": "summary_and_oee_window", "value": "since the last reset / since server start (not the range)"},
        {"field": "rl", "value": "Simulation-trained Q-learning policy; recommendations only, applied by an operator"},
        *({"field": f"rows_{k}", "value": n} for k, n in counts.items()),
    ]
    note = f"{rep['label']}. Range {s} → {e} (UTC)."
    sheets = [
        {"name": "About", "columns": ["field", "value"], "rows": about},
        {"name": "Summary", "columns": summary_cols, "rows": summary_rows, "note": "Since the last reset (not the range)"},
        {"name": "Telemetry", "columns": TELEMETRY_COLS, "rows": sections["telemetry"][0],
         "note": note + " Simulated / replayed telemetry (source SIM or REPLAY).", "time_columns": ["ts"]},
        {"name": "Real telemetry", "columns": TELEMETRY_COLS, "rows": sections["real_telemetry"][0],
         "note": f"Readings from real devices only (source LIVE). Range {s} → {e} (UTC).", "time_columns": ["ts"]},
        {"name": "Virtual (Wokwi)", "columns": VIRTUAL_COLS, "rows": sections["virtual_telemetry"][0],
         "note": f"VIRTUAL data from Wokwi simulator devices (simulated sensors and software models), not real machine "
                 f"measurements. Scenario labels are simulated conditions, not verified faults. Range {s} → {e} (UTC).",
         "time_columns": ["ts", "device_ts"]},
        {"name": "Energy & OEE", "columns": oee_cols, "rows": oee_rows, "note": "Since the last reset and since server start"},
        {"name": "Alerts", "columns": ALERT_COLS, "rows": sections["alerts"][0], "note": note,
         "time_columns": ["raised_at", "acknowledged_at", "resolved_at"]},
        {"name": "Work orders", "columns": WO_COLS, "rows": sections["work_orders"][0], "note": note,
         "time_columns": ["created_at", "closed_at"]},
        {"name": "Events", "columns": EVENT_COLS, "rows": sections["events"][0], "note": note, "time_columns": ["ts"]},
        {"name": "RL decisions", "columns": RL_COLS, "rows": sections["rl_decisions"][0],
         "note": "Each row: predicted risk + recommended action → simulated outcome → evaluation (reward ₹) → policy update (SIM only)",
         "time_columns": ["ts_start"]},
        {"name": "RL policy evaluation", "columns": ["policy", "scenario", "return_inr", "parts", "trips", "over_temp_s",
                                                      "zone_d_s", "maintenance_stops"], "rows": rl_eval,
         "note": "Held-out simulation seeds, mean per 12-minute shift (models/rl_metrics.json)"},
        *_ims_sheets(),
        *_cmapss_sheets(),
    ]
    body = exporter.xlsx_bytes(sheets, zone)
    return Response(content=body, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": f'attachment; filename="{_filename("report", s, e, "xlsx")}"'})


# -----------------------------------------------------------------------------
# Reinforcement-learning maintenance policy (ml/maintenance_rl.py)
# -----------------------------------------------------------------------------
@app.get("/api/rl")
def api_rl_status(recent: int = Query(default=20, ge=0, le=200)):
    """Current recommendation per CNC mill, feedback-loop statistics and recent evaluated decisions."""
    return rl_agent.status(recent)


@app.get("/api/rl/metrics")
def api_rl_metrics():
    if not RL_METRICS_PATH.exists():
        raise HTTPException(status_code=404, detail="Run python -m ml.train_rl")
    return json.loads(RL_METRICS_PATH.read_text(encoding="utf-8"))


@app.post("/api/rl/{machine_id}/propose")
def api_rl_propose(machine_id: str):
    """Turn the current RL recommendation into a pending proposal. Nothing is applied: an operator must
    approve it through POST /api/chat/proposals/{id}/approve."""
    rec = rl_agent.recommendation(machine_id)
    if rec is None:
        raise HTTPException(status_code=404, detail={"error": "no_recommendation",
                                                     "message": f"No RL recommendation for {machine_id} yet."})
    plant = factory_engine.plants.get(machine_id)
    current = plant.derate_pct if plant else 0.0
    action, why = rec["action"], f"RL policy ({rec['label']}) for state {rec['state']}; Q = {rec['q_values']}"
    if action == "maintain":
        p = advisor.tool_propose_action(machine_id, "work_order", why, title=f"Planned maintenance stop on {machine_id} (RL)")
    elif action == "inspect":  # the work-order title is how the feedback loop recognises an inspection
        p = advisor.tool_propose_action(machine_id, "work_order", why, title=f"Inspect {machine_id} (RL short stop)")
    elif ACTION_DERATE[action] != current:
        kind = "derate" if ACTION_DERATE[action] > 0 else "clear_derate"
        p = advisor.tool_propose_action(machine_id, kind, why, derate_pct=ACTION_DERATE[action] or None)
    else:
        raise HTTPException(status_code=409, detail={"error": "nothing_to_change",
                                                     "message": f"{machine_id} is already at {current:g} % derate."})
    return {"proposal": p, "recommendation": rec}


IMS_METRICS = ROOT / "ml" / "vibration" / "artifacts" / "ims_metrics.json"
IMS_TRENDS = ROOT / "ml" / "vibration" / "artifacts" / "ims_trends.json"


CMAPSS_METRICS = ROOT / "models" / "cmapss_metrics.json"


@app.get("/api/datasets/cmapss")
def api_cmapss_results(predictions: bool = False):
    """NASA C-MAPSS turbofan RUL experiment (models/cmapss_metrics.json, written by `python -m ml.cmapss`).
    A separate research module: it never feeds the CNC machines' RUL."""
    if not CMAPSS_METRICS.exists():
        raise HTTPException(status_code=404, detail="Run python -m ml.cmapss (data: ml/data/cmapss/raw)")
    out = json.loads(CMAPSS_METRICS.read_text(encoding="utf-8"))
    if not predictions:
        for sub in out["subsets"]:
            sub.pop("predictions", None)
    return out


@app.get("/api/datasets/ims")
def api_ims_validation(trends: bool = False):
    """IMS bearing run-to-failure results: ISO 20816-3 zone progression, early warning and RUL check
    (ml/vibration/artifacts/ims_metrics.json, written by `python -m ml.vibration.train_ims`)."""
    if not IMS_METRICS.exists():
        raise HTTPException(status_code=404, detail="Run python -m ml.vibration.datasets.ims and python -m ml.vibration.train_ims")
    out = json.loads(IMS_METRICS.read_text(encoding="utf-8"))
    if trends and IMS_TRENDS.exists():
        out["trends"] = json.loads(IMS_TRENDS.read_text(encoding="utf-8"))
    return out


@app.get("/api/metrics")
def api_get_metrics():
    """Test metrics of the deployed AI4I failure model: the registry's production version once the registry is in
    use, otherwise models/metrics.json (written by `python -m ml.train_ai4i`)."""
    if model_registry.index_path.is_file():
        version = model_registry.production()
        return {**model_registry.metrics(version), "registry_version": version}
    metrics_file = ROOT / "models" / "metrics.json"
    if metrics_file.exists():
        return json.loads(metrics_file.read_text(encoding="utf-8"))
    raise HTTPException(status_code=404, detail="Metrics not found")


# -----------------------------------------------------------------------------
# Advisor chatbot (api/advisor.py)
# -----------------------------------------------------------------------------
@app.get("/api/chat/status")
def api_chat_status():
    """Which chat model is configured (never returns a key)."""
    return advisor.status()


@app.post("/api/chat")
async def api_chat(req: ChatRequest):
    """Ask the Advisor. Facts come only from server tools; actions are proposals the operator must approve.

    Gemini runs on the SDK's async client and tools read the engine on this event loop, so telemetry keeps
    ticking and no tool sees a half-updated tick.
    """
    try:
        return await advisor.chat(req)
    except ChatError as err:
        raise HTTPException(status_code=err.status, detail=err.detail())


# Operator approval of Advisor proposals: the only way a chatbot suggestion is ever executed.
_LOOPBACK = {"127.0.0.1", "::1", "localhost"}
_PROPOSAL_ID = re.compile(r"^[0-9a-f]{32}$")


class ProposalDecision(BaseModel):
    operator: str = Field(default="operator", min_length=1, max_length=60)


def _authorize_operator(request: Request, token: Optional[str]) -> None:
    """Approval needs UDYAM_OPERATOR_TOKEN when it is set; otherwise only a browser on this machine may approve."""
    origin, host = request.headers.get("origin"), request.headers.get("host")
    if origin and host and urlparse(origin).netloc != host:
        raise HTTPException(status_code=403, detail={"error": "cross_origin", "message": "Approvals must come from the dashboard."})
    expected = _env_secret("UDYAM_OPERATOR_TOKEN")
    if expected:
        if not token or not hmac.compare_digest(token.encode(), expected.encode()):
            raise HTTPException(status_code=401, detail={"error": "operator_token_required",
                                                         "message": "A valid operator token is required to approve actions."})
        return
    if (request.client.host if request.client else "") not in _LOOPBACK:
        raise HTTPException(status_code=403, detail={
            "error": "approval_requires_local_operator",
            "message": "Approving from another device needs UDYAM_OPERATOR_TOKEN to be set on the server."})


def _decide(proposal_id: str, decision: str, operator: str) -> Dict[str, Any]:
    if not _PROPOSAL_ID.match(proposal_id):
        raise HTTPException(status_code=404, detail={"error": "unknown_proposal", "message": "Proposal not found."})
    try:
        return advisor.claim_proposal(proposal_id, decision, operator)
    except ChatError as err:
        raise HTTPException(status_code=err.status, detail=err.detail())


# -----------------------------------------------------------------------------
# Model registry: candidate training on AI4I + synthetic data, gated promotion, rollback (ml/model_registry.py)
# -----------------------------------------------------------------------------
class CandidateTrainRequest(BaseModel):
    datasets: List[str] = Field(min_length=1, max_length=10)
    synthetic_weight: float = Field(default=1.0, ge=0.1, le=5.0)
    use_confirmed_labels: bool = False


class ModelDeployRequest(BaseModel):
    operator: str = Field(default="operator", min_length=1, max_length=60)
    to_version: Optional[str] = Field(default=None, pattern="^v[0-9]{3}$")


def _registry_call(fn):
    try:
        return fn()
    except RegistryError as err:
        raise HTTPException(status_code=409, detail={"error": "model_registry", "message": str(err)})


def models_status() -> Dict[str, Any]:
    reg = _registry_call(model_registry.state)
    return {**reg, "gate_criteria": MODEL_GATE, "training": training_job.status(),
            "deployed_model": _deployed_model_path().name if not _deployed_model_path().is_relative_to(ROOT)
            else str(_deployed_model_path().relative_to(ROOT)),
            "synthetic_datasets": [{k: d.get(k) for k in ("dataset_id", "name", "config", "counts")}
                                   for d in synthetic_store.list()],
            "note": ("Versions marked real_machine_data include operator-confirmed samples from real machines; "
                     "all other training data is synthetic (AI4I 2020 and SYNTHETIC DIGITAL TWIN DATA)."
                     if any(v.get("real_machine_data") for v in reg["versions"]) else
                     "Every version is trained on synthetic data only (AI4I 2020 and SYNTHETIC DIGITAL TWIN DATA); "
                     "none has learned from real machines.")}


@app.get("/api/models/ai4i")
def get_models():
    """Model registry: versions with their evaluation gate, the production version, history and training job."""
    return models_status()


@app.get("/api/models/ai4i/{version}/metrics")
def get_model_metrics(version: str):
    return _registry_call(lambda: model_registry.metrics(version))


@app.post("/api/models/ai4i/train", status_code=202)
def post_train_candidate(req: CandidateTrainRequest):
    """Train a candidate in the background. Training never deploys anything: it is only registered."""
    _registry_call(model_registry.state)  # registers the baseline first if needed
    try:
        return training_job.start(req.datasets, req.synthetic_weight, by="dashboard", use_labels=req.use_confirmed_labels)
    except CandidateError as err:
        raise HTTPException(status_code=409 if "already" in str(err) else 422,
                            detail={"error": "candidate_training", "message": str(err)})


async def _model_switched(action: str, reg: Dict[str, Any], operator: str) -> Dict[str, Any]:
    use_model_path(_deployed_model_path)  # the twin reloads the model on its next evaluation
    last = reg["history"][-1]
    factory_engine._emit_event(None, "MODEL", "INFO",
                               f"Failure-risk model {action}: {last['from']} → {last['version']} (by {operator})",
                               data={"action": action, **last})
    await broadcast_messages(factory_engine.flush_events())
    return models_status()


@app.post("/api/models/ai4i/{version}/promote")
async def post_promote_model(version: str, request: Request, body: Optional[ModelDeployRequest] = None,
                             x_operator_token: Optional[str] = Header(default=None)):
    """Deploy a candidate that passed its evaluation gate. Needs an operator; refused in Real Data mode."""
    _authorize_operator(request, x_operator_token)
    _testing_only("Changing the deployed model")
    operator = (body.operator if body else "operator").strip() or "operator"
    reg = _registry_call(lambda: model_registry.promote(version, by=operator))
    return await _model_switched("promoted", reg, operator)


@app.post("/api/models/ai4i/rollback")
async def post_rollback_model(request: Request, body: Optional[ModelDeployRequest] = None,
                              x_operator_token: Optional[str] = Header(default=None)):
    """Make an earlier production version (default: the previous one) production again."""
    _authorize_operator(request, x_operator_token)
    _testing_only("Changing the deployed model")
    operator = (body.operator if body else "operator").strip() or "operator"
    reg = _registry_call(lambda: model_registry.rollback(by=operator, to_version=body.to_version if body else None))
    return await _model_switched("rolled back", reg, operator)


# -----------------------------------------------------------------------------
# Adaptive learning: input drift and operator-confirmed labels (ml/adaptive.py)
# -----------------------------------------------------------------------------
class BaselineRequest(BaseModel):
    machine_id: str
    source: str = Field(default="SIM", pattern="^(SIM|REPLAY|LIVE)$")


class LabelRequest(BaseModel):
    machine_id: str
    label: str = Field(pattern="^(failure|false_alarm|normal)$")
    source: Optional[str] = Field(default=None, pattern="^(SIM|REPLAY|LIVE)$")
    alert_id: Optional[str] = None
    minutes: Optional[float] = Field(default=None, gt=0, le=30)
    start: Optional[str] = None
    end: Optional[str] = None
    fault_type: Optional[str] = Field(default=None, max_length=60)
    note: Optional[str] = Field(default=None, max_length=300)
    operator: str = Field(default="operator", min_length=1, max_length=60)


def _current_source(machine_id: str) -> str:
    if machine_id in set(real_connection().get("live_machines") or []):
        return "LIVE"
    return "REPLAY" if factory_engine.replay_machine == machine_id else "SIM"


def _label_window(req: LabelRequest) -> tuple[datetime, datetime, Optional[str]]:
    now = datetime.now(timezone.utc)
    if req.alert_id:
        a = get_alert(req.alert_id)
        if a is None or a.get("machine_id") != req.machine_id:
            raise HTTPException(status_code=404, detail={"error": "unknown_alert", "message": "No such alert on that machine."})
        start = _parse_iso(a["raised_at"]) - timedelta(minutes=5)
        end = min(now, _parse_iso(a["last_seen_at"]) + timedelta(minutes=1), start + MAX_LABEL_WINDOW)
        return start, end, a["alert_id"]
    if req.minutes:
        return now - timedelta(minutes=req.minutes), now, None
    if req.start and req.end:
        return _parse_iso(req.start), _parse_iso(req.end), None
    raise HTTPException(status_code=422, detail={"error": "label_window", "message": "Give alert_id, minutes, or start and end."})


def _parse_iso(ts: str) -> datetime:
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        raise HTTPException(status_code=422, detail={"error": "bad_time", "message": f"Not an ISO time: {ts}"})
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def adaptive_status() -> Dict[str, Any]:
    labels = label_store.list(include_retracted=True)
    active = [l for l in labels if l["status"] == "ACTIVE"]
    return {"drift": drift_monitor.status(), "reference": production_reference().public(),
            "labels": labels[:100],
            "label_counts": {"active": len(active), "failure": sum(l["label"] == "failure" for l in active),
                             "false_alarm": sum(l["label"] == "false_alarm" for l in active),
                             "normal": sum(l["label"] == "normal" for l in active),
                             "from_real_machines": sum(l["source"] == "LIVE" for l in active)},
            "note": "Drift is information, not a fault: nothing is retrained or changed automatically. Only "
                    "operator-confirmed labels are used for training; unlabelled readings never are."}


@app.get("/api/adaptive")
def get_adaptive():
    return adaptive_status()


@app.post("/api/adaptive/baseline")
def post_adaptive_baseline(req: BaselineRequest):
    """Use the last 5 minutes as the machine's new baseline (e.g. after an intended change of job or settings)."""
    try:
        drift_monitor.set_baseline(req.machine_id, req.source, by="dashboard")
    except ValueError as err:
        raise HTTPException(status_code=409, detail={"error": "baseline", "message": str(err)})
    return adaptive_status()


@app.post("/api/adaptive/labels", status_code=201)
async def post_adaptive_label(req: LabelRequest, request: Request, x_operator_token: Optional[str] = Header(default=None)):
    """Confirm what happened in a window (failure / false alarm / normal). Needs an operator; allowed in both modes
    (labelling is a record, not a machine action). The buffered model inputs for the window are stored with it."""
    _authorize_operator(request, x_operator_token)
    start, end, alert_id = _label_window(req)
    source = req.source or _current_source(req.machine_id)
    try:
        lab = label_store.create(machine_id=req.machine_id, source=source, start=start, end=end, label=req.label,
                                 samples=drift_monitor.window(req.machine_id, source, start, end),
                                 operator=req.operator.strip() or "operator", fault_type=req.fault_type,
                                 alert_id=alert_id, note=req.note)
    except LabelError as err:
        raise HTTPException(status_code=422, detail={"error": "label", "message": str(err)})
    factory_engine._emit_event(req.machine_id, "LABEL", "INFO",
                               f"{lab['label_text']} confirmed by {lab['operator']} on {req.machine_id} ({source}, "
                               f"{lab['n_samples']} samples)", data={"label_id": lab["id"], "label": lab["label"], "source": source})
    await broadcast_messages(factory_engine.flush_events())
    return lab


@app.post("/api/adaptive/labels/{label_id}/retract")
def post_retract_label(label_id: int, request: Request, body: Optional[ProposalDecision] = None,
                       x_operator_token: Optional[str] = Header(default=None)):
    _authorize_operator(request, x_operator_token)
    try:
        return label_store.retract(label_id, (body.operator if body else "operator").strip() or "operator")
    except LabelError as err:
        raise HTTPException(status_code=409, detail={"error": "label", "message": str(err)})


@app.get("/api/adaptive/labels/export")
def get_labels_export(format: str = Query(default="csv", pattern="^(csv|json)$")):
    """Every active label's samples with their origin (real machine or simulation)."""
    rows = [{**r, "data_origin": "REAL MACHINE (operator-confirmed)" if r["source"] == "LIVE"
             else "SIMULATION (operator-confirmed)"} for r in label_store.training_rows()]
    cols = ["ts", "label_id", "source", "data_origin", *FEATURES_AI4I, "machine_failure"]
    if format == "json":
        return JSONResponse({"rows": rows, "columns": cols},
                            headers={"Content-Disposition": 'attachment; filename="confirmed_labels.json"'})
    return Response(_csv(rows, cols), media_type="text/csv",
                    headers={"Content-Disposition": 'attachment; filename="confirmed_labels.csv"'})


# -----------------------------------------------------------------------------
# What-if comparisons (sim/whatif.py): separate engines, never the live simulation
# -----------------------------------------------------------------------------
class WhatIfScenarioIn(BaseModel):
    name: str = Field(min_length=1, max_length=40)
    params: Dict[str, Dict[str, float]] = Field(default_factory=dict)
    fault: Optional[Dict[str, Any]] = None
    auto_derate: bool = True


class WhatIfRequest(BaseModel):
    scenarios: List[WhatIfScenarioIn] = Field(min_length=whatif.MIN_SCENARIOS, max_length=whatif.MAX_SCENARIOS)
    minutes: float = Field(default=15.0, ge=1.0, le=whatif.MAX_MINUTES)
    seed: int = Field(default=42, ge=0, le=2**31 - 1)
    focus_machine: str = "VMC-01"


@app.get("/api/whatif/options")
def get_whatif_options():
    probe = FactoryEngine(machines=4)
    return {"label": whatif.RESULT_LABEL, "faults": whatif.FAULTS, "max_scenarios": whatif.MAX_SCENARIOS,
            "max_minutes": whatif.MAX_MINUTES,
            "machines": [{"machine_id": mid, "name": p.name, "template": p.template,
                          "params": factory_engine.machine_params(mid)["specs"] if mid in factory_engine.plants else []}
                         for mid, p in probe.plants.items()],
            "live_params": {mid: v for mid, v in factory_engine.sim_params.items() if mid in probe.plants}}


@app.post("/api/whatif/run", status_code=202)
def post_whatif_run(req: WhatIfRequest):
    """Compare scenarios on separate simulated lines (allowed in both modes: nothing live is touched)."""
    scenarios = [whatif.Scenario(s.name.strip(), s.params, s.fault, s.auto_derate) for s in req.scenarios]
    try:
        return whatif_jobs.start(scenarios, req.minutes, req.seed, req.focus_machine, get_economics())
    except whatif.WhatIfError as err:
        raise HTTPException(status_code=409 if "already" in str(err) else 422,
                            detail={"error": "whatif", "message": str(err)})


@app.get("/api/whatif/job")
def get_whatif_job():
    return whatif_jobs.status()


@app.post("/api/whatif/job/cancel")
def post_whatif_cancel():
    whatif_jobs.cancel()
    return whatif_jobs.status()


@app.get("/api/whatif/results")
def get_whatif_results():
    return [{"id": r["id"], "created_at": r["created_at"], "minutes": r["minutes"], "focus_machine": r["focus_machine"],
             "scenarios": [s["name"] for s in r["scenarios"]]} for r in reversed(list(whatif_jobs.results.values()))]


def _whatif_result(rid: str) -> Dict[str, Any]:
    r = whatif_jobs.results.get(rid)
    if r is None:
        raise HTTPException(status_code=404, detail=f"No what-if result {rid}")
    return r


@app.get("/api/whatif/results/{rid}")
def get_whatif_result(rid: str):
    return _whatif_result(rid)


@app.get("/api/whatif/results/{rid}/export")
def get_whatif_export(rid: str, format: str = Query(default="csv", pattern="^(csv|json)$")):
    r = _whatif_result(rid)
    if format == "json":
        return JSONResponse(r, headers={"Content-Disposition": f'attachment; filename="{rid}.json"'})
    keys = list(r["scenarios"][0]["kpis"])
    rows = [{"label": r["label"], "scenario": s["name"], **s["kpis"],
             **{f"delta_{k}": (s.get("delta_vs_baseline") or {}).get(k) for k in keys}} for s in r["scenarios"]]
    cols = ["label", "scenario", *keys, *[f"delta_{k}" for k in keys]]
    return Response(_csv(rows, cols), media_type="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="{rid}.csv"'})


# -----------------------------------------------------------------------------
# Factory Run Designer (sim/factory_run.py): whole-factory test runs, fast or played live
# -----------------------------------------------------------------------------
class FactoryRunSpecIn(BaseModel):
    name: str = Field(default="Factory run", min_length=1, max_length=60)
    minutes: float = Field(default=30.0, ge=1.0, le=factory_run.MAX_MINUTES)
    seed: int = Field(default=7, ge=0, le=2**31 - 1)
    self_healing: bool = True
    events: List[Dict[str, Any]] = Field(default_factory=list, max_length=factory_run.MAX_EVENTS)

    def spec(self) -> factory_run.RunSpec:
        return factory_run.RunSpec(self.name.strip(), self.minutes, self.seed, self.self_healing, [dict(e) for e in self.events])


class FactoryBatchIn(BaseModel):
    spec: FactoryRunSpecIn
    runs: int = Field(default=10, ge=2, le=factory_run.MAX_BATCH)
    fault_time_jitter_min: float = Field(default=0.0, ge=0, le=30)
    severity_range: Optional[List[float]] = Field(default=None, min_length=2, max_length=2)
    param_jitter_pct: float = Field(default=0.0, ge=0, le=50)


def _run_error(err: Exception) -> HTTPException:
    return HTTPException(status_code=409 if "already" in str(err) else 422,
                         detail={"error": "factory_run", "message": str(err)})


@app.get("/api/factory-run/options")
def get_factory_run_options():
    machines = factory_run.machines_of(factory_engine.custom_machines)
    from sim import controls as _controls
    return {"label": factory_run.DATA_LABEL, "actions": list(factory_run.ACTIONS), "max_minutes": factory_run.MAX_MINUTES,
            "max_events": factory_run.MAX_EVENTS, "max_batch": factory_run.MAX_BATCH, "severity_range": factory_run.SEVERITY_RANGE,
            "presets": factory_run.PRESETS,
            "machines": [{"machine_id": mid, "name": m["name"], "template": m["template"],
                          "faults": list(factory_run.FAULTS_BY_TEMPLATE.get(m["template"], ())),
                          "params": _controls.specs_for(m["template"], m["nameplate"])} for mid, m in machines.items()]}


@app.post("/api/factory-run/run", status_code=202)
def post_factory_run(req: FactoryRunSpecIn):
    """Fast run on a separate engine (the live simulation is not touched)."""
    spec, custom, econ = req.spec(), list(factory_engine.custom_machines), get_economics()
    try:
        factory_run.validate(spec, factory_run.machines_of(custom))
        return factory_run_jobs.start("run", lambda progress, cancelled: factory_run.run(spec, econ, custom, cancelled), 1)
    except factory_run.RunError as err:
        raise _run_error(err)


@app.post("/api/factory-run/batch", status_code=202)
def post_factory_batch(req: FactoryBatchIn):
    spec, custom, econ = req.spec.spec(), list(factory_engine.custom_machines), get_economics()
    var = factory_run.Variation(req.fault_time_jitter_min, req.severity_range, req.param_jitter_pct)
    try:
        factory_run.validate(spec, factory_run.machines_of(custom))
        if var.severity_range and not (factory_run.SEVERITY_RANGE[0] <= var.severity_range[0] <= var.severity_range[1]
                                       <= factory_run.SEVERITY_RANGE[1]):
            raise factory_run.RunError("severity range must be within 0.5–2 and min ≤ max")
        return factory_run_jobs.start("batch", lambda progress, cancelled: factory_run.batch(
            spec, var, req.runs, econ, custom, progress, cancelled), req.runs)
    except factory_run.RunError as err:
        raise _run_error(err)


@app.get("/api/factory-run/job")
def get_factory_run_job():
    return factory_run_jobs.status()


@app.post("/api/factory-run/job/cancel")
def post_factory_run_cancel():
    factory_run_jobs.cancel()
    return factory_run_jobs.status()


@app.get("/api/factory-run/results")
def get_factory_run_results():
    return [{"id": r["id"], "kind": r["kind"], "name": r["name"], "created_at": r["created_at"], "minutes": r["minutes"],
             "runs": r.get("runs", 1)} for r in reversed(list(factory_run_jobs.results.values()))]


def _factory_result(rid: str) -> Dict[str, Any]:
    r = factory_run_jobs.results.get(rid)
    if r is None:
        raise HTTPException(status_code=404, detail=f"No factory run result {rid}")
    return r


@app.get("/api/factory-run/results/{rid}")
def get_factory_run_result(rid: str):
    return _factory_result(rid)


@app.get("/api/factory-run/results/{rid}/export")
def get_factory_run_export(rid: str, format: str = Query(default="csv", pattern="^(csv|json)$")):
    """CSV: one row per machine every 10 s, with the faults active at that moment as the label. JSON: everything."""
    r = _factory_result(rid)
    if format == "json":
        return JSONResponse(r, headers={"Content-Disposition": f'attachment; filename="SYNTHETIC_{rid}.json"'})
    if r["kind"] == "batch":  # a batch keeps each run's summary: export the ground-truth labels and detection
        cols = ["data_label", "run", "seed", "machine_id", "fault", "severity", "start_min", "end_min", "detected",
                "detection_delay_min", "first_alert"]
        rows = [{"data_label": factory_run.DATA_LABEL, "run": run_["name"], "seed": run_["seed"], **lab}
                for run_ in r["detail"] for lab in run_["labels"]]
        return Response(_csv(rows, cols), media_type="text/csv",
                        headers={"Content-Disposition": f'attachment; filename="SYNTHETIC_{rid}_labels.csv"'})
    cols = ["data_label", "run", "seed", "t_min", "machine_id", "state", "rpm", "power_kw", "temp_c", "vibration_mm_s",
            "pressure_bar", "derate_pct", "zone", "label_faults"]
    rows = []
    for run_ in [r]:
        trends = run_.get("trends")
        for mid, pts in trends.items():
            for p in pts:
                rows.append({"data_label": factory_run.DATA_LABEL, "run": run_["name"], "seed": run_["seed"], "machine_id": mid,
                             **{k: p.get(k) for k in cols if k in p}, "label_faults": "|".join(p["faults"]) or "normal"})
    return Response(_csv(rows, cols), media_type="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="SYNTHETIC_{rid}.csv"'})


@app.get("/api/factory-run/live")
def get_factory_live():
    return factory_playback.status()


@app.post("/api/factory-run/live/start")
async def post_factory_live_start(req: FactoryRunSpecIn):
    """Play the run on the live simulation in real time (Testing mode only). Resets the live plant first."""
    if factory_playback.state == "running":
        raise HTTPException(status_code=409, detail={"error": "factory_run", "message": "a run is already playing"})
    try:
        await factory_playback.start(req.spec())
    except factory_run.RunError as err:
        raise _run_error(err)
    return factory_playback.status()


@app.post("/api/factory-run/live/stop")
async def post_factory_live_stop():
    await factory_playback.stop("stopped by the operator")
    return factory_playback.status()


# -----------------------------------------------------------------------------
# Factory datasets (ml/factory_dataset.py): labelled whole-factory data collected from run designs
# -----------------------------------------------------------------------------
class FactoryDatasetPartIn(BaseModel):
    spec: FactoryRunSpecIn
    runs: int = Field(default=10, ge=1, le=factory_dataset.MAX_RUNS)
    fault_time_jitter_min: float = Field(default=0.0, ge=0, le=30)
    severity_range: Optional[List[float]] = Field(default=None, min_length=2, max_length=2)
    param_jitter_pct: float = Field(default=0.0, ge=0, le=50)


class FactoryDatasetIn(BaseModel):
    name: str = Field(default="", max_length=60)
    test_share: float = Field(default=0.2, ge=0.0, le=0.5)
    split_seed: int = Field(default=0, ge=0, le=2**31 - 1)
    parts: List[FactoryDatasetPartIn] = Field(min_length=1, max_length=factory_dataset.MAX_PARTS)


def _dataset_error(err: Exception, code: int = 422) -> HTTPException:
    return HTTPException(status_code=code, detail={"error": "factory_dataset", "message": str(err)})


def _factory_dataset_meta(dataset_id: str) -> Dict[str, Any]:
    try:
        return factory_datasets.metadata(dataset_id)
    except factory_dataset.DatasetError as err:
        raise _dataset_error(err, 400)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"No factory dataset {dataset_id}")


@app.post("/api/factory-dataset/collect", status_code=202)
def post_factory_dataset_collect(req: FactoryDatasetIn):
    """Collect a dataset from the recipe on separate engines (the live simulation is not touched)."""
    parts = [factory_dataset.Part(p.spec.spec(), p.runs, factory_run.Variation(p.fault_time_jitter_min, p.severity_range,
                                                                              p.param_jitter_pct)) for p in req.parts]
    try:
        return factory_datasets.start(parts, get_economics(), req.name, req.test_share, req.split_seed,
                                      list(factory_engine.custom_machines))
    except factory_dataset.DatasetError as err:
        raise _dataset_error(err, 409 if "already" in str(err) else 422)


@app.get("/api/factory-dataset/job")
def get_factory_dataset_job():
    return factory_datasets.status()


@app.post("/api/factory-dataset/job/cancel")
def post_factory_dataset_cancel():
    factory_datasets.cancel()
    return factory_datasets.status()


@app.get("/api/factory-dataset/datasets")
def get_factory_datasets():
    return {"data_label": factory_dataset.DATA_LABEL, "datasets": factory_datasets.list(),
            "limits": {"runs": factory_dataset.MAX_RUNS, "parts": factory_dataset.MAX_PARTS,
                       "simulated_minutes": factory_dataset.MAX_RUN_MINUTES}}


@app.get("/api/factory-dataset/datasets/{dataset_id}")
def get_factory_dataset(dataset_id: str):
    return _factory_dataset_meta(dataset_id)


@app.get("/api/factory-dataset/datasets/{dataset_id}/preview")
def get_factory_dataset_preview(dataset_id: str, run_id: Optional[str] = None, machine_id: Optional[str] = None):
    _factory_dataset_meta(dataset_id)
    return factory_datasets.preview(dataset_id, run_id, machine_id)


@app.get("/api/factory-dataset/datasets/{dataset_id}/download")
def get_factory_dataset_download(dataset_id: str,
                                 part: str = Query(default="zip", pattern="^(zip|metadata|readings|windows|line|events|labels|runs)$"),
                                 split: str = Query(default="all", pattern="^(all|train|test)$")):
    """zip: every file with metadata. A CSV part can be limited to the train or test runs."""
    meta = _factory_dataset_meta(dataset_id)
    base = f"SYNTHETIC_{dataset_id}"
    hdr = {"X-Data-Label": factory_dataset.DATA_LABEL}
    if part == "zip":
        return Response(factory_datasets.zip_bytes(dataset_id), media_type="application/zip",
                        headers={**hdr, "Content-Disposition": f'attachment; filename="{base}.zip"'})
    if part == "metadata":
        return Response(json.dumps(meta, indent=2), media_type="application/json",
                        headers={**hdr, "Content-Disposition": f'attachment; filename="{base}_metadata.json"'})
    path = factory_datasets.file(dataset_id, part)
    if split == "all":
        body = path.read_bytes()
    else:
        with open(path, newline="") as fh:
            reader = csv.reader(fh)
            head = next(reader)
            col = head.index("split")
            out = io.StringIO()
            w = csv.writer(out, lineterminator="\r\n")
            w.writerow(head)
            w.writerows(r for r in reader if r[col] == split)
        body = out.getvalue().encode()
    suffix = "" if split == "all" else f"_{split}"
    return Response(body, media_type="text/csv",
                    headers={**hdr, "Content-Disposition": f'attachment; filename="{base}_{part}{suffix}.csv"'})


@app.delete("/api/factory-dataset/datasets/{dataset_id}")
def delete_factory_dataset(dataset_id: str):
    try:
        factory_datasets.delete(dataset_id)
    except factory_dataset.DatasetError as err:
        raise _dataset_error(err, 409)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"No factory dataset {dataset_id}")
    return {"deleted": dataset_id}


# -----------------------------------------------------------------------------
# Factory learning (ml/factory_learning.py): fault diagnosis, time to limit and the RL policy, trained on factory
# datasets, evaluated on held-out runs, gated, deployed only by an operator, with rollback
# -----------------------------------------------------------------------------
class FactoryTrainIn(BaseModel):
    datasets: List[str] = Field(min_length=1, max_length=10)
    episodes: int = Field(default=1500, ge=200, le=20000)


def _factory_kind(kind: str):
    if kind not in factory_learning.KINDS:
        raise HTTPException(status_code=404, detail=f"No factory model kind {kind}")
    return factory_registries[kind]


def factory_learning_status() -> Dict[str, Any]:
    out = {}
    for kind, title in factory_learning.KINDS.items():
        reg = _registry_call(factory_registries[kind].state)
        out[kind] = {**reg, "title": title, "gate_criteria": factory_learning.GATES[kind]}
    return {"data_label": factory_learning.DATA_LABEL, "models": out, "training": factory_training.status(),
            "datasets": [{k: d.get(k) for k in ("dataset_id", "name", "created_at", "counts")} for d in factory_datasets.list()],
            "note": "Every factory model is trained and evaluated only on SYNTHETIC DIGITAL TWIN DATA (held-out simulated "
                    "runs); none has learned from real machines. Deploying needs an operator and Testing mode."}


def _rl_production_path() -> Optional[Path]:
    reg = factory_registries["rl"]
    if reg.index_path.is_file() and reg.production() not in (None, "v001"):
        return reg.production_model_path()
    return None


def _use_rl_production() -> None:
    """The live RL agent recommends with the production policy of the factory_rl registry (v001 is the shipped one)."""
    from ml.maintenance_rl import POLICY_PATH, QPolicy
    pol = QPolicy.load(_rl_production_path() or POLICY_PATH)
    if pol is not None:
        rl_agent.policy, rl_agent.trained = pol, True


_use_rl_production()


@app.get("/api/factory-learning")
def get_factory_learning():
    return factory_learning_status()


@app.get("/api/factory-learning/live")
def get_factory_learning_live():
    """The production fault and time-to-limit models on the live simulation's last 60 s (read-only)."""
    if operating_mode.mode != "TESTING":
        return {"machines": [], "models": {}, "note": "Factory models run on the simulation only (Testing mode)."}
    return live_factory_models.predict()


@app.post("/api/factory-learning/{kind}/train", status_code=202)
def post_factory_learning_train(kind: str, req: FactoryTrainIn):
    """Train a candidate in the background. Training never deploys anything: it is only registered with its gate."""
    reg = _factory_kind(kind)
    _registry_call(reg.state)  # registers the RL baseline (the shipped policy) first if needed
    try:
        return factory_training.start(kind, req.datasets, by="dashboard", episodes=req.episodes)
    except factory_learning.LearningError as err:
        raise HTTPException(status_code=409 if "already" in str(err) else 422,
                            detail={"error": "factory_learning", "message": str(err)})


@app.get("/api/factory-learning/{kind}/{version}/metrics")
def get_factory_learning_metrics(kind: str, version: str):
    reg = _factory_kind(kind)
    return _registry_call(lambda: reg.metrics(version))


async def _factory_model_switched(kind: str, action: str, reg: Dict[str, Any], operator: str) -> Dict[str, Any]:
    if kind == "rl":
        _use_rl_production()
    last = reg["history"][-1]
    factory_engine._emit_event(None, "MODEL", "INFO",
                               f"{factory_learning.KINDS[kind]} model {action}: {last['from'] or 'none'} → {last['version']} (by {operator})",
                               data={"action": action, "model": f"factory_{kind}", **last})
    await broadcast_messages(factory_engine.flush_events())
    return factory_learning_status()


@app.post("/api/factory-learning/{kind}/{version}/promote")
async def post_factory_learning_promote(kind: str, version: str, request: Request, body: Optional[ModelDeployRequest] = None,
                                        x_operator_token: Optional[str] = Header(default=None)):
    """Deploy a candidate that passed its gate. Needs an operator; refused in Real Data mode."""
    reg = _factory_kind(kind)
    _authorize_operator(request, x_operator_token)
    _testing_only("Changing a deployed factory model")
    operator = (body.operator if body else "operator").strip() or "operator"
    state = _registry_call(lambda: reg.promote(version, by=operator))
    return await _factory_model_switched(kind, "promoted", state, operator)


@app.post("/api/factory-learning/{kind}/rollback")
async def post_factory_learning_rollback(kind: str, request: Request, body: Optional[ModelDeployRequest] = None,
                                         x_operator_token: Optional[str] = Header(default=None)):
    """Make an earlier production version (default: the previous one) production again."""
    reg = _factory_kind(kind)
    _authorize_operator(request, x_operator_token)
    _testing_only("Changing a deployed factory model")
    operator = (body.operator if body else "operator").strip() or "operator"
    state = _registry_call(lambda: reg.rollback(by=operator, to_version=body.to_version if body else None))
    return await _factory_model_switched(kind, "rolled back", state, operator)


@app.post("/api/chat/proposals/{proposal_id}/approve")
async def api_approve_proposal(proposal_id: str, request: Request, body: Optional[ProposalDecision] = None,
                               x_operator_token: Optional[str] = Header(default=None)):
    """Operator approval: executes the proposal once, on the simulated plant only (never a physical machine)."""
    _authorize_operator(request, x_operator_token)
    operator = (body or ProposalDecision()).operator
    pending = advisor.proposals.get(proposal_id) or {}
    if pending.get("action") in ("derate", "clear_derate"):
        _testing_only("Applying a derate in the simulator")
    p = _decide(proposal_id, "approved", operator)
    if p["action"] in ("derate", "clear_derate"):
        pct = float(p.get("derate_pct", 0.0)) if p["action"] == "derate" else 0.0
        await broadcast_messages(factory_engine.command({"cmd": "set_derate", "machine_id": p["machine_id"], "pct": pct}))
        result = {"applied_to": "simulator", "machine_id": p["machine_id"], "derate_pct": pct}
    else:
        wo = create_work_order(
            machine_id=p["machine_id"], title=p.get("title") or f"Inspect {p['machine_id']}", priority="High",
            notes=f"From Advisor proposal {proposal_id}, approved by {operator}",
            cause=p.get("title") or "Advisor recommendation", evidence=p.get("reason", ""),
            recommended_action=p.get("reason", ""))
        await _broadcast_work_order("created", wo)
        result = {"work_order": wo["wo_id"], "machine_id": p["machine_id"]}
    advisor.record_execution(proposal_id, result)
    return {"status": "executed", "proposal_id": proposal_id, "approved_by": operator, "result": result,
            "note": "Applied in UDYAM-Twin only (simulated plant / work-order log); no command is sent to a physical machine."}


@app.post("/api/chat/proposals/{proposal_id}/dismiss")
async def api_dismiss_proposal(proposal_id: str, request: Request, body: Optional[ProposalDecision] = None,
                               x_operator_token: Optional[str] = Header(default=None)):
    _authorize_operator(request, x_operator_token)
    p = _decide(proposal_id, "dismissed", (body or ProposalDecision()).operator)
    return {"status": "dismissed", "proposal_id": proposal_id, "proposal": p}


@app.get("/api/sim-metrics")
def api_sim_metrics():
    """UDYAM-SIM model metrics (our own simulation dataset; see ml/data/UDYAM_SIM_DATASET.md)."""
    path = ROOT / "ml" / "sim_metrics.json"
    if not path.exists():
        raise HTTPException(status_code=404, detail="Run python -m tools.generate_dataset && python -m ml.train_sim")
    return json.loads(path.read_text())


# -----------------------------------------------------------------------------
# WebSocket Telemetry Channel
# -----------------------------------------------------------------------------
WS_TESTING_COMMANDS = {"inject_fault", "clear_faults", "start_scenario", "resolve", "reset", "auto_demo", "set_mode",
                       "set_derate"}


@app.websocket("/ws/telemetry")
async def websocket_telemetry_endpoint(ws: WebSocket):
    await ws.accept()
    # Initial connection handshakes per docs/SCHEMA.md
    await ws.send_text(json.dumps(factory_engine.hello()))
    await ws.send_text(json.dumps(factory_engine.registry(), ensure_ascii=False))
    connected_clients.add(ws)

    try:
        while True:
            text = await ws.receive_text()
            try:
                cmd = json.loads(text)
            except json.JSONDecodeError:
                continue
            # REAL DATA mode is read-only: simulator commands are refused (add_machine stays allowed).
            if operating_mode.is_real and cmd.get("cmd") in WS_TESTING_COMMANDS and not (
                    cmd.get("cmd") == "auto_demo" and not cmd.get("on", True)):
                await ws.send_text(json.dumps({"type": "error", "cmd": cmd.get("cmd"),
                                               "detail": ReadOnlyError(f"'{cmd.get('cmd')}'").detail()}))
                continue
            # Reset and Auto Demo go through the same paths as REST; everything else to the engine.
            if cmd.get("cmd") == "reset":
                await post_reset(None)
            elif cmd.get("cmd") == "auto_demo":
                await auto_demo.set(bool(cmd.get("on", True)))
            elif cmd.get("cmd") == "add_machine":
                try:
                    await post_add_machine(AddMachineRequest(**{k: v for k, v in cmd.items() if k != "cmd"}))
                except (HTTPException, ValueError) as err:
                    await ws.send_text(json.dumps({"type": "error", "cmd": "add_machine",
                                                   "detail": getattr(err, "detail", str(err))}))
            else:
                await broadcast_messages(factory_engine.command(cmd))
    except WebSocketDisconnect:
        connected_clients.discard(ws)
    except Exception:
        connected_clients.discard(ws)


# -----------------------------------------------------------------------------
# Static UI Mounts
# -----------------------------------------------------------------------------
if WEB_DIR.exists():
    @app.get("/")
    def serve_index():
        index_file = WEB_DIR / "index.html"
        if index_file.exists():
            return FileResponse(index_file)
        # Fallback to stream viewer if web/index.html is not yet created
        return FileResponse(ROOT / "tools" / "stream_viewer.html")

    @app.get("/operator")
    def serve_operator():
        op_file = WEB_DIR / "operator.html"
        if op_file.exists():
            return FileResponse(op_file)
        return FileResponse(WEB_DIR / "index.html" if (WEB_DIR / "index.html").exists() else ROOT / "tools" / "stream_viewer.html")

    @app.get("/iot")
    def serve_iot():
        return FileResponse(WEB_DIR / "iot.html")

    app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")

ASSETS_DIR = ROOT / "assets"
if ASSETS_DIR.exists():
    app.mount("/assets", StaticFiles(directory=str(ASSETS_DIR)), name="assets")
