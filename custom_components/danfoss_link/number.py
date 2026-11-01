"""Per-room comfort/eco/manual setpoints for Danfoss Link."""

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
from .entity import DanfossLinkRoomEntity

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
    async_add_entities(
        DanfossLinkRoomSetpoint(coordinator, room_id, description)
        for room_id in coordinator.data.rooms
        for description in SETPOINTS
    )


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
        await self.entity_description.set_fn(self.coordinator.api, self._room_id, value)
        await self.coordinator.async_request_refresh()
