"""Lock platform for Inotherm Inosmart BLE."""

from __future__ import annotations

import asyncio
from typing import Any, Optional

from homeassistant.components.lock import LockEntity, LockEntityFeature
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import InosmartDoorManager
from .entity import InosmartEntity
from .protocol import BleRTask


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    manager: InosmartDoorManager = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([InosmartDoorLock(manager)])


class InosmartDoorLock(InosmartEntity, LockEntity):
    """Represents the Inotherm front door lock controlled via BLE."""

    _attr_name = None
    _attr_supported_features = LockEntityFeature.OPEN

    def __init__(self, manager: InosmartDoorManager) -> None:
        super().__init__(manager, "main_lock")
        self._attr_is_locked = True
        self._relock_task: Optional[asyncio.Task[None]] = None

    async def _pulse_open(self, task: BleRTask) -> None:
        self._attr_is_unlocking = True
        self.async_write_ha_state()
        try:
            await self._manager.async_unlock(task)
            self._attr_is_locked = False
        finally:
            self._attr_is_unlocking = False
            self.async_write_ha_state()

        if self._relock_task and not self._relock_task.done():
            self._relock_task.cancel()

        async def _reset_locked_state() -> None:
            await asyncio.sleep(3.0)
            self._attr_is_locked = True
            self.async_write_ha_state()

        self._relock_task = self.hass.async_create_task(_reset_locked_state())

    async def async_unlock(self, **kwargs: Any) -> None:
        """Open/unlock the main door motor lock."""
        await self._pulse_open(BleRTask.MAIN_KEYLOCK_OPEN)

    async def async_open(self, **kwargs: Any) -> None:
        """Open the main door lock (retract latch)."""
        await self._pulse_open(BleRTask.MAIN_KEYLOCK_OPEN)

    async def async_lock(self, **kwargs: Any) -> None:
        """Report locked state (motor lock auto-locks on door close)."""
        self._attr_is_locked = True
        self.async_write_ha_state()
