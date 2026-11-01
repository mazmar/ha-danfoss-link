"""Climate entities (one per room) for Danfoss Link."""

from __future__ import annotations

from typing import Any

from homeassistant.components.climate import (
    ClimateEntity,
    ClimateEntityFeature,
    HVACAction,
    HVACMode,
)
from homeassistant.const import ATTR_TEMPERATURE, PRECISION_HALVES, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv, entity_platform
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
import voluptuous as vol

from .api import WEEKDAYS, Period
from .const import REGULATION_MANUAL, REGULATION_SCHEDULE
from .coordinator import DanfossLinkConfigEntry, DanfossLinkCoordinator
from .entity import DanfossLinkRoomEntity

PARALLEL_UPDATES = 1

# Room "regulation.type" <-> HVAC mode
REGULATION_TO_HVAC = {REGULATION_SCHEDULE: HVACMode.AUTO, REGULATION_MANUAL: HVACMode.HEAT}
HVAC_TO_REGULATION = {v: k for k, v in REGULATION_TO_HVAC.items()}


SERVICE_SET_SCHEDULE = "set_schedule"
ATTR_DAYS = "days"
ATTR_PERIODS = "periods"


def _parse_period(value: Any) -> Period:
    """"06:00-08:30" -> Period (comfort / AtHome), 15 minute steps, end may be 24:00."""
    try:
        start_s, end_s = str(value).replace(" ", "").split("-")
        start, end = (int(t.split(":")[0]) * 60 + int(t.split(":")[1]) for t in (start_s, end_s))
    except (ValueError, IndexError) as err:
        raise vol.Invalid(f"expected HH:MM-HH:MM, got {value!r}") from err
    if not 0 <= start < end <= 1440:
        raise vol.Invalid(f"invalid period {value!r}")
    if start % 15 or end % 15:
        raise vol.Invalid(f"period {value!r} must use 15 minute steps")
    return Period(start, end, "AtHome")


def _validate_periods(value: Any) -> list[Period]:
    periods = sorted(_parse_period(v) for v in cv.ensure_list(value))
    for prev, cur in zip(periods, periods[1:]):
        if cur.start < prev.end:
            raise vol.Invalid(f"periods {prev} and {cur} overlap")
    return periods


SET_SCHEDULE_SCHEMA = {
    vol.Optional(ATTR_DAYS, default=list(WEEKDAYS)): vol.All(
        cv.ensure_list, [vol.All(vol.Lower, vol.In(WEEKDAYS))]
    ),
    vol.Required(ATTR_PERIODS): vol.Any(None, _validate_periods),
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: DanfossLinkConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    async_add_entities(
        DanfossLinkClimate(coordinator, room_id) for room_id in coordinator.data.rooms
    )
    entity_platform.async_get_current_platform().async_register_entity_service(
        SERVICE_SET_SCHEDULE, SET_SCHEDULE_SCHEMA, "async_set_schedule"
    )


class DanfossLinkClimate(DanfossLinkRoomEntity, ClimateEntity):
    """A Danfoss Link room."""

    _attr_name = None
    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_precision = PRECISION_HALVES
    _attr_target_temperature_step = 0.5
    _attr_hvac_modes = [HVACMode.AUTO, HVACMode.HEAT]
    _attr_supported_features = ClimateEntityFeature.TARGET_TEMPERATURE

    def __init__(self, coordinator: DanfossLinkCoordinator, room_id: int) -> None:
        super().__init__(coordinator, room_id, "climate")

    @property
    def current_temperature(self) -> float | None:
        return self.room.ambient_temperature

    @property
    def target_temperature(self) -> float | None:
        return self.room.setpoint

    @property
    def min_temp(self) -> float:
        return self.room.min_setpoint

    @property
    def max_temp(self) -> float:
        return self.room.max_setpoint

    @property
    def hvac_mode(self) -> HVACMode | None:
        return REGULATION_TO_HVAC.get(self.room.regulation_type or REGULATION_SCHEDULE, HVACMode.AUTO)

    @property
    def hvac_action(self) -> HVACAction | None:
        if self.room.setpoint_type == "Off":
            return HVACAction.OFF
        if self.room.regulation_state in ("Heating", "ForecastHeating"):
            return HVACAction.HEATING
        if self.room.regulation_state == "Cooling":
            return HVACAction.COOLING
        return HVACAction.IDLE

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        room = self.room
        return {
            "room_state": room.state,
            "regulation_state": room.regulation_state,
            "setpoint_type": room.setpoint_type,
            "at_home_setpoint": room.at_home_setpoint,
            "away_setpoint": room.away_setpoint,
            "manual_setpoint": room.manual_setpoint,
            "zone": room.zone,
            "floor_setpoint": room.floor_setpoint if room.floor_setpoint_enabled else None,
            # comfort periods per day; anything not listed is eco (Away)
            "schedule": {
                day: [
                    str(p) if p.state == "AtHome" else f"{p} {p.state}"
                    for p in periods
                    if p.state != "Away"
                ]
                for day, periods in room.schedule.items()
            },
        }

    async def async_set_schedule(self, days: list[str], periods: list[Period] | None) -> None:
        """Set comfort periods for the given days; the rest of each day is eco."""
        self.coordinator.invalidate_schedules()
        await self.coordinator.async_write(
            self.coordinator.api.set_room_schedule(self.room, {day: periods or [] for day in days})
        )

    async def async_set_temperature(self, **kwargs: Any) -> None:
        if (temperature := kwargs.get(ATTR_TEMPERATURE)) is None:
            return
        api = self.coordinator.api
        if self.room.regulation_type == REGULATION_MANUAL:
            # In manual regulation the persistent target is the manual setpoint.
            await self.coordinator.async_write(api.set_room_manual_setpoint(self._room_id, temperature))
        else:
            # Same as the app's main dial: override until the next schedule change.
            await self.coordinator.async_write(api.set_room_setpoint(self._room_id, temperature))

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        await self.coordinator.async_write(
            self.coordinator.api.set_room_regulation(self.room, HVAC_TO_REGULATION[hvac_mode])
        )
