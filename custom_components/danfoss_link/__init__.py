"""The Danfoss Link integration."""

from __future__ import annotations

from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.typing import ConfigType

from .alarms import AlarmNotifier
from .api import DanfossLinkApi
from .const import (
    CONF_ALARM_NOTIFICATIONS,
    CONF_CERTIFICATE,
    CONF_DEVICE_NAME,
    CONF_PEER_ID,
    CONF_PRIVATE_KEY,
    DEFAULT_DEVICE_NAME,
    DOMAIN,
)
from .coordinator import DanfossLinkConfigEntry, DanfossLinkCoordinator
from .mdg.cc_json import CcJsonClient
from .mdg.identity import Identity
from .services import async_setup_services

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

PLATFORMS = [
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.CLIMATE,
    Platform.NUMBER,
    Platform.SELECT,
    Platform.SENSOR,
    Platform.SWITCH,
]


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    async_setup_services(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: DanfossLinkConfigEntry) -> bool:
    identity = Identity(entry.data[CONF_PRIVATE_KEY], entry.data[CONF_CERTIFICATE])
    client = CcJsonClient(
        identity, entry.data[CONF_PEER_ID], entry.data.get(CONF_DEVICE_NAME, DEFAULT_DEVICE_NAME)
    )
    entry.async_on_unload(client.close)
    coordinator = DanfossLinkCoordinator(hass, entry, DanfossLinkApi(client))
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator

    if entry.options.get(CONF_ALARM_NOTIFICATIONS, True):
        notifier = AlarmNotifier(hass, coordinator)
        notifier.async_start()
        entry.async_on_unload(notifier.async_stop)
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def _async_options_updated(hass: HomeAssistant, entry: DanfossLinkConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: DanfossLinkConfigEntry) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
