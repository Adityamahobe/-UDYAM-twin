"""Virtual PLC state machine and safety interlocks.

Evaluates scan-cycle states: IDLE -> RUN -> DERATE -> TRIP.
"""

from __future__ import annotations

from enum import Enum

from config import get_config


class PlcState(str, Enum):
    IDLE = "IDLE"
    RUN = "RUN"
    DERATE = "DERATE"
    TRIP = "TRIP"


class VirtualPlc:
    """Software virtual PLC with interlocks and scan-cycle evaluation."""

    def __init__(self, vib_trip_threshold: float | None = None):
        self.state: PlcState = PlcState.RUN
        if vib_trip_threshold is None:
            vib_trip_threshold = get_config().template("cnc_mill")["thresholds"]["vibration_trip_mm_s"]
        self.vib_trip_threshold = vib_trip_threshold

    def evaluate(
        self,
        *,
        has_critical_trip: bool = False,
        vibration_rms: float = 0.0,
        derate_pct: float = 0.0,
        is_idle: bool = False,
    ) -> PlcState:
        """Scan cycle: evaluate interlocks and return current PLC state.

        A trip latches, like a real safety interlock: the machine stays in TRIP until reset_trip()
        (a repair or clearing the faults), even though vibration falls once the spindle stops.
        """
        if self.state == PlcState.TRIP:
            return self.state
        if has_critical_trip or (vibration_rms > self.vib_trip_threshold):
            self.state = PlcState.TRIP
        elif is_idle:
            self.state = PlcState.IDLE
        elif derate_pct > 0.0:
            self.state = PlcState.DERATE
        else:
            self.state = PlcState.RUN
        return self.state

    def reset_trip(self) -> None:
        if self.state == PlcState.TRIP:
            self.state = PlcState.RUN

    @property
    def is_running(self) -> bool:
        return self.state in (PlcState.RUN, PlcState.DERATE)

    def speed_factor(self, derate_pct: float = 0.0) -> float:
        if self.state == PlcState.TRIP or self.state == PlcState.IDLE:
            return 0.0
        if self.state == PlcState.DERATE:
            return max(0.0, 1.0 - derate_pct / 100.0)
        return 1.0
