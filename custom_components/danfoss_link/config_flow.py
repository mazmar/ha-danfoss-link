"""Config flow for Danfoss Link.

* pair: Home Assistant becomes a new "app" of the CC using the code on its display.
* credentials: reuse an existing identity - the JSON written by
  tools/danfoss_cli.py, or app_cert.pem/app_key.pem + peer id of a paired app.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.const import CONF_SCAN_INTERVAL
from homeassistant.core import callback
from homeassistant.helpers.selector import (
    BooleanSelector,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    TextSelector,
    TextSelectorConfig,
)

from .api import DanfossLinkApi, DanfossLinkError
from .const import (
    CONF_ALARM_NOTIFICATIONS,
    CONF_CERTIFICATE,
    CONF_DEVICE_NAME,
    CONF_PAIRING_CODE,
    CONF_PEER_ID,
    CONF_PRIVATE_KEY,
    DEFAULT_DEVICE_NAME,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    MIN_SCAN_INTERVAL,
)
from .mdg.cc_json import CcJsonClient
from .mdg.connection import ControlConnection
from .mdg.errors import (
    MdgError,
    MdgInvalidPairingCode,
    MdgLanPairingCode,
    MdgPairingCodeExpired,
    MdgPairingError,
    MdgPairingTimeout,
    MdgPeerUnavailable,
    MdgWrongPairingCode,
)
from .mdg.identity import Identity
from .mdg.pace import pair

_LOGGER = logging.getLogger(__name__)

CONF_IDENTITY_JSON = "identity_json"

PAIRING_ERRORS: dict[type[Exception], str] = {
    MdgInvalidPairingCode: "invalid_code",
    MdgLanPairingCode: "lan_code",
    MdgPairingCodeExpired: "code_expired",
    MdgWrongPairingCode: "wrong_code",
    MdgPairingTimeout: "pairing_timeout",
}

_MULTILINE = TextSelector(TextSelectorConfig(multiline=True))


def _pairing_error(err: Exception) -> str:
    for cls, key in PAIRING_ERRORS.items():
        if isinstance(err, cls):
            return key
    return "pairing_failed" if isinstance(err, MdgPairingError) else "cannot_connect"


def _identity_from_input(user_input: dict[str, Any]) -> tuple[Identity, str]:
    """Build (identity, peer id) from the credentials form; raises ValueError."""
    raw = (user_input.get(CONF_IDENTITY_JSON) or "").strip()
    if raw:
        data = json.loads(raw)
        cert, key, peer_id = data["certificate"], data["private_key"], data["peer_id"]
    else:
        cert = user_input.get(CONF_CERTIFICATE) or ""
        key = user_input.get(CONF_PRIVATE_KEY) or ""
        peer_id = user_input.get(CONF_PEER_ID) or ""
        if not (cert and key and peer_id):
            raise ValueError("certificate, private key and peer id are required")
    peer_id = peer_id.strip().lower().replace("_", ":")
    if len(bytes.fromhex(peer_id.replace(":", ""))) != 20:
        raise ValueError("peer id must be 20 hex bytes")
    identity = Identity(key.strip() + "\n", cert.strip() + "\n")
    identity.validate()
    return identity, peer_id


async def _probe(identity: Identity, peer_id: str, name: str) -> str:
    """Connect to the CC and return its name (raises MdgError / DanfossLinkError)."""
    client = CcJsonClient(identity, peer_id, name)
    try:
        info = await DanfossLinkApi(client).get_system_info()
        await DanfossLinkApi(client).get_rooms()
    finally:
        await client.close()
    return info.name


def _connect_error(err: Exception) -> str:
    if isinstance(err, MdgPeerUnavailable):
        return "cc_offline"
    if isinstance(err, (MdgError, DanfossLinkError, OSError, TimeoutError)):
        return "cannot_connect"
    return "unknown"


class DanfossLinkConfigFlow(ConfigFlow, domain=DOMAIN):
    """Pair Home Assistant with a Danfoss Link CC through the Trifork MDG cloud."""

    VERSION = 1

    def __init__(self) -> None:
        self._identity: Identity | None = None
        self._peer_id: str | None = None
        self._device_name = DEFAULT_DEVICE_NAME
        self._cc_name: str | None = None
        self._options: dict[str, Any] = {}

    @staticmethod
    @callback
    def async_get_options_flow(config_entry) -> OptionsFlow:
        return DanfossLinkOptionsFlow()

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return self.async_show_menu(step_id="user", menu_options=["pair", "credentials"])

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return self.async_show_menu(step_id="reconfigure", menu_options=["pair", "credentials"])

    # --- pairing with the code from the CC ---------------------------------

    async def async_step_pair(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            self._device_name = user_input[CONF_DEVICE_NAME]
            self._options = {CONF_ALARM_NOTIFICATIONS: user_input[CONF_ALARM_NOTIFICATIONS]}
            identity = await self.hass.async_add_executor_job(Identity.generate)
            control = ControlConnection(identity, self._device_name)
            try:
                await control.connect([])
                pairing = await pair(
                    control, identity, user_input[CONF_PAIRING_CODE], self._device_name
                )
                # Let the cloud know we accept the CC (the app does this after pairing).
                await control.request("set_peer_whitelist", {"peer_name": [pairing.peer_id]})
            except (MdgError, OSError, TimeoutError) as err:
                _LOGGER.warning("Danfoss Link pairing failed: %r", err)
                errors["base"] = _pairing_error(err)
            except Exception:
                _LOGGER.exception("Unexpected error while pairing with Danfoss Link")
                errors["base"] = "unknown"
            else:
                self._identity, self._peer_id = identity, pairing.peer_id
                self._cc_name = pairing.name
                return await self.async_step_verify()
            finally:
                await control.close()

        return self.async_show_form(
            step_id="pair",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_PAIRING_CODE): str,
                    vol.Required(CONF_DEVICE_NAME, default=self._device_name): str,
                    vol.Required(CONF_ALARM_NOTIFICATIONS, default=True): BooleanSelector(),
                }
            ),
            errors=errors,
        )

    async def async_step_verify(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Paired - check the connection; a failure can be retried without a new code."""
        assert self._identity is not None and self._peer_id is not None
        errors: dict[str, str] = {}
        try:
            self._cc_name = await _probe(self._identity, self._peer_id, self._device_name)
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Danfoss Link: first connection after pairing failed: %r", err)
            errors["base"] = _connect_error(err)
            return self.async_show_form(step_id="verify", errors=errors)
        return await self._async_finish()

    # --- existing identity ------------------------------------------------

    async def async_step_credentials(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            self._device_name = user_input.get(CONF_DEVICE_NAME) or DEFAULT_DEVICE_NAME
            self._options = {CONF_ALARM_NOTIFICATIONS: user_input[CONF_ALARM_NOTIFICATIONS]}
            try:
                identity, peer_id = await self.hass.async_add_executor_job(
                    _identity_from_input, user_input
                )
            except (ValueError, KeyError, TypeError) as err:
                _LOGGER.warning("Danfoss Link: invalid credentials: %s", err)
                errors["base"] = "invalid_credentials"
            else:
                try:
                    self._cc_name = await _probe(identity, peer_id, self._device_name)
                except Exception as err:  # noqa: BLE001
                    _LOGGER.warning("Danfoss Link: cannot connect with credentials: %r", err)
                    errors["base"] = _connect_error(err)
                else:
                    self._identity, self._peer_id = identity, peer_id
                    return await self._async_finish()

        return self.async_show_form(
            step_id="credentials",
            data_schema=vol.Schema(
                {
                    vol.Optional(CONF_IDENTITY_JSON): _MULTILINE,
                    vol.Optional(CONF_CERTIFICATE): _MULTILINE,
                    vol.Optional(CONF_PRIVATE_KEY): _MULTILINE,
                    vol.Optional(CONF_PEER_ID): str,
                    vol.Required(CONF_DEVICE_NAME, default=self._device_name): str,
                    vol.Required(CONF_ALARM_NOTIFICATIONS, default=True): BooleanSelector(),
                }
            ),
            errors=errors,
        )

    # --- common -----------------------------------------------------------

    async def _async_finish(self) -> ConfigFlowResult:
        assert self._identity is not None and self._peer_id is not None
        data = {
            CONF_PEER_ID: self._peer_id,
            CONF_PRIVATE_KEY: self._identity.private_key_pem,
            CONF_CERTIFICATE: self._identity.certificate_pem,
            CONF_DEVICE_NAME: self._device_name,
        }
        await self.async_set_unique_id(self._peer_id)
        if self.source == "reconfigure":
            self._abort_if_unique_id_mismatch(reason="wrong_cc")
            return self.async_update_reload_and_abort(self._get_reconfigure_entry(), data=data)
        self._abort_if_unique_id_configured()
        return self.async_create_entry(
            title=self._cc_name or "Danfoss Link", data=data, options=self._options
        )


class DanfossLinkOptionsFlow(OptionsFlow):
    """Polling interval and alarm notifications."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(
                data={
                    CONF_SCAN_INTERVAL: int(user_input[CONF_SCAN_INTERVAL]),
                    CONF_ALARM_NOTIFICATIONS: user_input[CONF_ALARM_NOTIFICATIONS],
                }
            )
        options = self.config_entry.options
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_SCAN_INTERVAL,
                        default=options.get(
                            CONF_SCAN_INTERVAL, int(DEFAULT_SCAN_INTERVAL.total_seconds())
                        ),
                    ): NumberSelector(
                        NumberSelectorConfig(
                            min=MIN_SCAN_INTERVAL,
                            max=3600,
                            step=1,
                            unit_of_measurement="s",
                            mode=NumberSelectorMode.BOX,
                        )
                    ),
                    vol.Required(
                        CONF_ALARM_NOTIFICATIONS,
                        default=options.get(CONF_ALARM_NOTIFICATIONS, True),
                    ): BooleanSelector(),
                }
            ),
        )
