"""Fake stream server: lets R2/R3/R4 build against schema v1.0 before the real core exists.

Run:   python -m tools.fake_stream                 # ws://localhost:8765/ws/telemetry, viewer at http://localhost:8765
       python -m tools.fake_stream --machines 3    # start without the compressor (to test the onboarding wizard)
       python -m tools.fake_stream --no-auto       # no scripted faults; drive everything from commands
       python -m tools.fake_stream --record 600 --out samples/demo_stream.jsonl   # dump N ticks, no server

Client -> server commands (JSON text frames):
  {"cmd": "inject_fault", "machine_id": "VMC-01", "fault": "coolant_degradation"}
  {"cmd": "clear_faults", "machine_id": "VMC-01"}          # machine_id optional = all
  {"cmd": "set_derate", "machine_id": "VMC-01", "pct": 20}
  {"cmd": "add_machine", "template": "air_compressor", "name": "Air Compressor", "nameplate": {"rated_kw": 7.5}}
  {"cmd": "auto_demo", "on": false}
  {"cmd": "reset"}
"""


import argparse
import asyncio
import json
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from tools.fake_factory import TICK_S, FakeFactory

ROOT = Path(__file__).resolve().parent.parent


def record(ticks: int, out: Path, seed: int, machines: int) -> None:
    factory = FakeFactory(seed=seed, machines=machines)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as fh:
        for msg in [factory.hello(), factory.registry()]:
            fh.write(json.dumps(msg, ensure_ascii=False) + "\n")
        for _ in range(ticks):
            for msg in factory.step():
                fh.write(json.dumps(msg, ensure_ascii=False) + "\n")
    print(f"wrote {ticks} ticks to {out}")


def build_app(seed: int, machines: int, auto_demo: bool):
    from fastapi import FastAPI, WebSocket, WebSocketDisconnect
    from fastapi.responses import FileResponse

    factory = FakeFactory(seed=seed, start=datetime.now(timezone.utc), auto_demo=auto_demo, machines=machines)
    clients: set[WebSocket] = set()

    async def broadcast(messages: list[dict]) -> None:
        if not messages:
            return
        payloads = [json.dumps(m, ensure_ascii=False) for m in messages]
        for ws in list(clients):
            try:
                for p in payloads:
                    await ws.send_text(p)
            except Exception:
                clients.discard(ws)

    async def ticker() -> None:
        while True:
            await broadcast(factory.step())
            await asyncio.sleep(TICK_S)

    @asynccontextmanager
    async def lifespan(_app):
        task = asyncio.create_task(ticker())
        yield
        task.cancel()

    app = FastAPI(title="UDYAM-TWIN fake stream", lifespan=lifespan)

    @app.get("/")
    def viewer():
        return FileResponse(ROOT / "tools" / "stream_viewer.html")

    @app.get("/schema.json")
    def schema():
        return FileResponse(ROOT / "docs" / "schema.json")

    @app.websocket("/ws/telemetry")
    async def telemetry(ws: WebSocket):
        await ws.accept()
        await ws.send_text(json.dumps(factory.hello()))
        await ws.send_text(json.dumps(factory.registry(), ensure_ascii=False))
        clients.add(ws)
        try:
            while True:
                try:
                    cmd = json.loads(await ws.receive_text())
                except json.JSONDecodeError:
                    continue
                await broadcast(factory.command(cmd))
        except WebSocketDisconnect:
            clients.discard(ws)

    return app


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="0.0.0.0", help="0.0.0.0 so a phone on the same hotspot can connect")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--machines", type=int, choices=[3, 4], default=4)
    ap.add_argument("--no-auto", action="store_true", help="disable the scripted 150 s fault loop")
    ap.add_argument("--record", type=int, metavar="TICKS", help="write TICKS ticks to --out and exit")
    ap.add_argument("--out", type=Path, default=ROOT / "samples" / "demo_stream.jsonl")
    args = ap.parse_args()

    if args.record:
        record(args.record, args.out, args.seed, args.machines)
        return

    import uvicorn

    print(f"viewer:    http://localhost:{args.port}/\nwebsocket: ws://localhost:{args.port}/ws/telemetry")
    uvicorn.run(build_app(args.seed, args.machines, not args.no_auto), host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
