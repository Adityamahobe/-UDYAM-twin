"""Shop analytics computed from the tick stream: OEE, energy, CO₂, cost, and short trends.

Fed with every broadcast batch (observed, snapshot, line and event frames). Two windows:
- `window`: since the last reset (OEE and the energy shown on the dashboard);
- `session`: since the server started (survives resets; used by Reports).

OEE = Availability × Performance × Quality, per producing machine:
- Availability = ticks running (RUN or DERATE) ÷ all ticks in the window.
- Performance  = mean, over running ticks, of actual ÷ nominal rpm, capped at 1, and 0 while the
                 line station is STARVED or BLOCKED.
- Quality      = parts made outside a quality-hold condition ÷ parts made. A part is put on
                 quality hold when it completes while rule risk is HIGH (vibration zone D,
                 over-temperature, trip) or a tool-wear warning is active. This is a rule-based
                 estimate: the simulation has no inspection data.
Energy is integrated from reported power each tick; the site total is the sum over machines.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Any

TICK_S = 0.5
TREND_EVERY_TICKS = 4        # one trend point every 2 s
TREND_POINTS = 900           # 30 min of trend per machine
OEE_TEMPLATES = {"cnc_mill", "cnc_lathe", "bar_saw"}
RUNNING = {"RUN", "DERATE"}


@dataclass
class _Acc:
    ticks: int = 0
    running: int = 0
    perf_sum: float = 0.0
    parts: int = 0
    parts_hold: int = 0
    energy_kwh: float = 0.0
    alert_ticks: int = 0

    def oee(self) -> dict[str, Any]:
        a = self.running / self.ticks if self.ticks else None
        p = self.perf_sum / self.running if self.running else None
        q = (self.parts - self.parts_hold) / self.parts if self.parts else None
        oee = a * p * (q if q is not None else 1.0) if a is not None and p is not None else None
        return {
            "availability": _r(a), "performance": _r(p), "quality": _r(q), "oee": _r(oee),
            "quality_basis": "rule-based quality hold" if q is not None else "no parts completed yet (OEE uses Q = 1)",
            "running_s": self.running * TICK_S, "window_s": self.ticks * TICK_S,
            "parts": self.parts, "parts_quality_hold": self.parts_hold,
        }


def _r(v: float | None) -> float | None:
    return None if v is None else round(v, 4)


@dataclass
class Analytics:
    window: dict[str, _Acc] = field(default_factory=dict)
    session: dict[str, _Acc] = field(default_factory=dict)
    templates: dict[str, str] = field(default_factory=dict)
    trends: dict[str, deque] = field(default_factory=dict)
    window_started: str | None = None
    session_started: str | None = None
    _last_parts: dict[str, int] = field(default_factory=dict)
    _stations: dict[str, str] = field(default_factory=dict)
    _tick: int = 0

    def observe(self, msgs: list[dict[str, Any]]) -> None:
        obs = {m["machine_id"]: m for m in msgs if m.get("type") == "observed"}
        for m in msgs:
            t = m.get("type")
            if t == "event" and m.get("kind") == "RESET":
                self.window.clear()
                self._last_parts.clear()
                self.window_started = None
            elif t == "line":
                self._stations = {s["machine_id"]: s["state"] for s in m.get("stations", [])}
        snaps = [m for m in msgs if m.get("type") == "snapshot"]
        if not snaps:
            return
        self._tick += 1
        for s in snaps:
            self._observe_one(s, obs.get(s["machine_id"], {}))

    def _observe_one(self, s: dict[str, Any], o: dict[str, Any]) -> None:
        mid = s["machine_id"]
        self.templates[mid] = s["template"]
        self.window_started = self.window_started or s["ts"]
        self.session_started = self.session_started or s["ts"]
        running = s["state"] in RUNNING
        nominal, actual = s.get("rpm_nominal"), s.get("rpm")
        speed = min(1.0, actual / nominal) if running and nominal and isinstance(actual, (int, float)) else 0.0
        if self._stations.get(mid) in ("STARVED", "BLOCKED"):
            speed = 0.0
        parts = o.get("parts_done")
        new_parts = 0
        if isinstance(parts, int):
            prev = self._last_parts.get(mid)
            new_parts = max(0, parts - prev) if prev is not None else 0
            self._last_parts[mid] = parts
        risk = s.get("risk") or {}
        hold = risk.get("rules") == "HIGH" or (s.get("forecast") or {}).get("warning") == "TWF_RISK"
        power = s.get("power_kw")
        kwh = power * TICK_S / 3600.0 if isinstance(power, (int, float)) else 0.0

        for store in (self.window, self.session):
            acc = store.setdefault(mid, _Acc())
            acc.ticks += 1
            acc.running += running
            acc.perf_sum += speed if running else 0.0
            acc.parts += new_parts
            acc.parts_hold += new_parts if hold else 0
            acc.energy_kwh += kwh
            acc.alert_ticks += risk.get("rules") != "LOW"

        if self._tick % TREND_EVERY_TICKS == 0:
            self.trends.setdefault(mid, deque(maxlen=TREND_POINTS)).append({
                "ts": s["ts"], "mode": s["mode"], "state": s["state"], "rpm": s.get("rpm"),
                "vibration_rms_mm_s": s.get("vibration_rms_mm_s"), "temp_c": s.get("temp_c"),
                "power_kw": power, "load_pct": s.get("load_pct"), "zone": (s.get("severity") or {}).get("zone"),
                "risk": risk.get("rules"),
            })

    # ------------------------------------------------------------------ outputs
    def site_kwh(self, store: dict[str, _Acc]) -> float:
        return sum(a.energy_kwh for a in store.values())

    def summary(self, store_name: str = "window") -> dict[str, Any]:
        store = self.window if store_name == "window" else self.session
        machines = {}
        for mid, acc in store.items():
            tpl = self.templates.get(mid)
            machines[mid] = {
                "template": tpl,
                "energy_kwh": round(acc.energy_kwh, 6),
                "mean_power_kw": round(acc.energy_kwh * 3600.0 / (acc.ticks * TICK_S), 3) if acc.ticks else None,
                "oee": acc.oee() if tpl in OEE_TEMPLATES else None,
            }
        return {
            "since": self.window_started if store_name == "window" else self.session_started,
            "elapsed_s": max((a.ticks for a in store.values()), default=0) * TICK_S,
            "site_energy_kwh": round(self.site_kwh(store), 6),
            "machines": machines,
        }
