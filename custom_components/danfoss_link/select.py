"""Selects for Danfoss Link: house mode and each room's schedule/manual mode."""

from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import (
    MODE_AT_HOME,
    MODE_BOOST,
    MODE_PAUSE,
    MODE_VACATION,
    REGULATION_MANUAL,
    REGULATION_SCHEDULE,
)
from .coordinator import DanfossLinkConfigEntry, DanfossLinkCoordinator
from .entity import DanfossLinkEntity, DanfossLinkRoomEntity

PARALLEL_UPDATES = 1

OPTION_SCHEDULE = "schedule"
# option -> CC mode type
HOUSE_OPTIONS = {
    "at_home": MODE_AT_HOME,
    "vacation": MODE_VACATION,
    "pause": MODE_PAUSE,
    "boost": MODE_BOOST,
}
# option -> room regulation type (same setting as the climate entity's auto/heat)
ROOM_OPTIONS = {"schedule": REGULATION_SCHEDULE, "manual": REGULATION_MANUAL}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: DanfossLinkConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    entities: list[SelectEntity] = [DanfossLinkModeSelect(coordinator)]
    entities.extend(DanfossLinkRoomModeSelect(coordinator, room_id) for room_id in coordinator.data.rooms)
    async_add_entities(entities)


class DanfossLinkModeSelect(DanfossLinkEntity, SelectEntity):
    """House mode.

    "schedule" = no mode active, rooms follow their weekly schedules.
    "at_home"  = comfort temperature in all rooms until switched back (app: "I'm home").
    "vacation" / "pause" = vacation / pause temperature until switched back.
    "boost"    = only offered when the CC knows the Boost mode.
    """

    _attr_translation_key = "mode"

    def __init__(self, coordinator: DanfossLinkCoordinator) -> None:
        super().__init__(coordinator, "mode")

    @property
    def options(self) -> list[str]:
        known = set(self.coordinator.data.modes)
        return [OPTION_SCHEDULE] + [
            option
            for option, mode in HOUSE_OPTIONS.items()
            # AtHome, Vacation and Pause are always offered, like in the app
            if mode in known or mode != MODE_BOOST
        ]

    @property
    def current_option(self) -> str | None:
        active = self.coordinator.data.active_mode
        for option, mode in HOUSE_OPTIONS.items():
            if mode == active:
                return option
        return OPTION_SCHEDULE

    async def async_select_option(self, option: str) -> None:
        api = self.coordinator.api
        current = self.coordinator.data.active_mode
        target = HOUSE_OPTIONS.get(option)
        if current == target:
            return
        # AtHome is replaced by another mode; going back to the schedule, or
        # leaving a temporary mode, needs the active one to be ended first.
        if current is not None and (current != MODE_AT_HOME or target is None):
            await self.coordinator.async_write(api.set_mode(current, False), refresh=target is None)
        if target is not None:
            # the CC wants the list of rooms AtHome applies to
            rooms = sorted(self.coordinator.data.rooms) if target == MODE_AT_HOME else None
            await self.coordinator.async_write(api.set_mode(target, True, rooms=rooms))


class DanfossLinkRoomModeSelect(DanfossLinkRoomEntity, SelectEntity):
    """Whether a room follows its weekly schedule or holds the manual temperature."""

    _attr_translation_key = "room_mode"
    _attr_options = list(ROOM_OPTIONS)

    def __init__(self, coordinator: DanfossLinkCoordinator, room_id: int) -> None:
        super().__init__(coordinator, room_id, "room_mode")

    @property
    def current_option(self) -> str | None:
        for option, regulation in ROOM_OPTIONS.items():
            if regulation == self.room.regulation_type:
                return option
        return None

    async def async_select_option(self, option: str) -> None:
        await self.coordinator.async_write(
            self.coordinator.api.set_room_regulation(self.room, ROOM_OPTIONS[option])
        )
