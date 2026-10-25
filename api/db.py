"""SQLite database storage for events, alerts, work orders, parameter versions, and economics.

Lightweight and offline, with zero external database dependencies. Set UDYAM_DB_PATH to use
another file (the test suite points it at a temporary database; see tests/conftest.py).
"""

import json
import os
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from config import get_config

DB_PATH = Path(os.environ.get("UDYAM_DB_PATH", Path(__file__).resolve().parent.parent / "udyam_twin.sqlite"))
_WRITE_LOCK = threading.Lock()

WO_STATUSES = ("Open", "Acknowledged", "In Progress", "Closed")
WO_PRIORITIES = ("Low", "Medium", "High", "Critical")
# Timestamp column stamped when a work order first reaches each status.
WO_STATUS_TS = {"Acknowledged": "acknowledged_at", "In Progress": "started_at", "Closed": "closed_at"}
WO_EXTRA_COLUMNS = {
    "cause": "TEXT",
    "evidence": "TEXT",
    "recommended_action": "TEXT",
    "alert_id": "TEXT",
    "updated_at": "TEXT",
    "acknowledged_at": "TEXT",
    "started_at": "TEXT",
    "closed_at": "TEXT",
}
ALERT_STATUSES = ("ACTIVE", "ACKNOWLEDGED", "RESOLVED")


class LifecycleError(ValueError):
    """An invalid work-order or alert state change."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


_LOCAL = threading.local()


def get_connection(db_path: Path = DB_PATH) -> sqlite3.Connection:
    """One reused connection per thread and database file (callers never close it)."""
    conns = getattr(_LOCAL, "conns", None)
    if conns is None:
        conns = _LOCAL.conns = {}
    key = str(db_path)
    conn = conns.get(key)
    if conn is None:
        conn = conns[key] = sqlite3.connect(key, check_same_thread=False)
        conn.row_factory = sqlite3.Row
    return conn


def init_db(db_path: Path = DB_PATH) -> None:
    """Initialize database tables and default configuration."""
    conn = get_connection(db_path)
    with conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id TEXT UNIQUE NOT NULL,
                ts TEXT NOT NULL,
                machine_id TEXT,
                kind TEXT NOT NULL,
                severity TEXT NOT NULL,
                title TEXT NOT NULL,
                measured TEXT,
                inferred TEXT,
                suggested TEXT,
                data_json TEXT
            );
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS work_orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                wo_id TEXT UNIQUE NOT NULL,
                created_at TEXT NOT NULL,
                machine_id TEXT NOT NULL,
                title TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'Open',
                priority TEXT NOT NULL DEFAULT 'Medium',
                assigned_to TEXT,
                operator_notes TEXT,
                tool_wear_measured REAL
            );
        """)
        # Migrate work_orders created before these columns existed.
        have = {r["name"] for r in conn.execute("PRAGMA table_info(work_orders);")}
        for col, typ in WO_EXTRA_COLUMNS.items():
            if col not in have:
                conn.execute(f"ALTER TABLE work_orders ADD COLUMN {col} {typ};")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS alerts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                alert_id TEXT UNIQUE NOT NULL,
                rule TEXT NOT NULL,
                machine_id TEXT,
                kind TEXT NOT NULL,
                severity TEXT NOT NULL,
                title TEXT NOT NULL,
                measured TEXT,
                inferred TEXT,
                suggested TEXT,
                data_json TEXT,
                status TEXT NOT NULL DEFAULT 'ACTIVE',
                source_event_id TEXT,
                raised_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                occurrences INTEGER NOT NULL DEFAULT 1,
                acknowledged_at TEXT,
                acknowledged_by TEXT,
                resolved_at TEXT,
                resolution TEXT,
                wo_id TEXT
            );
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS custom_machines (
                machine_id TEXT PRIMARY KEY,
                spec_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS parameter_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,
                machine_id TEXT NOT NULL,
                param TEXT NOT NULL,
                from_val REAL NOT NULL,
                to_val REAL NOT NULL,
                version TEXT NOT NULL,
                residual_ewma REAL NOT NULL
            );
        """)
        # One row per machine every 2 s (what the trend charts plot), kept for time-range exports.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS telemetry (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,
                machine_id TEXT NOT NULL,
                mode TEXT, state TEXT, rpm REAL, derate_pct REAL, vibration_rms_mm_s REAL, zone TEXT,
                temp_c REAL, power_kw REAL, load_pct REAL, energy_kwh REAL, tool_wear_min REAL,
                risk TEXT, failure_risk_pct REAL, active_scenario TEXT
            );
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS telemetry_ts ON telemetry (ts);")
        # Reinforcement-learning feedback loop: one row per evaluated 20 s decision (ml/maintenance_rl.py).
        conn.execute("""
            CREATE TABLE IF NOT EXISTS rl_decisions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts_start TEXT NOT NULL,
                ts_end TEXT NOT NULL,
                machine_id TEXT NOT NULL,
                row_json TEXT NOT NULL
            );
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS rl_decisions_ts ON rl_decisions (ts_start);")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS economics_config (
                key TEXT PRIMARY KEY,
                value REAL NOT NULL,
                description TEXT NOT NULL
            );
        """)
        # Seed default MSME assumptions (config/machines.yaml `site`) if not present;
        # values edited in the dashboard are kept in this table.
        site = get_config().site
        defaults = [
            ("inr_per_part", site["inr_per_part"], "Gross contribution per finished good part (INR)"),
            ("downtime_cost_per_hr", site["downtime_cost_inr_per_hr"], "Estimated cost per machine breakdown hour (INR)"),
            ("electricity_tariff_per_kwh", site["tariff_inr_per_kwh"], f"Electricity tariff (INR / kWh): {site.get('tariff_source', '')}"),
            ("cea_co2_factor", site["grid_co2_kg_per_kwh"], f"Grid CO2 factor (kg CO2 / kWh): {site.get('grid_co2_source', '')}"),
            ("sensor_node_cost_inr", site["sensor_node_cost_inr"], "Indicative BOM cost per clip-on sensor node (INR)"),
            ("operating_hours_per_month", site["operating_hours_per_month"], "Operating hours per month (assumption)"),
            ("baseline_downtime_h_per_month", site["baseline_unplanned_downtime_h_per_month"],
             "Unplanned downtime per month before the twin (assumption)"),
            ("assumed_downtime_reduction_pct", site["assumed_downtime_reduction_pct"],
             "Assumed downtime reduction (illustration, not a measured result)"),
            ("sensor_nodes", site["sensor_nodes"], "Sensor nodes installed (assumption)"),
        ]
        for key, val, desc in defaults:
            conn.execute(
                "INSERT OR IGNORE INTO economics_config (key, value, description) VALUES (?, ?, ?);",
                (key, val, desc),
            )


def log_event(event: dict[str, Any], db_path: Path = DB_PATH) -> None:
    """Log an emitted telemetry event into the database."""
    conn = get_connection(db_path)
    data_json = json.dumps(event.get("data", {})) if event.get("data") else None
    with conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO events
            (event_id, ts, machine_id, kind, severity, title, measured, inferred, suggested, data_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
            """,
            (
                event["event_id"],
                event["ts"],
                event.get("machine_id"),
                event["kind"],
                event["severity"],
                event["title"],
                event.get("measured"),
                event.get("inferred"),
                event.get("suggested"),
                data_json,
            ),
        )


def get_events(limit: int = 50, db_path: Path = DB_PATH) -> list[dict[str, Any]]:
    """Retrieve recent events sorted newest first."""
    conn = get_connection(db_path)
    cur = conn.execute("SELECT * FROM events ORDER BY id DESC LIMIT ?;", (limit,))
    rows = cur.fetchall()
    out = []
    for r in rows:
        d = dict(r)
        if d.get("data_json"):
            d["data"] = json.loads(d["data_json"])
        else:
            d["data"] = {}
        out.append(d)
    return out


def _wo_row(conn: sqlite3.Connection, wo_id: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM work_orders WHERE wo_id = ?;", (wo_id,)).fetchone()
    return dict(row) if row else None


def create_work_order(
    machine_id: str,
    title: str,
    priority: str = "Medium",
    notes: str = "",
    db_path: Path = DB_PATH,
    *,
    cause: str | None = None,
    evidence: str | None = None,
    recommended_action: str | None = None,
    alert_id: str | None = None,
) -> dict[str, Any]:
    """Create a work order in status Open."""
    if priority not in WO_PRIORITIES:
        raise LifecycleError(f"priority must be one of {WO_PRIORITIES}")
    ts = _now()
    with _WRITE_LOCK:
        conn = get_connection(db_path)
        with conn:
            nxt = conn.execute("SELECT COALESCE(MAX(id), 0) + 1 FROM work_orders;").fetchone()[0]
            wo_id = f"WO-{nxt:04d}"
            conn.execute(
                """
                INSERT INTO work_orders
                (wo_id, created_at, updated_at, machine_id, title, status, priority, operator_notes,
                 cause, evidence, recommended_action, alert_id)
                VALUES (?, ?, ?, ?, ?, 'Open', ?, ?, ?, ?, ?, ?);
                """,
                (wo_id, ts, ts, machine_id, title, priority, notes, cause, evidence, recommended_action, alert_id),
            )
        return _wo_row(conn, wo_id)


def get_work_order(wo_id: str, db_path: Path = DB_PATH) -> dict[str, Any] | None:
    return _wo_row(get_connection(db_path), wo_id)


def update_work_order_status(
    wo_id: str,
    status: str,
    notes: str | None = None,
    tool_wear_measured: float | None = None,
    db_path: Path = DB_PATH,
) -> dict[str, Any] | None:
    """Move a work order along Open → Acknowledged → In Progress → Closed.

    Steps may be skipped forward (Open → In Progress also stamps acknowledged_at), never backward;
    Closed is final. Re-sending the current status only updates notes. Returns None if not found.
    """
    if status not in WO_STATUSES:
        raise LifecycleError(f"status must be one of {WO_STATUSES}")
    with _WRITE_LOCK:
        conn = get_connection(db_path)
        wo = _wo_row(conn, wo_id)
        if wo is None:
            return None
        cur = wo["status"] if wo["status"] in WO_STATUSES else "Open"
        if WO_STATUSES.index(status) < WO_STATUSES.index(cur) or (cur == "Closed" and status != "Closed"):
            raise LifecycleError(f"{wo_id} is {cur}; cannot move back to {status}")
        ts = _now()
        sets = {"status": status, "updated_at": ts}
        for st in WO_STATUSES[WO_STATUSES.index(cur) + 1: WO_STATUSES.index(status) + 1]:
            col = WO_STATUS_TS.get(st)
            if col and not wo.get(col):
                sets[col] = ts
        if notes is not None:
            sets["operator_notes"] = notes
        if tool_wear_measured is not None:
            sets["tool_wear_measured"] = tool_wear_measured
        with conn:
            conn.execute(
                f"UPDATE work_orders SET {', '.join(f'{k} = ?' for k in sets)} WHERE wo_id = ?;",
                (*sets.values(), wo_id),
            )
        return _wo_row(conn, wo_id)


def list_work_orders(status: str | None = None, db_path: Path = DB_PATH) -> list[dict[str, Any]]:
    conn = get_connection(db_path)
    if status:
        cur = conn.execute("SELECT * FROM work_orders WHERE status = ? ORDER BY id DESC;", (status,))
    else:
        cur = conn.execute("SELECT * FROM work_orders ORDER BY id DESC;")
    return [dict(r) for r in cur.fetchall()]


# ----------------------------------------------------------------------------- alerts
def _alert_out(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    d = dict(row)
    d["data"] = json.loads(d.pop("data_json") or "{}")
    return d


def get_alert(alert_id: str, db_path: Path = DB_PATH) -> dict[str, Any] | None:
    conn = get_connection(db_path)
    return _alert_out(conn.execute("SELECT * FROM alerts WHERE alert_id = ?;", (alert_id,)).fetchone())


def list_alerts(include_resolved: bool = False, limit: int = 200, db_path: Path = DB_PATH) -> list[dict[str, Any]]:
    conn = get_connection(db_path)
    where = "" if include_resolved else "WHERE status != 'RESOLVED'"
    rows = conn.execute(f"SELECT * FROM alerts {where} ORDER BY id DESC LIMIT ?;", (limit,)).fetchall()
    return [_alert_out(r) for r in rows]


SEVERITY_RANK = {"INFO": 0, "WARN": 1, "CRITICAL": 2}


def upsert_alert(rule: str, event: dict[str, Any], db_path: Path = DB_PATH) -> tuple[dict[str, Any], bool]:
    """Open an alert for `rule`, or fold a repeat into the open one. Returns (alert, created)."""
    ts = event.get("ts") or _now()
    with _WRITE_LOCK:
        conn = get_connection(db_path)
        row = conn.execute(
            "SELECT * FROM alerts WHERE rule = ? AND COALESCE(machine_id, '') = COALESCE(?, '') "
            "AND status != 'RESOLVED' ORDER BY id DESC LIMIT 1;",
            (rule, event.get("machine_id")),
        ).fetchone()
        with conn:
            if row is not None:
                if SEVERITY_RANK[event["severity"]] > SEVERITY_RANK[row["severity"]]:
                    # Escalation (e.g. vibration zone C → D): the card shows the newer, worse facts.
                    conn.execute(
                        "UPDATE alerts SET last_seen_at = ?, occurrences = occurrences + 1, severity = ?, title = ?, "
                        "measured = ?, inferred = ?, suggested = ?, data_json = ? WHERE id = ?;",
                        (ts, event["severity"], event["title"], event.get("measured"), event.get("inferred"),
                         event.get("suggested"), json.dumps(event.get("data") or {}), row["id"]),
                    )
                else:
                    conn.execute("UPDATE alerts SET last_seen_at = ?, occurrences = occurrences + 1 WHERE id = ?;",
                                 (ts, row["id"]))
                return _alert_out(conn.execute("SELECT * FROM alerts WHERE id = ?;", (row["id"],)).fetchone()), False
            nxt = conn.execute("SELECT COALESCE(MAX(id), 0) + 1 FROM alerts;").fetchone()[0]
            alert_id = f"AL-{nxt:04d}"
            conn.execute(
                """
                INSERT INTO alerts
                (alert_id, rule, machine_id, kind, severity, title, measured, inferred, suggested, data_json,
                 status, source_event_id, raised_at, last_seen_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'ACTIVE', ?, ?, ?);
                """,
                (alert_id, rule, event.get("machine_id"), event["kind"], event["severity"], event["title"],
                 event.get("measured"), event.get("inferred"), event.get("suggested"),
                 json.dumps(event.get("data") or {}), event.get("event_id"), ts, ts),
            )
        return get_alert(alert_id, db_path), True


def acknowledge_alert(alert_id: str, by: str | None = None, db_path: Path = DB_PATH) -> dict[str, Any] | None:
    """ACTIVE → ACKNOWLEDGED. Acknowledging twice is a no-op; a resolved alert can't be acknowledged."""
    with _WRITE_LOCK:
        a = get_alert(alert_id, db_path)
        if a is None:
            return None
        if a["status"] == "RESOLVED":
            raise LifecycleError(f"{alert_id} is already resolved")
        if a["status"] == "ACTIVE":
            conn = get_connection(db_path)
            with conn:
                conn.execute(
                    "UPDATE alerts SET status = 'ACKNOWLEDGED', acknowledged_at = ?, acknowledged_by = ? WHERE alert_id = ?;",
                    (_now(), by or "operator", alert_id),
                )
        return get_alert(alert_id, db_path)


def resolve_alerts(
    resolution: str,
    *,
    alert_id: str | None = None,
    wo_id: str | None = None,
    machine_id: str | None = None,
    db_path: Path = DB_PATH,
) -> list[dict[str, Any]]:
    """Resolve one alert, the alerts linked to a work order, a machine's alerts, or (no filter) every open alert."""
    with _WRITE_LOCK:
        conn = get_connection(db_path)
        where, args = "status != 'RESOLVED'", []
        if alert_id:
            where, args = where + " AND alert_id = ?", [alert_id]
        elif wo_id:
            where, args = where + " AND wo_id = ?", [wo_id]
        elif machine_id:
            where, args = where + " AND machine_id = ?", [machine_id]
        ids = [r["alert_id"] for r in conn.execute(f"SELECT alert_id FROM alerts WHERE {where};", args)]
        if ids:
            with conn:
                conn.execute(
                    f"UPDATE alerts SET status = 'RESOLVED', resolved_at = ?, resolution = ? "
                    f"WHERE alert_id IN ({','.join('?' * len(ids))});",
                    (_now(), resolution, *ids),
                )
    return [get_alert(i, db_path) for i in ids]


def link_alert_work_order(alert_id: str, wo_id: str, db_path: Path = DB_PATH) -> None:
    with _WRITE_LOCK:
        conn = get_connection(db_path)
        with conn:
            conn.execute("UPDATE alerts SET wo_id = ? WHERE alert_id = ?;", (wo_id, alert_id))


def log_parameter_change(
    machine_id: str,
    param: str,
    from_val: float,
    to_val: float,
    version: str,
    residual_ewma: float,
    ts: str | None = None,
    db_path: Path = DB_PATH,
) -> None:
    conn = get_connection(db_path)
    if not ts:
        ts = datetime.now(timezone.utc).isoformat()
    with conn:
        conn.execute(
            """
            INSERT INTO parameter_history
            (ts, machine_id, param, from_val, to_val, version, residual_ewma)
            VALUES (?, ?, ?, ?, ?, ?, ?);
            """,
            (ts, machine_id, param, from_val, to_val, version, residual_ewma),
        )


def get_parameter_history(machine_id: str | None = None, db_path: Path = DB_PATH) -> list[dict[str, Any]]:
    conn = get_connection(db_path)
    if machine_id:
        cur = conn.execute("SELECT * FROM parameter_history WHERE machine_id = ? ORDER BY id DESC;", (machine_id,))
    else:
        cur = conn.execute("SELECT * FROM parameter_history ORDER BY id DESC;")
    return [dict(r) for r in cur.fetchall()]


def get_economics(db_path: Path = DB_PATH) -> dict[str, float]:
    conn = get_connection(db_path)
    cur = conn.execute("SELECT key, value FROM economics_config;")
    return {r["key"]: r["value"] for r in cur.fetchall()}


def close_linked_work_orders(alerts: list[dict[str, Any]], note: str, db_path: Path = DB_PATH) -> list[dict[str, Any]]:
    """Close the still-open work orders generated from these alerts (scenario clean-up)."""
    closed = []
    for a in alerts:
        wo = get_work_order(a["wo_id"], db_path) if a.get("wo_id") else None
        if wo and wo["status"] != "Closed":
            closed.append(update_work_order_status(wo["wo_id"], "Closed", notes=note, db_path=db_path))
    return closed


def save_custom_machine(machine_id: str, spec: dict[str, Any], db_path: Path = DB_PATH) -> None:
    with _WRITE_LOCK:
        conn = get_connection(db_path)
        with conn:
            conn.execute("INSERT OR REPLACE INTO custom_machines (machine_id, spec_json, created_at) VALUES (?, ?, ?);",
                         (machine_id, json.dumps(spec), _now()))


def delete_custom_machine(machine_id: str, db_path: Path = DB_PATH) -> None:
    with _WRITE_LOCK:
        conn = get_connection(db_path)
        with conn:
            conn.execute("DELETE FROM custom_machines WHERE machine_id = ?;", (machine_id,))


def list_custom_machines(db_path: Path = DB_PATH) -> list[dict[str, Any]]:
    rows = get_connection(db_path).execute("SELECT spec_json FROM custom_machines ORDER BY created_at, machine_id;")
    return [json.loads(r["spec_json"]) for r in rows.fetchall()]


TELEMETRY_COLUMNS = ("ts", "machine_id", "mode", "state", "rpm", "derate_pct", "vibration_rms_mm_s", "zone", "temp_c",
                     "power_kw", "load_pct", "energy_kwh", "tool_wear_min", "risk", "failure_risk_pct", "active_scenario")


def log_telemetry(rows: list[dict[str, Any]], db_path: Path = DB_PATH) -> None:
    if not rows:
        return
    cols = ", ".join(TELEMETRY_COLUMNS)
    marks = ", ".join("?" for _ in TELEMETRY_COLUMNS)
    with _WRITE_LOCK:
        conn = get_connection(db_path)
        with conn:
            conn.executemany(f"INSERT INTO telemetry ({cols}) VALUES ({marks});",
                             [tuple(r.get(c) for c in TELEMETRY_COLUMNS) for r in rows])


def prune_telemetry(before_ts: str, db_path: Path = DB_PATH) -> int:
    with _WRITE_LOCK:
        conn = get_connection(db_path)
        with conn:
            return conn.execute("DELETE FROM telemetry WHERE ts < ?;", (before_ts,)).rowcount


def query_telemetry(start: str, end: str, machine_id: str | None = None, limit: int = 500_000,
                    db_path: Path = DB_PATH) -> list[dict[str, Any]]:
    """Telemetry rows with start <= ts <= end (UTC ISO strings in the stored format), oldest first."""
    sql = f"SELECT {', '.join(TELEMETRY_COLUMNS)} FROM telemetry WHERE ts >= ? AND ts <= ?"
    args: list[Any] = [start, end]
    if machine_id:
        sql += " AND machine_id = ?"
        args.append(machine_id)
    rows = get_connection(db_path).execute(sql + " ORDER BY ts, machine_id LIMIT ?;", (*args, limit)).fetchall()
    return [dict(r) for r in rows]


def log_rl_decision(row: dict[str, Any], db_path: Path = DB_PATH) -> None:
    with _WRITE_LOCK:
        conn = get_connection(db_path)
        with conn:
            conn.execute("INSERT INTO rl_decisions (ts_start, ts_end, machine_id, row_json) VALUES (?, ?, ?, ?);",
                         (row["ts_start"], row["ts_end"], row["machine_id"], json.dumps(row)))


def query_rl_decisions(start: str, end: str, limit: int = 100_000, db_path: Path = DB_PATH) -> list[dict[str, Any]]:
    rows = get_connection(db_path).execute(
        "SELECT row_json FROM rl_decisions WHERE ts_start >= ? AND ts_start <= ? ORDER BY ts_start, id LIMIT ?;",
        (start, end, limit)).fetchall()
    return [json.loads(r["row_json"]) for r in rows]


def query_events(start: str, end: str, limit: int = 100_000, db_path: Path = DB_PATH) -> list[dict[str, Any]]:
    rows = get_connection(db_path).execute(
        "SELECT event_id, ts, machine_id, kind, severity, title, measured, inferred, suggested FROM events "
        "WHERE ts >= ? AND ts <= ? ORDER BY ts, id LIMIT ?;", (start, end, limit)).fetchall()
    return [dict(r) for r in rows]


def update_economics(updates: dict[str, float], db_path: Path = DB_PATH) -> dict[str, float]:
    conn = get_connection(db_path)
    with conn:
        for k, v in updates.items():
            conn.execute("UPDATE economics_config SET value = ? WHERE key = ?;", (float(v), k))
    return get_economics(db_path)
