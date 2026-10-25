"""Factory Engine orchestrating physical plant simulators, digital twins, and line flow.

Serves as the research core engine for the digital twin platform.
"""

from __future__ import annotations

import random
from typing import Callable
from datetime import datetime, timedelta, timezone

from config import get_config
from ml.vibration.severity import SeverityAssessor
from sim import controls
from sim.line import ProductionLine
from sim.plant import PlantMachine, TEMPLATE_PREFIX, NAMEPLATE_DEFAULTS
from sim.scenarios import SCENARIOS
from twin.model import DigitalTwin
from twin.snapshot import build_snapshot

SCHEMA_VERSION = "1.0"
TICK_S = 0.5


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


class FactoryEngine:
    """Orchestrates multi-machine plant instances, twin models, and production line flow."""

    def __init__(
        self,
        seed: int = 42,
        start: datetime | None = None,
        auto_demo: bool = False,
        machines: int = 4,
    ):
        self.seed = seed
        # Kept for callers that pass it; the 90 s Auto Demo is driven from api/demo.py, not the engine.
        self.auto_demo = auto_demo
        # Supervised self-healing in SIM: apply the twin's proposed derate automatically.
        # False for "no action" runs in the UDYAM-SIM dataset (tools/generate_dataset.py).
        self.auto_derate = True
        self.n_machines = machines
        # Timestamps: real UTC wall clock by default. Passing `start` switches to simulated time
        # (start + elapsed ticks), which tests use for determinism; `clock` says which one is in use.
        self.start = start
        self.clock = "sim_time" if start is not None else "wall_utc"
        self.t = 0.0
        self.event_seq = 0
        self.pending_events: list[dict] = []
        self.registry_dirty = False
        self.common_cause_announced = False
        # Optional hook (set by the IoT hub) that swaps sensed signals for LIVE device readings.
        self.live_overlay: Callable[[dict], dict] | None = None

        self.plants: dict[str, PlantMachine] = {}
        self.twins: dict[str, DigitalTwin] = {}
        self.assessor = SeverityAssessor()
        # Latest `snapshot` per machine: what every panel renders (also served at /api/snapshots).
        self.snapshots: dict[str, dict] = {}
        # Fault-lab scenario running on each machine (machine_id → scenario_id)
        self.active_scenarios: dict[str, str] = {}
        # Telemetry mode: SIM, or REPLAY (AI4I rows into one CNC mill); LIVE comes from the IoT overlay.
        self.mode = "SIM"
        self.replay = None
        self.replay_machine: str | None = None
        # Machines added with "Add Your Machine": re-created after every reset, in order
        self.custom_machines: list[dict] = []
        self.line = ProductionLine()
        self.rng = random.Random(self.seed)
        # Testing-mode machine parameters (sim/controls.py), per machine; kept across resets.
        self.sim_params: dict[str, dict[str, float]] = {}
        # Run control: RUNNING, PAUSED (state frozen) or STOPPED (reset to baseline, not ticking).
        # `run_until_t` ends a timed run ("run for N minutes") by pausing at that simulation time.
        self.run_state = "RUNNING"
        self.run_until_t: float | None = None
        self.run_started_t: float | None = None

        self.reset(announce=False)

    def reset(self, announce: bool = True) -> list[dict]:
        self.rng = random.Random(self.seed)
        self.plants.clear()
        self.twins.clear()
        self.snapshots.clear()
        self.line.reset()
        self.mode, self.replay, self.replay_machine = "SIM", None, None
        self.common_cause_announced = False
        self.pending_events.clear()

        # Add initial machines
        self._add_machine("cnc_mill", "CNC Mill (VMC)", hero=True, x=8.0, y=0.0, order=2)
        self._add_machine("cnc_lathe", "CNC Lathe", hero=False, x=4.0, y=0.0, order=1)
        self._add_machine("bar_saw", "Bar Saw", hero=False, x=0.0, y=0.0, order=0)
        if self.n_machines >= 4:
            self._add_machine("air_compressor", "Air Compressor", hero=False, x=4.0, y=-4.0, order=-1)
        for spec in self.custom_machines:
            self._add_machine(spec["template"], spec["name"], x=spec["x"], y=spec["y"],
                              nameplate=spec.get("nameplate"), machine_id=spec["machine_id"])
        self.active_scenarios.clear()

        self.registry_dirty = announce
        if announce:
            self._emit_event(None, "RESET", "INFO", "Simulation reset")
        return self.flush_events()

    def _add_machine(
        self,
        template: str,
        name: str,
        hero: bool = False,
        x: float = 0.0,
        y: float = 0.0,
        order: int | None = None,
        nameplate: dict | None = None,
        machine_id: str | None = None,
    ) -> PlantMachine:
        mid = machine_id or self.next_machine_id(template)
        if order is None:
            order = -1 if template == "air_compressor" else len(self.plants)

        plant = PlantMachine(
            machine_id=mid,
            template=template,
            name=name,
            hero=hero,
            x=x,
            y=y,
            order=order,
            nameplate=dict(NAMEPLATE_DEFAULTS.get(template, {}) | (nameplate or {})),
        )
        twin = DigitalTwin(machine_id=mid, template=template, hero=hero)

        twin.rated_kw = plant.nameplate.get("rated_kw")
        if mid in self.sim_params:
            controls.apply(plant, self.sim_params[mid])
        self.plants[mid] = plant
        self.twins[mid] = twin
        return plant

    def next_machine_id(self, template: str) -> str:
        """Next free id for a template, e.g. VMC-02 (ids of removed machines are not reused)."""
        prefix = TEMPLATE_PREFIX[template]
        taken = set(self.plants) | {s["machine_id"] for s in self.custom_machines}
        n = 1 + sum(1 for m in self.plants.values() if m.template == template)
        while f"{prefix}-{n:02d}" in taken:
            n += 1
        return f"{prefix}-{n:02d}"

    def add_custom_machine(self, spec: dict, announce: bool = True) -> list[dict]:
        """Add a validated runtime machine (config already registered) that survives resets."""
        self.custom_machines.append(spec)
        plant = self._add_machine(spec["template"], spec["name"], x=spec["x"], y=spec["y"],
                                  nameplate=spec.get("nameplate"), machine_id=spec["machine_id"])
        if not announce:
            return []
        self._emit_event(plant.machine_id, "MACHINE_ADDED", "INFO", f"{plant.name} added from the '{plant.template}' template",
                         data={"template": plant.template, "nameplate": plant.nameplate, "calibration": "template defaults"})
        return [self.registry()] + self.flush_events()

    def remove_custom_machine(self, machine_id: str) -> list[dict]:
        self.custom_machines = [s for s in self.custom_machines if s["machine_id"] != machine_id]
        for store in (self.plants, self.twins, self.snapshots, self.active_scenarios, self.sim_params):
            store.pop(machine_id, None)
        self._emit_event(machine_id, "MACHINE_REMOVED", "INFO", f"{machine_id} removed")
        return [self.registry()] + self.flush_events()

    def hello(self) -> dict:
        return {
            "type": "hello",
            "schema_version": SCHEMA_VERSION,
            "generator": "udyam_core",
            "tick_ms": int(TICK_S * 1000),
        }

    def registry(self) -> dict:
        return {
            "type": "registry",
            "schema_version": SCHEMA_VERSION,
            "machines": [{**p.registry_entry(), "custom": any(s["machine_id"] == p.machine_id for s in self.custom_machines)}
                         for p in self.plants.values()],
        }

    def command(self, cmd: dict) -> list[dict]:
        """Process incoming client command frame."""
        kind = cmd.get("cmd")
        out: list[dict] = []

        if kind == "inject_fault":
            mid = cmd.get("machine_id", "")
            plant = self.plants.get(mid)
            if plant:
                fault = cmd["fault"]
                plant.faults.inject(fault, self.t)
                self._emit_event(
                    plant.machine_id,
                    "FAULT_INJECTED",
                    "INFO",
                    f"Injected '{fault}' on {plant.name} (plant only; the twin is not told)",
                    data={"fault": fault},
                )
        elif kind == "clear_faults":
            targets = [self.plants[cmd["machine_id"]]] if cmd.get("machine_id") in self.plants else self.plants.values()
            for plant in targets:
                plant.faults.clear()
                self.active_scenarios.pop(plant.machine_id, None)
        elif kind == "start_scenario":
            sc = SCENARIOS.get(cmd.get("scenario", ""))
            plant = self.plants.get(sc.machine_id) if sc else None
            if plant:
                for fault in sc.faults:
                    plant.faults.inject(fault, self.t)
                self.active_scenarios[plant.machine_id] = sc.scenario_id
                self._emit_event(
                    plant.machine_id, "FAULT_INJECTED", "INFO",
                    f"Demo simulation started: {sc.label} on {plant.name} (plant only; the twin is not told)",
                    data={"scenario": sc.scenario_id, "faults": list(sc.faults), "expect": sc.expect},
                )
        elif kind == "resolve":
            # Simulated repair of one machine (or every machine with an active fault or derate)
            mid = cmd.get("machine_id")
            targets = [self.plants[mid]] if mid in self.plants else [
                p for p in self.plants.values() if p.faults.active_faults or p.derate_pct or p.machine_id in self.active_scenarios]
            for plant in targets:
                sc = SCENARIOS.get(self.active_scenarios.pop(plant.machine_id, ""))
                had_coolant = plant.faults.is_active("coolant_degradation")
                self.pending_events.extend(plant.resolve(self._ts()))
                if had_coolant:
                    # Coolant serviced: re-baseline the thermal calibration (alert latches are kept).
                    self.twins[plant.machine_id].calibrator.reset()
                    self.twins[plant.machine_id].residual.reset()
                self._emit_event(
                    plant.machine_id, "RESOLUTION", "INFO",
                    f"Simulated resolution on {plant.name}: " + (sc.resolution if sc else "faults cleared, derate released"),
                    data={"scenario": sc.scenario_id if sc else None},
                )
        elif kind == "reset":
            events = self.reset()
            self.registry_dirty = False
            return [self.registry()] + events
        elif kind == "auto_demo":
            self.auto_demo = bool(cmd.get("on", True))
        elif kind == "set_mode":
            self.set_mode(cmd.get("mode", "SIM"), cmd.get("machine_id", "VMC-01"), int(cmd.get("start_row", 0)))
        elif kind == "set_derate":
            plant = self.plants.get(cmd.get("machine_id", ""))
            if plant:
                plant.derate_pct = max(0.0, min(100.0, float(cmd.get("pct", 0))))
        elif kind == "add_machine":
            tpl = cmd.get("template", "air_compressor")
            if tpl in TEMPLATE_PREFIX:
                plant = self._add_machine(
                    tpl,
                    cmd.get("name") or tpl.replace("_", " ").title(),
                    x=float(cmd.get("x", 4.0 * len(self.plants))),
                    y=float(cmd.get("y", -4.0)),
                    nameplate=cmd.get("nameplate"),
                )
                self._emit_event(
                    plant.machine_id,
                    "MACHINE_ADDED",
                    "INFO",
                    f"{plant.name} onboarded from '{tpl}' template",
                    data={"template": tpl, "calibration": "starting from template defaults"},
                )
                out.append(self.registry())

        return out + self.flush_events()

    def _emit_event(
        self,
        machine_id: str | None,
        kind: str,
        severity: str,
        title: str,
        measured: str | None = None,
        inferred: str | None = None,
        suggested: str | None = None,
        data: dict | None = None,
    ) -> None:
        self.event_seq += 1
        ev: dict = {
            "type": "event",
            "schema_version": SCHEMA_VERSION,
            "ts": self._ts(),
            "event_id": f"E{self.event_seq:05d}",
            "machine_id": machine_id,
            "kind": kind,
            "severity": severity,
            "title": title,
        }
        if measured is not None:
            ev["measured"] = measured
        if inferred is not None:
            ev["inferred"] = inferred
        if suggested is not None:
            ev["suggested"] = suggested
        if data is not None:
            ev["data"] = data
        self.pending_events.append(ev)

    def flush_events(self) -> list[dict]:
        flushed = list(self.pending_events)
        self.pending_events.clear()
        for ev in flushed:
            # Twin/plant events carry a fixed rule id (e.g. EV_CAL_VMC-01); make every event id unique.
            if not ev.get("event_id", "").startswith("E0"):
                self.event_seq += 1
                rule = ev.get("event_id") or ev.get("kind", "EV")
                ev["event_id"] = f"E{self.event_seq:05d}-{rule}"
        return flushed

    def _ts(self) -> str:
        if self.start is None:
            return _iso(datetime.now(timezone.utc))
        return _iso(self.start + timedelta(seconds=self.t))

    # ------------------------------------------------------------------
    # Testing-mode parameters and run control (simulation only)
    # ------------------------------------------------------------------
    def machine_params(self, machine_id: str) -> dict:
        plant = self.plants[machine_id]
        values = {**controls.defaults_for(plant.template), **self.sim_params.get(machine_id, {})}
        return {"machine_id": machine_id, "name": plant.name, "template": plant.template,
                "values": values, "default": controls.is_default(plant.template, values),
                "specs": controls.specs_for(plant.template, plant.nameplate)}

    def set_params(self, machine_id: str, changes: dict) -> list[dict]:
        """Validate and apply parameters to one simulated machine (ParamError if outside the limits)."""
        plant = self.plants.get(machine_id)
        if plant is None:
            raise controls.ParamError(f"Unknown machine {machine_id}")
        before = {**controls.defaults_for(plant.template), **self.sim_params.get(machine_id, {})}
        merged = controls.validate(plant.template, plant.nameplate, before, changes)
        changed = {k: {"from": before[k], "to": v} for k, v in merged.items() if abs(before[k] - v) > 1e-9}
        if controls.is_default(plant.template, merged):
            self.sim_params.pop(machine_id, None)
        else:
            self.sim_params[machine_id] = merged
        controls.apply(plant, merged)
        if changed:
            spec = controls.PARAMS
            text = ", ".join(f"{spec[k].label.lower()} {c['from']:g} → {c['to']:g} {spec[k].unit}" for k, c in changed.items())
            self._emit_event(machine_id, "SIM_PARAMS", "INFO", f"Simulation parameters on {plant.name}: {text}",
                             data={"changed": changed, "values": merged, "simulation_only": True})
        return self.flush_events()

    def reset_params(self, machine_id: str | None = None) -> list[dict]:
        out: list[dict] = []
        for mid in [machine_id] if machine_id else list(self.sim_params):
            if mid in self.plants and mid in self.sim_params:
                out += self.set_params(mid, controls.defaults_for(self.plants[mid].template))
        return out + self.flush_events()

    @property
    def running(self) -> bool:
        return self.run_state == "RUNNING"

    def run_status(self) -> dict:
        left = None if self.run_until_t is None else max(0.0, self.run_until_t - self.t)
        return {"state": self.run_state, "sim_time_s": round(self.t, 1),
                "run_for_s": None if self.run_until_t is None or self.run_started_t is None
                else round(self.run_until_t - self.run_started_t, 1),
                "remaining_s": None if left is None else round(left, 1)}

    def run_control(self, action: str, minutes: float | None = None) -> list[dict]:
        """pause | resume | stop | start | run_for (minutes). Simulation only."""
        if action == "pause":
            if self.run_state == "RUNNING":
                self.run_state = "PAUSED"
                self._emit_event(None, "SIM_RUN", "INFO", "Simulation paused", data={"action": action})
        elif action in ("resume", "start"):
            if self.run_state != "RUNNING":
                was = self.run_state
                self.run_state = "RUNNING"
                self._emit_event(None, "SIM_RUN", "INFO", "Simulation started" if was == "STOPPED" else "Simulation resumed",
                                 data={"action": action})
        elif action == "stop":
            out = self.reset()  # clean baseline; parameters are kept
            self.registry_dirty = False
            self.run_state, self.run_until_t, self.run_started_t = "STOPPED", None, None
            self._emit_event(None, "SIM_RUN", "INFO", "Simulation stopped and reset to a clean baseline",
                             data={"action": action})
            return [self.registry()] + out + self.flush_events()
        elif action == "run_for":
            if minutes is None or not (0 < float(minutes) <= controls.RUN_FOR_MAX_MIN):
                raise controls.ParamError(f"Run time must be more than 0 and at most {controls.RUN_FOR_MAX_MIN:g} minutes")
            self.run_state = "RUNNING"
            self.run_started_t, self.run_until_t = self.t, self.t + float(minutes) * 60.0
            self._emit_event(None, "SIM_RUN", "INFO", f"Simulation running for {float(minutes):g} min, then pausing",
                             data={"action": action, "minutes": float(minutes)})
        else:
            raise controls.ParamError(f"Unknown run action '{action}'")
        if action in ("pause", "stop", "resume", "start"):
            self.run_until_t = self.run_started_t = None
        return self.flush_events()

    def step(self) -> list[dict]:
        """Advance the factory simulation by one 500ms tick (nothing while paused or stopped)."""
        if not self.running:
            return []
        self.t += TICK_S

        comp = next((p for p in self.plants.values() if p.template == "air_compressor"), None)
        air_ok = comp.pressure_bar >= comp.limits["pressure_min_bar"] if comp else True

        out: list[dict] = []
        if self.registry_dirty:
            self.registry_dirty = False
            out.append(self.registry())

        ts_str = self._ts()

        # Step every machine
        for mid, plant in self.plants.items():
            twin = self.twins[mid]
            if plant.template in ("cnc_mill", "cnc_lathe"):
                obs, plant_events = plant.step_cnc(self.t, self.rng, air_ok, ts_str)
                if self.replay is not None and mid == self.replay_machine:
                    obs, plant_events = self.replay.apply(obs), []  # the simulated plant runs on, unseen
                obs = self._live(obs)
                self.pending_events.extend(plant_events)
                twin.auto_derate = self.auto_derate  # so its alert text says whether the derate is applied
                twin_msg, twin_events, proposed_derate = twin.step_cnc(obs)
                if proposed_derate is not None and self.auto_derate:
                    plant.derate_pct = proposed_derate
            else:
                step = {"bar_saw": plant.step_saw, "air_compressor": plant.step_compressor}[plant.template]
                obs = self._live(step(self.rng, ts_str))
                twin_msg, twin_events = twin.step_simple(obs)
            self.pending_events.extend(twin_events)
            out.extend([obs, twin_msg, self._snapshot(plant, obs, twin_msg)])

        # Common cause detection
        self._check_common_cause(air_ok)

        # Step production line
        line_msg = self.line.step(self.plants, air_ok, ts_str)
        out.append(line_msg)

        if self.run_until_t is not None and self.t >= self.run_until_t - 1e-9:
            minutes = (self.run_until_t - (self.run_started_t or 0.0)) / 60.0
            self.run_state, self.run_until_t, self.run_started_t = "PAUSED", None, None
            self._emit_event(None, "SIM_RUN", "INFO", f"Timed run finished ({minutes:g} min); simulation paused",
                             data={"action": "run_for_done", "minutes": minutes})

        return out + self.flush_events()

    def set_mode(self, mode: str, machine_id: str = "VMC-01", start_row: int = 0) -> None:
        """SIM or REPLAY. REPLAY streams AI4I rows into one CNC mill; its twin is re-baselined on each switch."""
        if mode not in ("SIM", "REPLAY"):
            raise ValueError(f"engine mode must be SIM or REPLAY, not {mode}")
        target = machine_id if mode == "REPLAY" else self.replay_machine
        if mode == "REPLAY":
            plant = self.plants.get(machine_id)
            if plant is None or plant.template != "cnc_mill":
                raise ValueError("REPLAY needs a CNC mill (AI4I is a milling dataset)")
            from sim.replay import Ai4iReplay
            self.replay, self.replay_machine = Ai4iReplay(start_row), machine_id
        else:
            self.replay, self.replay_machine = None, None
        self.mode = mode
        if target in self.twins:
            self.twins[target].reset()
        self._emit_event(target, "MODE", "INFO",
                         f"Telemetry mode {mode}" + (f": AI4I 2020 rows replayed into {target} from row {start_row}"
                                                     if mode == "REPLAY" else ""),
                         data={"mode": mode, "machine_id": target})

    def _snapshot(self, plant: PlantMachine, obs: dict, twin_msg: dict) -> dict:
        """Integrate energy from the reported power and build this machine's snapshot."""
        power = obs.get("power_kw")
        if isinstance(power, (int, float)):
            plant.energy_kwh += power * TICK_S / 3600.0
        snap = build_snapshot(
            obs,
            twin_msg,
            name=plant.name,
            rated_kw=plant.nameplate.get("rated_kw"),
            energy_kwh=plant.energy_kwh,
            stream_band_hz=get_config().stream_band_hz(plant.machine_id, plant.template),
            assessor=self.assessor,
            clock="replay_time" if obs.get("source_mode") == "REPLAY" else self.clock,
            active_scenario=self.active_scenarios.get(plant.machine_id),
        )
        self.snapshots[plant.machine_id] = snap
        return snap

    def _live(self, obs: dict) -> dict:
        return self.live_overlay(obs) if self.live_overlay else obs

    def _check_common_cause(self, air_ok: bool) -> None:
        if not air_ok and not self.common_cause_announced:
            self.common_cause_announced = True
            comp = next((p for p in self.plants.values() if p.template == "air_compressor"), None)
            p_min = comp.limits["pressure_min_bar"] if comp else None
            affected = [p.machine_id for p in self.plants.values() if p.template in ("cnc_mill", "cnc_lathe")]
            self._emit_event(
                "COMP-01",
                "COMMON_CAUSE",
                "WARN",
                "Common cause: compressed-air supply low",
                measured=f"Pressure below {p_min} bar; {', '.join(affected)} derated together",
                inferred="Root cause is the shared compressor, not the individual machines",
                suggested="Inspect compressor intake filter and leaks; do not service the CNCs",
                data={"affected": affected},
            )
        elif air_ok:
            self.common_cause_announced = False
