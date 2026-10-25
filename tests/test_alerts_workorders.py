"""Step 3: server-side alert store and work-order lifecycle (always on a temporary database)."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path

import jsonschema
import pytest
from fastapi.testclient import TestClient

from api import alerts as alert_store
from api import db
from api.db import LifecycleError

SCHEMA = json.loads((Path(__file__).resolve().parent.parent / "docs" / "schema.json").read_text(encoding="utf-8"))


def test_suite_uses_temporary_database():
    assert "udyam-test-" in str(db.DB_PATH)
    assert db.DB_PATH.name != "udyam_twin.sqlite"


@pytest.fixture
def tmpdb(tmp_path):
    path = tmp_path / "t.sqlite"
    db.init_db(path)
    return path


def ev(rule: str, severity: str = "WARN", kind: str = "ALERT", machine: str | None = "VMC-01", **extra) -> dict:
    return {
        "type": "event", "schema_version": "1.0", "ts": "2026-10-25T10:00:00.000Z",
        "event_id": f"E99001-{rule}", "machine_id": machine, "kind": kind, "severity": severity,
        "title": f"{rule} happened", "measured": "Fact: 3.1 mm/s ≥ B/C 2.8 mm/s",
        "inferred": "Inference", "suggested": "Inspect the bearing", **extra,
    }


# ----------------------------------------------------------------- work orders (DB)
def test_work_order_fields_and_ids(tmpdb):
    a = db.create_work_order("VMC-01", "Coolant check", "High", db_path=tmpdb, cause="Cooling loss",
                             evidence="h_cool 0.68", recommended_action="Inspect pump", alert_id="AL-0001")
    b = db.create_work_order("LATHE-01", "Other", db_path=tmpdb)
    assert (a["wo_id"], b["wo_id"]) == ("WO-0001", "WO-0002")
    assert a["status"] == "Open" and a["cause"] == "Cooling loss" and a["evidence"] == "h_cool 0.68"
    assert a["recommended_action"] == "Inspect pump" and a["priority"] == "High" and a["alert_id"] == "AL-0001"
    assert a["created_at"].endswith("Z")
    with pytest.raises(LifecycleError):
        db.create_work_order("VMC-01", "x", "Urgent", db_path=tmpdb)


def test_work_order_lifecycle_forward_only(tmpdb):
    wo = db.create_work_order("VMC-01", "t", db_path=tmpdb)["wo_id"]
    a = db.update_work_order_status(wo, "Acknowledged", db_path=tmpdb)
    assert a["status"] == "Acknowledged" and a["acknowledged_at"] and not a["started_at"]
    b = db.update_work_order_status(wo, "In Progress", notes="tech on site", db_path=tmpdb)
    assert b["started_at"] and b["operator_notes"] == "tech on site"
    with pytest.raises(LifecycleError, match="cannot move back"):
        db.update_work_order_status(wo, "Acknowledged", db_path=tmpdb)
    c = db.update_work_order_status(wo, "Closed", db_path=tmpdb)
    assert c["closed_at"] and c["acknowledged_at"] == a["acknowledged_at"]
    with pytest.raises(LifecycleError):
        db.update_work_order_status(wo, "In Progress", db_path=tmpdb)
    with pytest.raises(LifecycleError, match="status must be"):
        db.update_work_order_status(wo, "Done", db_path=tmpdb)
    assert db.update_work_order_status("WO-9999", "Closed", db_path=tmpdb) is None


def test_skipping_a_step_stamps_the_skipped_timestamps(tmpdb):
    wo = db.create_work_order("VMC-01", "t", db_path=tmpdb)["wo_id"]
    r = db.update_work_order_status(wo, "In Progress", db_path=tmpdb)  # the current dashboard's Acknowledge button
    assert r["acknowledged_at"] and r["started_at"]


def test_legacy_work_orders_table_is_migrated(tmp_path):
    path = tmp_path / "old.sqlite"
    conn = sqlite3.connect(path)
    conn.execute("""CREATE TABLE work_orders (id INTEGER PRIMARY KEY AUTOINCREMENT, wo_id TEXT UNIQUE NOT NULL,
                    created_at TEXT NOT NULL, machine_id TEXT NOT NULL, title TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'Open', priority TEXT NOT NULL DEFAULT 'Medium',
                    assigned_to TEXT, operator_notes TEXT, tool_wear_measured REAL);""")
    conn.execute("INSERT INTO work_orders (wo_id, created_at, machine_id, title, status) VALUES "
                 "('WO-0001', '2026-10-24', 'VMC-01', 'old', 'In Progress');")
    conn.commit()
    conn.close()
    db.init_db(path)
    old = db.get_work_order("WO-0001", db_path=path)
    assert old["title"] == "old" and "cause" in old and old["cause"] is None
    assert db.update_work_order_status("WO-0001", "Closed", db_path=path)["closed_at"]
    assert db.create_work_order("VMC-01", "new", db_path=path)["wo_id"] == "WO-0002"


# ----------------------------------------------------------------- alerts (DB)
def test_alert_dedupes_per_rule_and_machine(tmpdb):
    a, created = db.upsert_alert("EV_COOLING_VMC-01", ev("EV_COOLING_VMC-01"), db_path=tmpdb)
    assert created and a["alert_id"] == "AL-0001" and a["status"] == "ACTIVE" and a["occurrences"] == 1
    b, created = db.upsert_alert("EV_COOLING_VMC-01", ev("EV_COOLING_VMC-01", severity="CRITICAL"), db_path=tmpdb)
    assert not created and b["alert_id"] == "AL-0001" and b["occurrences"] == 2 and b["severity"] == "CRITICAL"
    c, created = db.upsert_alert("EV_COOLING_VMC-01", ev("EV_COOLING_VMC-01", machine="LATHE-01"), db_path=tmpdb)
    assert created and c["alert_id"] == "AL-0002"
    db.resolve_alerts("done", alert_id="AL-0001", db_path=tmpdb)
    d, created = db.upsert_alert("EV_COOLING_VMC-01", ev("EV_COOLING_VMC-01"), db_path=tmpdb)
    assert created and d["alert_id"] == "AL-0003"


def test_alert_keeps_card_format(tmpdb):
    a, _ = db.upsert_alert("R", ev("R", data={"work_order_draft": {"cause": "c"}}), db_path=tmpdb)
    for key in ("title", "measured", "inferred", "severity", "suggested", "status"):
        assert a[key]
    assert a["data"]["work_order_draft"]["cause"] == "c"


def test_acknowledge_and_resolve(tmpdb):
    a, _ = db.upsert_alert("R", ev("R"), db_path=tmpdb)
    ack = db.acknowledge_alert(a["alert_id"], by="Asha", db_path=tmpdb)
    assert ack["status"] == "ACKNOWLEDGED" and ack["acknowledged_by"] == "Asha" and ack["acknowledged_at"]
    again = db.acknowledge_alert(a["alert_id"], db_path=tmpdb)
    assert again["acknowledged_at"] == ack["acknowledged_at"]
    db.link_alert_work_order(a["alert_id"], "WO-0007", db_path=tmpdb)
    [res] = db.resolve_alerts("WO closed", wo_id="WO-0007", db_path=tmpdb)
    assert res["status"] == "RESOLVED" and res["resolution"] == "WO closed"
    with pytest.raises(LifecycleError):
        db.acknowledge_alert(a["alert_id"], db_path=tmpdb)
    assert db.acknowledge_alert("AL-9999", db_path=tmpdb) is None


def test_rule_of_and_draft():
    assert alert_store.rule_of({"event_id": "E00042-EV_COOLING_VMC-01", "kind": "ALERT"}) == "EV_COOLING_VMC-01"
    assert alert_store.rule_of({"event_id": "E00007", "kind": "COMMON_CAUSE", "machine_id": "COMP-01"}) == "COMMON_CAUSE_COMP-01"
    d = alert_store.work_order_draft({"title": "T", "machine_id": "VMC-01", "measured": "m", "suggested": "s",
                                      "severity": "CRITICAL", "data": {}})
    assert d == {"machine_id": "VMC-01", "cause": "T", "evidence": "m", "recommended_action": "s", "priority": "High"}


# ----------------------------------------------------------------- API end to end
@pytest.fixture
def client():
    from api.app import app
    with TestClient(app) as c:
        yield c


def broadcast(events):
    from api.app import broadcast_messages
    asyncio.run(broadcast_messages(events))


def test_api_work_order_lifecycle_persists(client):
    wo = client.post("/api/work-orders", json={
        "machine_id": "VMC-01", "cause": "Spindle bearing wear suspected", "evidence": "Zone D, 6.7 mm/s",
        "recommended_action": "Inspect spindle bearing", "priority": "Critical",
    }).json()
    assert wo["title"] == "Spindle bearing wear suspected" and wo["status"] == "Open"
    for status in ("Acknowledged", "In Progress", "Closed"):
        r = client.patch(f"/api/work-orders/{wo['wo_id']}", json={"status": status})
        assert r.status_code == 200 and r.json()["status"] == status
    assert client.patch(f"/api/work-orders/{wo['wo_id']}", json={"status": "Open"}).status_code == 409
    assert client.patch("/api/work-orders/WO-9999", json={"status": "Closed"}).status_code == 404
    # "Refresh": state comes back from SQLite.
    again = client.get(f"/api/work-orders/{wo['wo_id']}").json()
    assert again["status"] == "Closed" and again["closed_at"] and again["evidence"] == "Zone D, 6.7 mm/s"
    assert client.post("/api/work-orders", json={"machine_id": "VMC-01"}).status_code == 422


def test_api_alert_ack_generate_wo_and_close(client):
    broadcast([ev("EV_TEST_FLOW", data={"work_order_draft": {
        "machine_id": "VMC-01", "cause": "Estimated cooling capacity loss", "evidence": "h_cool 0.68, 32% below",
        "recommended_action": "Inspect coolant level, pump and filter", "priority": "High"}})])
    broadcast([ev("EV_TEST_FLOW")])  # repeat folds into the same alert
    [a] = [x for x in client.get("/api/alerts").json() if x["rule"] == "EV_TEST_FLOW"]
    assert a["status"] == "ACTIVE" and a["occurrences"] == 2

    ack = client.post(f"/api/alerts/{a['alert_id']}/acknowledge", json={"by": "operator-1"}).json()
    assert ack["status"] == "ACKNOWLEDGED"

    wo = client.post(f"/api/alerts/{a['alert_id']}/work-order").json()
    assert wo["existing"] is False and wo["alert_id"] == a["alert_id"]
    assert (wo["machine_id"], wo["cause"], wo["priority"]) == ("VMC-01", "Estimated cooling capacity loss", "High")
    assert wo["evidence"] == "h_cool 0.68, 32% below" and wo["recommended_action"].startswith("Inspect")
    dup = client.post(f"/api/alerts/{a['alert_id']}/work-order").json()
    assert dup["existing"] is True and dup["wo_id"] == wo["wo_id"]
    assert client.get(f"/api/alerts/{a['alert_id']}").json()["wo_id"] == wo["wo_id"]

    client.patch(f"/api/work-orders/{wo['wo_id']}", json={"status": "Closed"})
    closed = client.get(f"/api/alerts/{a['alert_id']}").json()
    assert closed["status"] == "RESOLVED" and wo["wo_id"] in closed["resolution"]
    assert all(x["rule"] != "EV_TEST_FLOW" for x in client.get("/api/alerts").json())
    assert any(x["rule"] == "EV_TEST_FLOW" for x in client.get("/api/alerts?include_resolved=true").json())
    assert client.post(f"/api/alerts/{a['alert_id']}/acknowledge").status_code == 409
    assert client.post("/api/alerts/AL-9999/acknowledge").status_code == 404


def test_info_events_do_not_open_alerts(client):
    broadcast([ev("EV_TEST_INFO", severity="INFO"), ev("EV_TEST_CAL", kind="CALIBRATION")])
    rules = {x["rule"] for x in client.get("/api/alerts?include_resolved=true").json()}
    assert not {"EV_TEST_INFO", "EV_TEST_CAL"} & rules


def test_reset_resolves_open_alerts(client):
    broadcast([ev("EV_TEST_RESET")])
    assert any(x["rule"] == "EV_TEST_RESET" for x in client.get("/api/alerts").json())
    client.post("/api/reset")
    assert client.get("/api/alerts").json() == []
    [r] = [x for x in client.get("/api/alerts?include_resolved=true").json() if x["rule"] == "EV_TEST_RESET"]
    assert r["resolution"] == "Scenario reset"


def test_websocket_pushes_alert_and_work_order_frames(client):
    with client.websocket_connect("/ws/telemetry") as ws:
        ws.receive_json(), ws.receive_json()  # hello, registry
        wo = client.post("/api/work-orders", json={"machine_id": "LATHE-01", "title": "WS check"}).json()
        client.patch(f"/api/work-orders/{wo['wo_id']}", json={"status": "Acknowledged"})
        seen = []
        for _ in range(200):
            m = ws.receive_json()
            if m["type"] == "work_order" and m["work_order"]["wo_id"] == wo["wo_id"]:
                jsonschema.validate(m, SCHEMA)
                seen.append((m["action"], m["work_order"]["status"]))
                if len(seen) == 2:
                    break
        assert seen == [("created", "Open"), ("updated", "Acknowledged")]


def test_alert_frames_validate():
    frame = alert_store.message("raised", {
        "alert_id": "AL-0001", "rule": "R", "machine_id": "VMC-01", "kind": "ALERT", "severity": "WARN",
        "title": "t", "status": "ACTIVE", "raised_at": "2026-10-25T10:00:00.000Z", "occurrences": 1, "data": {},
    })
    jsonschema.validate(frame, SCHEMA)


def test_engine_coolant_story_opens_one_alert_per_rule(tmp_path, monkeypatch):
    """Real engine events → alert store: cooling-loss and auto-derate alerts appear once each; reset resolves both."""
    from datetime import datetime, timezone

    from sim.factory import FactoryEngine

    path = tmp_path / "story.sqlite"
    db.init_db(path)
    for fn in ("upsert_alert", "resolve_alerts"):
        orig = getattr(db, fn)
        monkeypatch.setattr(db, fn, lambda *a, _o=orig, **k: _o(*a, **{**k, "db_path": path}))
    eng = FactoryEngine(seed=42, auto_demo=False, start=datetime(2026, 1, 1, tzinfo=timezone.utc))
    eng.command({"cmd": "inject_fault", "machine_id": "VMC-01", "fault": "coolant_degradation"})
    frames = []
    for _ in range(400):
        frames += alert_store.ingest(eng.step())
    raised = [f["alert"]["rule"] for f in frames if f["action"] == "raised"]
    assert sorted(raised) == ["EV_COOLING_VMC-01", "EV_DERATE_VMC-01"]
    assert len(db.list_alerts(db_path=path)) == 2
    resolved = alert_store.ingest(eng.command({"cmd": "reset"}))
    assert {f["alert"]["rule"] for f in resolved} == set(raised)
    assert db.list_alerts(db_path=path) == []
