"""Server-side alert store: turns alert-worthy events into persistent, acknowledgeable alerts.

Every WARN/CRITICAL event of an alerting kind opens one alert per (rule, machine). Repeats
of the same rule while the alert is open are folded into it (occurrences, last_seen_at), so
the UI never shows duplicates. Alerts resolve on scenario reset, server restart, explicit
resolve, a simulated resolution of their machine, or when their linked work order is closed.
Reset and resolution also close the work orders generated from the alerts they resolve.

Each alert keeps the card format: what happened (title) · fact (measured) · inference ·
severity · suggested action · acknowledge state.
"""

from __future__ import annotations

import re
from typing import Any

from api import db

ALERT_KINDS = {"ALERT", "ANOMALY", "COMMON_CAUSE"}
ALERTING_SEVERITIES = {"WARN", "CRITICAL"}
_RULE_RE = re.compile(r"^E\d+-(.+)$")


def rule_of(event: dict[str, Any]) -> str:
    """Stable rule key: the twin's rule id (e.g. EV_COOLING_VMC-01) or kind + machine."""
    m = _RULE_RE.match(event.get("event_id") or "")
    return m.group(1) if m else f"{event['kind']}_{event.get('machine_id') or 'FACTORY'}"


def message(action: str, alert: dict[str, Any]) -> dict[str, Any]:
    """WebSocket `alert` frame (schema v1.1)."""
    return {"type": "alert", "schema_version": "1.1", "ts": db._now(), "action": action, "alert": alert}


def work_order_message(action: str, wo: dict[str, Any]) -> dict[str, Any]:
    """WebSocket `work_order` frame (schema v1.1)."""
    return {"type": "work_order", "schema_version": "1.1", "ts": db._now(), "action": action, "work_order": wo}


def work_order_frames(action: str, wo: dict[str, Any]) -> list[dict[str, Any]]:
    """`work_order` frame plus the v1.0 WORK_ORDER event the current dashboard refreshes on."""
    return [work_order_message(action, wo), {
        "type": "event",
        "schema_version": "1.0",
        "ts": wo.get("updated_at") or wo["created_at"],
        "event_id": f"EV_{wo['wo_id']}_{action}_{wo['status'].replace(' ', '_')}",
        "machine_id": wo["machine_id"],
        "kind": "WORK_ORDER",
        "severity": "INFO",
        "title": f"Work order {wo['wo_id']} {action}: {wo['title']} ({wo['status']})",
        "data": wo,
    }]


def ingest(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Update the store from engine events; returns `alert` frames to broadcast."""
    out: list[dict[str, Any]] = []
    for ev in events:
        if ev.get("type") != "event":
            continue
        if ev.get("kind") in ("RESET", "RESOLUTION"):
            # Reset clears everything; a (simulated) resolution clears that machine's alerts. Work orders
            # generated from those alerts are closed with them, so no stale scenario state is left.
            reset = ev["kind"] == "RESET"
            why = "Scenario reset" if reset else "Simulated resolution"
            resolved = db.resolve_alerts(why, machine_id=None if reset else ev.get("machine_id"))
            out += [message("resolved", a) for a in resolved]
            for wo in db.close_linked_work_orders(resolved, f"Closed: {why.lower()}"):
                out += work_order_frames("updated", wo)
        elif ev.get("kind") in ALERT_KINDS and ev.get("severity") in ALERTING_SEVERITIES:
            alert, created = db.upsert_alert(rule_of(ev), ev)
            out.append(message("raised" if created else "updated", alert))
    return out


def work_order_draft(alert: dict[str, Any]) -> dict[str, Any]:
    """Work-order fields for an alert: the twin's draft when it gave one, else built from the card."""
    draft = (alert.get("data") or {}).get("work_order_draft")
    draft = draft if isinstance(draft, dict) else {}
    return {
        "machine_id": draft.get("machine_id") or alert.get("machine_id") or "FACTORY",
        "cause": draft.get("cause") or alert["title"],
        "evidence": draft.get("evidence") or alert.get("measured") or "",
        "recommended_action": draft.get("recommended_action") or alert.get("suggested") or "Inspect the machine",
        "priority": draft.get("priority") or ("High" if alert.get("severity") == "CRITICAL" else "Medium"),
    }
