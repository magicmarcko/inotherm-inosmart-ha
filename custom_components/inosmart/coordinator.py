"""Coordinator / BLE connection manager for an Inotherm Inosmart door."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import logging
from typing import Any, Callable, Coroutine, Optional, TypeVar

from bleak import BleakClient
from bleak.exc import BleakError
from bleak_retry_connector import establish_connection
from homeassistant.components import bluetooth
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from .client import (
    InosmartBleClient,
    InosmartDeniedError,
    InosmartError,
    InosmartNotPairingModeError,
)
from .protocol import (
    BleRTask,
    DeviceInfoResponse,
    FreeEntranceIO,
    SysClockResponse,
    SystemInfoResponse,
)

_LOGGER = logging.getLogger(__name__)
_T = TypeVar("_T")


@dataclass
class InosmartSnapshot:
    """Cached telemetry read from the door."""

    device_info: Optional[DeviceInfoResponse] = None
    system_info: Optional[SystemInfoResponse] = None
    sys_clock: Optional[SysClockResponse] = None
    last_task_status: str = "ready"


class InosmartDoorManager:
    """Serializes BLE GATT sessions to the Inosmart door via HA Bluetooth / ESPHome Proxy."""

    def __init__(
        self,
        hass: HomeAssistant,
        address: str,
        imei: str,
        name: str,
        admin_password: str = "",
    ) -> None:
        self.hass = hass
        self.address = address.upper()
        self.imei = imei
        self.name = name
        self.admin_password = admin_password
        self.snapshot = InosmartSnapshot()
        self._lock = asyncio.Lock()
        self._listeners: list[Callable[[], None]] = []

    def async_add_listener(self, cb: Callable[[], None]) -> Callable[[], None]:
        self._listeners.append(cb)

        def _remove() -> None:
            if cb in self._listeners:
                self._listeners.remove(cb)

        return _remove

    def _notify_listeners(self) -> None:
        for cb in list(self._listeners):
            try:
                cb()
            except Exception:
                _LOGGER.exception("Error in Inosmart entity listener")

    def _resolve_ble_device(self):
        ble_device = bluetooth.async_ble_device_from_address(
            self.hass, self.address, connectable=True
        )
        if ble_device is None:
            raise HomeAssistantError(
                f"Inosmart Tür ({self.address}) ist per Bluetooth nicht erreichbar "
                "(Bluetooth-Adapter oder ESPHome Bluetooth-Proxy prüfen)."
            )
        return ble_device

    async def _run_session(
        self,
        func: Callable[[InosmartBleClient], Coroutine[Any, Any, _T]],
    ) -> _T:
        async with self._lock:
            ble_device = self._resolve_ble_device()

            async def _connect(dev: Any) -> BleakClient:
                return await establish_connection(
                    BleakClient,
                    dev,
                    self.name or self.address,
                    max_attempts=3,
                )

            client = InosmartBleClient(
                ble_device,
                self.imei,
                connector=_connect,
                timeout=12.0,
            )
            try:
                await client.connect()
                result = await func(client)
                self._notify_listeners()
                return result
            except InosmartNotPairingModeError as err:
                raise HomeAssistantError(
                    "Tür ist nicht im Kopplungsmodus: Taste 1 + Admin-Finger drücken, bis beide LEDs weiß leuchten."
                ) from err
            except InosmartDeniedError as err:
                raise HomeAssistantError(f"Tür verweigert Befehl: {err}") from err
            except InosmartError as err:
                raise HomeAssistantError(f"Inosmart BLE-Fehler: {err}") from err
            except BleakError as err:
                raise HomeAssistantError(
                    f"Bluetooth-Verbindungsfehler zur Tür ({self.address}): {err}"
                ) from err
            finally:
                await client.disconnect()

    async def _ensure_admin_if_needed(self, client: InosmartBleClient) -> None:
        dev_info = await client.get_device_info()
        self.snapshot.device_info = dev_info
        if dev_info.requires_admin_session_on_ble:
            if not self.admin_password:
                raise InosmartDeniedError(
                    "Für diese Funktion wird das Admin-Passwort benötigt (plus Taste 3 / BT Demo-Modus an der Tür)."
                )
            await client.admin_login(self.admin_password)

    async def async_pair(self) -> None:
        async def _op(client: InosmartBleClient) -> None:
            await client.pair_phone()
            self.snapshot.last_task_status = "paired"

        await self._run_session(_op)

    async def async_unlock(self, task: BleRTask = BleRTask.MAIN_KEYLOCK_OPEN) -> None:
        async def _op(client: InosmartBleClient) -> None:
            await client.run_unlock_task(task)
            self.snapshot.last_task_status = task.name.lower()

        await self._run_session(_op)

    async def async_refresh_info(self, include_admin: bool = True) -> InosmartSnapshot:
        async def _op(client: InosmartBleClient) -> InosmartSnapshot:
            dev_info = await client.get_device_info()
            self.snapshot.device_info = dev_info
            if include_admin and self.admin_password:
                if dev_info.requires_admin_session_on_ble:
                    await client.admin_login(self.admin_password)
                self.snapshot.system_info = await client.get_system_info()
                try:
                    self.snapshot.sys_clock = await client.get_sys_clock()
                except Exception as err:
                    _LOGGER.debug("Clock read skipped: %s", err)
            self.snapshot.last_task_status = "synced"
            return self.snapshot

        return await self._run_session(_op)

    async def async_set_leds(
        self, *, hled: Optional[int] = None, sled: Optional[int] = None
    ) -> None:
        async def _op(client: InosmartBleClient) -> None:
            await self._ensure_admin_if_needed(client)
            await client.set_leds(hled=hled, sled=sled)
            self.snapshot.system_info = await client.get_system_info()
            self.snapshot.last_task_status = "led_updated"

        await self._run_session(_op)

    async def async_set_free_entrance(
        self,
        minutes: int,
        io_mask: int = int(FreeEntranceIO.MAIN_DOOR_RELAY),
    ) -> None:
        async def _op(client: InosmartBleClient) -> None:
            await self._ensure_admin_if_needed(client)
            await client.set_unlocked_entrance(minutes, io_mask)
            self.snapshot.last_task_status = "free_entrance_updated"

        await self._run_session(_op)

    async def async_sync_clock(self) -> None:
        async def _op(client: InosmartBleClient) -> None:
            await self._ensure_admin_if_needed(client)
            await client.set_sys_clock()
            self.snapshot.last_task_status = "clock_synced"

        await self._run_session(_op)
