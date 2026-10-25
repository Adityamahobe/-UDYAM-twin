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
from datetime import datetime, timezone
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
    get_parameter_history,
    init_db,
    list_work_orders,
    log_event,
    log_parameter_change,
    update_economics,
    update_work_order_status,
)
from api.iot import build_router
from config import ConfigError, get_config
from ml.vibration.severity import SeverityAssessor
from api.vibration import build_router as build_vibration_router
from hub import IoTHub
from hub.mqtt_bridge import MqttBridge
from ml.vibration.monitor import VibrationMonitor
from sim.analytics import Analytics
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
    iso_class: Optional[str] = Field(default=None, max_length=60, description="e.g. 'ISO 10816-3 Group 2, rigid'")
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


async def broadcast_messages(messages: List[Dict[str, Any]]) -> None:
    """Broadcast JSON payloads to all connected WebSocket clients and log events to SQLite.

    Alert-worthy events also update the alert store, whose `alert` frames go out in the same batch.
    """
    if not messages:
        return
    vibration_monitor.observe(messages)  # sees every frame, including RESET from a manual reset
    analytics.observe(messages)
    try:
        advisor.observe(messages)
    except Exception as err:  # the chatbot must never break telemetry
        print(f"[advisor observe error]: {err}")
    messages = list(messages) + alert_store.ingest(messages)

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
            await auto_demo.tick()
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
    _restore_custom_machines()
    # Simulation state does not survive a restart, so alerts left open by a previous run are stale.
    resolve_alerts("Server restart: simulation state reset")
    bridge = MqttBridge.from_env(iot_hub)
    if bridge:
        bridge.start()
        mqtt_ref["mqtt"] = bridge
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


app.include_router(build_router(iot_hub, factory_engine, mqtt_ref))
app.include_router(build_vibration_router(iot_hub, vibration_monitor, lambda: list(factory_engine.plants)))


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
    msgs = factory_engine.command({
        "cmd": "inject_fault",
        "machine_id": req.machine_id,
        "fault": req.fault,
    })
    await broadcast_messages(msgs)
    return {"status": "injected", "machine_id": req.machine_id, "fault": req.fault}


@app.post("/api/faults/clear")
async def post_clear_faults(req: FaultClearRequest):
    msgs = factory_engine.command({
        "cmd": "clear_faults",
        "machine_id": req.machine_id,
    })
    await broadcast_messages(msgs)
    return {"status": "cleared", "machine_id": req.machine_id}


@app.post("/api/derate")
async def post_set_derate(req: DerateRequest):
    msgs = factory_engine.command({
        "cmd": "set_derate",
        "machine_id": req.machine_id,
        "pct": req.pct,
    })
    await broadcast_messages(msgs)
    return {"status": "derate_applied", "machine_id": req.machine_id, "pct": req.pct}


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
        vib["profile"] = f"demo-iso10816-3-g2-{req.foundation}"
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
    return await auto_demo.set(on)


@app.get("/api/scenarios")
def get_scenarios():
    """Fault-injection lab catalogue (demo simulation only) and which scenarios are running."""
    active = factory_engine.active_scenarios
    return [{**sc.public(), "active": active.get(sc.machine_id) == sid} for sid, sc in SCENARIOS.items()]


@app.post("/api/scenarios/{scenario_id}/start")
async def post_start_scenario(scenario_id: str):
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


@app.get("/api/reports/export.csv")
def api_export_report(section: str = Query(default="summary"), trend_minutes: float = Query(default=10.0, gt=0, le=30)):
    if section not in REPORT_SECTIONS:
        raise HTTPException(status_code=422, detail=f"section must be one of {REPORT_SECTIONS}")
    body = report_csv(section, build_report(trend_minutes))
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%MZ")
    return Response(content=body, media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="udyam-twin-{section}-{stamp}.csv"'})


@app.get("/api/metrics")
def api_get_metrics():
    """AI4I 2020 failure-model test metrics (models/metrics.json, written by `python -m ml.train_ai4i`)."""
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


@app.post("/api/chat/proposals/{proposal_id}/approve")
async def api_approve_proposal(proposal_id: str, request: Request, body: Optional[ProposalDecision] = None,
                               x_operator_token: Optional[str] = Header(default=None)):
    """Operator approval: executes the proposal once, on the simulated plant only (never a physical machine)."""
    _authorize_operator(request, x_operator_token)
    operator = (body or ProposalDecision()).operator
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
