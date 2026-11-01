"""Base entity for Danfoss Link."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_PEER_ID, DOMAIN
from .coordinator import DanfossLinkCoordinator


class DanfossLinkEntity(CoordinatorEntity[DanfossLinkCoordinator]):
    """Entity attached to the Danfoss Link CC device."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: DanfossLinkCoordinator, key: str) -> None:
        super().__init__(coordinator)
        peer_id = coordinator.config_entry.data[CONF_PEER_ID]
        self._peer_id = peer_id
        self._attr_unique_id = f"{peer_id}_{key}"
        info = coordinator.data.info
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, peer_id)},
            manufacturer="Danfoss",
            model="Danfoss Link CC",
            name=info.name,
            sw_version=info.version,
            serial_number=info.serial_no,
        )


class DanfossLinkRoomEntity(DanfossLinkEntity):
    """Entity belonging to one room; each room is its own device (for areas)."""

    def __init__(self, coordinator: DanfossLinkCoordinator, room_id: int, key: str) -> None:
        super().__init__(coordinator, f"room_{room_id}_{key}")
        self._room_id = room_id
        room = coordinator.data.rooms[room_id]
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{self._peer_id}_room_{room_id}")},
            manufacturer="Danfoss",
            model="Danfoss Link room",
            name=room.name,
            via_device=(DOMAIN, self._peer_id),
        )

    @property
    def room(self):
        return self.coordinator.data.rooms.get(self._room_id)

    @property
    def available(self) -> bool:
        return super().available and self.room is not None
