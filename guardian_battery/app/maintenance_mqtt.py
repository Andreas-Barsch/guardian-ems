"""Home Assistant MQTT Event live signalling for newly captured maintenance."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from maintenance import MaintenanceEvent
from maintenance_ui import maintenance_deep_link
from version import GUARDIAN_VERSION
from mqtt_projection import MQTT_MAX_PAYLOAD_BYTES


LIVE_EVENT_TOLERANCE_SECONDS = 5 * 60
MAINTENANCE_EVENT_TYPE = "maintenance"
DISCOVERY_TOPIC = "homeassistant/event/guardian_battery/maintenance/config"


def is_live_create(event: MaintenanceEvent) -> bool:
    """Return true only for a current initial manual capture.

    Guardian deliberately does not publish future-dated or backfilled events.
    The persisted created_at timestamp must be between zero and five minutes
    after the fachlich authoritative occurred_at timestamp.
    """

    if event.revision != 1 or event.updated_at is not None or event.archived_at is not None:
        return False
    if event.source.get("kind") != "manual":
        return False
    occurred = datetime.fromisoformat(event.occurred_at)
    created = datetime.fromisoformat(event.created_at)
    delay = (created - occurred).total_seconds()
    return 0 <= delay <= LIVE_EVENT_TOLERANCE_SECONDS


class MaintenanceMqttPublisher:
    def __init__(self, client: Any, topic_prefix: str):
        self.client = client
        self.prefix = topic_prefix.rstrip("/")

    @property
    def event_topic(self) -> str:
        return f"{self.prefix}/battery/event/maintenance"

    def discovery(self, device: dict[str, Any]) -> None:
        payload = {
            "name": "Guardian Maintenance Event",
            "unique_id": "guardian_battery_maintenance_event",
            "state_topic": self.event_topic,
            "event_types": [MAINTENANCE_EVENT_TYPE],
            "availability_topic": f"{self.prefix}/battery/availability",
            "device": device,
            "icon": "mdi:tools",
        }
        encoded = json.dumps(payload, ensure_ascii=False)
        if (len(encoded.encode("utf-8")) + len(DISCOVERY_TOPIC.encode("utf-8")) + 7
                > MQTT_MAX_PAYLOAD_BYTES):
            raise ValueError("maintenance discovery MQTT payload exceeds 65536 bytes")
        self.client.publish(
            DISCOVERY_TOPIC,
            encoded,
            retain=True,
        )

    def publish_if_live(self, event: MaintenanceEvent) -> bool:
        if not is_live_create(event):
            return False
        payload = {
            "event_type": MAINTENANCE_EVENT_TYPE,
            "maintenance_event_id": event.maintenance_event_id,
            "category": event.category,
            "title": event.title,
            "occurred_at": event.occurred_at,
            "created_at": event.created_at,
            "affected_system": event.affected_system,
            "revision": event.revision,
            "deep_link": maintenance_deep_link(event.maintenance_event_id),
            "guardian_version": GUARDIAN_VERSION,
        }
        if event.module_number is not None:
            payload["module_number"] = event.module_number
        if event.cell_number is not None:
            payload["cell_number"] = event.cell_number
        encoded = json.dumps(payload, ensure_ascii=False)
        if (len(encoded.encode("utf-8")) + len(self.event_topic.encode("utf-8")) + 7
                > MQTT_MAX_PAYLOAD_BYTES):
            raise ValueError("maintenance MQTT payload exceeds 65536 bytes")
        result = self.client.publish(
            self.event_topic,
            encoded,
            retain=False,
        )
        result_code = getattr(result, "rc", 0)
        if result_code not in (None, 0):
            raise RuntimeError(f"MQTT maintenance publish failed with code {result_code}")
        return True
