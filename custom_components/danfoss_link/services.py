"""Integration-wide actions: plan and cancel a vacation (the app's vacation calendar)."""

from __future__ import annotations

from datetime import datetime

import voluptuous as vol

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.util import dt as dt_util

from .const import DOMAIN, MODE_VACATION
from .coordinator import DanfossLinkConfigEntry, DanfossLinkCoordinator

ATTR_CONFIG_ENTRY_ID = "config_entry_id"
ATTR_START = "start"
ATTR_END = "end"

SERVICE_PLAN_VACATION = "plan_vacation"
SERVICE_CANCEL_VACATION = "cancel_vacation"

_ENTRY = {vol.Optional(ATTR_CONFIG_ENTRY_ID): cv.string}
PLAN_VACATION_SCHEMA = vol.Schema(
    {**_ENTRY, vol.Required(ATTR_START): cv.datetime, vol.Required(ATTR_END): cv.datetime}
)
CANCEL_VACATION_SCHEMA = vol.Schema(_ENTRY)


def _coordinator(call: ServiceCall) -> DanfossLinkCoordinator:
    entries: list[DanfossLinkConfigEntry] = [
        entry
        for entry in call.hass.config_entries.async_entries(DOMAIN)
        if entry.state is ConfigEntryState.LOADED
    ]
    if entry_id := call.data.get(ATTR_CONFIG_ENTRY_ID):
        entries = [entry for entry in entries if entry.entry_id == entry_id]
    elif len(entries) > 1:
        raise ServiceValidationError(translation_domain=DOMAIN, translation_key="choose_config_entry")
    if not entries:
        raise ServiceValidationError(translation_domain=DOMAIN, translation_key="not_loaded")
    return entries[0].runtime_data


def _aware(value: datetime) -> datetime:
    # the datetime selector sends local time without a zone
    if value.tzinfo is None:
        value = value.replace(tzinfo=dt_util.get_default_time_zone())
    return value.replace(second=0, microsecond=0)


async def _plan_vacation(call: ServiceCall) -> None:
    coordinator = _coordinator(call)
    start, end = _aware(call.data[ATTR_START]), _aware(call.data[ATTR_END])
    # same checks as the app's vacation calendar
    if start >= end:
        raise ServiceValidationError(translation_domain=DOMAIN, translation_key="end_before_start")
    if end <= dt_util.now():
        raise ServiceValidationError(translation_domain=DOMAIN, translation_key="end_in_past")
    # The CC keeps it as "Planned" until the start time.
    await coordinator.async_write(
        coordinator.api.set_mode(MODE_VACATION, True, start=start, end=end)
    )


async def _cancel_vacation(call: ServiceCall) -> None:
    coordinator = _coordinator(call)
    await coordinator.async_write(coordinator.api.set_mode(MODE_VACATION, False))


def async_setup_services(hass: HomeAssistant) -> None:
    hass.services.async_register(
        DOMAIN, SERVICE_PLAN_VACATION, _plan_vacation, schema=PLAN_VACATION_SCHEMA
    )
    hass.services.async_register(
        DOMAIN, SERVICE_CANCEL_VACATION, _cancel_vacation, schema=CANCEL_VACATION_SCHEMA
    )
