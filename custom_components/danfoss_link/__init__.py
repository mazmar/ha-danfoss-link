"""The Danfoss Link integration."""

from __future__ import annotations

from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .api import DanfossLinkApi
from .const import CONF_CERTIFICATE, CONF_DEVICE_NAME, CONF_PEER_ID, CONF_PRIVATE_KEY
from .coordinator import DanfossLinkConfigEntry, DanfossLinkCoordinator
from .mdg.cc_json import CcJsonClient
from .mdg.identity import Identity

PLATFORMS = [Platform.CLIMATE, Platform.NUMBER, Platform.SELECT, Platform.SENSOR, Platform.SWITCH]


async def async_setup_entry(hass: HomeAssistant, entry: DanfossLinkConfigEntry) -> bool:
    identity = Identity(entry.data[CONF_PRIVATE_KEY], entry.data[CONF_CERTIFICATE])
    client = CcJsonClient(identity, entry.data[CONF_PEER_ID], entry.data[CONF_DEVICE_NAME])
    coordinator = DanfossLinkCoordinator(hass, entry, DanfossLinkApi(client))
    entry.async_on_unload(client.close)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: DanfossLinkConfigEntry) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
