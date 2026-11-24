"""Base entity class for Inotherm Inosmart BLE entities."""

from __future__ import annotations

from homeassistant.helpers.device_registry import CONNECTION_BLUETOOTH, DeviceInfo
from homeassistant.helpers.entity import Entity

from .const import DOMAIN
from .coordinator import InosmartDoorManager


class InosmartEntity(Entity):
    """Shared base entity providing DeviceInfo and snapshot updates."""

    _attr_has_entity_name = True

    def __init__(self, manager: InosmartDoorManager, key: str) -> None:
        self._manager = manager
        self._attr_unique_id = f"{manager.address}_{key}"
        dev_info = manager.snapshot.device_info
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, manager.address)},
            connections={(CONNECTION_BLUETOOTH, manager.address)},
            name=manager.name,
            manufacturer="Inotherm",
            model="Inosmart BT 640",
            sw_version=dev_info.main_fw_version if dev_info else None,
        )

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            self._manager.async_add_listener(self.async_write_ha_state)
        )
