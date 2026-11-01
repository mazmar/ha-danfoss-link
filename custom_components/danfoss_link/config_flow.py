"""Config flow for Danfoss Link: pair a new "app" with the CC."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult

from .const import (
    CONF_CERTIFICATE,
    CONF_DEVICE_NAME,
    CONF_PAIRING_CODE,
    CONF_PEER_ID,
    CONF_PRIVATE_KEY,
    DEFAULT_DEVICE_NAME,
    DOMAIN,
)
from .mdg.connection import ControlConnection
from .mdg.errors import MdgConnectionError, MdgPairingError, MdgPeerUnavailable
from .mdg.identity import Identity
from .mdg.pace import pair

_LOGGER = logging.getLogger(__name__)

STEP_USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_PAIRING_CODE): str,
        vol.Required(CONF_DEVICE_NAME, default=DEFAULT_DEVICE_NAME): str,
    }
)


class DanfossLinkConfigFlow(ConfigFlow, domain=DOMAIN):
    """Pair Home Assistant with a Danfoss Link CC through the Trifork MDG cloud."""

    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            name = user_input[CONF_DEVICE_NAME]
            identity = await self.hass.async_add_executor_job(Identity.generate)
            control = ControlConnection(identity, name)
            try:
                await control.connect([])
                pairing = await pair(control, identity, user_input[CONF_PAIRING_CODE], name)
                # Let the cloud know we accept the CC (the app does this after pairing).
                await control.request("set_peer_whitelist", {"peer_name": [pairing.peer_id]})
            except (MdgPairingError, MdgPeerUnavailable) as err:
                _LOGGER.warning("Danfoss Link pairing failed: %s", err)
                errors["base"] = "invalid_code"
            except MdgConnectionError as err:
                _LOGGER.warning("Danfoss Link pairing failed: %s", err)
                errors["base"] = "cannot_connect"
            except Exception:
                _LOGGER.exception("Unexpected error while pairing with Danfoss Link")
                errors["base"] = "unknown"
            else:
                await self.async_set_unique_id(pairing.peer_id)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=pairing.name,
                    data={
                        CONF_PEER_ID: pairing.peer_id,
                        CONF_PRIVATE_KEY: identity.private_key_pem,
                        CONF_CERTIFICATE: identity.certificate_pem,
                        CONF_DEVICE_NAME: name,
                    },
                )
            finally:
                await control.close()

        return self.async_show_form(
            step_id="user", data_schema=STEP_USER_SCHEMA, errors=errors
        )
