"""Sensors for Danfoss Link."""

from __future__ import annotations

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import DanfossLinkConfigEntry, DanfossLinkCoordinator
from .entity import DanfossLinkEntity

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: DanfossLinkConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    entities: list[SensorEntity] = [
        DanfossLinkSystemStateSensor(coordinator),
        DanfossLinkAlarmSensor(coordinator),
    ]
    if coordinator.data.status.outdoor_temperature is not None:
        entities.append(DanfossLinkOutdoorTemperatureSensor(coordinator))
    async_add_entities(entities)


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
    """Current system state (AtHome, Away, Asleep, Vacation, Pause, ...)."""

    _attr_translation_key = "system_state"

    def __init__(self, coordinator: DanfossLinkCoordinator) -> None:
        super().__init__(coordinator, "system_state")

    @property
    def native_value(self) -> str:
        return self.coordinator.data.status.current_state

    @property
    def extra_state_attributes(self) -> dict[str, str | None]:
        return {"secondary_state": self.coordinator.data.status.secondary_state}


class DanfossLinkAlarmSensor(DanfossLinkEntity, SensorEntity):
    """Number of unread alarms."""

    _attr_translation_key = "unread_alarms"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator: DanfossLinkCoordinator) -> None:
        super().__init__(coordinator, "unread_alarms")

    @property
    def native_value(self) -> int:
        return sum(1 for a in self.coordinator.data.alarms if not a.is_read)

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        return {
            "alarms": [
                {
                    "id": a.id,
                    "level": a.level,
                    "room": a.room,
                    "node": a.node,
                    "title": a.title_text_id,
                    "description": a.description_text_id,
                    "created": a.created,
                    "read": a.is_read,
                }
                for a in self.coordinator.data.alarms
            ]
        }
