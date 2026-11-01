"""Sensors for Danfoss Link."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import (
    SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
    EntityCategory,
    UnitOfTemperature,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .alarms import alarm_text
from .const import MODE_ACTIVE, MODE_PLANNED, MODE_VACATION
from .coordinator import DanfossLinkConfigEntry, DanfossLinkCoordinator
from .entity import DanfossLinkEntity, DanfossLinkRoomEntity

PARALLEL_UPDATES = 0

# CC system state (/api/system/status currentState) -> enum option
SYSTEM_STATES = {
    "AtHome": "at_home",
    "Away": "away",
    "Asleep": "asleep",
    "Vacation": "vacation",
    "Pause": "pause",
    "Boost": "boost",
    "Manual": "manual",
    "Off": "off",
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: DanfossLinkConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    data = coordinator.data
    entities: list[SensorEntity] = [
        DanfossLinkSystemStateSensor(coordinator),
        DanfossLinkAlarmSensor(coordinator),
        DanfossLinkVacationTimeSensor(coordinator, "start"),
        DanfossLinkVacationTimeSensor(coordinator, "end"),
    ]
    entities.extend(
        DanfossLinkRoomTemperatureSensor(coordinator, room_id)
        for room_id, room in data.rooms.items()
        if room.has_temperature_sensor
    )
    if data.status.outdoor_temperature is not None:
        entities.append(DanfossLinkOutdoorTemperatureSensor(coordinator))
    if data.info.wifi_signal is not None:
        entities.append(DanfossLinkWifiSignalSensor(coordinator))
    async_add_entities(entities)


class DanfossLinkRoomTemperatureSensor(DanfossLinkRoomEntity, SensorEntity):
    """Measured room temperature, for history and statistics."""

    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS

    def __init__(self, coordinator: DanfossLinkCoordinator, room_id: int) -> None:
        super().__init__(coordinator, room_id, "temperature")

    @property
    def native_value(self) -> float | None:
        return self.room.ambient_temperature


class DanfossLinkOutdoorTemperatureSensor(DanfossLinkEntity, SensorEntity):
    """Outdoor temperature reported by the CC."""

    _attr_translation_key = "outdoor_temperature"
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS

    def __init__(self, coordinator: DanfossLinkCoordinator) -> None:
        super().__init__(coordinator, "outdoor_temperature")

    @property
    def native_value(self) -> float | None:
        return self.coordinator.data.status.outdoor_temperature


class DanfossLinkSystemStateSensor(DanfossLinkEntity, SensorEntity):
    """What the house is doing now.

    From the schedule: at_home (comfort), away (eco), asleep - or the active
    house mode (vacation, pause, boost).
    """

    _attr_translation_key = "system_state"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = list(SYSTEM_STATES.values())

    def __init__(self, coordinator: DanfossLinkCoordinator) -> None:
        super().__init__(coordinator, "system_state")

    @property
    def native_value(self) -> str | None:
        return SYSTEM_STATES.get(self.coordinator.data.status.current_state)

    @property
    def extra_state_attributes(self) -> dict[str, str | None]:
        status = self.coordinator.data.status
        return {"raw_state": status.current_state, "secondary_state": status.secondary_state}


class DanfossLinkVacationTimeSensor(DanfossLinkEntity, SensorEntity):
    """Start or end of a planned or running vacation; unknown when there is none."""

    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(self, coordinator: DanfossLinkCoordinator, which: str) -> None:
        self._which = which
        self._attr_translation_key = f"vacation_{which}"
        super().__init__(coordinator, f"vacation_{which}")

    @property
    def native_value(self) -> datetime | None:
        mode = self.coordinator.data.modes.get(MODE_VACATION)
        if mode is None or mode.state not in (MODE_ACTIVE, MODE_PLANNED):
            return None
        tz = self.coordinator.api.cc_timezone
        return mode.start(tz) if self._which == "start" else mode.end(tz)


class DanfossLinkAlarmSensor(DanfossLinkEntity, SensorEntity):
    """Number of unread alarms."""

    _attr_translation_key = "unread_alarms"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator: DanfossLinkCoordinator) -> None:
        super().__init__(coordinator, "unread_alarms")

    @property
    def native_value(self) -> int:
        return len(self.coordinator.data.unread_alarms)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        data = self.coordinator.data
        language = self.hass.config.language if self.hass else "en"
        return {
            "alarms": [
                {
                    "id": a.id,
                    "level": a.level,
                    "title": alarm_text(language, a.title_text_id),
                    "description": alarm_text(language, a.description_text_id),
                    "type": a.title_text_id,
                    "room": _room_name(data, a.room),
                    "node": a.node,
                    "created": a.created,
                    "read": a.is_read,
                }
                for a in data.alarms
            ]
        }


class DanfossLinkWifiSignalSensor(DanfossLinkEntity, SensorEntity):
    """Wi-Fi signal strength of the CC."""

    _attr_translation_key = "wifi_signal"
    _attr_device_class = SensorDeviceClass.SIGNAL_STRENGTH
    _attr_native_unit_of_measurement = SIGNAL_STRENGTH_DECIBELS_MILLIWATT
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False

    def __init__(self, coordinator: DanfossLinkCoordinator) -> None:
        super().__init__(coordinator, "wifi_signal")

    @property
    def native_value(self) -> int | None:
        return self.coordinator.data.info.wifi_signal


def _room_name(data, room: str | None) -> str | None:
    try:
        return data.rooms[int(room)].name
    except (TypeError, ValueError, KeyError):
        return room
