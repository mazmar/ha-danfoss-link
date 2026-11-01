"""Entities, actions and alarm notifications against a fake CC."""

from __future__ import annotations

from homeassistant.components import persistent_notification
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from custom_components.danfoss_link.const import DOMAIN


async def test_entities(hass: HomeAssistant, setup_integration) -> None:
    state = hass.states.get("climate.kupelna")
    assert state.state == "auto"
    assert state.attributes["current_temperature"] == 21.5
    assert state.attributes["temperature"] == 21.0
    assert state.attributes["schedule"]["monday"] == ["06:30-08:00"]
    assert hass.states.get("climate.obyvacka").attributes["hvac_action"] == "heating"

    assert hass.states.get("sensor.kupelna_temperature").state == "21.5"
    assert hass.states.get("sensor.domov_outdoor_temperature").state == "7.5"
    assert hass.states.get("sensor.domov_system_state").state == "at_home"
    assert hass.states.get("sensor.domov_unread_alarms").state == "1"
    assert hass.states.get("sensor.domov_vacation_start").state == "unknown"
    assert hass.states.get("select.domov_mode").state == "schedule"
    assert hass.states.get("select.kupelna_regulation").state == "schedule"
    assert hass.states.get("number.domov_vacation_temperature").state == "15.0"
    assert hass.states.get("number.kupelna_comfort_temperature").state == "21.0"
    assert hass.states.get("binary_sensor.obyvacka_living_zone").state == "on"
    assert hass.states.get("binary_sensor.kupelna_living_zone").state == "off"
    assert hass.states.get("switch.domov_kotol").state == "off"

    # diagnostic entities are disabled by default
    registry = er.async_get(hass)
    battery = registry.async_get("binary_sensor.kupelna_battery")
    assert battery is not None and battery.disabled_by is not None
    assert registry.async_get("sensor.domov_wi_fi_signal").disabled_by is not None


async def test_set_temperature_and_mode(hass: HomeAssistant, setup_integration) -> None:
    cc = setup_integration
    await hass.services.async_call(
        "climate", "set_temperature", {"entity_id": "climate.kupelna", "temperature": 22.5}, blocking=True
    )
    assert cc.writes[-1] == ("PATCH", "/api/rooms/1", {"id": 1, "setpoint": {"temperature": 22.5}})

    await hass.services.async_call(
        "select", "select_option", {"entity_id": "select.obyvacka_regulation", "option": "manual"},
        blocking=True,
    )
    # living zone rooms are switched on the zone
    assert cc.writes[-1] == (
        "PATCH", "/api/zones/living", {"name": "Living", "regulationType": "Manual"}
    )


async def test_set_schedule(hass: HomeAssistant, setup_integration) -> None:
    cc = setup_integration
    await hass.services.async_call(
        DOMAIN,
        "set_schedule",
        {"entity_id": "climate.kupelna", "days": ["monday"], "periods": ["06:00-07:30", "18:00-24:00"]},
        blocking=True,
    )
    method, uri, body = cc.writes[-1]
    assert (method, uri) == ("PATCH", "/api/rooms/1")
    assert body["schedule"]["monday"] == [
        {"startTimeFromMidnight": "00:00:00", "endTimeFromMidnight": "06:00:00", "state": "Away"},
        {"startTimeFromMidnight": "06:00:00", "endTimeFromMidnight": "07:30:00", "state": "AtHome"},
        {"startTimeFromMidnight": "07:30:00", "endTimeFromMidnight": "18:00:00", "state": "Away"},
        {"startTimeFromMidnight": "18:00:00", "endTimeFromMidnight": "1.00:00:00", "state": "AtHome"},
    ]


async def test_house_mode(hass: HomeAssistant, setup_integration) -> None:
    cc = setup_integration

    async def select(option: str) -> None:
        await hass.services.async_call(
            "select", "select_option", {"entity_id": "select.domov_mode", "option": option},
            blocking=True,
        )

    await select("at_home")
    assert cc.writes[-1] == (
        "PUT", "/api/modes/athome", {"type": "AtHome", "state": "Active", "rooms": [0, 1, 2]}
    )
    # AtHome is replaced by pause without ending it first
    await select("pause")
    assert cc.writes[-1] == ("PUT", "/api/modes/pause", {"type": "Pause", "state": "Active"})
    await select("schedule")
    assert cc.writes[-1] == ("PUT", "/api/modes/pause", {"type": "Pause", "state": "Inactive"})


async def test_plan_vacation(hass: HomeAssistant, setup_integration) -> None:
    cc = setup_integration
    await hass.services.async_call(
        DOMAIN,
        "plan_vacation",
        {"start": "2030-12-20 08:00:00", "end": "2030-12-27 16:00:00"},
        blocking=True,
    )
    method, uri, body = cc.writes[-1]
    assert (method, uri) == ("PUT", "/api/modes/vacation")
    assert body["type"] == "Vacation" and body["state"] == "Active"
    # the CC speaks UTC (systemTime ends with Z): times are sent as UTC with Z
    assert body["startTime"].endswith("Z") and body["endTime"].endswith("Z")


async def test_alarm_notification(hass: HomeAssistant, setup_integration) -> None:
    cc = setup_integration
    notifications = persistent_notification._async_get_or_create_notifications(hass)
    ours = [n for n in notifications if "_alarm_7" in n]
    assert len(ours) == 1
    assert "Low battery level" in notifications[ours[0]]["title"]
    assert "Kupelna" in notifications[ours[0]]["message"]

    persistent_notification.async_dismiss(hass, ours[0])
    await hass.async_block_till_done()
    assert ("PATCH", "/api/alarms/7", {"isRead": True, "id": 7}) in cc.writes
    assert hass.states.get("sensor.domov_unread_alarms").state == "0"


async def test_unload(hass: HomeAssistant, setup_integration, config_entry) -> None:
    assert await hass.config_entries.async_unload(config_entry.entry_id)
    await hass.async_block_till_done()
    notifications = persistent_notification._async_get_or_create_notifications(hass)
    assert not [n for n in notifications if "_alarm_" in n]
