"""IoT hub core: device registry, token auth, latest-reading cache, LIVE overlay.

Thread-safe: the MQTT bridge calls `ingest` from its network thread while the
FastAPI ticker calls `overlay` from the event loop.
"""

from __future__ import annotations

import hashlib
import secrets
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from hub.normalize import normalize

STALE_S = 3.0      # no reading for this long → dq_flag STALE (values held)
OFFLINE_S = 30.0   # no reading for this long → dq_flag MISSING (live values nulled)
STUCK_WINDOW = 60  # identical raw temperatures in a row → STUCK (DS18B20 is 0.0625 °C steps)


class HubError(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


@dataclass
class Device:
    device_id: str
    token_hash: str
    machine_id: str | None
    name: str
    temp_offset_k: float = 0.0
    simulated: bool = False  # a software stand-in (e.g. tools/fake_device): its data is SIM, never LIVE
    created_at: str = field(default_factory=_now_iso)
    # Runtime state (not persisted)
    last_seen: float | None = None
    last_seen_iso: str | None = None
    last_transport: str | None = None
    last_raw: dict[str, Any] = field(default_factory=dict)
    last_fields: dict[str, float | None] = field(default_factory=dict)
    last_dq: str = "OK"
    msg_count: int = 0
    rejected_count: int = 0
    temp_history: list[float] = field(default_factory=list)

    def status(self, now: float) -> str:
        if self.last_seen is None:
            return "NEVER_SEEN"
        age = now - self.last_seen
        if age > OFFLINE_S:
            return "OFFLINE"
        if age > STALE_S:
            return "STALE"
        return "ONLINE"

    def public(self, now: float) -> dict[str, Any]:
        return {
            "device_id": self.device_id,
            "name": self.name,
            "machine_id": self.machine_id,
            "simulated": self.simulated,
            "status": self.status(now),
            "created_at": self.created_at,
            "last_seen": self.last_seen_iso,
            "age_s": None if self.last_seen is None else round(now - self.last_seen, 1),
            "last_transport": self.last_transport,
            "last_reading": self.last_fields,
            "last_raw": self.last_raw,
            "dq_flag": self.last_dq,
            "temp_offset_k": round(self.temp_offset_k, 3),
            "msg_count": self.msg_count,
            "rejected_count": self.rejected_count,
        }


class IoTHub:
    def __init__(
        self,
        db_path: Path | None = None,
        template_of: Callable[[str], str | None] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ):
        """`template_of(machine_id)` resolves a machine's template (None = unknown machine)."""
        self.db_path = db_path
        self.template_of = template_of or (lambda _mid: None)
        self.clock = clock
        self.devices: dict[str, Device] = {}
        self._lock = threading.RLock()
        if db_path is not None:
            self._init_db()
            self._load()

    # ------------------------------------------------------------------ persistence
    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        conn = self._conn()
        with conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS iot_devices (
                    device_id TEXT PRIMARY KEY,
                    token_hash TEXT NOT NULL,
                    machine_id TEXT,
                    name TEXT NOT NULL,
                    temp_offset_k REAL NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL
                );
            """)
            cols = {r[1] for r in conn.execute("PRAGMA table_info(iot_devices)")}
            if "simulated" not in cols:
                conn.execute("ALTER TABLE iot_devices ADD COLUMN simulated INTEGER NOT NULL DEFAULT 0")
        conn.close()

    def _load(self) -> None:
        conn = self._conn()
        rows = conn.execute("SELECT * FROM iot_devices").fetchall()
        conn.close()
        for r in rows:
            self.devices[r["device_id"]] = Device(
                device_id=r["device_id"],
                token_hash=r["token_hash"],
                machine_id=r["machine_id"],
                name=r["name"],
                temp_offset_k=r["temp_offset_k"],
                simulated=bool(r["simulated"]),
                created_at=r["created_at"],
            )

    def _save(self, d: Device) -> None:
        if self.db_path is None:
            return
        conn = self._conn()
        with conn:
            conn.execute(
                """INSERT INTO iot_devices (device_id, token_hash, machine_id, name, temp_offset_k, simulated, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(device_id) DO UPDATE SET token_hash=excluded.token_hash,
                     machine_id=excluded.machine_id, name=excluded.name, temp_offset_k=excluded.temp_offset_k,
                     simulated=excluded.simulated""",
                (d.device_id, d.token_hash, d.machine_id, d.name, d.temp_offset_k, int(d.simulated), d.created_at),
            )
        conn.close()

    def _delete_row(self, device_id: str) -> None:
        if self.db_path is None:
            return
        conn = self._conn()
        with conn:
            conn.execute("DELETE FROM iot_devices WHERE device_id = ?", (device_id,))
        conn.close()

    # ------------------------------------------------------------------ registry
    def _check_machine(self, machine_id: str | None, exclude: str | None = None) -> None:
        if machine_id is None:
            return
        if self.template_of(machine_id) is None:
            raise HubError(404, f"Machine {machine_id} not found")
        for d in self.devices.values():
            if d.machine_id == machine_id and d.device_id != exclude:
                raise HubError(409, f"Machine {machine_id} is already bound to {d.device_id}")

    def register(
        self, device_id: str, machine_id: str | None = None, name: str | None = None, simulated: bool = False
    ) -> dict[str, Any]:
        """Create a device and return its token. The token is shown only once."""
        device_id = device_id.strip()
        if not device_id or len(device_id) > 64:
            raise HubError(422, "device_id must be 1-64 characters")
        with self._lock:
            if device_id in self.devices:
                raise HubError(409, f"Device {device_id} already exists")
            self._check_machine(machine_id)
            token = secrets.token_urlsafe(24)
            d = Device(device_id=device_id, token_hash=_hash(token), machine_id=machine_id, name=name or device_id,
                       simulated=simulated)
            self.devices[device_id] = d
            self._save(d)
            return {**d.public(self.clock()), "token": token}

    def rotate_token(self, device_id: str) -> dict[str, Any]:
        with self._lock:
            d = self._get(device_id)
            token = secrets.token_urlsafe(24)
            d.token_hash = _hash(token)
            self._save(d)
            return {"device_id": device_id, "token": token}

    def remove(self, device_id: str) -> None:
        with self._lock:
            self._get(device_id)
            del self.devices[device_id]
            self._delete_row(device_id)

    def bind(self, device_id: str, machine_id: str | None) -> dict[str, Any]:
        with self._lock:
            d = self._get(device_id)
            self._check_machine(machine_id, exclude=device_id)
            if machine_id != d.machine_id:
                d.temp_offset_k = 0.0
                d.temp_history.clear()
            d.machine_id = machine_id
            self._save(d)
            return d.public(self.clock())

    def set_offset(self, device_id: str, offset_k: float) -> dict[str, Any]:
        with self._lock:
            d = self._get(device_id)
            d.temp_offset_k = float(offset_k)
            self._save(d)
            return d.public(self.clock())

    def raw_temp_k(self, device_id: str) -> float | None:
        """Latest raw probe temperature in K (before offset), for taring."""
        with self._lock:
            d = self._get(device_id)
            return d.temp_history[-1] if d.temp_history else None

    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            now = self.clock()
            return [d.public(now) for d in self.devices.values()]

    def get(self, device_id: str) -> dict[str, Any]:
        with self._lock:
            return self._get(device_id).public(self.clock())

    def _get(self, device_id: str) -> Device:
        d = self.devices.get(device_id)
        if d is None:
            raise HubError(404, f"Device {device_id} not found")
        return d

    def authenticate(self, device_id: str, token: str | None) -> Device:
        d = self.devices.get(device_id)
        if d is None or not token or not secrets.compare_digest(d.token_hash, _hash(token)):
            if d is not None:
                d.rejected_count += 1
            raise HubError(401, "Invalid device id or token")
        return d

    # ------------------------------------------------------------------ ingestion
    def ingest(self, device_id: str, token: str | None, payload: dict[str, Any], transport: str) -> dict[str, Any]:
        """Authenticate and store one reading. Returns the normalised result."""
        if not isinstance(payload, dict):
            raise HubError(422, "Payload must be a JSON object")
        with self._lock:
            d = self.authenticate(device_id, token)
            template = self.template_of(d.machine_id) if d.machine_id else None
            fields, dq = normalize(payload, template or "", d.temp_offset_k)

            raw_temp = fields.get("process_temp_k")
            if raw_temp is not None:
                d.temp_history = (d.temp_history + [round(raw_temp - d.temp_offset_k, 4)])[-STUCK_WINDOW:]
                if len(d.temp_history) == STUCK_WINDOW and len(set(d.temp_history)) == 1 and dq == "OK":
                    dq = "STUCK"

            d.last_seen = self.clock()
            d.last_seen_iso = _now_iso()
            d.last_transport = transport
            d.last_raw = {k: v for k, v in payload.items() if k != "token"}
            d.last_fields = fields
            d.last_dq = dq
            d.msg_count += 1
            return {"device_id": device_id, "machine_id": d.machine_id, "accepted": sorted(fields), "dq_flag": dq}

    # ------------------------------------------------------------------ overlay
    def overlay(self, obs: dict[str, Any]) -> dict[str, Any]:
        """Replace sensed signals in a SIM `observed` frame with the bound device's live values."""
        mid = obs.get("machine_id")
        with self._lock:
            d = next((x for x in self.devices.values() if x.machine_id == mid), None)
            if d is None or d.last_seen is None:
                return obs
            status = d.status(self.clock())
            out = dict(obs)
            out["source_mode"] = "SIM" if d.simulated else "LIVE"
            out["live_fields"] = sorted(d.last_fields)  # the rest of the frame is still the simulation's
            if status == "OFFLINE":
                for k in d.last_fields:
                    out[k] = None
                out["dq_flag"] = "MISSING"
                return out
            out.update(d.last_fields)
            out["dq_flag"] = "STALE" if status == "STALE" else d.last_dq
            return out

    def bound_machines(self) -> dict[str, str]:
        with self._lock:
            return {d.machine_id: d.device_id for d in self.devices.values() if d.machine_id}

    def summary(self) -> dict[str, Any]:
        with self._lock:
            now = self.clock()
            counts: dict[str, int] = {}
            for d in self.devices.values():
                s = d.status(now)
                counts[s] = counts.get(s, 0) + 1
            return {"devices": len(self.devices), "by_status": counts, "bound": self.bound_machines()}
