"""REST API of the Danfoss Link CC, as used by the official Danfoss Link app 1.3.1.

The CC exposes a JSON REST API on its internal HTTP server. The app never talks
HTTP directly; every request is wrapped in a ``JsonWebRequest`` envelope and
tunnelled through an MDG peer connection (protocol ``cc_json``). This module is
transport agnostic: it only needs an object implementing :class:`Transport`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import logging
from typing import Any, Protocol

from .const import MODE_PATHS

_LOGGER = logging.getLogger(__name__)

WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")


class DanfossLinkError(Exception):
    """Generic API error."""


class DanfossLinkResponseError(DanfossLinkError):
    """The CC answered with a non-2xx status."""

    def __init__(self, status: int, content: Any) -> None:
        super().__init__(f"CC returned HTTP {status}: {content!r}")
        self.status = status
        self.content = content


class Transport(Protocol):
    """Something that can carry a JsonWebRequest to the CC."""

    async def request(
        self, method: str, uri: str, content: dict[str, Any] | None = None
    ) -> tuple[int, Any]:
        """Send a request and return (http status, decoded JSON content)."""


@dataclass(frozen=True, order=True)
class Period:
    """Part of a day schedule, minutes from midnight (end exclusive, max 1440)."""

    start: int
    end: int
    state: str  # AtHome / Away / Asleep for rooms, On / Off for on/off devices

    def __str__(self) -> str:
        return f"{_hhmm(self.start)}-{_hhmm(self.end)}"


def _hhmm(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def parse_timespan(value: str) -> int:
    """TimeSpan "HH:MM:SS" or "D.HH:MM:SS" -> minutes."""
    hours, minutes = value.split(":")[:2]
    days = "0"
    if "." in hours:
        days, hours = hours.split(".")
    return int(days) * 1440 + int(hours) * 60 + int(minutes)


def format_timespan(minutes: int) -> str:
    """Minutes -> TimeSpan, like the app (1440 -> "1.00:00:00")."""
    days, rest = divmod(minutes, 1440)
    prefix = f"{days}." if days else ""
    return f"{prefix}{rest // 60:02d}:{rest % 60:02d}:00"


def parse_periods(items: list[dict[str, Any]]) -> list[Period]:
    """Parse a day like the app: round to 15 minute slots and sort."""
    periods = []
    for item in items:
        start = parse_timespan(item["startTimeFromMidnight"]) // 15 * 15
        end = (parse_timespan(item["endTimeFromMidnight"]) + 14) // 15 * 15
        periods.append(Period(start, end, item.get("state", "Invalid")))
    return sorted(periods)


def build_day(periods: list[Period], filler: str) -> list[dict[str, str]]:
    """Serialize a day covering 00:00-24:00, gaps filled with ``filler`` (app: Away/Off)."""
    out = []
    pos = 0
    for period in sorted(periods):
        if period.start > pos:
            out.append(_period_json(pos, period.start, filler))
        out.append(_period_json(period.start, period.end, period.state))
        pos = period.end
    if pos < 1440:
        out.append(_period_json(pos, 1440, filler))
    return out


def _period_json(start: int, end: int, state: str) -> dict[str, str]:
    return {
        "startTimeFromMidnight": format_timespan(start),
        "endTimeFromMidnight": format_timespan(end),
        "state": state,
    }


@dataclass
class Room:
    """A room (radiator/floor thermostat group) on the CC."""

    id: int
    name: str
    ambient_temperature: float | None
    setpoint: float | None
    setpoint_type: str  # Adjustable | Fixed | Off | Invalid
    min_setpoint: float
    max_setpoint: float
    state: str  # AtHome | Away | Asleep | Vacation | Pause | Boost | ...
    regulation_state: str  # Heating | Cooling | Deadband | ForecastHeating | Invalid
    regulation_type: str | None  # Schedule | Manual
    at_home_setpoint: float | None
    away_setpoint: float | None
    manual_setpoint: float | None
    zone: str  # "Living" or "None"
    sort_order: int
    floor_setpoint_enabled: bool = False
    floor_setpoint: float | None = None
    # weekday -> periods; empty when the CC did not include a schedule
    schedule: dict[str, list[Period]] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> Room:
        setpoint = data.get("setpoint") or {}
        schedule = data.get("schedule") or {}
        regulation = data.get("regulation") or {}
        floor = data.get("floorSetpoint") or {}
        return cls(
            id=int(data["id"]),
            name=data.get("name") or f"Room {data['id']}",
            ambient_temperature=_opt_float(data.get("ambientTemperature")),
            setpoint=_opt_float(setpoint.get("temperature")),
            setpoint_type=setpoint.get("type", "Invalid"),
            min_setpoint=float(data.get("minSetpoint", 6.0)),
            max_setpoint=float(data.get("maxSetpoint", 35.0)),
            state=data.get("state", "Normal"),
            regulation_state=data.get("regulationState", "Invalid"),
            regulation_type=regulation.get("type"),
            at_home_setpoint=_opt_float(regulation.get("atHomeSetpoint")),
            away_setpoint=_opt_float(regulation.get("awaySetpoint")),
            manual_setpoint=_opt_float(regulation.get("manualSetpoint")),
            zone=data.get("zone", "None"),
            sort_order=int(data.get("sortOrder", data["id"])),
            floor_setpoint_enabled=floor.get("state") == "Enabled",
            floor_setpoint=_opt_float(floor.get("temperature")),
            schedule={
                day: parse_periods(schedule[day]) for day in WEEKDAYS if schedule.get(day)
            },
            raw=data,
        )

    @property
    def in_living_zone(self) -> bool:
        """Rooms in the living zone share their regulation type."""
        return self.zone == "Living"


@dataclass
class OnOffDevice:
    """A relay (on/off) device controlled by the CC."""

    id: int
    name: str
    state: str  # On | Off
    regulation: str  # Schedule | Manual
    activity_simulation: bool
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> OnOffDevice:
        return cls(
            id=int(data["id"]),
            name=data.get("name") or f"Device {data['id']}",
            state=data.get("state", "Off"),
            regulation=data.get("regulation", "Manual"),
            activity_simulation=bool(data.get("activitySimulationEnabled", False)),
            raw=data,
        )


@dataclass
class Mode:
    """A system mode (AtHome override, Vacation, Pause, Boost)."""

    type: str
    state: str  # Active | Inactive | Planned
    start_time: str | None
    end_time: str | None
    rooms: list[int] | None

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> Mode:
        return cls(
            type=data.get("type", "Invalid"),
            state=data.get("state", "Inactive"),
            start_time=data.get("startTimeUtc", data.get("startTime")),
            end_time=data.get("endTimeUtc", data.get("endTime")),
            rooms=data.get("rooms"),
        )


@dataclass
class SystemStatus:
    """Result of GET /api/system/status."""

    current_state: str
    secondary_state: str | None
    outdoor_temperature: float | None

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> SystemStatus:
        # The app strips everything after the first comma, e.g. "AtHome,Boost".
        state = data.get("currentState") or "Invalid"
        primary, _, secondary = state.partition(",")
        return cls(
            current_state=primary,
            secondary_state=secondary or None,
            outdoor_temperature=_opt_float(data.get("outdoorTemperature")),
        )


@dataclass
class SystemInfo:
    """Result of GET /api/system/info."""

    name: str
    version: str | None
    serial_no: str | None
    branding_type: str | None
    system_time: str | None
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> SystemInfo:
        return cls(
            name=data.get("name") or "Danfoss Link",
            version=data.get("version"),
            serial_no=data.get("serialNo"),
            branding_type=data.get("brandingType"),
            system_time=data.get("systemTime"),
            raw=data,
        )


@dataclass
class Alarm:
    """An alarm/notification raised by the CC."""

    id: int
    is_read: bool
    level: str | None
    room: str | None
    node: str | None
    title_text_id: str | None
    description_text_id: str | None
    created: str | None

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> Alarm:
        return cls(
            id=int(data["id"]),
            is_read=bool(data.get("isRead", False)),
            level=data.get("level"),
            room=data.get("room"),
            node=data.get("node"),
            title_text_id=data.get("titleTextId"),
            description_text_id=data.get("descriptionTextId"),
            created=data.get("createdDate"),
        )


def _opt_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def cc_timezone(system_time: str | None) -> timezone | None:
    """Detect the CC time convention from a timestamp it sent.

    Like the app: a trailing "Z" means the CC speaks UTC, a "+HH:MM"/"-HH:MM"
    suffix means it speaks local time with that offset. ``None`` = UTC.
    """
    if not system_time or system_time.endswith("Z") or len(system_time) <= 6:
        return None
    suffix = system_time[-6:]
    if suffix[0] not in "+-":
        return None
    sign = 1 if suffix[0] == "+" else -1
    hours, minutes = int(suffix[1:3]), int(suffix[4:6])
    return timezone(sign * timedelta(hours=hours, minutes=minutes))


def format_cc_time(value: datetime, tz: timezone | None = None) -> str:
    """Format a timestamp the way the app does.

    UTC with a trailing "Z", or - when the CC uses a fixed offset - local time
    in that offset without any suffix.
    """
    if tz is None:
        return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return value.astimezone(tz).strftime("%Y-%m-%dT%H:%M:%S")


class DanfossLinkApi:
    """High level access to the CC REST API."""

    def __init__(self, transport: Transport) -> None:
        self._transport = transport
        self._cc_tz: timezone | None = None

    async def _call(
        self, method: str, uri: str, content: dict[str, Any] | None = None
    ) -> Any:
        status, body = await self._transport.request(method, uri, content)
        if not 200 <= status < 300:
            raise DanfossLinkResponseError(status, body)
        return body

    # --- system -----------------------------------------------------------

    async def get_api_versions(self) -> list[int]:
        body = await self._call("GET", "/api/system/apiversions")
        return sorted(int(v) for v in body.get("versions", []))

    async def get_system_info(self) -> SystemInfo:
        info = SystemInfo.from_json(await self._call("GET", "/api/system/info"))
        self._cc_tz = cc_timezone(info.system_time)
        return info

    async def get_system_status(self) -> SystemStatus:
        return SystemStatus.from_json(await self._call("GET", "/api/system/status"))

    async def get_temperature_settings(self) -> dict[str, Any]:
        """pauseTemperature / vacationTemperature: {minSetpoint, maxSetpoint, setpoint}."""
        return await self._call("GET", "/api/settings/temperature")

    async def set_vacation_temperature(self, temperature: float) -> None:
        await self._call(
            "PATCH",
            "/api/settings/temperature",
            {"vacationTemperature": {"setpoint": temperature}},
        )

    # --- rooms ------------------------------------------------------------

    async def get_rooms(self) -> list[Room]:
        return [Room.from_json(r) for r in await self._call("GET", "/api/rooms")]

    async def get_room(self, room_id: int) -> Room:
        return Room.from_json(await self._call("GET", f"/api/rooms/{room_id}"))

    async def _patch_room(self, room_id: int, payload: dict[str, Any]) -> Room:
        body = await self._call("PATCH", f"/api/rooms/{room_id}", {"id": room_id, **payload})
        return Room.from_json(body)

    async def set_room_setpoint(self, room_id: int, temperature: float) -> Room:
        """Change the current target temperature (what the app's main dial does)."""
        return await self._patch_room(room_id, {"setpoint": {"temperature": temperature}})

    async def set_room_at_home_setpoint(self, room_id: int, temperature: float) -> Room:
        return await self._patch_room(room_id, {"regulation": {"atHomeSetpoint": temperature}})

    async def set_room_away_setpoint(self, room_id: int, temperature: float) -> Room:
        return await self._patch_room(room_id, {"regulation": {"awaySetpoint": temperature}})

    async def set_room_manual_setpoint(self, room_id: int, temperature: float) -> Room:
        return await self._patch_room(room_id, {"regulation": {"manualSetpoint": temperature}})

    async def set_room_schedule(self, room: Room, days: dict[str, list[Period]]) -> None:
        """Replace the schedule of the given weekdays (gaps become "Away").

        Like the app, rooms in the living zone are changed through
        /api/zones/living, which applies to all rooms of that zone.
        """
        schedule = {day: build_day(periods, "Away") for day, periods in days.items()}
        if room.in_living_zone:
            await self._call(
                "PATCH", "/api/zones/living", {"name": room.zone, "schedule": schedule}
            )
        else:
            await self._patch_room(room.id, {"schedule": schedule})

    async def set_room_regulation(self, room: Room, regulation_type: str) -> None:
        """Switch between "Schedule" and "Manual".

        Rooms in the living zone are switched together via /api/zones/living,
        exactly like the app does.
        """
        if room.in_living_zone:
            await self._call(
                "PATCH", "/api/zones/living", {"name": room.zone, "regulationType": regulation_type}
            )
        else:
            await self._patch_room(room.id, {"regulation": {"type": regulation_type}})

    # --- on/off devices ---------------------------------------------------

    async def get_onoff_devices(self) -> list[OnOffDevice]:
        body = await self._call("GET", "/api/onoffdevices")
        return [OnOffDevice.from_json(d) for d in body if "id" in d]

    async def set_onoff_device(
        self,
        device_id: int,
        *,
        state: str | None = None,
        regulation: str | None = None,
        activity_simulation: bool | None = None,
    ) -> None:
        payload: dict[str, Any] = {"id": device_id}
        if regulation is not None:
            payload["regulation"] = regulation
        if activity_simulation is not None:
            payload["activitySimulationEnabled"] = activity_simulation
        if state is not None:
            payload["state"] = state
        await self._call("PATCH", f"/api/onoffdevices/{device_id}", payload)

    # --- modes ------------------------------------------------------------

    async def get_modes(self) -> list[Mode]:
        return [Mode.from_json(m) for m in await self._call("GET", "/api/modes")]

    async def set_mode(
        self,
        mode_type: str,
        active: bool,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
        rooms: list[int] | None = None,
    ) -> None:
        """Activate/deactivate AtHome, Vacation, Pause or Boost."""
        payload: dict[str, Any] = {
            "type": mode_type,
            "state": "Active" if active else "Inactive",
        }
        if start is not None:
            payload["startTime"] = format_cc_time(start, self._cc_tz)
        if end is not None:
            payload["endTime"] = format_cc_time(end, self._cc_tz)
        if mode_type == "AtHome" and active and rooms is not None:
            payload["rooms"] = rooms
        await self._call("PUT", f"/api/modes/{MODE_PATHS[mode_type]}", payload)

    # --- alarms -----------------------------------------------------------

    async def get_alarms(self) -> list[Alarm]:
        return [Alarm.from_json(a) for a in await self._call("GET", "/api/alarms")]

    async def mark_alarm_read(self, alarm_id: int, read: bool = True) -> None:
        await self._call("PATCH", f"/api/alarms/{alarm_id}", {"isRead": read, "id": alarm_id})
