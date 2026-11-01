"""Data update coordinator for Danfoss Link."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from dataclasses import dataclass, field
from datetime import timedelta
import logging
import time
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_SCAN_INTERVAL
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import (
    Alarm,
    DanfossLinkApi,
    DanfossLinkError,
    DanfossLinkResponseError,
    Mode,
    OnOffDevice,
    Room,
    SystemInfo,
    SystemStatus,
)
from .const import DEFAULT_SCAN_INTERVAL, DOMAIN, MODE_ACTIVE, MODE_AT_HOME
from .mdg.errors import MdgError

_LOGGER = logging.getLogger(__name__)

SCHEDULE_INTERVAL = 15 * 60  # seconds


def _monotonic() -> float:
    return time.monotonic()


@dataclass
class DanfossLinkData:
    """Snapshot of the CC state."""

    info: SystemInfo
    status: SystemStatus
    rooms: dict[int, Room] = field(default_factory=dict)
    devices: dict[int, OnOffDevice] = field(default_factory=dict)
    modes: dict[str, Mode] = field(default_factory=dict)
    alarms: list[Alarm] = field(default_factory=list)
    temperature_settings: dict[str, Any] = field(default_factory=dict)

    @property
    def active_mode(self) -> str | None:
        """The active house mode; a temporary mode (vacation, pause, boost) wins over AtHome."""
        active = [m.type for m in self.modes.values() if m.state == MODE_ACTIVE]
        for mode in active:
            if mode != MODE_AT_HOME:
                return mode
        return active[0] if active else None

    @property
    def unread_alarms(self) -> list[Alarm]:
        return [a for a in self.alarms if not a.is_read]


type DanfossLinkConfigEntry = ConfigEntry[DanfossLinkCoordinator]


class DanfossLinkCoordinator(DataUpdateCoordinator[DanfossLinkData]):
    """Polls the CC through the MDG tunnel."""

    config_entry: DanfossLinkConfigEntry

    def __init__(
        self, hass: HomeAssistant, entry: DanfossLinkConfigEntry, api: DanfossLinkApi
    ) -> None:
        interval = entry.options.get(CONF_SCAN_INTERVAL)
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=timedelta(seconds=interval) if interval else DEFAULT_SCAN_INTERVAL,
        )
        self.api = api
        self.api_versions: list[int] | None = None
        # Schedules are only part of GET /api/rooms/{id}; they rarely change,
        # so fetch them on start, every SCHEDULE_INTERVAL and after edits.
        self._schedules: dict[int, dict] = {}
        # monotonic() starts near 0 after boot, so "never" must not be 0.0
        self._schedules_fetched: float | None = None

    def invalidate_schedules(self) -> None:
        self._schedules_fetched = None

    async def _async_update_data(self) -> DanfossLinkData:
        try:
            if self.api_versions is None:
                self.api_versions = await self.api.get_api_versions()
            # Keep requests sequential; the CC handles at most 17 in parallel
            # and answers 503 when its cc_json semaphore is exhausted.
            info = await self.api.get_system_info()
            status = await self.api.get_system_status()
            rooms = await self.api.get_rooms()
            if (
                self._schedules_fetched is None
                or _monotonic() - self._schedules_fetched > SCHEDULE_INTERVAL
            ):
                self._schedules = {r.id: (await self.api.get_room(r.id)).schedule for r in rooms}
                self._schedules_fetched = _monotonic()
            for room in rooms:
                room.schedule = self._schedules.get(room.id, {})
            modes = await self.api.get_modes()
            temperature_settings = await self._optional(self.api.get_temperature_settings())
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
            temperature_settings=temperature_settings or {},
        )

    @staticmethod
    async def _optional(request: Awaitable[Any]) -> Any:
        """Endpoints that may be missing on some firmware versions."""
        try:
            return await request
        except DanfossLinkResponseError as err:
            _LOGGER.debug("Optional request failed: %s", err)
            return None

    async def async_write(self, request: Awaitable[Any], *, refresh: bool = True) -> None:
        """Send a change to the CC with user friendly errors, then refresh."""
        try:
            await request
        except DanfossLinkResponseError as err:
            raise HomeAssistantError(
                f"The Danfoss Link CC rejected the change (HTTP {err.status}): {err.content}"
            ) from err
        except (DanfossLinkError, MdgError, asyncio.TimeoutError, OSError) as err:
            raise HomeAssistantError(f"Cannot reach the Danfoss Link CC: {err}") from err
        if refresh:
            # Not debounced: show what the CC accepted right away.
            await self.async_refresh()

    async def async_mark_alarms_read(self, alarm_ids: list[int]) -> None:
        """Mark alarms as read, as the app does when one is opened."""
        for alarm_id in alarm_ids:
            await self.async_write(self.api.mark_alarm_read(alarm_id), refresh=False)
        await self.async_refresh()
