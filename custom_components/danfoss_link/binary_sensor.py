"""Binary sensors for Danfoss Link.

The CC reports no battery level or radio quality per device; the app only
learns about problems from alarms, so battery and radio are derived from the
room's alarms (diagnostic, disabled by default).
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .api import Alarm
from .const import ALARM_BATTERY_CRITICAL, ALARM_BATTERY_LOW, ALARM_NODE_DEAD
from .coordinator import DanfossLinkConfigEntry, DanfossLinkCoordinator
from .entity import DanfossLinkRoomEntity

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: DanfossLinkConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    entities: list[BinarySensorEntity] = []
    for room_id in coordinator.data.rooms:
        entities.append(DanfossLinkLivingZoneSensor(coordinator, room_id))
        entities.append(DanfossLinkBatterySensor(coordinator, room_id))
        entities.append(DanfossLinkRadioSensor(coordinator, room_id))
    async_add_entities(entities)


class DanfossLinkLivingZoneSensor(DanfossLinkRoomEntity, BinarySensorEntity):
    """On when the room is in the living zone (shared mode and schedule)."""

    _attr_translation_key = "living_zone"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: DanfossLinkCoordinator, room_id: int) -> None:
        super().__init__(coordinator, room_id, "living_zone")

    @property
    def is_on(self) -> bool:
        return self.room.in_living_zone


class _RoomAlarmSensor(DanfossLinkRoomEntity, BinarySensorEntity):
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False

    def _alarms(self, *text_ids: str) -> list[Alarm]:
        room = str(self._room_id)
        return [
            a for a in self.coordinator.data.alarms if a.room == room and a.title_text_id in text_ids
        ]


class DanfossLinkBatterySensor(_RoomAlarmSensor):
    """On when a device in the room reports a low or critical battery."""

    _attr_device_class = BinarySensorDeviceClass.BATTERY

    def __init__(self, coordinator: DanfossLinkCoordinator, room_id: int) -> None:
        super().__init__(coordinator, room_id, "battery_low")

    @property
    def is_on(self) -> bool:
        return bool(self._alarms(ALARM_BATTERY_LOW, ALARM_BATTERY_CRITICAL))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        if self._alarms(ALARM_BATTERY_CRITICAL):
            level = "critical"
        elif self._alarms(ALARM_BATTERY_LOW):
            level = "low"
        else:
            level = "ok"
        return {"battery_state": level}


class DanfossLinkRadioSensor(_RoomAlarmSensor):
    """Off when the CC lost radio contact with a device in the room."""

    _attr_translation_key = "radio"
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY

    def __init__(self, coordinator: DanfossLinkCoordinator, room_id: int) -> None:
        super().__init__(coordinator, room_id, "radio")

    @property
    def is_on(self) -> bool:
        return not self._alarms(ALARM_NODE_DEAD)
