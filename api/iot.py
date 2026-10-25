"""IoT hub REST + WebSocket endpoints.

Admin (dashboard):   /api/iot/devices, /bind, /tare, /token, /api/iot/status
Device ingestion:    POST /api/iot/telemetry   (header X-Device-Id + X-Device-Token)
                     WS   /ws/device/{device_id}?token=...   (one JSON reading per frame)
                     MQTT udyam/<device_id>/telemetry          (see hub/mqtt_bridge.py)
"""

from __future__ import annotations

import json
from typing import Any, Dict, Optional

from fastapi import APIRouter, Header, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel

from hub import HubError, IoTHub
from hub.mqtt_bridge import MqttBridge
from sim.factory import FactoryEngine


class DeviceCreateRequest(BaseModel):
    device_id: str
    machine_id: Optional[str] = None
    name: Optional[str] = None
    simulated: bool = False


class DeviceBindRequest(BaseModel):
    machine_id: Optional[str] = None


class DeviceTareRequest(BaseModel):
    offset_k: Optional[float] = None


def build_router(hub: IoTHub, engine: FactoryEngine, bridge_ref: Dict[str, Optional[MqttBridge]]) -> APIRouter:
    router = APIRouter()

    def call(fn, *args):
        try:
            return fn(*args)
        except HubError as err:
            raise HTTPException(status_code=err.status, detail=err.detail)

    @router.get("/api/iot/status")
    def iot_status():
        bridge = bridge_ref.get("mqtt")
        return {
            **hub.summary(),
            "transports": {
                "http": "POST /api/iot/telemetry",
                "websocket": "/ws/device/{device_id}?token=...",
                "mqtt": bridge.status() if bridge else {"enabled": False, "hint": "set UDYAM_MQTT_HOST"},
            },
        }

    @router.get("/api/iot/devices")
    def list_devices():
        return hub.list()

    @router.post("/api/iot/devices", status_code=201)
    def create_device(req: DeviceCreateRequest):
        return call(hub.register, req.device_id, req.machine_id, req.name, req.simulated)

    @router.get("/api/iot/devices/{device_id}")
    def get_device(device_id: str):
        return call(hub.get, device_id)

    @router.delete("/api/iot/devices/{device_id}")
    def delete_device(device_id: str):
        call(hub.remove, device_id)
        return {"status": "deleted", "device_id": device_id}

    @router.post("/api/iot/devices/{device_id}/bind")
    def bind_device(device_id: str, req: DeviceBindRequest):
        return call(hub.bind, device_id, req.machine_id)

    @router.post("/api/iot/devices/{device_id}/token")
    def rotate_token(device_id: str):
        return call(hub.rotate_token, device_id)

    @router.post("/api/iot/devices/{device_id}/tare")
    def tare_device(device_id: str, req: DeviceTareRequest):
        """Align the probe with the twin: by default, set the offset so the latest
        reading equals the twin's current predicted process temperature."""
        if req.offset_k is not None:
            return call(hub.set_offset, device_id, req.offset_k)
        dev = call(hub.get, device_id)
        twin = engine.twins.get(dev["machine_id"] or "")
        if twin is None or twin.pred_temp is None:
            raise HTTPException(status_code=409, detail="Device must be bound to a CNC machine to auto-tare")
        raw_k = hub.raw_temp_k(device_id)
        if raw_k is None:
            raise HTTPException(status_code=409, detail="No temperature reading received yet")
        return call(hub.set_offset, device_id, twin.pred_temp - raw_k)

    @router.post("/api/iot/telemetry")
    def post_telemetry(
        payload: Dict[str, Any],
        x_device_id: Optional[str] = Header(default=None),
        x_device_token: Optional[str] = Header(default=None),
    ):
        device_id = x_device_id or payload.get("device_id")
        if not device_id:
            raise HTTPException(status_code=422, detail="Missing X-Device-Id header or device_id field")
        return call(hub.ingest, device_id, x_device_token or payload.get("token"), payload, "http")

    @router.websocket("/ws/device/{device_id}")
    async def device_socket(ws: WebSocket, device_id: str, token: Optional[str] = None):
        try:
            hub.authenticate(device_id, token)
        except HubError:
            await ws.close(code=4401)
            return
        await ws.accept()
        try:
            while True:
                text = await ws.receive_text()
                try:
                    result = hub.ingest(device_id, token, json.loads(text), "websocket")
                    await ws.send_text(json.dumps({"ok": True, **result}))
                except (json.JSONDecodeError, HubError) as err:
                    await ws.send_text(json.dumps({"ok": False, "error": str(err)}))
        except WebSocketDisconnect:
            pass

    return router
