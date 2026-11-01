"""Data update coordinator for Danfoss Link."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import logging
import time

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import (
    Alarm,
    DanfossLinkApi,
    DanfossLinkError,
    Mode,
    OnOffDevice,
    Room,
    SystemInfo,
    SystemStatus,
)
from .const import DEFAULT_SCAN_INTERVAL, DOMAIN
from .mdg.errors import MdgError

_LOGGER = logging.getLogger(__name__)

SCHEDULE_INTERVAL = 15 * 60  # seconds


@dataclass
class DanfossLinkData:
    """Snapshot of the CC state."""

    info: SystemInfo
    status: SystemStatus
    rooms: dict[int, Room] = field(default_factory=dict)
    devices: dict[int, OnOffDevice] = field(default_factory=dict)
    modes: dict[str, Mode] = field(default_factory=dict)
    alarms: list[Alarm] = field(default_factory=list)

    @property
    def active_mode(self) -> str | None:
        for mode in self.modes.values():
            if mode.state == "Active":
                return mode.type
        return None


type DanfossLinkConfigEntry = ConfigEntry[DanfossLinkCoordinator]


class DanfossLinkCoordinator(DataUpdateCoordinator[DanfossLinkData]):
    """Polls the CC through the MDG tunnel."""

    config_entry: DanfossLinkConfigEntry

    def __init__(
        self, hass: HomeAssistant, entry: DanfossLinkConfigEntry, api: DanfossLinkApi
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=DEFAULT_SCAN_INTERVAL,
        )
        self.api = api
        self.api_versions: list[int] | None = None
        # Schedules are only part of GET /api/rooms/{id}; they rarely change,
        # so fetch them on start, every SCHEDULE_INTERVAL and after edits.
        self._schedules: dict[int, dict] = {}
        self._schedules_fetched = 0.0

    def invalidate_schedules(self) -> None:
        self._schedules_fetched = 0.0

    async def _async_update_data(self) -> DanfossLinkData:
        try:
            if self.api_versions is None:
                self.api_versions = await self.api.get_api_versions()
            # Keep requests sequential; the CC handles at most 17 in parallel
            # and answers 503 when its cc_json semaphore is exhausted.
            info = await self.api.get_system_info()
            status = await self.api.get_system_status()
            rooms = await self.api.get_rooms()
            if time.monotonic() - self._schedules_fetched > SCHEDULE_INTERVAL:
                self._schedules = {r.id: (await self.api.get_room(r.id)).schedule for r in rooms}
                self._schedules_fetched = time.monotonic()
            for room in rooms:
                room.schedule = self._schedules.get(room.id, {})
            modes = await self.api.get_modes()
            # Like the app: onoffdevices needs API version 2, alarms version 3.
            devices = await self.api.get_onoff_devices() if 2 in self.api_versions else []
            alarms = await self.api.get_alarms() if 3 in self.api_versions else []
        except (DanfossLinkError, MdgError, asyncio.TimeoutError, OSError) as err:
            raise UpdateFailed(f"Error communicating with Danfoss Link CC: {err}") from err
        return DanfossLinkData(
            info=info,
            status=status,
            rooms={r.id: r for r in rooms},
            devices={d.id: d for d in devices},
            modes={m.type: m for m in modes},
            alarms=alarms,
        )
