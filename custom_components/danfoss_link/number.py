"""Temperature setpoints for Danfoss Link: per room comfort/eco/manual, vacation."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from homeassistant.components.number import (
    NumberDeviceClass,
    NumberEntity,
    NumberEntityDescription,
    NumberMode,
)
from homeassistant.const import EntityCategory, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .api import DanfossLinkApi, Room
from .coordinator import DanfossLinkConfigEntry, DanfossLinkCoordinator
from .entity import DanfossLinkEntity, DanfossLinkRoomEntity

PARALLEL_UPDATES = 1


@dataclass(frozen=True, kw_only=True)
class RoomSetpointDescription(NumberEntityDescription):
    """Describes a room setpoint."""

    value_fn: Callable[[Room], float | None]
    set_fn: Callable[[DanfossLinkApi, int, float], Awaitable[object]]


SETPOINTS = (
    RoomSetpointDescription(
        key="at_home_setpoint",
        translation_key="at_home_setpoint",
        value_fn=lambda r: r.at_home_setpoint,
        set_fn=lambda api, room_id, v: api.set_room_at_home_setpoint(room_id, v),
    ),
    RoomSetpointDescription(
        key="away_setpoint",
        translation_key="away_setpoint",
        value_fn=lambda r: r.away_setpoint,
        set_fn=lambda api, room_id, v: api.set_room_away_setpoint(room_id, v),
    ),
    RoomSetpointDescription(
        key="manual_setpoint",
        translation_key="manual_setpoint",
        value_fn=lambda r: r.manual_setpoint,
        set_fn=lambda api, room_id, v: api.set_room_manual_setpoint(room_id, v),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: DanfossLinkConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    entities: list[NumberEntity] = [
        DanfossLinkRoomSetpoint(coordinator, room_id, description)
        for room_id in coordinator.data.rooms
        for description in SETPOINTS
    ]
    if coordinator.data.temperature_settings.get("vacationTemperature"):
        entities.append(DanfossLinkVacationTemperature(coordinator))
    async_add_entities(entities)


class DanfossLinkRoomSetpoint(DanfossLinkRoomEntity, NumberEntity):
    """Comfort (at home), eco (away) or manual setpoint of a room."""

    entity_description: RoomSetpointDescription
    _attr_device_class = NumberDeviceClass.TEMPERATURE
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_native_step = 0.5
    _attr_mode = NumberMode.BOX
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(
        self,
        coordinator: DanfossLinkCoordinator,
        room_id: int,
        description: RoomSetpointDescription,
    ) -> None:
        super().__init__(coordinator, room_id, description.key)
        self.entity_description = description

    @property
    def native_value(self) -> float | None:
        return self.entity_description.value_fn(self.room)

    @property
    def native_min_value(self) -> float:
        return self.room.min_setpoint

    @property
    def native_max_value(self) -> float:
        return self.room.max_setpoint

    async def async_set_native_value(self, value: float) -> None:
        await self.coordinator.async_write(
            self.entity_description.set_fn(self.coordinator.api, self._room_id, value)
        )


class DanfossLinkVacationTemperature(DanfossLinkEntity, NumberEntity):
    """Temperature all rooms use while the house is in vacation mode."""

    _attr_translation_key = "vacation_temperature"
    _attr_device_class = NumberDeviceClass.TEMPERATURE
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_native_step = 0.5
    _attr_mode = NumberMode.BOX
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: DanfossLinkCoordinator) -> None:
        super().__init__(coordinator, "vacation_temperature")

    @property
    def _settings(self) -> dict:
        return self.coordinator.data.temperature_settings.get("vacationTemperature") or {}

    @property
    def available(self) -> bool:
        return super().available and bool(self._settings)

    @property
    def native_value(self) -> float | None:
        return self._settings.get("setpoint")

    @property
    def native_min_value(self) -> float:
        return float(self._settings.get("minSetpoint", 5.0))

    @property
    def native_max_value(self) -> float:
        return float(self._settings.get("maxSetpoint", 28.0))

    async def async_set_native_value(self, value: float) -> None:
        await self.coordinator.async_write(self.coordinator.api.set_vacation_temperature(value))
