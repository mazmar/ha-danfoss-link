"""Diagnostics for Danfoss Link (keys, certificate, peer id and serial are redacted)."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from .const import CONF_CERTIFICATE, CONF_PEER_ID, CONF_PRIVATE_KEY
from .coordinator import DanfossLinkConfigEntry

TO_REDACT = {CONF_CERTIFICATE, CONF_PRIVATE_KEY, CONF_PEER_ID, "serial_no", "serialNo"}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: DanfossLinkConfigEntry
) -> dict[str, Any]:
    coordinator = entry.runtime_data
    data = coordinator.data
    return {
        "entry": async_redact_data(dict(entry.data), TO_REDACT),
        "options": dict(entry.options),
        "api_versions": coordinator.api_versions,
        "last_update_success": coordinator.last_update_success,
        "data": async_redact_data(asdict(data), TO_REDACT) if data else None,
    }
