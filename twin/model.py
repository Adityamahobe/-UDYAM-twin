"""Digital Twin model for machine assets.

Calculates model predictions, evaluates residuals, performs self-calibration,
generates forecasts, and assesses failure risk.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

from config import get_config
from ml.ai4i import AI4IPredictor
from twin.calibrate import TwinCalibrator
from twin.forecast import ForecastEngine
from twin.residuals import ResidualState, ResidualTracker

SCHEMA_VERSION = "1.0"
TICK_S = 0.5
RISK_ORDER = ("LOW", "MEDIUM", "HIGH")
SEV_RANK = {"WARN": 1, "CRITICAL": 2}
K_TO_C = 273.15
ML_EVERY_TICKS = 4          # AI4I risk is re-evaluated every 2 s and held in between
RUL_WINDOW_S = 60.0         # tool-wear trend window for the RUL estimate
RUL_MIN_POINTS = 20         # at least 10 s of trend before an RUL is given


@lru_cache(maxsize=1)
def ai4i_predictor() -> AI4IPredictor:
    """One shared model per process (loaded on first use)."""
    return AI4IPredictor()


def _c(k: float) -> str:
    """Kelvin → '°C' text for alert cards (the UI shows °C; the model works in K like AI4I)."""
    return f"{k - K_TO_C:.1f} °C"


def vibration_risk(vib_rms: float, zones: tuple[float, float] | None) -> str:
    """Rule risk from the machine's vibration profile: zone C → MEDIUM, zone D → HIGH."""
    if zones is None:
        return "LOW"
    medium, high = zones
    return "HIGH" if vib_rms >= high else "MEDIUM" if vib_rms >= medium else "LOW"


@dataclass
class DigitalTwin:
    """Twin model maintaining parameter estimates and running predictive physics."""

    machine_id: str
    template: str
    hero: bool = False

    calibrator: TwinCalibrator = field(default_factory=TwinCalibrator)
    residual: ResidualTracker = field(default_factory=ResidualTracker)
    pred_temp: float | None = None
    pred_wear: float = 118.0
    alert_announced: bool = False
    limits: dict = field(default_factory=dict)
    vib_zones: tuple[float, float] | None = None
    # Calibration event throttle and the single cooling-loss alert (thresholds in config/machines.yaml)
    ticks: int = 0
    cal_logged_h: float = 1.0
    cal_logged_t: float = float("-inf")
    cal_updates: int = 0
    cooling_alerted: bool = False
    # Failure-risk model (config `ml`) and tool-wear history for the RUL estimate
    ml_cfg: dict = field(default_factory=dict)
    ml_detail: dict | None = None
    wear_hist: deque = field(default_factory=lambda: deque(maxlen=int(RUL_WINDOW_S / TICK_S)))
    rule_state: dict = field(default_factory=dict)  # debounced alert latches, see _rule()
    rated_kw: float | None = None                   # set by the engine from the machine's nameplate

    def __post_init__(self):
        cfg = get_config()
        self.ml_cfg = cfg.ml(self.machine_id, self.template)
        self.limits = cfg.thresholds(self.machine_id, self.template)
        self.vib_zones = cfg.risk_zones(self.machine_id, self.template)
        if self.template == "air_compressor":
            self.residual.signal = "pressure_bar"
            self.residual.band = 0.5
        elif self.template == "bar_saw":
            self.residual.signal = "power_kw"
            self.residual.band = 0.5
        elif self.template == "cnc_lathe":
            self.pred_temp = 310.8
            self.pred_wear = 64.0
        elif self.template == "cnc_mill":
            self.pred_temp = 311.5
            self.pred_wear = 118.0

    # ------------------------------------------------------------------ alert rules
    def _rule(self, rule: str, level: str | None, build) -> dict | None:
        """Debounced alert latch for one rule.

        A level (None, "WARN", "CRITICAL") must hold for `alert_sustain_s` before it takes effect.
        An alert event is built on entry and on escalation, never repeated while the level holds;
        the rule re-arms only after the condition has been clear for the sustain time.
        """
        st = self.rule_state.setdefault(rule, {"latched": None, "pending": None, "ticks": 0})
        if level != st["pending"]:
            st["pending"], st["ticks"] = level, 0
        st["ticks"] += 1
        need = max(1, round(self.limits.get("alert_sustain_s", 0.0) / TICK_S))
        if st["ticks"] < need or level == st["latched"]:
            return None
        prev, st["latched"] = st["latched"], level
        if level is None or (prev is not None and SEV_RANK[level] <= SEV_RANK[prev]):
            return None
        return build(level)

    def _alert(self, ts: str, rule: str, level: str, title: str, measured: str, inferred: str, suggested: str,
               data: dict | None = None) -> dict:
        return {
            "type": "event", "schema_version": SCHEMA_VERSION, "ts": ts,
            "event_id": f"EV_{rule}_{self.machine_id}", "machine_id": self.machine_id,
            "kind": "ALERT", "severity": level, "title": title,
            "measured": measured, "inferred": inferred, "suggested": suggested, "data": data or {},
        }

    def _vibration_alert(self, obs: dict[str, Any], vib: float, derate_to: float | None) -> dict | None:
        """Zone C → WARN, zone D → CRITICAL (same alert, escalated). `derate_to`: new setpoint if derating."""
        if self.vib_zones is None or obs.get("vibration_rms_mm_s") is None:
            return None
        b_c, c_d = self.vib_zones
        level = "CRITICAL" if vib >= c_d else "WARN" if vib >= b_c else None

        def build(lv: str) -> dict:
            zone, bound, name = ("D", c_d, "C/D") if lv == "CRITICAL" else ("C", b_c, "B/C")
            title = f"Vibration zone {zone}: {vib:.2f} mm/s ≥ {name} {bound:g} mm/s"
            data: dict[str, Any] = {"zone": zone, "threshold": {"crossed": name, "value": bound, "unit": "mm/s"},
                                    "work_order_draft": {
                                        "machine_id": self.machine_id,
                                        "cause": f"Vibration zone {zone} (possible bearing or imbalance problem)",
                                        "evidence": f"Velocity RMS {vib:.2f} mm/s ≥ {name} {bound:g} mm/s "
                                                    f"(demo thresholds referencing ISO 20816-3)",
                                        "recommended_action": "Inspect spindle bearing, tool holder and balance; "
                                                              "check accelerometer mounting",
                                        "priority": "Critical" if lv == "CRITICAL" else "High"}}
            if lv == "CRITICAL" and derate_to is not None:
                sp_from = float(obs.get("rpm_setpoint") or obs.get("rpm_nominal") or 0.0)
                title += f"; auto-derated 20% (SIM only), setpoint {sp_from:.0f} → {derate_to:.0f} rpm"
                data.update({"derate_pct": 20.0, "rpm_setpoint_from": sp_from, "rpm_setpoint_to": derate_to,
                             "mode": "SIM_AUTO"})
            return self._alert(
                obs["ts"], "VIB", lv, title,
                measured=f"Velocity RMS {vib:.2f} mm/s, zone {zone} (boundary {name} {bound:g} mm/s); "
                         f"speed {float(obs.get('rpm') or 0):.0f} rpm",
                inferred="Vibration well above this machine's healthy level; consistent with a developing bearing "
                         "or imbalance problem (rule-based, not a diagnosis)",
                suggested=("Stop at the next safe point and inspect the spindle bearing; "
                           "on a LIVE machine any derate needs operator approval") if lv == "CRITICAL"
                          else "Plan an inspection of the spindle bearing; watch the trend",
                data=data)

        return self._rule("VIB", level, build)

    def step_cnc(
        self,
        obs: dict[str, Any],
    ) -> tuple[dict, list[dict], float | None]:
        """Step the CNC twin model forward given an observation frame.

        Returns:
            (twin_message, list_of_events, proposed_derate_pct_or_none)
        """
        events: list[dict] = []
        proposed_derate: float | None = None
        self.ticks += 1
        t_now = self.ticks * TICK_S

        power = float(obs.get("power_kw") or 0.0)
        air_temp = float(obs.get("air_temp_k") or 300.0)
        h_est = self.calibrator.h_cool

        # Physics prediction: process temperature
        pred_target = air_temp + 8.0 + 0.7 * power / h_est
        if self.pred_temp is None:
            self.pred_temp = float(obs.get("process_temp_k") or 311.5)
        self.pred_temp += (pred_target - self.pred_temp) * TICK_S / 20.0

        # Residual evaluation. The twin is not told about injected faults: calibration is frozen
        # because the measured vibration is abnormal (zone C/D) or jumped, not because of the fault flag.
        obs_temp = obs.get("process_temp_k")
        vib_rms = float(obs.get("vibration_rms_mm_s") or 0.0)
        dq_flag = str(obs.get("dq_flag") or "OK")
        vib_risk = vibration_risk(vib_rms, self.vib_zones)

        replay = obs.get("source_mode") == "REPLAY"
        if replay:
            # AI4I rows are independent samples, not a time series: no drift detection or calibration.
            e = (obs_temp - self.pred_temp) if obs_temp is not None else 0.0
            state, is_new_drift, is_new_anomaly = ResidualState.IN_BAND, False, False
            self.residual.frozen_reason = "replay"
        else:
            e, state, is_new_drift, is_new_anomaly = self.residual.update(
                obs_val=obs_temp,
                pred_val=self.pred_temp,
                vibration_rms=vib_rms,
                dq_flag=dq_flag,
                # a tripped machine stays frozen too: its spindle has stopped, so low vibration says nothing
            vibration_abnormal=vib_risk != "LOW" or obs.get("plc_state") == "TRIP",
                tick_s=TICK_S,
            )

        ts_str = obs["ts"]

        # Handle events from residual states
        if state == ResidualState.ANOMALY and is_new_anomaly:
            if self.residual.frozen_reason == "anomaly":
                events.append({
                    "type": "event",
                    "schema_version": SCHEMA_VERSION,
                    "ts": ts_str,
                    "event_id": f"EV_ANOMALY_{self.machine_id}",
                    "machine_id": self.machine_id,
                    "kind": "ANOMALY",
                    "severity": "INFO",
                    "title": "Calibration frozen: vibration abnormal",
                    "measured": f"Vibration {vib_rms} mm/s RMS (was ~{self.residual.vib_ewma:.1f})",
                    "inferred": "A mechanical change, not thermal drift: thermal calibration is paused so it is not "
                                "absorbed into the model",
                    "suggested": "See the vibration alert",
                    "data": {"freeze_reason": "anomaly"},
                })
            elif dq_flag != "OK" and self.residual.frozen_reason == "data_quality":
                events.append({
                    "type": "event",
                    "schema_version": SCHEMA_VERSION,
                    "ts": ts_str,
                    "event_id": f"EV_DQ_{self.machine_id}",
                    "machine_id": self.machine_id,
                    "kind": "ANOMALY",
                    "severity": "WARN",
                    "title": f"Temperature sensor {dq_flag.lower()}: calibration frozen",
                    "measured": f"dq_flag={dq_flag}",
                    "inferred": "Sensor data not trustworthy",
                    "suggested": "Check sensor wiring and mounting",
                    "data": {"freeze_reason": "data_quality"},
                })

        if state == ResidualState.DRIFT and is_new_drift:
            obs_disp = _c(obs_temp) if obs_temp is not None else "N/A"
            events.append({
                "type": "event",
                "schema_version": SCHEMA_VERSION,
                "ts": ts_str,
                "event_id": f"EV_DRIFT_{self.machine_id}",
                "machine_id": self.machine_id,
                "kind": "DRIFT",
                "severity": "WARN",
                "title": f"Model mismatch: sustained {self.residual.ewma:+.1f} °C on process temperature",
                "measured": f"Observed {obs_disp} vs predicted {_c(self.pred_temp)}",
                "inferred": "Machine behaviour has changed gradually; twin will recalibrate",
            })

        # Bounded parameter calibration if in DRIFT and not frozen
        if state == ResidualState.DRIFT:
            cal_rec = self.calibrator.step_thermal_calibration(
                power_kw=power,
                air_temp_k=air_temp,
                y_filt=self.residual.y_filt,
                dy_filt=self.residual.dy_filt,
                residual_ewma=self.residual.ewma,
                is_frozen=self.residual.frozen_reason is not None,
            )
            if cal_rec:
                self.cal_updates += 1
                h = self.calibrator.h_cool
                # Collapse small, frequent updates into one event (the twin message still carries every value).
                if (abs(h - self.cal_logged_h) >= self.limits["calibration_event_min_delta"]
                        and t_now - self.cal_logged_t >= self.limits["calibration_event_min_interval_s"]):
                    n = self.cal_updates
                    events.append({
                        "type": "event",
                        "schema_version": SCHEMA_VERSION,
                        "ts": ts_str,
                        "event_id": f"EV_CAL_{self.machine_id}",
                        "machine_id": self.machine_id,
                        "kind": "CALIBRATION",
                        "severity": "INFO",
                        "title": f"Recalibrated h_cool {self.cal_logged_h:.2f} → {h:.2f}" + (f" ({n} updates)" if n > 1 else ""),
                        "inferred": f"Cooling capacity estimated {100 * (1 - h):.0f}% below nominal",
                        "suggested": "Check coolant level, pump and filter",
                        "data": {
                            "param": cal_rec.param,
                            "from": round(self.cal_logged_h, 3),
                            "to": round(h, 3),
                            "version": cal_rec.version,
                            "residual_ewma": cal_rec.residual_ewma,
                            "updates": n,
                        },
                    })
                    self.cal_logged_h, self.cal_logged_t, self.cal_updates = h, t_now, 0

        # Forecast horizon
        tool_wear = float(obs.get("tool_wear_min") or 0.0)
        now_temp = obs_temp if obs_temp is not None else self.pred_temp
        temp_limit = self.limits["process_temp_limit_k"]
        minutes, warning = ForecastEngine.forecast_cnc(
            air_temp_k=air_temp,
            power_kw=power,
            h_est=self.calibrator.h_cool,
            current_temp_k=now_temp,
            tool_wear_min=tool_wear,
            temp_limit_k=temp_limit,
            tool_wear_forecast_min=self.limits["tool_wear_forecast_min"],
            tool_wear_limit_min=self.limits["tool_wear_limit_min"],
        )

        # Vibration rule (zone C/D). The hero machine auto-derates 20 % in SIM on zone D.
        current_derate = float(obs.get("derate_pct") or 0.0)
        derate_to = None
        if self.hero and not replay and current_derate == 0.0 and vib_rms >= (self.vib_zones or (0, float("inf")))[1]:
            derate_to = round(float(obs.get("rpm_nominal") or 0.0) * 0.8, 1)
        vib_event = self._vibration_alert(obs, vib_rms, derate_to)
        if vib_event:
            events.append(vib_event)
            if vib_event["data"].get("derate_pct"):
                proposed_derate = 20.0

        # Overload: spindle power above the configured share of rated power
        load_pct = 100.0 * power / self.rated_kw if self.rated_kw else None
        over = load_pct is not None and load_pct >= self.limits["overload_load_pct"]
        ev = self._rule("OVERLOAD", "WARN" if over else None, lambda lv: self._alert(
            ts_str, "OVERLOAD", lv,
            f"Spindle overload: load {load_pct:.0f}% of rated {self.rated_kw:g} kW (limit {self.limits['overload_load_pct']:.0f}%)",
            measured=f"Power {power:.2f} kW, torque {float(obs.get('torque_nm') or 0):.1f} N·m at {float(obs.get('rpm') or 0):.0f} rpm",
            inferred="Cutting load is above the machine's sustained rating (heavier cut or order surge)",
            suggested="Check feed rate, depth of cut and tool condition; spread the order load",
            data={"load_pct": round(load_pct, 1), "limit_pct": self.limits["overload_load_pct"],
                  "work_order_draft": {"machine_id": self.machine_id, "cause": "Spindle overload",
                                       "evidence": f"Load {load_pct:.0f}% of rated {self.rated_kw:g} kW",
                                       "recommended_action": "Review cutting parameters and tool; check spindle drive",
                                       "priority": "Medium"}}))
        if ev:
            events.append(ev)

        # Supervised self-healing in SIM mode
        if (self.hero and not replay and warning == "OVERTEMP" and minutes is not None
                and minutes < self.limits["overtemp_derate_horizon_min"] and current_derate == 0.0
                and proposed_derate is None):
            proposed_derate = 20.0
            # Derate acts on the commanded setpoint, so the alert quotes setpoints, not a noisy measured rpm.
            sp_from = float(obs.get("rpm_setpoint") or obs.get("rpm_nominal") or 0.0)
            sp_to = round(float(obs.get("rpm_nominal") or sp_from) * (1.0 - proposed_derate / 100.0), 1)
            evidence = (f"Process temp {_c(now_temp)} and rising; twin forecasts {_c(temp_limit)} limit "
                        f"in ~{minutes:.0f} min at current settings")
            events.append({
                "type": "event",
                "schema_version": SCHEMA_VERSION,
                "ts": ts_str,
                "event_id": f"EV_DERATE_{self.machine_id}",
                "machine_id": self.machine_id,
                "kind": "ALERT",
                "severity": "WARN",
                "title": f"Over-temperature forecast: auto-derated 20% (SIM only), setpoint {sp_from:.0f} → {sp_to:.0f} rpm",
                "measured": f"Process temp {_c(now_temp)}, rising; spindle setpoint {sp_from:.0f} rpm",
                "inferred": f"Calibrated twin forecasts {_c(temp_limit)} in ~{minutes:.0f} min at current settings",
                "suggested": "Check coolant flow and filter. On a LIVE machine this derate needs operator approval.",
                "data": {
                    "derate_pct": proposed_derate,
                    "rpm_setpoint_from": sp_from,
                    "rpm_setpoint_to": sp_to,
                    "mode": "SIM_AUTO",
                    "work_order_draft": {
                        "machine_id": self.machine_id,
                        "cause": "Over-temperature forecast (cooling degraded)",
                        "evidence": evidence,
                        "recommended_action": "Inspect coolant flow, level and filter; keep 20% derate until checked",
                        "priority": "High",
                    },
                },
            })

        # One alert when the calibrated cooling capacity falls past the configured limit.
        cooling_loss = 100.0 * (1.0 - self.calibrator.h_cool)
        if not self.cooling_alerted and cooling_loss >= self.limits["cooling_loss_alert_pct"]:
            self.cooling_alerted = True
            evidence = (f"Calibrated cooling coefficient h_cool {self.calibrator.h_cool:.2f} (nominal 1.00), "
                        f"{cooling_loss:.0f}% below nominal; limit {self.limits['cooling_loss_alert_pct']:.0f}%")
            events.append({
                "type": "event",
                "schema_version": SCHEMA_VERSION,
                "ts": ts_str,
                "event_id": f"EV_COOLING_{self.machine_id}",
                "machine_id": self.machine_id,
                "kind": "ALERT",
                "severity": "WARN",
                "title": f"Cooling capacity estimated {cooling_loss:.0f}% below nominal",
                "measured": f"{evidence}; process temp {_c(now_temp)}",
                "inferred": "Coolant flow or heat removal has likely degraded (estimated by the calibrated twin, not measured directly)",
                "suggested": "Inspect coolant level, pump and filter at the next safe stop",
                "data": {
                    "param": "h_cool",
                    "h_cool": round(self.calibrator.h_cool, 3),
                    "cooling_loss_pct": round(cooling_loss, 1),
                    "limit_pct": self.limits["cooling_loss_alert_pct"],
                    "work_order_draft": {
                        "machine_id": self.machine_id,
                        "cause": "Estimated cooling capacity loss",
                        "evidence": evidence,
                        "recommended_action": "Inspect coolant level, pump and filter",
                        "priority": "High",
                    },
                },
            })
        elif self.cooling_alerted and cooling_loss < self.limits["cooling_loss_rearm_pct"]:
            self.cooling_alerted = False

        # Risk assessment
        plc_state = str(obs.get("plc_state") or "RUN")
        over_temp = (now_temp or 0) > temp_limit
        if over_temp or vib_risk == "HIGH" or plc_state == "TRIP":
            rules_risk = "HIGH"
        elif warning or vib_risk == "MEDIUM" or over:
            rules_risk = "MEDIUM"
        else:
            rules_risk = "LOW"

        # This alert is about temperature, so only temperature raises it (vibration has its own path).
        if over_temp and not self.alert_announced and self.residual.frozen_reason != "anomaly":
            self.alert_announced = True
            events.append({
                "type": "event",
                "schema_version": SCHEMA_VERSION,
                "ts": ts_str,
                "event_id": f"EV_ALERTHIGH_{self.machine_id}",
                "machine_id": self.machine_id,
                "kind": "ALERT",
                "severity": "CRITICAL",
                "title": "Process temperature above limit",
                "measured": f"{_c(now_temp)} > {_c(temp_limit)} limit",
                "inferred": "Cooling insufficient for current load",
                "suggested": "Reduce load; inspect coolant system",
            })

        top_signals = []
        if warning == "OVERTEMP" or e > self.residual.band:
            top_signals.append("process_temp_k")
        if vib_risk != "LOW":
            top_signals.append("vibration_rms_mm_s")
        if over:
            top_signals.append("power_kw")
        if tool_wear > self.limits["tool_wear_warn_min"]:
            top_signals.append("tool_wear_min")

        model_p, ml_detail = self._failure_risk(obs, air_temp, tool_wear)
        rul = self._tool_rul(t_now, tool_wear)
        if warning == "TWF_RISK" and rul["status"] == "OK":
            minutes = rul["rul_minutes"]  # one tool-life number everywhere: the RUL estimate
        worn = tool_wear >= self.limits["tool_wear_forecast_min"]
        ev = self._rule("TOOL_WEAR", "WARN" if worn else None, lambda lv: self._alert(
            ts_str, "TOOL_WEAR", lv,
            f"Tool wear {tool_wear:.0f} min ≥ {self.limits['tool_wear_forecast_min']:.0f} min"
            + (f"; RUL ≈ {rul['rul_minutes']:.1f} min (estimate)" if rul["status"] == "OK" else ""),
            measured=f"Tool wear {tool_wear:.1f} min of {self.limits['tool_wear_limit_min']:.0f} min tool life"
                     + (f", wearing {rul['wear_rate_min_per_h']:.0f} min/h" if rul["status"] == "OK" else ""),
            inferred="Tool is near end of life; surface finish and tool-breakage risk rise from here",
            suggested="Plan a tool change at the next safe stop",
            data={"rul": rul, "work_order_draft": {
                "machine_id": self.machine_id, "cause": "Tool near end of life",
                "evidence": f"Tool wear {tool_wear:.0f} min; RUL {rul['rul_minutes']} min (estimate)",
                "recommended_action": "Change the tool and record the measured wear", "priority": "Medium"}}))
        if ev:
            events.append(ev)

        twin_msg = {
            "type": "twin",
            "schema_version": SCHEMA_VERSION,
            "ts": ts_str,
            "machine_id": self.machine_id,
            "predicted": {
                "process_temp_k": round(self.pred_temp, 2),
                "tool_wear_min": round(tool_wear, 1),
            },
            "residual": {
                "signal": self.residual.signal,
                "value": round(e, 3),
                "ewma": round(self.residual.ewma, 3),
                "cusum": round(self.residual.cusum, 3),
                "band": self.residual.band,
                "state": state.value,
            },
            "params": {
                "version": self.calibrator.version,
                "values": self.calibrator.values,
                "frozen": self.residual.frozen_reason is not None,
                "freeze_reason": self.residual.frozen_reason,
            },
            "forecast": {
                "minutes_to_warning": minutes,
                "warning": warning,
            },
            "risk": {
                "rules": rules_risk,
                "model": model_p,
                "top_signals": top_signals,
                "ml": ml_detail,
            },
            "rul": rul,
        }
        return twin_msg, events, proposed_derate

    def _failure_risk(self, obs: dict[str, Any], air_temp: float, tool_wear: float) -> tuple[float | None, dict]:
        """AI4I failure risk for machines configured with it; otherwise say why there is none."""
        if self.ml_cfg.get("failure_model") != "ai4i":
            self.ml_detail = None
            return None, {"status": "NOT_APPLICABLE",
                          "detail": f"No failure-risk model is configured for {self.template} "
                                    "(AI4I 2020 is a milling dataset)."}
        if obs.get("process_temp_k") is None or not isinstance(obs.get("rpm"), (int, float)):
            self.ml_detail = None
            return None, {"status": "NO_DATA", "detail": "Process temperature or speed not available this tick."}
        # Replayed rows are independent samples, so each one gets its own prediction.
        if self.ml_detail is None or self.ticks % ML_EVERY_TICKS == 1 or obs.get("source_mode") == "REPLAY":
            self.ml_detail = {
                **ai4i_predictor().predict(
                    air_temp_k=air_temp,
                    process_temp_k=float(obs["process_temp_k"]),
                    rpm=float(obs["rpm"]),
                    torque_nm=float(obs.get("torque_nm") or 0.0),
                    tool_wear_min=tool_wear,
                ),
                "evaluated_at": obs["ts"],
            }
        pct = self.ml_detail.get("failure_risk_pct")
        return (None if pct is None else round(pct / 100.0, 3)), self.ml_detail

    def _tool_rul(self, t_now: float, tool_wear: float) -> dict[str, Any]:
        """Tool-wear RUL: least-squares slope of wear over the last RUL_WINDOW_S, extrapolated to the limit."""
        h = self.wear_hist
        if h and tool_wear < h[-1][1] - 1.0:
            h.clear()  # tool changed: start a new trend
        h.append((t_now, tool_wear))
        limit = self.limits["tool_wear_limit_min"]
        out: dict[str, Any] = {
            "label": "estimate",
            "method": f"Linear least-squares fit of tool wear over the last {RUL_WINDOW_S:.0f} s, "
                      f"extrapolated to the {limit:.0f} min tool-life limit (config/machines.yaml)",
            "tool_wear_min": round(tool_wear, 1),
            "limit_min": limit,
            "remaining_tool_min": round(max(0.0, limit - tool_wear), 1),
            "wear_rate_min_per_h": None,
            "rul_minutes": None,
            "window_s": round(h[-1][0] - h[0][0], 1),
            "n_points": len(h),
            "note": "Wall-clock time at the current wear rate. In SIM, tool wear runs faster than real time.",
        }
        if len(h) < RUL_MIN_POINTS:
            return {**out, "status": "INSUFFICIENT_DATA"}
        n = len(h)
        mt = sum(t for t, _ in h) / n
        mw = sum(w for _, w in h) / n
        var = sum((t - mt) ** 2 for t, _ in h)
        slope = sum((t - mt) * (w - mw) for t, w in h) / var if var else 0.0  # wear-min per second
        if slope <= 1e-6:
            return {**out, "status": "NOT_WEARING", "wear_rate_min_per_h": 0.0}
        return {**out, "status": "OK", "wear_rate_min_per_h": round(slope * 3600.0, 1),
                "rul_minutes": round(out["remaining_tool_min"] / slope / 60.0, 1)}

    def step_simple(self, obs: dict[str, Any]) -> tuple[dict, list[dict]]:
        """Rule twin for the saw and the compressor: (twin message, alert events)."""
        ts_str = obs["ts"]
        events: list[dict] = []
        th = self.limits
        forecast: dict[str, Any] = {"minutes_to_warning": None, "warning": None}
        top: list[str] = []
        band = 0.5
        if self.template == "air_compressor":
            signal, predicted = "pressure_bar", 7.0
            value = float(obs.get("pressure_bar") or 7.0)
            low = value < th["pressure_min_bar"]
            rules = "HIGH" if low else ("MEDIUM" if value < th["pressure_warn_bar"] else "LOW")
            if low:
                forecast = {"minutes_to_warning": 0.0, "warning": "PRESSURE_LOW"}
            top = ["pressure_bar", "duty_pct"] if rules != "LOW" else []
        else:
            signal, predicted = "power_kw", 1.4 + 0.006 * 35.0
            value = float(obs.get("power_kw") or 1.4)
            blade_wear = float(obs.get("blade_wear_pct") or 0.0)
            rules = ("HIGH" if blade_wear > th["blade_wear_high_pct"]
                     else "MEDIUM" if blade_wear > th["blade_wear_medium_pct"] else "LOW")
            if rules != "LOW":
                forecast = {"minutes_to_warning": None, "warning": "BLADE_WEAR"}
                top = ["blade_wear_pct"]

        if obs.get("vibration_rms_mm_s") is not None:
            vib_risk = vibration_risk(float(obs["vibration_rms_mm_s"]), self.vib_zones)
            if RISK_ORDER.index(vib_risk) > RISK_ORDER.index(rules):
                rules = vib_risk
            if vib_risk != "LOW":
                top.append("vibration_rms_mm_s")

        e = value - predicted
        state = ResidualState.DRIFT if abs(e) > band else ResidualState.IN_BAND

        return {
            "type": "twin",
            "schema_version": SCHEMA_VERSION,
            "ts": ts_str,
            "machine_id": self.machine_id,
            "predicted": {signal: round(predicted, 3)},
            "residual": {
                "signal": signal,
                "value": round(e, 3),
                "ewma": round(e, 3),
                "cusum": 0.0,
                "band": round(band, 3),
                "state": state.value,
            },
            "params": {
                "version": "v1.0",
                "values": {},
                "frozen": False,
                "freeze_reason": None,
            },
            "forecast": forecast,
            "risk": {
                "rules": rules,
                "model": None,
                "top_signals": top,
            },
        }, events

    def reset(self) -> None:
        self.calibrator.reset()
        self.residual.reset()
        self.alert_announced = False
        self.ticks, self.cal_updates = 0, 0
        self.cal_logged_h, self.cal_logged_t = 1.0, float("-inf")
        self.cooling_alerted = False
        self.ml_detail = None
        self.wear_hist.clear()
        self.rule_state.clear()
        if self.template == "cnc_lathe":
            self.pred_temp = 310.8
        elif self.template == "cnc_mill":
            self.pred_temp = 311.5
        else:
            self.pred_temp = None
