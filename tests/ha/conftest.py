"""Fixtures: a fake CC behind a fake cc_json client."""

from __future__ import annotations

import copy
from typing import Any
from unittest.mock import patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.danfoss_link.const import (
    CONF_CERTIFICATE,
    CONF_DEVICE_NAME,
    CONF_PEER_ID,
    CONF_PRIVATE_KEY,
    DOMAIN,
)
from custom_components.danfoss_link.mdg.identity import Identity

PEER_ID = "e7:af:e3:77:00:11:22:33:44:55:66:77:88:99:aa:bb:cc:dd:ee:ff"

DAY = [
    {"state": "Away", "startTimeFromMidnight": "00:00:00", "endTimeFromMidnight": "06:30:00"},
    {"state": "AtHome", "startTimeFromMidnight": "06:30:00", "endTimeFromMidnight": "08:00:00"},
    {"state": "Away", "startTimeFromMidnight": "08:00:00", "endTimeFromMidnight": "1.00:00:00"},
]


def _room(room_id: int, name: str, zone: str) -> dict[str, Any]:
    return {
        "id": room_id,
        "name": name,
        "zone": zone,
        "ambientTemperature": 20.5 + room_id,
        "hasTemperatureSensor": True,
        "minSetpoint": 5.0,
        "maxSetpoint": 28.0,
        "mode": "Normal",
        "state": "AtHome",
        "regulationState": "Heating" if room_id == 0 else "Deadband",
        "setpoint": {"type": "Adjustable", "temperature": 21.0},
        "regulation": {
            "type": "Schedule",
            "atHomeSetpoint": 21.0,
            "awaySetpoint": 17.0,
            "manualSetpoint": 19.0,
        },
    }


class FakeCC:
    """Answers cc_json requests like a Danfoss Link CC and records writes."""

    def __init__(self) -> None:
        self.rooms = {
            0: _room(0, "Obyvacka", "Living"),
            1: _room(1, "Kupelna", "None"),
            2: _room(2, "Spalna", "Living"),
        }
        self.modes = [
            {"type": t, "state": "Inactive"} for t in ("AtHome", "Vacation", "Pause")
        ]
        self.alarms = [
            {
                "id": 7,
                "isRead": False,
                "level": "Warning",
                "room": 1,
                "node": "Radiator 1",
                "titleTextId": "IDS_UA1_ALERTVIEWER_BATTERY_LOW_ALERT",
                "createdDate": "2026-10-30T10:00:00",
            }
        ]
        self.writes: list[tuple[str, str, Any]] = []

    async def request(self, method: str, uri: str, content: dict | None = None):
        if method != "GET":
            self.writes.append((method, uri, copy.deepcopy(content)))
            if uri.startswith("/api/modes/"):
                for mode in self.modes:
                    if mode["type"] == content["type"]:
                        mode.update(content)
                return 200, content
            if uri.startswith("/api/alarms/"):
                for alarm in self.alarms:
                    if alarm["id"] == content["id"]:
                        alarm["isRead"] = content["isRead"]
                return 200, self.alarms[0]
            if uri.startswith("/api/rooms/"):
                room = self.rooms[int(uri.rsplit("/", 1)[1])]
                return 200, room
            return 200, {}
        if uri == "/api/system/apiversions":
            return 200, {"versions": [1, 2, 3]}
        if uri == "/api/system/info":
            return 200, {
                "name": "Domov",
                "version": "4.2.1797",
                "serialNo": "SECRET123",
                "systemTime": "2026-11-01T12:00:00Z",
                "wifiSignalStrengthDb": -61,
            }
        if uri == "/api/system/status":
            return 200, {"currentState": "AtHome", "outdoorTemperature": 7.5}
        if uri == "/api/rooms":
            return 200, list(self.rooms.values())
        if uri.startswith("/api/rooms/"):
            room = dict(self.rooms[int(uri.rsplit("/", 1)[1])])
            room["schedule"] = {"monday": DAY, "tuesday": DAY}
            return 200, room
        if uri == "/api/modes":
            return 200, self.modes
        if uri == "/api/settings/temperature":
            return 200, {
                "vacationTemperature": {"minSetpoint": 12.0, "maxSetpoint": 28.0, "setpoint": 15.0},
                "pauseTemperature": {"minSetpoint": 8.0, "maxSetpoint": 28.0, "setpoint": 12.0},
            }
        if uri == "/api/onoffdevices":
            return 200, [{"id": 1, "name": "Kotol", "state": "Off", "regulation": "Schedule"}]
        if uri == "/api/alarms":
            return 200, self.alarms
        return 404, None

    async def close(self) -> None:
        pass


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    yield


@pytest.fixture(scope="session")
def identity() -> Identity:
    return Identity.generate()


@pytest.fixture
def fake_cc():
    cc = FakeCC()
    with patch(
        "custom_components.danfoss_link.CcJsonClient", side_effect=lambda *a, **k: cc
    ):
        yield cc


@pytest.fixture
def config_entry(hass, identity) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Domov",
        unique_id=PEER_ID,
        data={
            CONF_PEER_ID: PEER_ID,
            CONF_PRIVATE_KEY: identity.private_key_pem,
            CONF_CERTIFICATE: identity.certificate_pem,
            CONF_DEVICE_NAME: "Home Assistant",
        },
    )
    entry.add_to_hass(hass)
    return entry


@pytest.fixture
async def setup_integration(hass, config_entry, fake_cc):
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    return fake_cc
