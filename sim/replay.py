"""REPLAY mode: stream UCI AI4I 2020 rows, one per 500 ms tick, into a CNC mill's readings.

AI4I rows have no timestamps, so replayed frames carry the replay (wall) time with
`clock: "replay_time"` and the row's UDI. Only the five AI4I process columns are replayed;
AI4I has no vibration or current, so those readings are null (never invented). Each frame
also carries the row's own failure labels, so the model's risk can be shown next to the
dataset's ground truth, and whether the row was held out of training (`held_out`): risk on
training rows looks better than the model really is.
"""

from __future__ import annotations

import math
from typing import Any

from sklearn.model_selection import train_test_split

from ml.ai4i import MODES, SOURCE_URL, load_dataset
from ml.train_ai4i import SEED


class Ai4iReplay:
    def __init__(self, start_row: int = 0):
        df = load_dataset()
        cols = ["UDI", "Product ID", "variant_type", "air_temp_k", "process_temp_k", "rpm", "torque_nm",
                "tool_wear_min", "machine_failure", *MODES]
        self.rows: list[dict[str, Any]] = df[cols].to_dict("records")
        # Same split as ml/train_ai4i.py, so each row can say whether the model saw it in training.
        _, test_idx = train_test_split(df.index, test_size=0.2, random_state=SEED, stratify=df["machine_failure"])
        self.held_out = set(int(i) for i in test_idx)
        self.i = start_row % len(self.rows)
        self.wrapped = 0

    def status(self) -> dict[str, Any]:
        return {"dataset": "AI4I 2020", "source": SOURCE_URL, "row": self.i, "rows_total": len(self.rows),
                "wrapped": self.wrapped}

    def apply(self, obs: dict[str, Any]) -> dict[str, Any]:
        """Replace a simulated CNC frame's process readings with the next AI4I row."""
        r, held_out = self.rows[self.i], self.i in self.held_out
        self.i += 1
        if self.i >= len(self.rows):
            self.i, self.wrapped = 0, self.wrapped + 1
        rpm, torque = float(r["rpm"]), float(r["torque_nm"])
        power = torque * rpm * 2.0 * math.pi / 60.0 / 1000.0
        return {
            **obs,
            "source_mode": "REPLAY",
            "plc_state": "RUN",
            "dq_flag": "OK",
            "derate_pct": 0.0,
            "active_fault": None,
            "air_temp_k": float(r["air_temp_k"]),
            "process_temp_k": float(r["process_temp_k"]),
            "rpm": rpm,
            "rpm_setpoint": None,
            "torque_nm": torque,
            "tool_wear_min": float(r["tool_wear_min"]),
            "power_kw": round(power, 3),
            "current_a": None,             # not in AI4I
            "vibration_rms_mm_s": None,    # not in AI4I
            "replay": {
                "dataset": "AI4I 2020",
                "udi": int(r["UDI"]),
                "product_id": r["Product ID"],
                "variant": r["variant_type"],
                "labels": {"machine_failure": int(r["machine_failure"]), **{m: int(r[m]) for m in MODES}},
                "held_out": held_out,
            },
        }
