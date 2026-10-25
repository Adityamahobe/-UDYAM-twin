"""Analytical forecasting for impending warnings and remaining life.

Projects current operating conditions forward in time using the calibrated
reduced-order model parameters.
"""

from __future__ import annotations

import math

from config import get_config

# Defaults for callers that don't pass a machine's own limits (config/machines.yaml).
_CNC = get_config().template("cnc_mill")["thresholds"]
TEMP_LIMIT_K = _CNC["process_temp_limit_k"]
TOOL_WEAR_FORECAST_MIN = _CNC["tool_wear_forecast_min"]
TOOL_WEAR_LIMIT_MIN = _CNC["tool_wear_limit_min"]
PRESSURE_MIN_BAR = get_config().template("air_compressor")["thresholds"]["pressure_min_bar"]
TICK_S = 0.5
# Minutes are real (wall-clock) minutes at the current rate, the same unit as the RUL estimate.


class ForecastEngine:
    """Computes time-to-warning and failure risk horizons."""

    @staticmethod
    def forecast_cnc(
        *,
        air_temp_k: float,
        power_kw: float,
        h_est: float,
        current_temp_k: float,
        tool_wear_min: float,
        temp_limit_k: float = TEMP_LIMIT_K,
        tool_wear_forecast_min: float = TOOL_WEAR_FORECAST_MIN,
        tool_wear_limit_min: float = TOOL_WEAR_LIMIT_MIN,
    ) -> tuple[float | None, str | None]:
        """Calculate minutes to temperature or tool-wear warning at current operating conditions."""
        steady_state_temp = air_temp_k + 8.0 + 0.7 * power_kw / h_est
        minutes: float | None = None
        warning: str | None = None

        if current_temp_k >= temp_limit_k:
            minutes, warning = 0.0, "OVERTEMP"
        elif steady_state_temp > temp_limit_k and (steady_state_temp - current_temp_k) > 0.01:
            ratio = (steady_state_temp - temp_limit_k) / (steady_state_temp - current_temp_k)
            if 0.0 < ratio < 1.0:
                secs = -20.0 * math.log(ratio)
                minutes = round(secs / 60.0, 1)
                warning = "OVERTEMP"
        elif tool_wear_min > tool_wear_forecast_min:
            remaining_min = max(0.0, tool_wear_limit_min - tool_wear_min)
            minutes = round(remaining_min / 0.05 * TICK_S / 60.0, 1)  # replaced by the RUL fit in the twin
            warning = "TWF_RISK"

        return minutes, warning

    @staticmethod
    def forecast_compressor(pressure_bar: float, pressure_min_bar: float = PRESSURE_MIN_BAR) -> tuple[float | None, str | None]:
        """Forecast low pressure condition."""
        if pressure_bar < pressure_min_bar:
            return 0.0, "PRESSURE_LOW"
        return None, None
