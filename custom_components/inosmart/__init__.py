"""Home Assistant integration for Inotherm Inosmart (BT 640) via BLE."""

from __future__ import annotations

from typing import Optional
import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ADDRESS, Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv

from .const import (
    ATTR_ENTRY_ID,
    ATTR_HLED,
    ATTR_IO_MASK,
    ATTR_MINUTES,
    ATTR_SLED,
    ATTR_TASK,
    CONF_ADMIN_PASSWORD,
    CONF_DOOR_NAME,
    CONF_IMEI,
    DOMAIN,
    SERVICE_PAIR,
    SERVICE_SET_FREE_ENTRANCE,
    SERVICE_SET_LED,
    SERVICE_SYNC_CLOCK,
    SERVICE_UNLOCK,
)
from .coordinator import InosmartDoorManager
from .protocol import BleRTask, FreeEntranceIO

PLATFORMS: list[Platform] = [
    Platform.LOCK,
    Platform.BUTTON,
    Platform.NUMBER,
    Platform.SENSOR,
]


def _get_manager(hass: HomeAssistant, entry_id: Optional[str]) -> InosmartDoorManager:
    managers: dict[str, InosmartDoorManager] = hass.data.get(DOMAIN, {})
    if not managers:
        raise HomeAssistantError("Keine Inosmart-Tür konfiguriert.")
    if entry_id:
        if entry_id not in managers:
            raise HomeAssistantError(f"Unbekannte entry_id: {entry_id}")
        return managers[entry_id]
    return next(iter(managers.values()))


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Inosmart from a config entry."""
    manager = InosmartDoorManager(
        hass=hass,
        address=entry.data[CONF_ADDRESS],
        imei=entry.data[CONF_IMEI],
        name=entry.data.get(CONF_DOOR_NAME, entry.title),
        admin_password=entry.data.get(CONF_ADMIN_PASSWORD, ""),
    )
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = manager

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    if not hass.services.has_service(DOMAIN, SERVICE_UNLOCK):
        async def _svc_unlock(call: ServiceCall) -> None:
            mgr = _get_manager(hass, call.data.get(ATTR_ENTRY_ID))
            task_val = int(call.data.get(ATTR_TASK, int(BleRTask.MAIN_KEYLOCK_OPEN)))
            await mgr.async_unlock(BleRTask(task_val))

        async def _svc_pair(call: ServiceCall) -> None:
            mgr = _get_manager(hass, call.data.get(ATTR_ENTRY_ID))
            await mgr.async_pair()

        async def _svc_set_led(call: ServiceCall) -> None:
            mgr = _get_manager(hass, call.data.get(ATTR_ENTRY_ID))
            await mgr.async_set_leds(
                hled=call.data.get(ATTR_HLED),
                sled=call.data.get(ATTR_SLED),
            )

        async def _svc_free_entrance(call: ServiceCall) -> None:
            mgr = _get_manager(hass, call.data.get(ATTR_ENTRY_ID))
            await mgr.async_set_free_entrance(
                minutes=int(call.data[ATTR_MINUTES]),
                io_mask=int(call.data.get(ATTR_IO_MASK, int(FreeEntranceIO.MAIN_DOOR_RELAY))),
            )

        async def _svc_sync_clock(call: ServiceCall) -> None:
            mgr = _get_manager(hass, call.data.get(ATTR_ENTRY_ID))
            await mgr.async_sync_clock()

        hass.services.async_register(
            DOMAIN,
            SERVICE_UNLOCK,
            _svc_unlock,
            schema=vol.Schema(
                {
                    vol.Optional(ATTR_ENTRY_ID): cv.string,
                    vol.Optional(ATTR_TASK, default=int(BleRTask.MAIN_KEYLOCK_OPEN)): vol.Coerce(int),
                }
            ),
        )
        hass.services.async_register(
            DOMAIN,
            SERVICE_PAIR,
            _svc_pair,
            schema=vol.Schema({vol.Optional(ATTR_ENTRY_ID): cv.string}),
        )
        hass.services.async_register(
            DOMAIN,
            SERVICE_SET_LED,
            _svc_set_led,
            schema=vol.Schema(
                {
                    vol.Optional(ATTR_ENTRY_ID): cv.string,
                    vol.Optional(ATTR_HLED): vol.All(vol.Coerce(int), vol.Range(min=0, max=60)),
                    vol.Optional(ATTR_SLED): vol.All(vol.Coerce(int), vol.Range(min=0, max=75)),
                }
            ),
        )
        hass.services.async_register(
            DOMAIN,
            SERVICE_SET_FREE_ENTRANCE,
            _svc_free_entrance,
            schema=vol.Schema(
                {
                    vol.Optional(ATTR_ENTRY_ID): cv.string,
                    vol.Required(ATTR_MINUTES): vol.All(vol.Coerce(int), vol.Range(min=0, max=1440)),
                    vol.Optional(ATTR_IO_MASK, default=int(FreeEntranceIO.MAIN_DOOR_RELAY)): vol.Coerce(int),
                }
            ),
        )
        hass.services.async_register(
            DOMAIN,
            SERVICE_SYNC_CLOCK,
            _svc_sync_clock,
            schema=vol.Schema({vol.Optional(ATTR_ENTRY_ID): cv.string}),
        )

    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id, None)
        if not hass.data[DOMAIN]:
            for svc in (
                SERVICE_UNLOCK,
                SERVICE_PAIR,
                SERVICE_SET_LED,
                SERVICE_SET_FREE_ENTRANCE,
                SERVICE_SYNC_CLOCK,
            ):
                hass.services.async_remove(DOMAIN, svc)
    return unload_ok
