"""Async BLE client for Inotherm Inosmart (BT 640) doors using bleak."""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass
import logging
from typing import Any, Awaitable, Callable, Deque, Dict, List, Optional

from .protocol import (
    HEADER_LENGTH,
    MAX_EXTERNAL_LED_POWER,
    MAX_INTEGRATED_LED_POWER,
    PAIR_NOT_READY_CODES,
    PAIR_SUCCESS_CODES,
    REQUESTED_MTU_SIZE,
    UUID_ADVERTISING_SERVICE,
    UUID_NOTIFY,
    UUID_SERVICE,
    UUID_WRITE,
    AckResponse,
    BleRTask,
    BleRUser,
    DeviceInfoResponse,
    FreeEntranceIO,
    LogDataResponse,
    LogEventType,
    MotorHandleInfoResponse,
    MotorHandleMask,
    MsgType,
    MsgsHeader,
    RLoginAccessResponse,
    RLoginTaskResponse,
    SysClockResponse,
    SysInfoMask,
    SystemInfoResponse,
    SystemStateResponse,
    UnlockedEntranceResponse,
    ble_key_gen,
    ble_key_unlock_gen,
    build_get_device_info,
    build_get_log_data,
    build_get_motor_handle_info,
    build_get_rlogin_access,
    build_get_sys_clock,
    build_get_system_info,
    build_get_system_state,
    build_get_unlocked_entrance,
    build_set_motor_handle_info,
    build_set_rlogin_task,
    build_set_sys_clock,
    build_set_system_info,
    build_set_unlocked_entrance,
    chunk_for_ble,
    encrypt_rc6_s1,
    parse_ack_response,
    parse_device_info_response,
    parse_log_data_response,
    parse_motor_handle_info_response,
    parse_rlogin_access_response,
    parse_rlogin_task_response,
    parse_sys_clock_response,
    parse_system_info_response,
    parse_system_state_response,
    parse_unlocked_entrance_response,
    split_response_packets,
    validate_imei,
)

_LOGGER = logging.getLogger(__name__)


class InosmartError(Exception):
    """Base error for Inosmart BLE communication."""


class InosmartDeniedError(InosmartError):
    """Door refused request (e.g. Admin mode not active or IMEI not paired)."""


class InosmartNotPairingModeError(InosmartError):
    """Door is not currently in Phone Pairing Mode (Button 1 + Admin finger)."""


class InosmartTimeoutError(InosmartError):
    """Timed out waiting for response packet from door."""


@dataclass
class DiscoveredDoor:
    name: str
    address: str
    rssi: Optional[int] = None


class InosmartBleClient:
    """Async BLE client implementing the Inosmart UART framing & auth flows."""

    def __init__(
        self,
        device_or_address: Any,
        imei: str,
        *,
        connector: Optional[Callable[[Any], Awaitable[Any]]] = None,
        timeout: float = 12.0,
    ) -> None:
        self._target = device_or_address
        self.imei = validate_imei(imei)
        self._connector = connector
        self.timeout = timeout

        self._client: Any = None
        self._rx_buf = bytearray()
        self._waiters: Dict[int, Deque[asyncio.Future[bytes]]] = {}
        self._session_counter = 1
        self._admin_session_token: str = ""
        self._mtu = REQUESTED_MTU_SIZE
        self._write_response: Optional[bool] = None
        self._io_lock = asyncio.Lock()

    @property
    def admin_session_token(self) -> str:
        return self._admin_session_token

    @property
    def is_connected(self) -> bool:
        return bool(self._client and getattr(self._client, "is_connected", False))

    async def __aenter__(self) -> "InosmartBleClient":
        await self.connect()
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.disconnect()

    async def connect(self) -> None:
        if self.is_connected:
            return
        try:
            from bleak import BleakClient  # type: ignore[import-not-found]
        except ImportError as exc:  # pragma: no cover
            raise InosmartError(
                "bleak is required for BLE communication (pip install bleak)"
            ) from exc

        if self._connector is not None:
            self._client = await self._connector(self._target)
        else:
            target = self._target
            if isinstance(target, str):
                from bleak import BleakScanner  # type: ignore[import-not-found]

                dev = await BleakScanner.find_device_by_address(target, timeout=min(self.timeout, 10.0))
                if dev is not None:
                    target = dev
            client = BleakClient(target, timeout=self.timeout)
            await client.connect()
            self._client = client

        # Negotiate MTU if supported (Android app requests 248)
        acquire_mtu = getattr(self._client, "_acquire_mtu", None)
        if callable(acquire_mtu):
            try:
                await acquire_mtu()
            except Exception as err:
                _LOGGER.debug("MTU acquisition skipped/failed: %s", err)
        mtu = getattr(self._client, "mtu_size", None)
        if isinstance(mtu, int) and mtu >= 23:
            self._mtu = mtu

        # Inspect write characteristic properties cleanly
        try:
            services = self._client.services
            char = services.get_characteristic(UUID_WRITE)
            if char is not None:
                props = {p.lower() for p in char.properties}
                if "write" in props:
                    self._write_response = True
                elif "write-without-response" in props:
                    self._write_response = False
        except Exception:
            self._write_response = None

        self._rx_buf.clear()
        self._admin_session_token = ""
        await self._client.start_notify(UUID_NOTIFY, self._on_notify)

    async def disconnect(self) -> None:
        self._admin_session_token = ""
        for q in self._waiters.values():
            while q:
                fut = q.popleft()
                if not fut.done():
                    fut.set_exception(InosmartError("Disconnected"))
        self._waiters.clear()
        if self._client is not None:
            try:
                if getattr(self._client, "is_connected", False):
                    try:
                        await self._client.stop_notify(UUID_NOTIFY)
                    except Exception:
                        pass
                    await self._client.disconnect()
            finally:
                self._client = None

    def _next_session_id(self) -> int:
        sid = self._session_counter
        self._session_counter = (self._session_counter + 1) & 0xFFFF
        if self._session_counter == 0:
            self._session_counter = 1
        return sid

    def _on_notify(self, _sender: Any, data: bytearray) -> None:
        self._rx_buf.extend(data)
        for pkt in split_response_packets(self._rx_buf):
            if len(pkt) < HEADER_LENGTH:
                continue
            hdr = MsgsHeader.decode(pkt)
            _LOGGER.debug(
                "RX msg_id=%d session=%d packet_idx=%d len=%d",
                hdr.message_id,
                hdr.session_id,
                hdr.packet_index,
                len(pkt),
            )
            queue = self._waiters.get(hdr.message_id)
            if queue:
                fut = queue.popleft()
                if not fut.done():
                    fut.set_result(pkt)

    async def _send_and_wait(
        self,
        packet: bytes,
        expected_msg_id: int,
        *,
        timeout: Optional[float] = None,
    ) -> bytes:
        async with self._io_lock:
            if not self.is_connected:
                await self.connect()
            assert self._client is not None

            loop = asyncio.get_running_loop()
            fut: asyncio.Future[bytes] = loop.create_future()
            self._waiters.setdefault(expected_msg_id, deque()).append(fut)

            try:
                use_response = (
                    self._write_response if self._write_response is not None else True
                )
                for chunk in chunk_for_ble(packet, self._mtu):
                    await self._client.write_gatt_char(
                        UUID_WRITE, chunk, response=use_response
                    )
                return await asyncio.wait_for(fut, timeout=timeout or self.timeout)
            except asyncio.TimeoutError as exc:
                queue = self._waiters.get(expected_msg_id)
                if queue and fut in queue:
                    queue.remove(fut)
                raise InosmartTimeoutError(
                    f"Timed out waiting for response to msg_id={expected_msg_id}"
                ) from exc

    # ------------------------------------------------------------------
    # Challenge-Response Flows (Pairing / Unlock / Admin)
    # ------------------------------------------------------------------

    async def request_rlogin_access(
        self, rtask: BleRTask, ruser: BleRUser = BleRUser.USER_PHONE
    ) -> RLoginAccessResponse:
        sid = self._next_session_id()
        req = build_get_rlogin_access(
            rtask,
            ruser=ruser,
            session_id=sid,
            admin_session_token=self._admin_session_token,
        )
        raw = await self._send_and_wait(req, MsgType.GET_RLOGIN_ACCESS)
        return parse_rlogin_access_response(raw)

    async def submit_rlogin_task(
        self, rtask: BleRTask, key_hex: str
    ) -> RLoginTaskResponse:
        sid = self._next_session_id()
        req = build_set_rlogin_task(
            rtask,
            key_hex,
            session_id=sid,
            admin_session_token=self._admin_session_token,
        )
        raw = await self._send_and_wait(req, MsgType.SET_RLOGIN_TASK)
        return parse_rlogin_task_response(raw)

    async def pair_phone(self) -> RLoginTaskResponse:
        """Pair client IMEI to door. Door must be in Phone Pairing Mode (white LEDs)."""
        access = await self.request_rlogin_access(BleRTask.LEARN_NEW_DEVICE)
        if access.is_denied:
            raise InosmartNotPairingModeError(
                "Door denied pairing request (press Button 1 + Admin finger on door first)."
            )
        derived_hex = encrypt_rc6_s1(self.imei, access.access_key_hex)
        resp = await self.submit_rlogin_task(BleRTask.LEARN_NEW_DEVICE, derived_hex)
        if resp.err in PAIR_NOT_READY_CODES:
            raise InosmartNotPairingModeError(
                f"Door is not in Phone Pairing Mode (err=0x{resp.err:02X})."
            )
        if resp.err not in PAIR_SUCCESS_CODES:
            raise InosmartError(f"Phone pairing failed (err=0x{resp.err:02X}).")
        return resp

    async def run_unlock_task(
        self, rtask: BleRTask = BleRTask.MAIN_KEYLOCK_OPEN
    ) -> RLoginTaskResponse:
        """Execute an authenticated unlock or relay task using enrolled IMEI."""
        access = await self.request_rlogin_access(rtask)
        if access.is_denied:
            raise InosmartDeniedError(
                f"Door denied task {rtask.name} (check Bluetooth Türöffner setting & IMEI pairing)."
            )
        derived_hex = ble_key_unlock_gen(access.access_key_hex, self.imei)
        resp = await self.submit_rlogin_task(rtask, derived_hex)
        if not resp.success:
            raise InosmartDeniedError(
                f"Door rejected {rtask.name} with err=0x{resp.err:02X}."
            )
        return resp

    async def unlock(self) -> RLoginTaskResponse:
        return await self.run_unlock_task(BleRTask.MAIN_KEYLOCK_OPEN)

    async def ready_to_open(self) -> RLoginTaskResponse:
        return await self.run_unlock_task(BleRTask.MAIN_KEYLOCK_READY2OPEN)

    async def open_relay2(self) -> RLoginTaskResponse:
        return await self.run_unlock_task(BleRTask.RELAY2_OPEN)

    async def open_gw_relay1(self) -> RLoginTaskResponse:
        return await self.run_unlock_task(BleRTask.GW_RELAY1_OPEN)

    async def open_gw_relay2(self) -> RLoginTaskResponse:
        return await self.run_unlock_task(BleRTask.GW_RELAY2_OPEN)

    async def admin_login(self, password: str) -> str:
        """Authenticate as Admin (requires Button 3 + Admin finger / blue LED or BT Demo Mode)."""
        access = await self.request_rlogin_access(BleRTask.ADMINISTRATION)
        if access.is_denied:
            raise InosmartDeniedError(
                "BLE Admin Mode is disabled on the door (press Button 3 + Admin finger until LED is blue)."
            )
        ct_hex, session_token = ble_key_gen(password, access.access_key_hex)
        resp = await self.submit_rlogin_task(BleRTask.ADMINISTRATION, ct_hex)
        if resp.err != 0:
            raise InosmartDeniedError(
                f"Admin login failed (wrong password or door not in Admin mode, err=0x{resp.err:02X})."
            )
        self._admin_session_token = session_token
        return session_token

    # ------------------------------------------------------------------
    # Device, System, LED, Clock, Free Entrance & Logs
    # ------------------------------------------------------------------

    async def get_device_info(self) -> DeviceInfoResponse:
        sid = self._next_session_id()
        raw = await self._send_and_wait(
            build_get_device_info(
                session_id=sid, admin_session_token=self._admin_session_token
            ),
            MsgType.GET_DEVICE_INFO,
        )
        return parse_device_info_response(raw)

    async def get_system_info(self) -> SystemInfoResponse:
        sid = self._next_session_id()
        raw = await self._send_and_wait(
            build_get_system_info(
                session_id=sid, admin_session_token=self._admin_session_token
            ),
            MsgType.GET_SYSTEM_INFO,
        )
        return parse_system_info_response(raw)

    async def get_sys_clock(self) -> SysClockResponse:
        sid = self._next_session_id()
        raw = await self._send_and_wait(
            build_get_sys_clock(
                session_id=sid, admin_session_token=self._admin_session_token
            ),
            MsgType.GET_SYS_CLOCK,
        )
        return parse_sys_clock_response(raw)

    async def get_system_state(self) -> SystemStateResponse:
        sid = self._next_session_id()
        raw = await self._send_and_wait(
            build_get_system_state(
                session_id=sid, admin_session_token=self._admin_session_token
            ),
            MsgType.GET_SYSTEM_STATE,
        )
        return parse_system_state_response(raw)

    async def set_leds(
        self, *, hled: Optional[int] = None, sled: Optional[int] = None
    ) -> AckResponse:
        """Update integrated handle LED (0..60) and/or external LED (0..75)."""
        if hled is None and sled is None:
            raise ValueError("Provide at least one of hled or sled")
        current = await self.get_system_info()
        mask = 0
        new_hled = current.hled
        new_sled = current.sled
        if hled is not None:
            new_hled = max(0, min(MAX_INTEGRATED_LED_POWER, int(hled)))
            mask |= int(SysInfoMask.HLED)
        if sled is not None:
            new_sled = max(0, min(MAX_EXTERNAL_LED_POWER, int(sled)))
            mask |= int(SysInfoMask.SLED)

        sid = self._next_session_id()
        req = build_set_system_info(
            mask,
            ip="192.168.5.1",
            port=55555,
            encryption=current.encryption,
            ssid="",
            pwd="",
            hled=new_hled,
            sled=new_sled,
            io_locker=0,
            io_user_cs=0,
            key_length=current.key_len,
            bell_io=0,
            sensor_io=0,
            admin_imei_hex="0000000000000000",
            session_id=sid,
            admin_session_token=self._admin_session_token,
        )
        raw = await self._send_and_wait(req, MsgType.SET_SYSTEM_INFO)
        return parse_ack_response(raw)

    async def get_motor_handle_info(self) -> MotorHandleInfoResponse:
        sid = self._next_session_id()
        raw = await self._send_and_wait(
            build_get_motor_handle_info(
                session_id=sid, admin_session_token=self._admin_session_token
            ),
            MsgType.GET_MOTOR_HANDLE_INFO,
        )
        return parse_motor_handle_info_response(raw)

    async def set_motor_handle_leds(self, led1: int, led2: int) -> AckResponse:
        sid = self._next_session_id()
        req = build_set_motor_handle_info(
            int(MotorHandleMask.DATA_LED),
            led1=led1,
            led2=led2,
            session_id=sid,
            admin_session_token=self._admin_session_token,
        )
        raw = await self._send_and_wait(req, MsgType.SET_MOTOR_HANDLE_INFO)
        return parse_ack_response(raw)

    async def get_unlocked_entrance(self) -> UnlockedEntranceResponse:
        sid = self._next_session_id()
        raw = await self._send_and_wait(
            build_get_unlocked_entrance(
                session_id=sid, admin_session_token=self._admin_session_token
            ),
            MsgType.GET_UNLOCKED_ENTRANCE,
        )
        return parse_unlocked_entrance_response(raw)

    async def set_unlocked_entrance(
        self, minutes: int, io_mask: int = int(FreeEntranceIO.MAIN_DOOR_RELAY)
    ) -> AckResponse:
        sid = self._next_session_id()
        req = build_set_unlocked_entrance(
            minutes,
            io_mask,
            session_id=sid,
            admin_session_token=self._admin_session_token,
        )
        raw = await self._send_and_wait(req, MsgType.SET_UNLOCKED_ENTRANCE)
        return parse_ack_response(raw)

    async def get_logs(
        self, count: int = 20, event_type: int = int(LogEventType.ALL)
    ) -> LogDataResponse:
        sid = self._next_session_id()
        req = build_get_log_data(
            event_type=event_type,
            count=count,
            session_id=sid,
            admin_session_token=self._admin_session_token,
        )
        raw = await self._send_and_wait(req, MsgType.GET_LOG_DATA)
        return parse_log_data_response(raw)

    async def set_sys_clock(self, timestamp_sec: Optional[int] = None) -> bytes:
        sid = self._next_session_id()
        req = build_set_sys_clock(
            timestamp_sec=timestamp_sec,
            session_id=sid,
            admin_session_token=self._admin_session_token,
        )
        return await self._send_and_wait(req, MsgType.SET_SYS_CLOCK)


async def discover_inosmart_doors(timeout: float = 6.0) -> List[DiscoveredDoor]:
    """Scan for Inosmart BLE peripherals matching advertising/UART service UUIDs."""
    from bleak import BleakScanner  # type: ignore[import-not-found]

    discovered = await BleakScanner.discover(timeout=timeout, return_adv=True)
    results: List[DiscoveredDoor] = []
    target_uuids = {UUID_ADVERTISING_SERVICE.lower(), UUID_SERVICE.lower()}
    for dev, adv in discovered.values():
        uuids = {u.lower() for u in (adv.service_uuids or [])}
        name = dev.name or adv.local_name or ""
        if (uuids & target_uuids) or name.upper().startswith(("INO", "IN0")):
            results.append(
                DiscoveredDoor(
                    name=name or "Inosmart",
                    address=dev.address,
                    rssi=getattr(adv, "rssi", None),
                )
            )
    return results
