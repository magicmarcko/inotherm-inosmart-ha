# Inotherm Inosmart (BT 640) BLE Protocol Specification

Reverse-engineered from the official Android application (`com.ismart.XF_InoSmart` v4.00.59, `XF_InoSmart.dll` CIL).

---

## 1. BLE GATT Profile

- **Scan / Advertising Service UUID:** `51071364-4456-4a4e-9b6f-46708d4c1ea4`
- **Primary Transport Service (Nordic UART Service / NUS):**
  - Service UUID: `6e400001-b5a3-f393-e0a9-e50e24dcca9e`
  - RX Characteristic (Write): `6e400002-b5a3-f393-e0a9-e50e24dcca9e`
  - TX Characteristic (Notify): `6e400003-b5a3-f393-e0a9-e50e24dcca9e`
- **Other Service UUIDs observed in `XF_InoSmart.Common.BLE_service`:**
  - Touch Module Service: `51071364-4456-4a4e-9b6f-46708d4c1eb0` (Adv: `64130751-5644-4e4a-9b6f-46708d4c1eb0`)
  - MCUboot / SMP DFU Service: `8d53dc1d-1db7-4cd3-868b-8a527460aa84`, Characteristic: `da2e7828-fbce-4e01-ae9e-261174997c48`
- **MTU & Chunking:**
  - App requests `MTU = 248` (`requestedMTUSize = 248`).
  - Outgoing messages are split into chunks of `max(mtu - 3, 20)` bytes (`MtuSize - 3`) and written sequentially to `6e400002-...`.

---

## 2. Packet Framing (`C_MsgsHeader`)

Every request and response uses a **16-byte big-endian binary header** (`HeaderLength = 16`, `HeaderLengthNetto = 12`) followed by the command payload and zero-byte alignment padding to a multiple of **4 bytes** (`len(packet) % 4 == 0`).

| Offset | Size | Field | Description |
|-------:|-----:|-------|-------------|
| `0` | 2 | `packet_type + bus_no` | `0x0000` = Request IO bus, `0x0001` = Response IO bus (`+4` = Gateway bus `BUS_GW`) |
| `2` | 2 | `packet_id` | Always `0x0000` in standard commands |
| `4` | 2 | `message_id` | Command ID (`MSG_TYPE`, see Section 3) |
| `6` | 2 | `message_len` | `12 + len(payload)` (length starting at byte offset 4, excluding 4-byte padding) |
| `8` | 4 | `flags` | `0x00000000`, or the 4-byte **`AdminSessionToken`** once Admin Login (`0xA1`) succeeds |
| `12` | 4 | `reserved` | Request: 32-bit `SessionId`. Response: byte `12` = `PacketIndex`, bytes `14..16` = `SessionID` (`uint16`) |
| `16` | N | `payload` | Command-specific binary payload |
| `16+N` | 0..3 | `padding` | `0x00` bytes so total packet length is divisible by 4 |

### Multi-Packet Reassembly (`PrepareResponseMsgs`)

Multiple responses may arrive concatenated in a notification stream (or one response split across notifications):
1. Check remaining bytes `>= 16` (in C#: remaining `> 16`, so payload >= 1 or empty header + 0 padding; practical packets with 0 payload also have `message_len = 12`, total aligned = `16`).
2. `raw_len = uint16_be(buf[offset+6 : offset+8]) + 4`.
3. `aligned_len = (raw_len + 3) & ~3`.
4. Slice `buf[offset : offset + aligned_len]` and advance `offset += aligned_len`.

---

## 3. Command IDs (`MSG_TYPE`)

| ID | Name | Direction / Description |
|---:|------|-------------------------|
| `1` | `GET_SYS_CLOCK` | Read device RTC + internal temperature |
| `4` | `SET_SYS_CLOCK` | Sync device RTC (`C_SetSysClock_Request` sets `message_id = 4`, though enum lists `SET_SYS_CLOCK = 2`; `C_SetSysClock_Request` hardcodes `4`) |
| `15` | `GET_SYSTEM_INFO` | Read LEDs (`HLED`/`SLED`), relay pulse times, Wi-Fi/BT flags, main lock counter |
| `16` | `SET_SYSTEM_INFO` | Update LED power, relay time, input/bell config, BT always-on/Demo, or add admin IMEI (masked by `SYSINFO_MASK`) |
| `17` | `GET_DEVICE_INFO` | Read model (`MODELS`, `MODELS_IO`), FW versions, uptime, UIDs, peripheral bitmask |
| `19` | `GET_SYSTEM_STATE` | Poll system enrollment/standby state |
| `24` | `GET_LOG_DATA` | Read event log (`count` entries) |
| `25` | `SET_UI_INFO` | Keypad/bell LED & buzzer thresholds |
| `26` | `GET_UI_INFO` | Read UI & touch module parameters |
| `27` | `SET_UNLOCKED_ENTRANCE` | Enable/disable Free Passage ("Freier Durchgang") |
| `28` | `GET_UNLOCKED_ENTRANCE` | Query Free Passage state, duration & target relays |
| `29` | `SET_IO_RELAY` | Direct IO relay message (`C_SetIORelay_Request`) |
| `30` | `SET_LOGIN_INFO` | Login metadata |
| `35` (`0x23`) | `GET_RLOGIN_ACCESS` | Step 1 of BLE challenge-response (`BLE_RTASK` + challenge nonce) |
| `36` (`0x24`) | `SET_RLOGIN_TASK` | Step 2 of BLE challenge-response (submit derived auth key + execute task) |
| `44` (`0x2C`) | `SET_MOTOR_HANDLE_INFO` | Configure motor handle LEDs (`led1`/`led2`) & motor calibration |
| `45` (`0x2D`) | `GET_MOTOR_HANDLE_INFO` | Read motor handle LEDs & motor parameters |

---

## 4. Client Identifier (`IMEI`)

The door identifies phones using a **15-digit numeric ASCII string** (`cIMEI`), such as `359123456789012`.
- Leading zeros in generated Android IDs are replaced with `'1'` via `ReplaceLeadingZeros` so `Int64` conversion preserves 15 decimal digits (important when calling `EncyptIMEI4BT`).
- Home Assistant / standalone clients generate and persist a random 15-digit IMEI starting with `'1'..'9'`.

---

## 5. Cryptography (`RC6` & Key Derivation)

### 5.1 RC6-32/20/16-ECB (`XF_InoSmart.Common.RC6`)

- Word size `w = 32` bits, `r = 20` rounds (`44` `uint32` subkeys), key length `16` bytes (`4` little-endian `uint32` words).
- Magic constants: `Pw = 0xB7E15163`, `Qw = 0x9E3779B9`.
- **Padding:** Always appends `pad_len = 16 - (len(data) % 16)` bytes: `0x80` followed by `(pad_len - 1)` `0x00` bytes.
- **`RC6.Encrypt(data, key)`:** Encrypts all padded 16-byte blocks in ECB mode (used by `EncryptRC6_S1`).
- **`RC6.Encrypt16(data, key)`:** Applies the same `16 - (len(data) % 16)` padding, encrypts the **first 16-byte block only**, and returns those **16 bytes** (`Array.Copy(loc4, 0, loc0, 0, 16)`, discarding any trailing padding block — used by `BLEKeyGen` when encrypting a 16-byte plaintext).

### 5.2 Challenge-Response Workflows (`0x23` -> `0x24`)

#### Step 1: Request Challenge (`MSG_TYPE = 35 / GET_RLOGIN_ACCESS`)
Payload (12 bytes):
- `data_ruser` (4B BE): `0` (`USER_PHONE`)
- `data_rtask` (4B BE): `BLE_RTASK` code
- `data_reserved1` (2B BE): `0`
- `data_reserved2` (2B BE): `0`

Response (`C_GetRLoginAccess_Response`, 24 bytes payload):
- `data_rtask` (4B)
- `data_access_key` (16B -> 32 uppercase hex chars `inKey`)
- `data_sys_info` (4B BE bitmask `BLE_SYS_INFO`):
  - `0x00000001`: `PHONE_ADD_PROGRESS`
  - `0x00000002`: `PHONE_DELETE_PROGRESS`
  - `0x00001000`: `ADMIN_ALREADY_ACTIVE`
  - `0x80000000`: `DENIED` (e.g., Admin mode not unlocked on the door via Button 3)

#### Task Codes (`BLE_RTASK`)

| Code | Name | Description | Auth Algorithm |
|-----:|------|-------------|----------------|
| `0` | `MAIN_KEYLOCK_OPEN` | Open main door lock | `BleKeyUnlockGen` |
| `1` | `MAIN_KEYLOCK_READY2OPEN` | Trigger Ready-to-Open / Day mode | `BleKeyUnlockGen` |
| `2` | `RELAY2_OPEN` | Relay 2 (user relay) | `BleKeyUnlockGen` |
| `3` | `GW_RELAY1_OPEN` | Gateway Relay 1 | `BleKeyUnlockGen` |
| `4` | `GW_RELAY2_OPEN` | Gateway Relay 2 | `BleKeyUnlockGen` |
| `5` | `GW_RELAY1_RELAY2_OPEN` | Both Gateway Relays | `BleKeyUnlockGen` |
| `160` (`0xA0`) | `LEARN_NEW_DEVICE` | Pair phone IMEI (requires Button 1 + Admin finger on door) | `EncryptRC6_S1` |
| `161` (`0xA1`) | `ADMINISTRATION` | Start Admin Session (requires Button 3 + Admin finger or BT Demo mode) | `BLEKeyGen` |
| `162` (`0xA2`) | `DELETE` | Delete paired phone IMEI | `EncryptRC6_S1` |

---

#### A. Phone Pairing (`LEARN_NEW_DEVICE = 0xA0`) — No Admin Password Needed!
1. Put the door into Phone Pairing Mode (**Button 1 / Taste 1** on the door control unit + **Admin finger** -> both LEDs turn solid **white**).
2. Send `GET_RLOGIN_ACCESS(user=0, task=0xA0)`.
3. Receive `inKey` (32 uppercase hex chars).
4. Run **`EncryptRC6_S1(imei, inKey)`**:
   - `suffix_int = int(inKey[-4:], 16)` -> decimal string `dec = str(suffix_int)`.
   - Repeat `dec` cyclically to `len(imei)` characters -> `mask_str`.
   - `digits = "".join(str((int(imei[i]) + int(mask_str[i])) % 10) for i in range(len(imei)))`.
   - Encrypt `digits.encode("utf-8")` (15 ASCII bytes + `0x80` = 16 bytes) using `RC6.Encrypt(..., bytes.fromhex(inKey))`.
   - Convert ciphertext to 32 uppercase hex chars (`cKey`).
5. Send `SET_RLOGIN_TASK(key=cKey, rtask=0xA0, reserved="00000000")`.
6. Response `data_err == 0x00` or `0x17` indicates successful permanent phone enrollment (`0x21`/`0x24`/`0xFF` = door not in pairing state).

---

#### B. Door Unlocking & Relays (`0x00 .. 0x05`)
Requires the client `imei` to be paired (and "Bluetooth-Türöffner" active on the door). **No admin password or Button 3 is needed for day-to-day unlocking.**
1. Send `GET_RLOGIN_ACCESS(user=0, task=task)`.
2. Receive `inKey` (32 uppercase hex chars).
3. Run **`BleKeyUnlockGen(inKey, imei)`**:
   - Split `inKey` into four 32-bit big-endian unsigned ints `v0, v1, v2, v3` (`int(inKey[i:i+8], 16)`).
   - `v4 = v0 ^ v2`.
   - Concatenate decimal representations: `s = f"{v0}{v1}{v2}{v3}{v4}"`.
   - Character-interleave `s` and `imei`:
     `interleaved = "".join(s[i] + imei[i] for i in range(len(imei))) + s[len(imei):]`.
   - `cKey = SHA1(interleaved.encode("utf-8")).hexdigest().upper()` (40 hex chars = 20 bytes).
4. Send `SET_RLOGIN_TASK(key=cKey, rtask=task, reserved="00000000")`.

---

#### C. Admin Session (`ADMINISTRATION = 0xA1`)
Required on BT 640 (`MODELS_IO = MODEL_IO_BT_SOLO = 32`) or firmware `>= 1.6.152` to read/write system configuration (`GET_SYSTEM_INFO`, `SET_SYSTEM_INFO` LED dimmers, `SET_UNLOCKED_ENTRANCE`, `GET_LOG_DATA`, etc.). On the physical door, Admin mode must be active (**Button 3 / Taste 3 + Admin finger** -> blue LED, or **BT Demo Mode** enabled).

1. Send `GET_RLOGIN_ACCESS(user=0, task=0xA1)`.
   - If `data_sys_info & 0x80000000` (`DENIED`), the door is not currently in BLE Admin Mode.
2. Run **`BLEKeyGen(password, inKey)`**:
   - `p1 = inKey[0:16]`, `p2 = inKey[16:32]`, `p3 = (bytes.fromhex(p1) XOR bytes.fromhex(p2)).hex().upper()`.
   - `pwd_hex = "".join(f"{ord(c):X}" for c in password)`.
   - `concat_hex = p3[0:8] + pwd_hex + "00" + p1[8:16] + p2[8:16] + p3[8:16] + p1[0:8] + p2[0:8]`.
   - `sha1_hex = SHA1(bytes.fromhex(concat_hex)).hexdigest().upper()` (40 hex chars).
   - Construct 16-byte RC6 key `rc6_key` by scanning `idx = 40, 38, ..., 2` and keeping `sha1_hex[idx-2:idx]` whenever `idx not in (36, 24, 10, 2)` (16 bytes reversed).
   - Generate 4 random bytes `session_token` (`randint(1, 253)` per byte -> 8 uppercase hex chars).
   - Plaintext = `bytes.fromhex(inKey[:-8] + session_token_hex)` (16 bytes).
   - `cKey = RC6.Encrypt16(plaintext, bytes.fromhex(rc6_key)).hex().upper()` (32 uppercase hex chars).
3. Send `SET_RLOGIN_TASK(key=cKey, rtask=0xA1, reserved="00000000")`.
4. If `data_err == 0x00`: set `AdminSessionToken = session_token_hex` and place it in `C_MsgsHeader.flags` on all subsequent admin commands in this connection.

---

## 6. Key Application Payloads

### `SET_RLOGIN_TASK` (`MSG_TYPE = 36`, Request)
- `data_rtask` (4B BE)
- `data_key` (variable: 20B for SHA-1 unlock or 16B for RC6 pairing/admin; sent raw bytes from uppercase hex string)
- `data_reserved` (4B: `0x00000000`)

### `SET_RLOGIN_TASK` (`MSG_TYPE = 36`, Response)
- `data_rtask` (4B BE)
- `data_err` (1B -> `0x00` / `0x17` success)
- `data_reserved` (4B)

### `GET_SYSTEM_INFO` (`MSG_TYPE = 15`, Response)
- `data_ip` (4B), `data_port` (2B), `data_encryption` (1B: `0x80 = ALWAYS_ON`, `0x40 = ADMIN/Demo`)
- `data_ssid` (null-terminated UTF-8), `data_pwd` (null-terminated UTF-8)
- `data_hled` (1B: Integrated LED `0..60`, factory default `20`)
- `data_sled` (1B: External LED `0..75`, factory default `12`)
- `data_io_locker` (2B), `data_io_user` (2B: Relay 2 time * 100, in 10ms units)
- `data_key_len` (1B), `data_rcbell` (1B), `data_sensor` (1B)
- `data_mainlock` (4B: main lock activation counter)

### `SET_SYSTEM_INFO` (`MSG_TYPE = 16`, Request)
- `data_mask` (`2B` bitmask `SYSINFO_MASK`: `HLED = 0x0020`, `USER_RELAY_TIME = 0x0040`, `ADMIN_IMEI = 0x0080`, `SLED = 0x0200`, `IO_BELL_SETTINGS = 0x0800`, `IO_SENSOR_INPUT = 0x1000`, etc.)
- Followed in exact order by: `data_ip` (4B), `data_port` (2B), `data_encryption` (1B), `data_ssid\0`, `data_pwd\0`, `data_hled` (1B), `data_sled` (1B), `data_io_locker` (2B), `data_io_user` (2B), `data_key_length` (1B), `data_bell_io` (1B), `data_sensor_io` (1B), `data_admin_IMEI` (8B, `'0000000000000000'` or `EncyptIMEI4BT(int(imei))`).

### `GET_DEVICE_INFO` (`MSG_TYPE = 17`, Response, 79 bytes)
- `up_time` (4B), `proto` (1B), `ex_info` (4B), `peripheral` (4B: `1=NFC, 2=TOUCH, 4=SI_BUS, 8=GATEWAY, 16=MOTOR_HANDLE`), `fsw` (4B version `b0.b1.u16(b2:b4)`), `fbuild` (8B), `fhw_uid1..3` (3x4B), `storage` (2B), `max_fpc` (2B), `dev_model` (1B), `dev_model_hw` (1B), `fpc` (2B), `fpc_sw` (4B version), `iosw` (4B version), `iobuild` (8B), `iohw_uid1..3` (3x4B), `dev_model_io` (1B: `32 = MODEL_IO_BT_SOLO` / BT 640), `dev_model_io_hw` (1B), `ext_net_fw_ver` (4B version).

### `SET_UNLOCKED_ENTRANCE` (`MSG_TYPE = 27`) / `GET_UNLOCKED_ENTRANCE` (`MSG_TYPE = 28`)
- `data_time` (2B BE): duration in minutes (`0` = disabled).
- `data_io` (1B): relay bitmask (`0x01 = MainDoorRelay`, `0x04 = UserRelay`, `0x10 = GatewayRelay1`, `0x20 = GatewayRelay2`).

### `SET_MOTOR_HANDLE_INFO` (`MSG_TYPE = 44`, Request)
- `data_mask` (2B: `1 = DATA_LED`, `2 = DATA_M_STEPS`, `8 = DATA_CLOSE_OFFSET`, `16 = DATA_CLOSE_DELAY_MS`)
- `data_led1` (1B), `data_led2` (1B), `data_m_steps` (1B), `data_close_offset` (1B), `data_close_delay_ms` (2B).
