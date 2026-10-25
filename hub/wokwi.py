"""Wokwi virtual devices: MQTT telemetry from ESP32 sketches running in the Wokwi simulator (wokwi/udyam-virtual-machine).

Separate from the IoT hub and from the simulation engine on purpose: Wokwi data never enters the Testing-mode
simulation or any simulated machine. It is VIRTUAL data (Wokwi's simulated sensors plus software models), never a
measurement of a real machine, and is stored in its own table (`virtual_telemetry`) tagged WOKWI_SIMULATOR.

Enabled only when UDYAM_WOKWI_BROKER is set:
    UDYAM_WOKWI_BROKER=public.cloud.shiftr.io   broker host (free Wokwi can only reach internet brokers)
    UDYAM_WOKWI_PORT=1883                       (8883 with UDYAM_WOKWI_TLS=1)
    UDYAM_WOKWI_TOPIC_PREFIX=udyam-twin/virtual-machines
    UDYAM_WOKWI_USER / UDYAM_WOKWI_PASS          broker login, server-side only (shiftr.io public: public / public)

Topics: <prefix>/<machine_id>/telemetry (JSON) and <prefix>/<machine_id>/status ("online"/"offline", retained, last will).
A public broker is untrusted: every message is validated (schema, source, topic/machine match, ranges, size, rate)
and anything else is rejected and counted with its reason.
"""

from __future__ import annotations

import json
import math
import os
import re
import sqlite3
import threading
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

SCHEMA = "udyam.wokwi.v1"
SOURCE = "WOKWI_SIMULATOR"
DEFAULT_PREFIX = "udyam-twin/virtual-machines"
MACHINE_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,31}$")
SCENARIOS = ("normal", "high_load", "bearing_wear", "overheat", "sensor_dropout")
FAULTS = (None, "bearing_wear", "overheating", "temperature_sensor_dropout")
RANGES = {"rpm": (0.0, 10000.0), "temp_c": (-40.0, 150.0), "vibration_rms_mm_s": (0.0, 100.0), "current_a": (0.0, 100.0)}
MAX_PAYLOAD = 4096
MIN_GAP_S = 0.2            # faster than this per machine is rejected (flood protection on a public broker)
MAX_MACHINES = 10
HISTORY_N = 150            # in-memory points per machine for the dashboard
RETENTION = timedelta(days=7)
COLUMNS = ("ts", "device_ts", "machine_id", "source", "seq", "scenario", "simulated_fault", "rpm", "temp_c",
           "vibration_rms_mm_s", "current_a", "ts_source", "value_sources")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    dt = dt.astimezone(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def _num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def validate(topic: str, raw: bytes, prefix: str, received: datetime) -> tuple[dict[str, Any] | None, str | None]:
    """A normalised record, or the reason the message is rejected."""
    if len(raw) > MAX_PAYLOAD:
        return None, f"payload larger than {MAX_PAYLOAD} bytes"
    parts = topic[len(prefix) + 1:].split("/") if topic.startswith(prefix + "/") else []
    if len(parts) != 2 or parts[1] != "telemetry":
        return None, "unexpected topic"
    try:
        p = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None, "not valid JSON"
    if not isinstance(p, dict):
        return None, "payload is not a JSON object"
    if p.get("schema") != SCHEMA:
        return None, f"schema must be {SCHEMA}"
    if p.get("source") != SOURCE or p.get("virtual") is not True:
        return None, f"source must be {SOURCE} with virtual=true"
    mid = p.get("machine_id")
    if not isinstance(mid, str) or not MACHINE_RE.match(mid):
        return None, "machine_id must be 2–32 lowercase letters, digits or '-'"
    if mid != parts[0]:
        return None, "machine_id does not match the topic"
    rec: dict[str, Any] = {"machine_id": mid, "source": SOURCE}
    for key, (lo, hi) in RANGES.items():
        v = p.get(key)
        if v is None and key == "temp_c":  # the sensor_dropout scenario sends null
            rec[key] = None
            continue
        if not _num(v) or not lo <= v <= hi:
            return None, f"{key} missing or outside {lo:g}–{hi:g}"
        rec[key] = float(v)
    if p.get("scenario") not in SCENARIOS:
        return None, f"scenario must be one of {', '.join(SCENARIOS)}"
    if p.get("simulated_fault") not in FAULTS:
        return None, "unknown simulated_fault"
    seq = p.get("seq")
    if not isinstance(seq, int) or isinstance(seq, bool) or seq < 0:
        return None, "seq must be a non-negative integer"
    rec.update(scenario=p["scenario"], simulated_fault=p["simulated_fault"], seq=seq,
               interval_ms=p.get("interval_ms") if _num(p.get("interval_ms")) else None)
    # The device clock (NTP in Wokwi) is used only if it is plausible; storage always uses the receive time.
    dev_ts, ts_source = None, "received"
    if isinstance(p.get("ts"), str):
        try:
            d = datetime.fromisoformat(p["ts"].replace("Z", "+00:00"))
            if d.tzinfo and abs((d - received).total_seconds()) < 300:
                dev_ts, ts_source = _iso(d), "device"
            else:
                ts_source = "received (device clock off)"
        except ValueError:
            ts_source = "received (device ts unreadable)"
    rec.update(ts=_iso(received), device_ts=dev_ts, ts_source=ts_source)
    vs = p.get("value_sources")
    rec["value_sources"] = {k: str(v)[:80] for k, v in vs.items()} if isinstance(vs, dict) else {}
    return rec, None


class VirtualDeviceStore:
    """Latest state, short history and persistent rows for Wokwi virtual machines."""

    def __init__(self, db_path: Path | None):
        self.db_path = db_path
        self.lock = threading.Lock()
        self.devices: dict[str, dict[str, Any]] = {}
        # status topic ("online"/"offline") per machine; it can arrive before the first telemetry (retained, or sent first)
        self.statuses: dict[str, tuple[str, datetime]] = {}  # text and when it arrived
        self.received = self.accepted = self.rejected = 0
        self.last_reject: dict[str, Any] | None = None
        self._inserts = 0
        self._conn: sqlite3.Connection | None = None

    def init(self) -> None:
        if self.db_path is None:
            return
        with self.lock:
            self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
            with self._conn:
                self._conn.execute("""
                    CREATE TABLE IF NOT EXISTS virtual_telemetry (
                        id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, device_ts TEXT, machine_id TEXT NOT NULL,
                        source TEXT NOT NULL, seq INTEGER, scenario TEXT, simulated_fault TEXT, rpm REAL, temp_c REAL,
                        vibration_rms_mm_s REAL, current_a REAL, ts_source TEXT, value_sources TEXT);""")
                self._conn.execute("CREATE INDEX IF NOT EXISTS virtual_telemetry_ts ON virtual_telemetry (ts);")

    def reject(self, reason: str, topic: str) -> None:
        with self.lock:
            self.received += 1
            self.rejected += 1
            self.last_reject = {"reason": reason, "topic": topic[:120], "at": _iso(_now())}

    def ingest(self, topic: str, raw: bytes, prefix: str, now: datetime | None = None) -> dict[str, Any] | None:
        now = now or _now()
        rec, why = validate(topic, raw, prefix, now)
        if rec is None:
            self.reject(why, topic)
            return None
        with self.lock:
            dev = self.devices.get(rec["machine_id"])
            if dev is None and len(self.devices) >= MAX_MACHINES:
                why = f"more than {MAX_MACHINES} virtual machines"
            elif dev and (now - dev["last_at"]).total_seconds() < MIN_GAP_S:
                why = "messages too frequent"
            if why:
                self.received += 1
                self.rejected += 1
                self.last_reject = {"reason": why, "topic": topic[:120], "at": _iso(now)}
                return None
            self.received += 1
            self.accepted += 1
            if dev is None:
                dev = self.devices[rec["machine_id"]] = {"history": deque(maxlen=HISTORY_N), "messages": 0,
                                                          "first_at": now}
            dev.update(last=rec, last_at=now, messages=dev["messages"] + 1)
            dev["history"].append({k: rec[k] for k in ("ts", "rpm", "temp_c", "vibration_rms_mm_s", "current_a", "scenario")})
            if self._conn is not None:
                with self._conn:
                    self._conn.execute(f"INSERT INTO virtual_telemetry ({', '.join(COLUMNS)}) VALUES ({', '.join('?' * len(COLUMNS))});",
                                       tuple(json.dumps(rec[c]) if c == "value_sources" else rec.get(c) for c in COLUMNS))
                    self._inserts += 1
                    if self._inserts % 500 == 0:
                        self._conn.execute("DELETE FROM virtual_telemetry WHERE ts < ?;", (_iso(now - RETENTION),))
        return rec

    def status_message(self, topic: str, raw: bytes, prefix: str, now: datetime | None = None) -> None:
        parts = topic[len(prefix) + 1:].split("/") if topic.startswith(prefix + "/") else []
        if len(parts) == 2 and parts[1] == "status" and MACHINE_RE.match(parts[0]):
            text = raw.decode("utf-8", "replace").strip()[:16]
            with self.lock:
                if parts[0] in self.statuses or len(self.statuses) < 4 * MAX_MACHINES:
                    self.statuses[parts[0]] = (text, now or _now())

    @staticmethod
    def device_state(dev: dict[str, Any], now: datetime, status: tuple[str, datetime] | None = None) -> tuple[str, float]:
        age = (now - dev["last_at"]).total_seconds()
        interval = (dev["last"].get("interval_ms") or 2000) / 1000.0
        # "offline" counts only if it is newer than the last telemetry: after a restart, the old connection's last will
        # can reach the broker after the new connection is already sending.
        offline_said = bool(status) and status[0] == "offline" and status[1] >= dev["last_at"]
        if offline_said or age > max(10 * interval, 30.0):
            return "OFFLINE", age
        if age > max(3 * interval, 6.0):
            return "STALE", age
        return "ONLINE", age

    def snapshot(self, now: datetime | None = None, history: bool = True) -> list[dict[str, Any]]:
        now = now or _now()
        out = []
        with self.lock:
            for mid, dev in sorted(self.devices.items()):
                status = self.statuses.get(mid)
                state, age = self.device_state(dev, now, status)
                status_msg = status[0] if status and (status[0] != "offline" or state == "OFFLINE") else ("online" if status else None)
                last = dict(dev["last"])
                if state == "OFFLINE":  # old values are not shown as current
                    for k in RANGES:
                        last[k] = None
                out.append({"machine_id": mid, "source": SOURCE, "data_label": "VIRTUAL (Wokwi simulator)",
                            "state": state, "age_s": round(age, 1), "messages": dev["messages"],
                            "last_message_at": dev["last"]["ts"], "device_status": status_msg,
                            "last": last, "history": list(dev["history"]) if history else None})
        return out

    def counters(self) -> dict[str, Any]:
        with self.lock:
            return {"received": self.received, "accepted": self.accepted, "rejected": self.rejected,
                    "last_reject": self.last_reject}

    def query(self, start: str, end: str, machine_id: str | None = None, limit: int = 500_000) -> list[dict[str, Any]]:
        if self._conn is None:
            return []
        sql = f"SELECT {', '.join(COLUMNS)} FROM virtual_telemetry WHERE ts >= ? AND ts <= ?"
        args: list[Any] = [start, end]
        if machine_id:
            sql += " AND machine_id = ?"
            args.append(machine_id)
        with self.lock:
            rows = self._conn.execute(sql + " ORDER BY ts LIMIT ?;", (*args, limit)).fetchall()
        return [{**dict(r), "data_label": "VIRTUAL (Wokwi simulator)"} for r in rows]


class WokwiBridge:
    """Subscribes to the Wokwi topics on its own MQTT connection (independent of the device bridge)."""

    def __init__(self, store: VirtualDeviceStore, host: str, port: int = 1883, prefix: str = DEFAULT_PREFIX,
                 username: str | None = None, password: str | None = None, tls: bool = False):
        self.store, self.host, self.port, self.prefix = store, host, port, prefix.strip("/")
        self.username, self.password, self.tls = username, password, tls
        self.client: Any = None
        self.state = "NOT_STARTED"
        self.last_error: str | None = None
        self.connected_at: str | None = None

    @classmethod
    def from_env(cls, store: VirtualDeviceStore) -> "WokwiBridge | None":
        host = os.environ.get("UDYAM_WOKWI_BROKER")
        if not host:
            return None
        tls = os.environ.get("UDYAM_WOKWI_TLS", "0") == "1"
        return cls(store, host, int(os.environ.get("UDYAM_WOKWI_PORT", "8883" if tls else "1883")),
                   os.environ.get("UDYAM_WOKWI_TOPIC_PREFIX", DEFAULT_PREFIX),
                   os.environ.get("UDYAM_WOKWI_USER"), os.environ.get("UDYAM_WOKWI_PASS"), tls)

    @property
    def topics(self) -> list[str]:
        return [f"{self.prefix}/+/telemetry", f"{self.prefix}/+/status"]

    def start(self) -> bool:
        try:
            import paho.mqtt.client as mqtt
        except ImportError:
            self.state, self.last_error = "ERROR", "paho-mqtt not installed"
            return False
        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"udyam-twin-wokwi-{os.getpid()}")
        if self.username:
            self.client.username_pw_set(self.username, self.password)
        if self.tls:
            self.client.tls_set()
        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect
        self.client.on_message = lambda c, u, m: self.handle(m.topic, m.payload)
        self.client.reconnect_delay_set(1, 30)
        self.state = "CONNECTING"
        try:
            self.client.connect_async(self.host, self.port, keepalive=30)
            self.client.loop_start()
            return True
        except Exception as err:  # noqa: BLE001 - reported in status
            self.state, self.last_error = "ERROR", str(err)
            return False

    def stop(self) -> None:
        if self.client:
            self.client.loop_stop()
            self.client.disconnect()

    def _on_connect(self, client, userdata, flags, reason_code, properties=None) -> None:
        if getattr(reason_code, "is_failure", False):
            self.state, self.last_error = "ERROR", f"connect refused: {reason_code}"
            return
        for t in self.topics:
            client.subscribe(t, qos=1)
        self.state, self.last_error, self.connected_at = "CONNECTED", None, _iso(_now())

    def _on_disconnect(self, client, userdata, flags, reason_code, properties=None) -> None:
        self.state = "DISCONNECTED"
        self.last_error = f"disconnected: {reason_code}"

    def handle(self, topic: str, raw: bytes) -> None:
        if topic.endswith("/status"):
            self.store.status_message(topic, raw, self.prefix)
        else:
            self.store.ingest(topic, raw, self.prefix)

    def status(self) -> dict[str, Any]:
        return {"enabled": True, "broker": f"{self.host}:{self.port}", "tls": self.tls, "topics": self.topics,
                "state": self.state, "last_error": self.last_error, "connected_at": self.connected_at,
                "authenticated": bool(self.username)}
