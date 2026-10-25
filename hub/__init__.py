"""IoT hub: device registry, authenticated ingestion, and LIVE overlay onto the twin stream.

Devices (ESP32 nodes, gateways) push raw sensor readings over HTTP, WebSocket or MQTT.
The hub authenticates them, normalises units, flags data quality, and overlays the
readings onto the bound machine's `observed` frame with `source_mode: "LIVE"`.
The frozen v1.0 message contract is unchanged; LIVE is already a valid source mode.
"""

from hub.core import IoTHub, HubError, STALE_S, OFFLINE_S

__all__ = ["IoTHub", "HubError", "STALE_S", "OFFLINE_S"]
