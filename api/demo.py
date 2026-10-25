"""Auto Demo: the 90-second story, played deterministically and looped while it is on.

    healthy baseline → vibration fault → alert → auto-derate → work order → resolution → baseline

The director is advanced once per engine tick (500 ms), before the engine steps, and its
clock counts ticks, not wall time. Each cycle starts with a clean reset of the seeded engine,
so every cycle produces the same readings, alerts and work order at the same ticks.
Turning it off resets to a clean baseline.
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable, Protocol

from sim.factory import TICK_S

CYCLE_S = 90.0
MACHINE = "VMC-01"
SCENARIO = "vibration_fault"

# (time in s, action, narration). Expected engine behaviour in between (seed 42):
# ~21 s zone C alert (WARN) · ~27 s zone D alert (CRITICAL) + auto-derate 20 % · ~80 s back in zone A.
TIMELINE: list[tuple[float, str, str]] = [
    (0.0, "baseline", "Healthy baseline: every machine in zone A, no open alerts"),
    (15.0, "start", "Vibration fault starts on VMC-01 (demo simulation; the twin is not told)"),
    (40.0, "work_order", "Work order generated from the vibration alert"),
    (48.0, "wo_ack", "Maintenance acknowledges the work order"),
    (54.0, "wo_start", "Technician starts work on the spindle bearing"),
    (65.0, "resolve", "Simulated repair: bearing replaced, derate released"),
    (85.0, "check", "Back to healthy baseline; the story repeats"),
]


class DemoActions(Protocol):
    clean_reset: Callable[[], Awaitable[None]]
    start_scenario: Callable[[str], Awaitable[None]]
    resolve: Callable[[str], Awaitable[None]]
    work_order_for_rule: Callable[[str], Awaitable[dict[str, Any] | None]]
    set_work_order_status: Callable[[str, str], Awaitable[None]]
    narrate: Callable[[str, dict[str, Any]], Awaitable[None]]


class AutoDemo:
    def __init__(self, actions: DemoActions):
        self.actions = actions
        self.on = False
        self.t = 0.0
        self.cycle = 0
        self.idx = 0
        self.wo_id: str | None = None

    def status(self) -> dict[str, Any]:
        nxt = TIMELINE[self.idx] if self.on and self.idx < len(TIMELINE) else None
        return {
            "on": self.on,
            "cycle": self.cycle,
            "t_s": round(self.t, 1),
            "cycle_s": CYCLE_S,
            "work_order": self.wo_id,
            "next": {"at_s": nxt[0], "action": nxt[1], "narration": nxt[2]} if nxt else None,
            "timeline": [{"at_s": t, "action": a, "narration": n} for t, a, n in TIMELINE],
            "label": "Demo simulation",
        }

    async def set(self, on: bool) -> dict[str, Any]:
        if on and not self.on:
            self.on, self.t, self.idx = True, 0.0, 0
        elif not on and self.on:
            self.on = False
            await self.actions.clean_reset()
            await self.actions.narrate("Auto Demo stopped: clean baseline", {"cycle": self.cycle})
        return self.status()

    def restart_cycle(self) -> None:
        """Start the story again from 0 s on the next tick (used after a manual reset)."""
        self.t, self.idx = 0.0, 0

    async def tick(self) -> None:
        """Run every timeline step due at the current demo time, then advance one tick."""
        if not self.on:
            return
        if self.t == 0.0 and self.idx == 0:
            self.cycle += 1
            self.wo_id = None
        while self.on and self.idx < len(TIMELINE) and TIMELINE[self.idx][0] <= self.t + 1e-9:
            at, action, narration = TIMELINE[self.idx]
            self.idx += 1
            await self._run(action)
            await self.actions.narrate(f"Auto Demo {int(at) // 60}:{int(at) % 60:02d}: {narration}",
                                       {"cycle": self.cycle, "at_s": at, "action": action, "work_order": self.wo_id})
        self.t += TICK_S
        if self.t >= CYCLE_S - 1e-9:
            self.restart_cycle()

    async def _run(self, action: str) -> None:
        a = self.actions
        if action == "baseline":
            await a.clean_reset()
        elif action == "start":
            await a.start_scenario(SCENARIO)
        elif action == "work_order":
            wo = await a.work_order_for_rule(f"EV_VIB_{MACHINE}")
            self.wo_id = wo["wo_id"] if wo else None
        elif action in ("wo_ack", "wo_start") and self.wo_id:
            await a.set_work_order_status(self.wo_id, "Acknowledged" if action == "wo_ack" else "In Progress")
        elif action == "resolve":
            await a.resolve(MACHINE)
