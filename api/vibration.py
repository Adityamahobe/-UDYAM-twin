"""Vibration health REST endpoints.

GET  /api/vibration                         status of severity config + ML model, and every machine
GET  /api/vibration/{machine_id}            one machine, with its velocity trend
POST /api/iot/waveform                      device waveform upload (X-Device-Id + X-Device-Token)
POST /api/vibration/{machine_id}/recording  waveform from a recorded file, with its source named

The v1.0 WebSocket contract is frozen, so vibration health is served here, not in the stream.
"""

from __future__ import annotations

from typing import Callable, List, Optional

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, Field

from hub import HubError, IoTHub
from ml.vibration.features import MAX_SAMPLES
from ml.vibration.monitor import VibrationMonitor


class WaveformUpload(BaseModel):
    sampling_rate_hz: float
    unit: str = Field(description="m/s2, g, mm/s, m/s, um or mm")
    samples: List[float] = Field(max_length=MAX_SAMPLES)
    axis: Optional[str] = None
    location: Optional[str] = None
    sensor_range: Optional[float] = Field(default=None, description="full-scale amplitude in `unit`, for clipping checks")
    rpm: Optional[float] = None
    ts: Optional[str] = None


class RecordingUpload(WaveformUpload):
    source: str = Field(min_length=3, description="where the recording came from (file, instrument, date)")


def build_router(hub: IoTHub, monitor: VibrationMonitor, machine_ids: Callable[[], List[str]]) -> APIRouter:
    router = APIRouter()

    @router.get("/api/vibration")
    def vibration_overview():
        ids = machine_ids()
        return {
            "severity_config": monitor.assessor.status(),
            "ml_model": monitor.predictor.info(),
            "machines": [s for s in (monitor.snapshot(mid, trend=False) for mid in ids) if s],
        }

    @router.get("/api/vibration/{machine_id}")
    def vibration_machine(machine_id: str):
        snap = monitor.snapshot(machine_id)
        if snap is None:
            raise HTTPException(status_code=404, detail=f"Machine {machine_id} not found")
        return snap

    @router.post("/api/iot/waveform")
    def device_waveform(
        req: WaveformUpload,
        x_device_id: Optional[str] = Header(default=None),
        x_device_token: Optional[str] = Header(default=None),
    ):
        if not x_device_id:
            raise HTTPException(status_code=422, detail="Missing X-Device-Id header")
        try:
            dev = hub.authenticate(x_device_id, x_device_token)
        except HubError as err:
            raise HTTPException(status_code=err.status, detail=err.detail)
        if not dev.machine_id:
            raise HTTPException(status_code=409, detail=f"Device {x_device_id} is not bound to a machine")
        return monitor.ingest_waveform(
            dev.machine_id, req.model_dump(),
            provenance="simulated" if dev.simulated else "measured",
            source=f"iot:{dev.device_id}",
        )

    @router.post("/api/vibration/{machine_id}/recording")
    def recorded_waveform(machine_id: str, req: RecordingUpload):
        if monitor.machine_info(machine_id) is None:
            raise HTTPException(status_code=404, detail=f"Machine {machine_id} not found")
        return monitor.ingest_waveform(machine_id, req.model_dump(), provenance="recorded", source=req.source)

    return router
