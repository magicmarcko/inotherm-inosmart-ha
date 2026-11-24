"""Pure-Python implementation of the Inotherm Inosmart BLE protocol & crypto.

Derived directly from XF_InoSmart.dll CIL (v4.00.59):
- C_MsgsManager / C_MsgsHeader framing & multi-packet splitting
- RC6-32/20/16-ECB cipher (including Encrypt vs Encrypt16 behavior)
- BleKeyUnlockGen (door unlock & relays), EncryptRC6_S1 (phone pairing), BLEKeyGen (admin session)
- Request builders and response parsers
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import IntEnum, IntFlag
import hashlib
import os
import secrets
from typing import Iterable, List, Optional, Tuple


# ---------------------------------------------------------------------------
# BLE GATT UUIDs & Constants
# ---------------------------------------------------------------------------

UUID_ADVERTISING_SERVICE = "51071364-4456-4a4e-9b6f-46708d4c1ea4"
UUID_SERVICE = "6e400001-b5a3-f393-e0a9-e50e24dcca9e"
UUID_WRITE = "6e400002-b5a3-f393-e0a9-e50e24dcca9e"
UUID_NOTIFY = "6e400003-b5a3-f393-e0a9-e50e24dcca9e"

UUID_TOUCH_MODULE_SERVICE = "51071364-4456-4a4e-9b6f-46708d4c1eb0"
UUID_TOUCH_MODULE_ADV = "64130751-5644-4e4a-9b6f-46708d4c1eb0"

REQUESTED_MTU_SIZE = 248
HEADER_LENGTH = 16
HEADER_LENGTH_NETTO = 12

MAX_INTEGRATED_LED_POWER = 60
DEFAULT_INTEGRATED_LED_POWER = 20
MAX_EXTERNAL_LED_POWER = 75
DEFAULT_EXTERNAL_LED_POWER = 12


# ---------------------------------------------------------------------------
# Protocol Enums (XF_InoSmart.Ino)
# ---------------------------------------------------------------------------


class MsgBus(IntEnum):
    IO = 0
    GW = 4


class MsgType(IntEnum):
    GET_SYS_CLOCK = 1
    SET_SYS_CLOCK_ENUM = 2
    SET_SYS_CLOCK = 4  # C_SetSysClock_Request sets header.message_id = 4
    SET_NFC_NEW = 8
    GET_SYSTEM_INFO = 15
    SET_SYSTEM_INFO = 16
    GET_DEVICE_INFO = 17
    GET_SYSTEM_STATE = 19
    GET_LOG_DATA = 24
    SET_UI_INFO = 25
    GET_UI_INFO = 26
    SET_UNLOCKED_ENTRANCE = 27
    GET_UNLOCKED_ENTRANCE = 28
    SET_IO_RELAY = 29
    SET_LOGIN_INFO = 30
    GET_RLOGIN_ACCESS = 35
    SET_RLOGIN_TASK = 36
    SET_MOTOR_HANDLE_INFO = 44
    GET_MOTOR_HANDLE_INFO = 45


class BleRTask(IntEnum):
    MAIN_KEYLOCK_OPEN = 0
    MAIN_KEYLOCK_READY2OPEN = 1
    RELAY2_OPEN = 2
    GW_RELAY1_OPEN = 3
    GW_RELAY2_OPEN = 4
    GW_RELAY1_RELAY2_OPEN = 5
    LEARN_NEW_DEVICE = 160  # 0xA0 - Button 1 + Admin finger (white LEDs)
    ADMINISTRATION = 161    # 0xA1 - Button 3 + Admin finger (blue LED) or Demo Mode
    DELETE = 162            # 0xA2
    DONGLE = 176            # 0xB0


class BleRUser(IntEnum):
    USER_PHONE = 0
    USER_DONGLE = 1


class BleSysInfo(IntFlag):
    NONE = 0
    PHONE_ADD_PROGRESS = 1
    PHONE_DELETE_PROGRESS = 2
    ADMIN_ALREADY_ACTIVE = 4096
    DENIED = 0x80000000


class SysInfoMask(IntFlag):
    IP = 1
    PORT = 2
    ENCRYPTION = 4
    SSID = 8
    PWD = 16
    HLED = 32
    USER_RELAY_TIME = 64
    ADMIN_IMEI = 128
    LOCKER_SIGNAL_TIME = 256
    SLED = 512
    KEYCODE_LENGTH = 1024
    IO_BELL_SETTINGS = 2048
    IO_SENSOR_INPUT = 4096
    RESET_MAIN_MODUL = 8192
    RESET_IO_MODUL = 16384
    FACTORY_DEFAULT = 32768


class MotorHandleMask(IntFlag):
    DATA_LED = 1
    DATA_M_STEPS = 2
    RESERVED = 4
    DATA_CLOSE_OFFSET = 8
    DATA_CLOSE_DELAY_MS = 16
    FACTORY_RESET = 32768


class FreeEntranceIO(IntFlag):
    NONE = 0
    MAIN_DOOR_RELAY = 1
    USER_RELAY = 4
    GATEWAY_RELAY1 = 16
    GATEWAY_RELAY2 = 32


class DataPeripheral(IntFlag):
    NONE = 0x00
    NFC = 0x01
    TOUCH = 0x02
    SI_BUS = 0x04
    GATEWAY = 0x08
    MOTOR_HANDLE = 0x10


class Models(IntEnum):
    UNDEFINED = 0
    MODEL_K_LED = 16
    MODEL_K = 17
    MODEL_K_FLEX_LED = 18
    MODEL_K_FLEX = 19
    MODEL_R_LED = 48
    MODEL_R = 49
    MODEL_R_FLEX_LED = 50
    MODEL_R_FLEX = 51
    MODEL_RS_LED = 80
    MODEL_RS = 81


class ModelsIO(IntEnum):
    UNDEFINED = 0
    MODEL_IO_WIFI = 16
    MODEL_IO_WIFI_BELL = 17
    MODEL_IO_BT = 18
    MODEL_IO_BT_V2 = 19
    MODEL_IO_BT_SOLO = 32


class SysInfoIoBell(IntEnum):
    WIFI_BELL = 0
    USER_RELAY = 1
    USER_RELAY_AND_BELL = 2


class SysInfoIoSensor(IntEnum):
    LOCK = 0
    RELAY = 1
    LOCK_AND_RELAY = 2
    WIFI_BELL = 3


class LogEventSource(IntEnum):
    IO_MODUL = 0
    APP = 1
    TIMER = 2
    NONE = 255


class LogEventType(IntEnum):
    ALL = 0
    AUTH_ADMIN = 1
    AUTH_FP = 2
    AUTH_NFC = 3
    AUTH_KEYCODE = 4
    AUTH_ADMIN_FAILED = 5
    AUTH_USER_FP_FAILED = 6
    AUTH_USER_NFC_FAILED = 7
    AUTH_USER_KEYCODE_FAILED = 8
    DEVICE_LOCKED = 9
    OPEN_MAIN_DOOR = 10
    OPEN_SECOND_RELAY = 11
    OPEN_MAINDOOR_SECOND_RELAY = 12
    OPEN_SENSOR_SIGNAL = 13
    GATEWAY_RELAY = 14
    ADD_FP = 20
    ADD_NFC = 21
    ADD_KEYCODE = 22
    ADD_ADMIN = 23
    ADD_USER = 24
    EDIT_USERNAME = 30
    DELETE_USER = 31
    DELETE_FP = 32
    DELETE_NFC = 33
    DELETE_KEYCODE = 34
    USER_ENABLED = 40
    USER_DISABLED = 41
    FP_IO_CHANGED = 42
    NFC_IO_CHANGED = 43
    KEYCODE_IO_CHANGED = 44
    FP_ASSIGN_USER = 45
    NFC_ASSIGN_USER = 46
    KEYCODE_ASSIGN_USER = 47
    TIMER_ADDED = 50
    TIMER_DELETED = 51
    TIMER_ENABLED = 52
    TIMER_DISABLED = 53
    TIMER_UPDATED = 54
    SYSTEM_SETTINGS_UPDATE = 60
    AUTH_FP_FAILED = 80
    AUTH_NFC_FAILED = 81
    AUTH_KEYCODE_FAILED = 82
    ERROR_GENERAL = 101
    ERROR_IO_MODUL = 102
    ERROR_MAIN_MODUL = 103
    ERROR_FP_SENSOR = 104
    ERROR_PAIRING = 105
    ADD_ADMIN_ERROR = 106
    ERROR_FPC_ERROR = 107
    ERROR_NDEF_ERROR = 108
    DEVICE_REBOOT = 200
    FACTORY_RESET = 201
    PAIRING_SUCCESS = 202
    WIFI_ENABLED = 203
    WIFI_DISABLED = 204
    FREE_ENTRANCE_ON = 205
    FREE_ENTRANCE_OFF = 206
    DB_CORRUPTED_FACTORY_RESET = 207
    BT_ENABLED = 208
    BT_DISABLED = 209


# Pairing / RLogin result status codes observed in BleDevicePairPopup & DataController
PAIR_SUCCESS_CODES = frozenset({0x00, 0x17})
PAIR_NOT_READY_CODES = frozenset({0x21, 0x24, 0xFF})


# ---------------------------------------------------------------------------
# Helpers & IMEI Generation
# ---------------------------------------------------------------------------


def generate_synthetic_imei() -> str:
    """Generate a valid 15-digit IMEI string with no leading zeros."""
    first = str(secrets.randbelow(9) + 1)  # 1..9 so Int64 keeps all 15 digits
    rest = "".join(str(secrets.randbelow(10)) for _ in range(14))
    return first + rest


def validate_imei(imei: str) -> str:
    """Validate/normalize a 15-digit numeric IMEI."""
    cleaned = imei.strip()
    if len(cleaned) != 15 or not cleaned.isdigit():
        raise ValueError(f"IMEI must be a 15-digit numeric string, got {imei!r}")
    return cleaned


def replace_leading_zeros(value: str, replacement: str = "1") -> str:
    """Match XF_InoSmart.Common.Rutine.ReplaceLeadingZeros."""
    if not value:
        return value
    stripped = value.lstrip("0")
    return stripped.rjust(len(value), replacement[0])


def int_to_hex(value: int, byte_len: int) -> str:
    """Big-endian uppercase hex string of fixed byte length (masked)."""
    mask = (1 << (byte_len * 8)) - 1
    return f"{value & mask:0{byte_len * 2}X}"


def pad_packet(packet: bytes) -> bytes:
    """Match Rutine.DataPaddingString: pad with 0x00 bytes to a multiple of 4 bytes."""
    rem = len(packet) % 4
    if rem == 0:
        return packet
    return packet + (b"\x00" * (4 - rem))


def format_version(raw: bytes) -> str:
    """Match DataController.VersionFormat for 4-byte firmware versions."""
    if len(raw) != 4:
        return ""
    major = raw[0]
    minor = raw[1]
    build = int.from_bytes(raw[2:4], "big")
    return f"{major}.{minor}.{build}"


def ip_to_bytes(ip: str) -> bytes:
    parts = [int(x) for x in ip.split(".")]
    if len(parts) != 4 or any(p < 0 or p > 255 for p in parts):
        raise ValueError(f"Invalid IPv4 address: {ip!r}")
    return bytes(parts)


# ---------------------------------------------------------------------------
# RC6-32/20/16-ECB (XF_InoSmart.Common.RC6)
# ---------------------------------------------------------------------------


def _rotl32(val: int, shift: int) -> int:
    s = shift & 31
    return ((val << s) | (val >> (32 - s))) & 0xFFFFFFFF


def _rotr32(val: int, shift: int) -> int:
    s = shift & 31
    return ((val >> s) | (val << (32 - s))) & 0xFFFFFFFF


class RC6:
    """Exact port of XF_InoSmart.Common.RC6 (w=32, r=20, ISO padding)."""

    W = 32
    R = 20
    PW = 0xB7E15163
    QW = 0x9E3779B9

    def __init__(self, key: bytes) -> None:
        self._s = self._generate_key(key)

    @classmethod
    def _generate_key(cls, key: bytes) -> List[int]:
        if len(key) % 4 != 0:
            raise ValueError(f"RC6 key length must be a multiple of 4 bytes, got {len(key)}")
        c = max(len(key) // 4, 1)
        l_words = [
            int.from_bytes(key[i * 4 : (i + 1) * 4], "little")
            for i in range(c)
        ]
        s_len = 2 * cls.R + 4  # 44
        s = [0] * s_len
        s[0] = cls.PW
        for i in range(1, s_len):
            s[i] = (s[i - 1] + cls.QW) & 0xFFFFFFFF

        a = b = i = j = 0
        v = 3 * max(c, s_len)
        for _ in range(v):
            a = s[i] = _rotl32((s[i] + a + b) & 0xFFFFFFFF, 3)
            b = l_words[j] = _rotl32((l_words[j] + a + b) & 0xFFFFFFFF, (a + b) & 31)
            i = (i + 1) % s_len
            j = (j + 1) % c
        return s

    def encrypt_block(self, block: bytes) -> bytes:
        if len(block) != 16:
            raise ValueError("RC6 block must be 16 bytes")
        s = self._s
        a = int.from_bytes(block[0:4], "little")
        b = int.from_bytes(block[4:8], "little")
        c = int.from_bytes(block[8:12], "little")
        d = int.from_bytes(block[12:16], "little")

        b = (b + s[0]) & 0xFFFFFFFF
        d = (d + s[1]) & 0xFFFFFFFF
        for i in range(1, self.R + 1):
            t = _rotl32((b * ((2 * b + 1) & 0xFFFFFFFF)) & 0xFFFFFFFF, 5)
            u = _rotl32((d * ((2 * d + 1) & 0xFFFFFFFF)) & 0xFFFFFFFF, 5)
            a = (_rotl32(a ^ t, u) + s[2 * i]) & 0xFFFFFFFF
            c = (_rotl32(c ^ u, t) + s[2 * i + 1]) & 0xFFFFFFFF
            a, b, c, d = b, c, d, a
        a = (a + s[2 * self.R + 2]) & 0xFFFFFFFF
        c = (c + s[2 * self.R + 3]) & 0xFFFFFFFF
        return b"".join(x.to_bytes(4, "little") for x in (a, b, c, d))

    def decrypt_block(self, block: bytes) -> bytes:
        if len(block) != 16:
            raise ValueError("RC6 block must be 16 bytes")
        s = self._s
        a = int.from_bytes(block[0:4], "little")
        b = int.from_bytes(block[4:8], "little")
        c = int.from_bytes(block[8:12], "little")
        d = int.from_bytes(block[12:16], "little")

        c = (c - s[2 * self.R + 3]) & 0xFFFFFFFF
        a = (a - s[2 * self.R + 2]) & 0xFFFFFFFF
        for i in range(self.R, 0, -1):
            a, b, c, d = d, a, b, c
            u = _rotl32((d * ((2 * d + 1) & 0xFFFFFFFF)) & 0xFFFFFFFF, 5)
            t = _rotl32((b * ((2 * b + 1) & 0xFFFFFFFF)) & 0xFFFFFFFF, 5)
            c = _rotr32((c - s[2 * i + 1]) & 0xFFFFFFFF, t) ^ u
            a = _rotr32((a - s[2 * i]) & 0xFFFFFFFF, u) ^ t
        d = (d - s[1]) & 0xFFFFFFFF
        b = (b - s[0]) & 0xFFFFFFFF
        return b"".join(x.to_bytes(4, "little") for x in (a, b, c, d))

    @staticmethod
    def pad_iso(data: bytes) -> bytes:
        """ISO/IEC 7816-4 padding (always adds 1..16 bytes: 0x80 then 0x00s)."""
        pad_len = 16 - (len(data) % 16)
        return data + b"\x80" + (b"\x00" * (pad_len - 1))

    @staticmethod
    def unpad_iso(padded: bytes) -> bytes:
        idx = padded.rfind(b"\x80")
        if idx == -1 or any(b != 0 for b in padded[idx + 1 :]):
            raise ValueError("Invalid RC6 ISO padding")
        return padded[:idx]

    @classmethod
    def encrypt(cls, data: bytes, key: bytes) -> bytes:
        """Port of RC6.Encrypt: ISO pad and ECB-encrypt all 16-byte blocks."""
        cipher = cls(key)
        padded = cls.pad_iso(data)
        return b"".join(
            cipher.encrypt_block(padded[i : i + 16])
            for i in range(0, len(padded), 16)
        )

    @classmethod
    def encrypt16(cls, data: bytes, key: bytes) -> bytes:
        """Port of RC6.Encrypt16: ISO pad, encrypt block 0 only, return 16 bytes."""
        cipher = cls(key)
        padded = cls.pad_iso(data)
        return cipher.encrypt_block(padded[:16])

    @classmethod
    def decrypt(cls, ciphertext: bytes, key: bytes, unpad: bool = True) -> bytes:
        if len(ciphertext) % 16 != 0:
            raise ValueError("Ciphertext length must be a multiple of 16 bytes")
        cipher = cls(key)
        raw = b"".join(
            cipher.decrypt_block(ciphertext[i : i + 16])
            for i in range(0, len(ciphertext), 16)
        )
        return cls.unpad_iso(raw) if unpad else raw


# ---------------------------------------------------------------------------
# Inosmart Auth Key Derivations (XF_InoSmart.Common.Rutine)
# ---------------------------------------------------------------------------


def encrypt_rc6_s1(imei: str, nfc_key_hex: str) -> str:
    """Port of Rutine.EncryptRC6_S1 (used for LEARN_NEW_DEVICE=0xA0 pairing)."""
    if len(nfc_key_hex) < 4:
        raise ValueError("nfc_key_hex must be at least 4 hex chars")
    suffix_dec = str(int(nfc_key_hex[-4:], 16))
    mask_digits = (suffix_dec * ((len(imei) // len(suffix_dec)) + 1))[: len(imei)]
    obfuscated_digits = "".join(
        str((int(imei[i]) + int(mask_digits[i])) % 10)
        for i in range(len(imei))
    )
    ct = RC6.encrypt(obfuscated_digits.encode("utf-8"), bytes.fromhex(nfc_key_hex))
    return ct.hex().upper()


def ble_key_unlock_gen(in_key_hex: str, imei: str) -> str:
    """Port of Rutine.BleKeyUnlockGen (used for door open / relays 0x00..0x05)."""
    if len(in_key_hex) != 32:
        raise ValueError(f"Expected 32 hex chars in_key_hex, got {len(in_key_hex)}")
    v0 = int(in_key_hex[0:8], 16)
    v1 = int(in_key_hex[8:16], 16)
    v2 = int(in_key_hex[16:24], 16)
    v3 = int(in_key_hex[24:32], 16)
    v4 = v0 ^ v2
    dec_str = f"{v0}{v1}{v2}{v3}{v4}"

    n = min(len(imei), len(dec_str))
    interleaved = "".join(dec_str[i] + imei[i] for i in range(n))
    if len(dec_str) > n:
        interleaved += dec_str[n:]
    elif len(imei) > n:
        interleaved += imei[n:]

    return hashlib.sha1(interleaved.encode("utf-8")).hexdigest().upper()


def ble_key_gen(
    password: str,
    in_key_hex: str,
    session_bytes: Optional[bytes] = None,
) -> Tuple[str, str]:
    """Port of Rutine.BLEKeyGen (used for ADMINISTRATION=0xA1).

    Returns (encrypted_key_hex_32chars, session_token_hex_8chars).
    """
    if len(in_key_hex) != 32:
        raise ValueError(f"Expected 32 hex chars in_key_hex, got {len(in_key_hex)}")
    in_key_upper = in_key_hex.upper()
    p1 = in_key_upper[0:16]
    p2 = in_key_upper[16:32]
    p3 = bytes(a ^ b for a, b in zip(bytes.fromhex(p1), bytes.fromhex(p2))).hex().upper()

    # Ascii2Hex uses "{0:X}" per character
    pwd_hex = "".join(f"{ord(ch):X}" for ch in password)
    concat_hex = (
        p3[0:8]
        + pwd_hex
        + "00"
        + p1[8:16]
        + p2[8:16]
        + p3[8:16]
        + p1[0:8]
        + p2[0:8]
    )
    sha1_hex = hashlib.sha1(bytes.fromhex(concat_hex)).hexdigest().upper()

    # Build 16-byte RC6 key in reverse order, skipping 1-based end indices (36, 24, 10, 2)
    skip = {36, 24, 10, 2}
    rc6_key_hex = "".join(
        sha1_hex[idx - 2 : idx]
        for idx in range(40, 0, -2)
        if idx not in skip
    )

    if session_bytes is None:
        session_bytes = bytes((secrets.randbelow(253) + 1) for _ in range(4))
    if len(session_bytes) != 4:
        raise ValueError("session_bytes must be exactly 4 bytes")
    session_token_hex = session_bytes.hex().upper()

    plaintext = bytes.fromhex(in_key_upper[:-8] + session_token_hex)
    encrypted = RC6.encrypt16(plaintext, bytes.fromhex(rc6_key_hex))
    return encrypted.hex().upper(), session_token_hex


def encrypt_imei_for_bt(imei: str, rnd_byte: Optional[int] = None) -> str:
    """Port of Rutine.EncyptIMEI4BT(long imei) -> 16 uppercase hex chars."""
    imei_int = int(imei)
    h = f"{imei_int:016X}"
    swapped = h[4:8] + h[12:16] + h[0:4] + h[8:12]
    if rnd_byte is None:
        rnd_byte = secrets.randbelow(245) + 10  # 10..254 matching Random.Next(10, 255)
    mask_hex = f"{rnd_byte & 0xFF:02X}" * 8
    xored = int(swapped, 16) ^ int(mask_hex, 16)
    return f"{xored:016X}"


# ---------------------------------------------------------------------------
# Packet Header & Framing
# ---------------------------------------------------------------------------


@dataclass
class MsgsHeader:
    packet_type: int = 0
    bus_no: int = MsgBus.IO
    packet_id: int = 0
    message_id: int = 0
    message_len: int = HEADER_LENGTH_NETTO
    flags: str = "00000000"
    session_id: int = 0
    packet_index: int = 0

    def encode(self, admin_session_token: str = "", payload_len: int = 0) -> bytes:
        flags_hex = admin_session_token if admin_session_token else self.flags
        if len(flags_hex) != 8:
            raise ValueError(f"Header flags must be 8 hex chars, got {flags_hex!r}")
        total_netto = HEADER_LENGTH_NETTO + payload_len
        self.message_len = total_netto
        return (
            ((self.packet_type + self.bus_no) & 0xFFFF).to_bytes(2, "big")
            + (self.packet_id & 0xFFFF).to_bytes(2, "big")
            + (self.message_id & 0xFFFF).to_bytes(2, "big")
            + (total_netto & 0xFFFF).to_bytes(2, "big")
            + bytes.fromhex(flags_hex)
            + (self.session_id & 0xFFFFFFFF).to_bytes(4, "big")
        )

    @classmethod
    def decode(cls, raw: bytes) -> "MsgsHeader":
        if len(raw) < HEADER_LENGTH:
            raise ValueError(f"Packet shorter than {HEADER_LENGTH} bytes: {len(raw)}")
        return cls(
            packet_type=int.from_bytes(raw[0:2], "big"),
            packet_id=int.from_bytes(raw[2:4], "big"),
            message_id=int.from_bytes(raw[4:6], "big"),
            message_len=int.from_bytes(raw[6:8], "big"),
            flags=raw[8:12].hex().upper(),
            packet_index=raw[12],
            session_id=int.from_bytes(raw[14:16], "big"),
        )


def build_packet(
    message_id: int,
    payload: bytes = b"",
    *,
    session_id: int = 1,
    admin_session_token: str = "",
    bus_no: int = MsgBus.IO,
    packet_type: int = 0,
    packet_id: int = 0,
    flags: str = "00000000",
) -> bytes:
    header = MsgsHeader(
        packet_type=packet_type,
        bus_no=bus_no,
        packet_id=packet_id,
        message_id=message_id,
        flags=flags,
        session_id=session_id,
    ).encode(admin_session_token=admin_session_token, payload_len=len(payload))
    return pad_packet(header + payload)


def chunk_for_ble(packet: bytes, mtu: int = REQUESTED_MTU_SIZE) -> List[bytes]:
    """Split packet into GATT write chunks of (mtu - 3), min 20 bytes."""
    chunk_size = max(mtu - 3, 20)
    return [packet[i : i + chunk_size] for i in range(0, len(packet), chunk_size)]


def split_response_packets(buffer: bytearray) -> List[bytes]:
    """Extract complete Inosmart packets from a BLE notification accumulator buffer.

    Mutates ``buffer`` in-place, removing consumed complete packets.
    Matches DataController.PrepareResponseMsgs packet length calculation.
    """
    packets: List[bytes] = []
    offset = 0
    total = len(buffer)
    while total - offset >= HEADER_LENGTH:
        msg_len_netto = int.from_bytes(buffer[offset + 6 : offset + 8], "big")
        raw_len = msg_len_netto + 4
        aligned_len = (raw_len + 3) & ~3
        if aligned_len < HEADER_LENGTH:
            break
        if total - offset < aligned_len:
            break
        packets.append(bytes(buffer[offset : offset + aligned_len]))
        offset += aligned_len
    if offset > 0:
        del buffer[:offset]
    return packets


# ---------------------------------------------------------------------------
# Request Builders
# ---------------------------------------------------------------------------


def build_get_rlogin_access(
    rtask: BleRTask,
    *,
    ruser: BleRUser = BleRUser.USER_PHONE,
    reserved1: int = 0,
    reserved2: int = 0,
    session_id: int = 1,
    admin_session_token: str = "",
) -> bytes:
    payload = (
        int(ruser).to_bytes(1, "big")
        + int(rtask).to_bytes(1, "big")
        + (reserved1 & 0xFFFFFFFF).to_bytes(4, "big")
        + (reserved2 & 0xFFFFFFFF).to_bytes(4, "big")
    )
    return build_packet(
        MsgType.GET_RLOGIN_ACCESS,
        payload,
        session_id=session_id,
        admin_session_token=admin_session_token,
    )


def build_set_rlogin_task(
    rtask: BleRTask,
    key_hex: str,
    *,
    reserved: int = 0,
    session_id: int = 1,
    admin_session_token: str = "",
) -> bytes:
    payload = (
        int(rtask).to_bytes(1, "big")
        + (reserved & 0xFFFFFFFF).to_bytes(4, "big")
        + bytes.fromhex(key_hex)
    )
    return build_packet(
        MsgType.SET_RLOGIN_TASK,
        payload,
        session_id=session_id,
        admin_session_token=admin_session_token,
    )


def build_get_device_info(*, session_id: int = 1, admin_session_token: str = "") -> bytes:
    return build_packet(
        MsgType.GET_DEVICE_INFO,
        b"",
        session_id=session_id,
        admin_session_token=admin_session_token,
    )


def build_get_system_info(*, session_id: int = 1, admin_session_token: str = "") -> bytes:
    return build_packet(
        MsgType.GET_SYSTEM_INFO,
        b"",
        session_id=session_id,
        admin_session_token=admin_session_token,
    )


def build_get_system_state(*, session_id: int = 1, admin_session_token: str = "") -> bytes:
    return build_packet(
        MsgType.GET_SYSTEM_STATE,
        b"",
        session_id=session_id,
        admin_session_token=admin_session_token,
    )


def build_get_sys_clock(*, session_id: int = 1, admin_session_token: str = "") -> bytes:
    return build_packet(
        MsgType.GET_SYS_CLOCK,
        b"",
        session_id=session_id,
        admin_session_token=admin_session_token,
    )


def build_set_sys_clock(
    timestamp_sec: Optional[int] = None,
    *,
    session_id: int = 1,
    admin_session_token: str = "",
) -> bytes:
    if timestamp_sec is None:
        timestamp_sec = int(datetime.now(timezone.utc).timestamp())
    payload = int(timestamp_sec).to_bytes(8, "big", signed=False)
    return build_packet(
        MsgType.SET_SYS_CLOCK,
        payload,
        session_id=session_id,
        admin_session_token=admin_session_token,
    )


def build_get_unlocked_entrance(*, session_id: int = 1, admin_session_token: str = "") -> bytes:
    return build_packet(
        MsgType.GET_UNLOCKED_ENTRANCE,
        b"",
        session_id=session_id,
        admin_session_token=admin_session_token,
    )


def build_set_unlocked_entrance(
    minutes: int,
    io_mask: int = int(FreeEntranceIO.MAIN_DOOR_RELAY),
    *,
    session_id: int = 1,
    admin_session_token: str = "",
) -> bytes:
    payload = (minutes & 0xFFFF).to_bytes(2, "big") + (io_mask & 0xFF).to_bytes(1, "big")
    return build_packet(
        MsgType.SET_UNLOCKED_ENTRANCE,
        payload,
        session_id=session_id,
        admin_session_token=admin_session_token,
    )


def build_set_system_info(
    mask: int,
    *,
    ip: str = "192.168.5.1",
    port: int = 55555,
    encryption: int = 3,
    ssid: str = "",
    pwd: str = "",
    hled: int = 0,
    sled: int = 0,
    io_locker: int = 0,
    io_user_cs: int = 0,
    key_length: int = 0,
    bell_io: int = 0,
    sensor_io: int = 0,
    admin_imei_hex: str = "0000000000000000",
    session_id: int = 1,
    admin_session_token: str = "",
) -> bytes:
    """Build C_SetSystemInfo_Request packet (MSG_TYPE = 16)."""
    if not admin_imei_hex or not admin_imei_hex.strip():
        admin_imei_hex = "0000000000000000"
    if len(admin_imei_hex) != 16:
        raise ValueError("admin_imei_hex must be 16 hex chars (8 bytes)")
    ssid_bytes = ssid.strip().encode("utf-8") + b"\x00"
    pwd_bytes = pwd.strip().encode("utf-8") + b"\x00"
    payload = (
        (mask & 0xFFFF).to_bytes(2, "big")
        + ip_to_bytes(ip)
        + (port & 0xFFFF).to_bytes(2, "big")
        + (encryption & 0xFF).to_bytes(1, "big")
        + ssid_bytes
        + pwd_bytes
        + (hled & 0xFF).to_bytes(1, "big")
        + (sled & 0xFF).to_bytes(1, "big")
        + (io_locker & 0xFFFF).to_bytes(2, "big")
        + (io_user_cs & 0xFFFF).to_bytes(2, "big")
        + (key_length & 0xFF).to_bytes(1, "big")
        + (bell_io & 0xFF).to_bytes(1, "big")
        + (sensor_io & 0xFF).to_bytes(1, "big")
        + bytes.fromhex(admin_imei_hex)
    )
    return build_packet(
        MsgType.SET_SYSTEM_INFO,
        payload,
        session_id=session_id,
        admin_session_token=admin_session_token,
    )


def build_get_motor_handle_info(*, session_id: int = 1, admin_session_token: str = "") -> bytes:
    return build_packet(
        MsgType.GET_MOTOR_HANDLE_INFO,
        b"",
        session_id=session_id,
        admin_session_token=admin_session_token,
    )


def build_set_motor_handle_info(
    mask: int = int(MotorHandleMask.DATA_LED),
    *,
    led1: int = 0,
    led2: int = 0,
    m_steps: int = 0,
    close_offset: int = 0,
    close_delay_ms: int = 0,
    session_id: int = 1,
    admin_session_token: str = "",
) -> bytes:
    payload = (
        (mask & 0xFFFF).to_bytes(2, "big")
        + (led1 & 0xFF).to_bytes(1, "big")
        + (led2 & 0xFF).to_bytes(1, "big")
        + (m_steps & 0xFF).to_bytes(1, "big")
        + (close_offset & 0xFF).to_bytes(1, "big")
        + (close_delay_ms & 0xFFFF).to_bytes(2, "big")
    )
    return build_packet(
        MsgType.SET_MOTOR_HANDLE_INFO,
        payload,
        session_id=session_id,
        admin_session_token=admin_session_token,
    )


def build_set_io_relay(
    relay: int,
    state: int,
    sync_cnt: int = 0,
    io_0: int = 0,
    io_1: int = 0,
    *,
    session_id: int = 1,
    admin_session_token: str = "",
) -> bytes:
    payload = (
        (relay & 0xFF).to_bytes(1, "big")
        + (state & 0xFF).to_bytes(1, "big")
        + (sync_cnt & 0xFFFF).to_bytes(2, "big")
        + (io_0 & 0xFFFFFFFF).to_bytes(4, "big")
        + (io_1 & 0xFFFFFFFF).to_bytes(4, "big")
    )
    return build_packet(
        MsgType.SET_IO_RELAY,
        payload,
        session_id=session_id,
        admin_session_token=admin_session_token,
    )


def build_get_log_data(
    event_type: int = int(LogEventType.ALL),
    count: int = 20,
    *,
    session_id: int = 1,
    admin_session_token: str = "",
) -> bytes:
    payload = (event_type & 0xFF).to_bytes(1, "big") + (count & 0xFFFF).to_bytes(2, "big")
    return build_packet(
        MsgType.GET_LOG_DATA,
        payload,
        session_id=session_id,
        admin_session_token=admin_session_token,
    )


# ---------------------------------------------------------------------------
# Response Models & Parsers
# ---------------------------------------------------------------------------


@dataclass
class RLoginAccessResponse:
    header: MsgsHeader
    rtask: int
    access_key_hex: str
    sys_info: int

    @property
    def is_denied(self) -> bool:
        return bool(self.sys_info & int(BleSysInfo.DENIED))


@dataclass
class RLoginTaskResponse:
    header: MsgsHeader
    rtask: int
    err: int
    reserved: int

    @property
    def success(self) -> bool:
        return self.err in PAIR_SUCCESS_CODES


@dataclass
class SystemInfoResponse:
    header: MsgsHeader
    ip: str
    port: int
    encryption: int
    ssid: str
    pwd: str
    hled: int
    sled: int
    io_locker: int
    user_relay_time_sec: float
    key_len: int
    rc_bell: int
    sensor: int
    mainlock_counter: int

    @property
    def bluetooth_always_on(self) -> bool:
        return bool(self.encryption & 0x80)

    @property
    def demo_mode(self) -> bool:
        return bool(self.encryption & 0x40)


@dataclass
class DeviceInfoResponse:
    header: MsgsHeader
    uptime_seconds: int
    protocol_version: int
    extra_info_hex: str
    peripheral_mask: int
    main_fw_version: str
    main_build_raw_hex: str
    fhw_uid: Tuple[str, str, str]
    storage: int
    max_fpc: int
    dev_model: int
    dev_model_hw: int
    fpc_count: int
    fpc_fw_version: str
    io_fw_version: str
    io_build_raw_hex: str
    io_hw_uid: Tuple[str, str, str]
    dev_model_io: int
    dev_model_io_hw: int
    ext_net_fw_version: str

    @property
    def requires_admin_session_on_ble(self) -> bool:
        if self.dev_model_io == ModelsIO.MODEL_IO_BT_SOLO:
            return True
        try:
            parts = tuple(int(p) for p in self.main_fw_version.split("."))
            return parts >= (1, 6, 152)
        except ValueError:
            return False


@dataclass
class SysClockResponse:
    header: MsgsHeader
    unix_timestamp: int
    device_datetime: datetime
    temperature_c: int


@dataclass
class SystemStateResponse:
    header: MsgsHeader
    state: int
    state_ext: int


@dataclass
class UnlockedEntranceResponse:
    header: MsgsHeader
    minutes: int
    io_mask: int


@dataclass
class MotorHandleInfoResponse:
    header: MsgsHeader
    led1: int
    led2: int
    m_steps: int
    close_offset: int
    close_delay_ms: int


@dataclass
class AckResponse:
    header: MsgsHeader
    cmd: int
    err: int

    @property
    def success(self) -> bool:
        return self.err == 0


@dataclass
class LogItem:
    event_type: int
    raw_time: int
    timestamp: datetime
    text: str
    user_id: Optional[int]
    auth_item_id: Optional[int]
    source: int


@dataclass
class LogDataResponse:
    header: MsgsHeader
    count: int
    items: List[LogItem] = field(default_factory=list)


def _read_cstr(data: bytes, offset: int) -> Tuple[str, int]:
    end = data.find(b"\x00", offset)
    if end == -1:
        return data[offset:].decode("utf-8", errors="replace"), len(data)
    return data[offset:end].decode("utf-8", errors="replace"), end + 1


def parse_rlogin_access_response(packet: bytes) -> RLoginAccessResponse:
    header = MsgsHeader.decode(packet)
    p = packet[HEADER_LENGTH:]
    return RLoginAccessResponse(
        header=header,
        rtask=0,
        access_key_hex=p[0:16].hex().upper(),
        sys_info=int.from_bytes(p[16:20], "big"),
    )


def parse_rlogin_task_response(packet: bytes) -> RLoginTaskResponse:
    header = MsgsHeader.decode(packet)
    p = packet[HEADER_LENGTH:]
    return RLoginTaskResponse(
        header=header,
        rtask=p[0],
        err=p[1],
        reserved=0,
    )


def parse_system_info_response(packet: bytes) -> SystemInfoResponse:
    header = MsgsHeader.decode(packet)
    p = packet[HEADER_LENGTH:]
    ip = ".".join(str(b) for b in p[0:4])
    port = int.from_bytes(p[4:6], "big")
    encryption = p[6]
    pos = 7
    ssid, pos = _read_cstr(p, pos)
    pwd, pos = _read_cstr(p, pos)
    hled = p[pos] if pos < len(p) else 0
    sled = p[pos + 1] if pos + 1 < len(p) else 0
    io_locker = int.from_bytes(p[pos + 2 : pos + 4], "big") if pos + 4 <= len(p) else 0
    io_user = int.from_bytes(p[pos + 4 : pos + 6], "big") if pos + 6 <= len(p) else 0
    key_len = p[pos + 6] if pos + 6 < len(p) else 0
    rc_bell = p[pos + 7] if pos + 7 < len(p) else 0
    sensor = p[pos + 8] if pos + 8 < len(p) else 0
    mainlock_counter = (
        int.from_bytes(p[pos + 9 : pos + 13], "big") if pos + 13 <= len(p) else 0
    )
    return SystemInfoResponse(
        header=header,
        ip=ip,
        port=port,
        encryption=encryption,
        ssid=ssid,
        pwd=pwd,
        hled=hled,
        sled=sled,
        io_locker=io_locker,
        user_relay_time_sec=io_user / 100.0,
        key_len=key_len,
        rc_bell=rc_bell,
        sensor=sensor,
        mainlock_counter=mainlock_counter,
    )


def parse_device_info_response(packet: bytes) -> DeviceInfoResponse:
    header = MsgsHeader.decode(packet)
    p = packet[HEADER_LENGTH:]
    uptime = int.from_bytes(p[0:4], "big") if len(p) >= 4 else 0
    proto = p[4] if len(p) >= 5 else 0
    extra_hex = p[5:9].hex().upper() if len(p) >= 9 else ""
    periph = int.from_bytes(p[9:13], "big") if len(p) >= 13 else 0
    fsw = format_version(p[13:17]) if len(p) >= 17 else ""
    fbuild = p[17:25].hex().upper() if len(p) >= 25 else ""
    fhw_uid = (
        p[25:29].hex().upper() if len(p) >= 29 else "",
        p[29:33].hex().upper() if len(p) >= 33 else "",
        p[33:37].hex().upper() if len(p) >= 37 else "",
    )
    storage = int.from_bytes(p[37:39], "big") if len(p) >= 39 else 0
    max_fpc = int.from_bytes(p[39:41], "big") if len(p) >= 41 else 0
    dev_model = p[41] if len(p) >= 42 else 0
    dev_model_hw = p[42] if len(p) >= 43 else 0
    fpc_count = int.from_bytes(p[43:45], "big") if len(p) >= 45 else 0
    fpc_sw = format_version(p[45:49]) if len(p) >= 49 else ""
    iosw = format_version(p[49:53]) if len(p) >= 53 else ""
    iobuild = p[53:61].hex().upper() if len(p) >= 61 else ""
    iohw_uid = (
        p[61:65].hex().upper() if len(p) >= 65 else "",
        p[65:69].hex().upper() if len(p) >= 69 else "",
        p[69:73].hex().upper() if len(p) >= 73 else "",
    )
    dev_model_io = p[73] if len(p) >= 74 else 0
    dev_model_io_hw = p[74] if len(p) >= 75 else 0
    ext_net_fw_ver = format_version(p[75:79]) if len(p) >= 79 else ""
    return DeviceInfoResponse(
        header=header,
        uptime_seconds=uptime,
        protocol_version=proto,
        extra_info_hex=extra_hex,
        peripheral_mask=periph,
        main_fw_version=fsw,
        main_build_raw_hex=fbuild,
        fhw_uid=fhw_uid,
        storage=storage,
        max_fpc=max_fpc,
        dev_model=dev_model,
        dev_model_hw=dev_model_hw,
        fpc_count=fpc_count,
        fpc_fw_version=fpc_sw,
        io_fw_version=iosw,
        io_build_raw_hex=iobuild,
        io_hw_uid=iohw_uid,
        dev_model_io=dev_model_io,
        dev_model_io_hw=dev_model_io_hw,
        ext_net_fw_version=ext_net_fw_ver,
    )


def parse_sys_clock_response(packet: bytes) -> SysClockResponse:
    header = MsgsHeader.decode(packet)
    p = packet[HEADER_LENGTH:]
    ts = int.from_bytes(p[0:8], "big")
    temp_signed = int.from_bytes(p[8:9], "big", signed=True)
    return SysClockResponse(
        header=header,
        unix_timestamp=ts,
        device_datetime=datetime.fromtimestamp(ts, tz=timezone.utc),
        temperature_c=temp_signed,
    )


def parse_system_state_response(packet: bytes) -> SystemStateResponse:
    header = MsgsHeader.decode(packet)
    p = packet[HEADER_LENGTH:]
    return SystemStateResponse(
        header=header,
        state=int.from_bytes(p[0:2], "big"),
        state_ext=p[2],
    )


def parse_unlocked_entrance_response(packet: bytes) -> UnlockedEntranceResponse:
    header = MsgsHeader.decode(packet)
    p = packet[HEADER_LENGTH:]
    return UnlockedEntranceResponse(
        header=header,
        minutes=int.from_bytes(p[0:2], "big"),
        io_mask=p[2],
    )


def parse_motor_handle_info_response(packet: bytes) -> MotorHandleInfoResponse:
    header = MsgsHeader.decode(packet)
    p = packet[HEADER_LENGTH:]
    offset_signed = int.from_bytes(p[3:4], "big", signed=True)
    return MotorHandleInfoResponse(
        header=header,
        led1=p[0],
        led2=p[1],
        m_steps=p[2],
        close_offset=offset_signed,
        close_delay_ms=int.from_bytes(p[4:6], "big"),
    )


def parse_ack_response(packet: bytes) -> AckResponse:
    header = MsgsHeader.decode(packet)
    p = packet[HEADER_LENGTH:]
    return AckResponse(header=header, cmd=p[0], err=p[1])


def parse_log_data_response(packet: bytes) -> LogDataResponse:
    header = MsgsHeader.decode(packet)
    p = packet[HEADER_LENGTH:]
    count = int.from_bytes(p[0:2], "big")
    pos = 2
    epoch_2000 = datetime(2000, 1, 1, tzinfo=timezone.utc)
    items: List[LogItem] = []
    for _ in range(count):
        if pos + 5 > len(p):
            break
        ev_type = p[pos]
        raw_time = int.from_bytes(p[pos + 1 : pos + 5], "big")
        pos += 5
        text, pos = _read_cstr(p, pos)
        raw_uid = int.from_bytes(p[pos : pos + 2], "big")
        raw_aid = int.from_bytes(p[pos + 2 : pos + 4], "big")
        source = p[pos + 4]
        pos += 5
        items.append(
            LogItem(
                event_type=ev_type,
                raw_time=raw_time,
                timestamp=epoch_2000 + timedelta(seconds=raw_time),
                text=text,
                user_id=None if raw_uid == 0xFFFF else raw_uid,
                auth_item_id=None if (raw_aid == 0xFFFF or raw_aid > 100) else raw_aid,
                source=source,
            )
        )
    return LogDataResponse(header=header, count=count, items=items)


def parse_response(packet: bytes):
    """Parse any incoming Inosmart response packet according to its message_id."""
    header = MsgsHeader.decode(packet)
    mid = header.message_id
    if mid == MsgType.GET_RLOGIN_ACCESS:
        return parse_rlogin_access_response(packet)
    if mid == MsgType.SET_RLOGIN_TASK:
        return parse_rlogin_task_response(packet)
    if mid == MsgType.GET_SYSTEM_INFO:
        return parse_system_info_response(packet)
    if mid == MsgType.GET_DEVICE_INFO:
        return parse_device_info_response(packet)
    if mid == MsgType.GET_SYS_CLOCK:
        return parse_sys_clock_response(packet)
    if mid == MsgType.GET_SYSTEM_STATE:
        return parse_system_state_response(packet)
    if mid == MsgType.GET_UNLOCKED_ENTRANCE:
        return parse_unlocked_entrance_response(packet)
    if mid == MsgType.GET_MOTOR_HANDLE_INFO:
        return parse_motor_handle_info_response(packet)
    if mid == MsgType.GET_LOG_DATA:
        return parse_log_data_response(packet)
    if mid in (
        MsgType.SET_SYSTEM_INFO,
        MsgType.SET_UNLOCKED_ENTRANCE,
        MsgType.SET_MOTOR_HANDLE_INFO,
        MsgType.SET_IO_RELAY,
    ):
        return parse_ack_response(packet)
    return header
