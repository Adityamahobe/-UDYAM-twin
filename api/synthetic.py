"""Synthetic dataset REST endpoints (SYNTHETIC DIGITAL TWIN DATA; ml/synthetic.py).

GET    /api/synthetic/options                       machines, faults, parameter limits and defaults
POST   /api/synthetic/generate                      start one generation job (202), validated against the limits
GET    /api/synthetic/job                           progress of the current or last job
POST   /api/synthetic/job/cancel                    stop the running job (nothing is kept)
GET    /api/synthetic/datasets                      generated datasets, newest first
GET    /api/synthetic/datasets/{id}                 full metadata
GET    /api/synthetic/datasets/{id}/download        part=windows|episodes|metadata, format=csv|json
DELETE /api/synthetic/datasets/{id}

Generation runs on separate engines, so it is allowed in both operating modes: it never touches the live
simulation or real telemetry. Every file and response carries the SYNTHETIC DIGITAL TWIN DATA label.
"""

from __future__ import annotations

import csv
import json
from typing import Any, Dict, List

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field

from config import get_config
from ml import synthetic
from sim import controls


class GenerateRequest(BaseModel):
    machine_id: str = "VMC-01"
    episodes: int = Field(default=20, ge=1, le=synthetic.MAX_EPISODES)
    minutes: float = Field(default=10.0, ge=1.0, le=synthetic.MAX_MINUTES)
    seed: int = Field(default=2026, ge=0, le=2**31 - 1)
    faults: List[str] = Field(default_factory=lambda: list(synthetic.FAULTS))
    param_ranges: Dict[str, List[float]] = Field(default_factory=dict)
    extra_ranges: Dict[str, List[float]] = Field(default_factory=dict)
    auto_derate_share: float = Field(default=0.5, ge=0.0, le=1.0)
    name: str = Field(default="", max_length=60)


def _num(v: str) -> Any:
    if v == "":
        return None
    try:
        f = float(v)
    except ValueError:
        return v
    return int(f) if f.is_integer() and "." not in v and "e" not in v.lower() else f


def build_router(store: synthetic.SyntheticStore) -> APIRouter:
    router = APIRouter()

    def bad(err: Exception, code: int = 422):
        raise HTTPException(status_code=code, detail={"error": "synthetic_generator", "message": str(err)})

    @router.get("/api/synthetic/options")
    def options():
        machines = []
        for mid, tpl in synthetic.MACHINES.items():
            machines.append({"machine_id": mid, "template": tpl,
                             "params": controls.specs_for(tpl, get_config().nameplate(tpl))})
        return {"data_label": synthetic.DATA_LABEL, "generator": synthetic.GENERATOR_VERSION,
                "machines": machines, "faults": list(synthetic.FAULTS),
                "extra_ranges": {k: {"min": lo, "max": hi, "default": list(synthetic.DEFAULT_EXTRA[k])}
                                 for k, (lo, hi) in synthetic.EXTRA_RANGES.items()},
                "limits": {"episodes": synthetic.MAX_EPISODES, "minutes": synthetic.MAX_MINUTES,
                           "episode_minutes": synthetic.MAX_EPISODE_MINUTES},
                "window_s": synthetic.WINDOW_S, "stride_s": synthetic.STRIDE_S}

    @router.post("/api/synthetic/generate", status_code=202)
    def generate(req: GenerateRequest):
        cfg = synthetic.GeneratorConfig(**req.model_dump())
        try:
            return store.start(cfg)
        except synthetic.GeneratorError as err:
            bad(err, 409 if "already" in str(err) else 422)

    @router.get("/api/synthetic/job")
    def job():
        return store.status() or {"state": "idle"}

    @router.post("/api/synthetic/job/cancel")
    def cancel():
        store.cancel()
        return store.status() or {"state": "idle"}

    @router.get("/api/synthetic/datasets")
    def datasets():
        return {"data_label": synthetic.DATA_LABEL, "datasets": store.list()}

    def _meta(dataset_id: str) -> Dict[str, Any]:
        try:
            return store.metadata(dataset_id)
        except synthetic.GeneratorError as err:
            bad(err, 400)
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail=f"No dataset {dataset_id}")

    @router.get("/api/synthetic/datasets/{dataset_id}")
    def metadata(dataset_id: str):
        return _meta(dataset_id)

    @router.get("/api/synthetic/datasets/{dataset_id}/download")
    def download(dataset_id: str, part: str = Query(default="windows", pattern="^(windows|episodes|metadata)$"),
                 format: str = Query(default="csv", pattern="^(csv|json)$")):
        meta = _meta(dataset_id)
        base = f"SYNTHETIC_{dataset_id}_{part}"
        if part == "metadata":
            if format == "csv":
                bad("metadata is JSON only; use format=json", 400)
            return Response(json.dumps(meta, indent=2), media_type="application/json",
                            headers={"Content-Disposition": f'attachment; filename="{base}.json"'})
        path = store.path(dataset_id) / f"{part}.csv"
        if format == "csv":
            return Response(path.read_bytes(), media_type="text/csv",
                            headers={"Content-Disposition": f'attachment; filename="{base}.csv"',
                                     "X-Data-Label": synthetic.DATA_LABEL})
        with open(path, newline="") as fh:
            rows = [{k: _num(v) for k, v in r.items()} for r in csv.DictReader(fh)]
        body = {"data_label": synthetic.DATA_LABEL, "notice": meta["notice"], "dataset_id": dataset_id,
                "part": part, "metadata": {k: meta[k] for k in ("generator", "created_at", "config", "counts")},
                "columns": meta["columns"][part], "rows": rows}
        return JSONResponse(body, headers={"Content-Disposition": f'attachment; filename="{base}.json"',
                                           "X-Data-Label": synthetic.DATA_LABEL})

    @router.delete("/api/synthetic/datasets/{dataset_id}")
    def delete(dataset_id: str):
        try:
            store.delete(dataset_id)
        except synthetic.GeneratorError as err:
            bad(err, 409)
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail=f"No dataset {dataset_id}")
        return {"deleted": dataset_id}

    return router
