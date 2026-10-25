"""Operating mode: TESTING (the existing simulation) or REAL DATA (telemetry from real devices).

- TESTING: the virtual plant runs exactly as before; every simulator control is available.
- REAL DATA: monitoring of readings that real (not simulated) devices send through the IoT hub
  (MQTT, HTTP or WebSocket). Read-only: simulator controls are refused, because in this mode an
  operator could mistake a simulated change for something done to a real machine. Nothing in
  UDYAM-TWIN sends commands to physical equipment in either mode.

The engine keeps simulating in the background in REAL DATA mode (switching back to TESTING must not
lose the simulation), but its readings stay tagged SIM and the dashboard does not present them as real.

This is separate from the telemetry *source* of each machine (`/api/mode`: SIM, REPLAY, LIVE).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

TESTING = "TESTING"
REAL_DATA = "REAL_DATA"
MODES = (TESTING, REAL_DATA)
LABEL = {TESTING: "Testing mode · simulation", REAL_DATA: "Real data mode · live telemetry"}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


class ReadOnlyError(Exception):
    """A simulator control was used in REAL DATA mode."""

    def __init__(self, action: str):
        super().__init__(action)
        self.action = action

    def detail(self) -> dict[str, str]:
        return {"error": "real_data_read_only",
                "message": f"{self.action} is a Testing-mode (simulator) control. Real Data mode is read-only "
                           "monitoring; switch to Testing mode to use the simulator."}


class OperatingMode:
    def __init__(self, mode: str = TESTING):
        self.mode = mode
        self.since = _now_iso()
        self.changed_by = "startup"
        self.history: list[dict[str, Any]] = [{"mode": mode, "at": self.since, "by": "startup"}]

    @property
    def is_real(self) -> bool:
        return self.mode == REAL_DATA

    def set(self, mode: str, by: str = "dashboard") -> bool:
        """Switch mode; returns True if it changed."""
        if mode not in MODES:
            raise ValueError(f"mode must be one of {MODES}")
        if mode == self.mode:
            return False
        self.mode, self.since, self.changed_by = mode, _now_iso(), by
        self.history = (self.history + [{"mode": mode, "at": self.since, "by": by}])[-50:]
        return True

    def guard(self, action: str) -> None:
        if self.is_real:
            raise ReadOnlyError(action)

    def public(self) -> dict[str, Any]:
        return {"mode": self.mode, "label": LABEL[self.mode], "since": self.since, "changed_by": self.changed_by,
                "read_only": self.is_real, "history": self.history[-10:]}


def connection_state(mqtt: dict[str, Any] | None, devices: list[dict[str, Any]]) -> dict[str, Any]:
    """One connection state for REAL DATA mode, from the MQTT bridge and the hub's devices.

    Only real devices count (`simulated` stand-ins such as tools/fake_device never do):
    CONNECTED    a real device is sending data (ONLINE), over MQTT, HTTP or WebSocket
    STALE_DATA   real devices were seen but none is sending now (values are held or nulled)
    CONNECTING / DISCONNECTED / ERROR   the MQTT broker connection, while no real data is arriving
    NOT_CONFIGURED  no broker configured and no real device has ever sent data
    """
    real = [d for d in devices if not d.get("simulated") and d.get("machine_id")]
    online = [d for d in real if d.get("status") == "ONLINE"]
    seen = [d for d in real if d.get("last_seen")]
    stale = [d for d in seen if d.get("status") in ("STALE", "OFFLINE")]
    broker = (mqtt or {}).get("state") if (mqtt or {}).get("enabled") else "NOT_CONFIGURED"
    if online:
        state = "CONNECTED"
        message = f"{len(online)} real device(s) sending data"
    elif stale:
        state = "STALE_DATA"
        oldest = max((d.get("age_s") or 0) for d in stale)
        message = f"No new readings from {len(stale)} real device(s) (last {oldest:.0f} s ago); values are not live"
    elif broker in ("CONNECTING", "DISCONNECTED", "ERROR"):
        state = broker
        message = {"CONNECTING": "Connecting to the MQTT broker…",
                   "DISCONNECTED": "MQTT broker disconnected; retrying",
                   "ERROR": f"MQTT error: {(mqtt or {}).get('last_error') or 'unknown'}"}[broker]
    elif broker == "CONNECTED":
        state = "CONNECTED"
        message = "MQTT broker connected; no real device has sent data yet"
    else:
        state = "NOT_CONFIGURED"
        message = "No MQTT broker configured (UDYAM_MQTT_HOST) and no real device has sent data"
    last = max((d["last_seen"] for d in seen), default=None)
    return {
        "state": state, "message": message, "verified_live": bool(online),
        "broker": {"state": broker, "address": (mqtt or {}).get("broker"), "topic": (mqtt or {}).get("topic"),
                   "received": (mqtt or {}).get("received"), "rejected": (mqtt or {}).get("rejected"),
                   "last_error": (mqtt or {}).get("last_error"), "last_message_at": (mqtt or {}).get("last_message_at")},
        "devices": {"real": len(real), "online": len(online), "stale_or_offline": len(stale),
                    "simulated_stand_ins": sum(1 for d in devices if d.get("simulated"))},
        "live_machines": sorted({d["machine_id"] for d in online}),
        "last_message_at": last,
        "checked_at": _now_iso(),
    }
