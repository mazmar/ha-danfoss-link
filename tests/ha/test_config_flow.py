"""Config, reconfigure and options flows."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from custom_components.danfoss_link.const import CONF_PEER_ID, DOMAIN
from custom_components.danfoss_link.mdg.errors import (
    MdgLanPairingCode,
    MdgPairingCodeExpired,
    MdgPeerUnavailable,
    MdgWrongPairingCode,
)
from custom_components.danfoss_link.mdg.pace import Pairing

from .conftest import PEER_ID

FLOW = "custom_components.danfoss_link.config_flow"


async def _start(hass: HomeAssistant, step: str):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.MENU
    return await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": step})


async def test_pair(hass: HomeAssistant) -> None:
    result = await _start(hass, "pair")
    assert result["step_id"] == "pair"
    with (
        patch(f"{FLOW}.ControlConnection") as control,
        patch(f"{FLOW}.pair", AsyncMock(side_effect=MdgPairingCodeExpired)),
    ):
        control.return_value.connect = AsyncMock()
        control.return_value.close = AsyncMock()
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"pairing_code": "1234-56789", "device_name": "HA", "alarm_notifications": True}
        )
    assert result["errors"] == {"base": "code_expired"}

    for error, key in ((MdgWrongPairingCode, "wrong_code"), (MdgLanPairingCode, "lan_code")):
        with (
            patch(f"{FLOW}.ControlConnection") as control,
            patch(f"{FLOW}.pair", AsyncMock(side_effect=error)),
        ):
            control.return_value.connect = AsyncMock()
            control.return_value.close = AsyncMock()
            result = await hass.config_entries.flow.async_configure(
                result["flow_id"], {"pairing_code": "1234-56789", "device_name": "HA", "alarm_notifications": True}
            )
        assert result["errors"] == {"base": key}

    with (
        patch(f"{FLOW}.ControlConnection") as control,
        patch(f"{FLOW}.pair", AsyncMock(return_value=Pairing(PEER_ID, "Domov", b"", 0))),
        patch(f"{FLOW}._probe", AsyncMock(side_effect=[MdgPeerUnavailable, "Domov"])),
    ):
        control.return_value.connect = AsyncMock()
        control.return_value.close = AsyncMock()
        control.return_value.request = AsyncMock()
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"pairing_code": "1234-56789", "device_name": "HA", "alarm_notifications": False}
        )
        # paired, but the first connection failed -> retry without a new code
        assert result["step_id"] == "verify" and result["errors"] == {"base": "cc_offline"}
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Domov"
    assert result["data"][CONF_PEER_ID] == PEER_ID
    assert result["options"] == {"alarm_notifications": False}


async def test_credentials_json(hass: HomeAssistant, identity) -> None:
    result = await _start(hass, "credentials")
    with patch(f"{FLOW}._probe", AsyncMock(return_value="Domov")):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {"identity_json": "{bad json", "device_name": "HA", "alarm_notifications": True},
        )
        assert result["errors"] == {"base": "invalid_credentials"}
        cli_file = json.dumps(
            {
                "peer_id": PEER_ID.replace(":", "_"),
                "private_key": identity.private_key_pem,
                "certificate": identity.certificate_pem,
            }
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"identity_json": cli_file, "device_name": "HA", "alarm_notifications": True}
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_PEER_ID] == PEER_ID


async def test_credentials_key_mismatch(hass: HomeAssistant, identity) -> None:
    from custom_components.danfoss_link.mdg.identity import Identity

    other = Identity.generate()
    result = await _start(hass, "credentials")
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            "certificate": identity.certificate_pem,
            "private_key": other.private_key_pem,
            "peer_id": PEER_ID,
            "device_name": "HA",
            "alarm_notifications": True,
        },
    )
    assert result["errors"] == {"base": "invalid_credentials"}


async def test_options(hass: HomeAssistant, setup_integration, config_entry) -> None:
    result = await hass.config_entries.options.async_init(config_entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"scan_interval": 30, "alarm_notifications": False}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert config_entry.options == {"scan_interval": 30, "alarm_notifications": False}
    assert config_entry.runtime_data.update_interval.total_seconds() == 30
