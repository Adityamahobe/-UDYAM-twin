"""Optional MQTT bridge: subscribe to a broker and feed readings into the hub.

Enabled only when UDYAM_MQTT_HOST is set and paho-mqtt is installed, so the demo
never depends on a broker. Topic: `udyam/<device_id>/telemetry`, payload is the same
JSON as the HTTP endpoint and must carry `"token"`.

    UDYAM_MQTT_HOST=localhost UDYAM_MQTT_PORT=1883 python -m uvicorn api.app:app
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any

from hub.core import HubError, IoTHub

TOPIC = "udyam/+/telemetry"


class MqttBridge:
    def __init__(self, hub: IoTHub, host: str, port: int = 1883, username: str | None = None, password: str | None = None):
        self.hub = hub
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.client: Any = None
        self.connected = False
        self.received = 0
        self.rejected = 0
        self.last_error: str | None = None
        # CONNECTING → CONNECTED; DISCONNECTED (paho keeps retrying) or ERROR otherwise
        self.state = "NOT_STARTED"
        self.last_message_at: str | None = None

    @classmethod
    def from_env(cls, hub: IoTHub) -> "MqttBridge | None":
        host = os.environ.get("UDYAM_MQTT_HOST")
        if not host:
            return None
        return cls(
            hub,
            host,
            int(os.environ.get("UDYAM_MQTT_PORT", "1883")),
            os.environ.get("UDYAM_MQTT_USER"),
            os.environ.get("UDYAM_MQTT_PASS"),
        )

    def start(self) -> bool:
        try:
            import paho.mqtt.client as mqtt
        except ImportError:
            self.last_error = "paho-mqtt not installed (pip install paho-mqtt)"
            self.state = "ERROR"
            return False
        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="udyam-hub")
        if self.username:
            self.client.username_pw_set(self.username, self.password)
        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect
        self.client.on_message = self._on_message
        try:
            self.state = "CONNECTING"
            self.client.connect_async(self.host, self.port, keepalive=30)
            self.client.loop_start()
        except Exception as err:  # noqa: BLE001 - surface any broker error in status
            self.last_error = str(err)
            self.state = "ERROR"
            return False
        return True

    def stop(self) -> None:
        if self.client is not None:
            self.client.loop_stop()
            self.client.disconnect()

    def _on_connect(self, client, _userdata, _flags, reason_code, _props=None) -> None:
        self.connected = not reason_code.is_failure
        if self.connected:
            client.subscribe(TOPIC, qos=0)
            self.last_error = None
            self.state = "CONNECTED"
        else:
            self.last_error = f"connect failed: {reason_code}"
            self.state = "ERROR"

    def _on_disconnect(self, _client, _userdata, _flags, reason_code, _props=None) -> None:
        self.connected = False
        self.state = "DISCONNECTED"
        if reason_code.is_failure:
            self.last_error = f"disconnected: {reason_code}"

    def _on_message(self, _client, _userdata, msg) -> None:
        self.handle(msg.topic, msg.payload)

    def handle(self, topic: str, raw: bytes) -> None:
        """Process one MQTT message (split out so it is testable without a broker)."""
        self.received += 1
        self.last_message_at = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
        parts = topic.split("/")
        if len(parts) != 3:
            self.rejected += 1
            return
        try:
            payload = json.loads(raw)
            self.hub.ingest(parts[1], payload.get("token"), payload, transport="mqtt")
        except (json.JSONDecodeError, AttributeError, HubError) as err:
            self.rejected += 1
            self.last_error = str(err)

    def status(self) -> dict[str, Any]:
        return {
            "enabled": True,
            "broker": f"{self.host}:{self.port}",
            "topic": TOPIC,
            "connected": self.connected,
            "state": self.state,
            "last_message_at": self.last_message_at,
            "received": self.received,
            "rejected": self.rejected,
            "last_error": self.last_error,
        }
