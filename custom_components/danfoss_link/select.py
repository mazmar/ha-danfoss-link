"""System mode select for Danfoss Link."""

from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import DanfossLinkConfigEntry, DanfossLinkCoordinator
from .entity import DanfossLinkEntity

PARALLEL_UPDATES = 1

OPTION_SCHEDULE = "schedule"
# option -> CC mode type
OPTIONS = {
    "at_home": "AtHome",
    "pause": "Pause",
    "vacation": "Vacation",
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: DanfossLinkConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities([DanfossLinkModeSelect(entry.runtime_data)])


class DanfossLinkModeSelect(DanfossLinkEntity, SelectEntity):
    """Override mode of the whole house.

    "schedule" = no override active, rooms follow their weekly schedules.
    "at_home"  = stay at comfort temperature in all rooms (app: "I'm home").
    "pause"    = heating paused (frost protection / pause temperature).
    "vacation" = vacation temperature until switched back.
    """

    _attr_translation_key = "mode"
    _attr_options = [OPTION_SCHEDULE, *OPTIONS]

    def __init__(self, coordinator: DanfossLinkCoordinator) -> None:
        super().__init__(coordinator, "mode")

    @property
    def current_option(self) -> str | None:
        active = self.coordinator.data.active_mode
        for option, mode in OPTIONS.items():
            if mode == active:
                return option
        return OPTION_SCHEDULE

    async def async_select_option(self, option: str) -> None:
        api = self.coordinator.api
        active = self.coordinator.data.active_mode
        if option == OPTION_SCHEDULE:
            if active is not None:
                await api.set_mode(active, False)
        else:
            mode = OPTIONS[option]
            rooms = list(self.coordinator.data.rooms) if mode == "AtHome" else None
            await api.set_mode(mode, True, rooms=rooms)
        await self.coordinator.async_request_refresh()
