"""On/off (relay) devices for Danfoss Link."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import REGULATION_MANUAL
from .coordinator import DanfossLinkConfigEntry, DanfossLinkCoordinator
from .entity import DanfossLinkEntity

PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant,
    entry: DanfossLinkConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    async_add_entities(
        DanfossLinkOnOffSwitch(coordinator, device_id) for device_id in coordinator.data.devices
    )


class DanfossLinkOnOffSwitch(DanfossLinkEntity, SwitchEntity):
    """A relay controlled by the CC.

    Switching it from Home Assistant puts the device into manual regulation,
    like the app does when you toggle it.
    """

    def __init__(self, coordinator: DanfossLinkCoordinator, device_id: int) -> None:
        super().__init__(coordinator, f"onoff_{device_id}")
        self._device_id = device_id
        self._attr_name = coordinator.data.devices[device_id].name

    @property
    def _device(self):
        return self.coordinator.data.devices.get(self._device_id)

    @property
    def available(self) -> bool:
        return super().available and self._device is not None

    @property
    def is_on(self) -> bool:
        return self._device.state == "On"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "regulation": self._device.regulation,
            "activity_simulation": self._device.activity_simulation,
        }

    async def _set(self, state: str) -> None:
        await self.coordinator.async_write(
            self.coordinator.api.set_onoff_device(
                self._device_id, regulation=REGULATION_MANUAL, state=state
            )
        )

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._set("On")

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._set("Off")
