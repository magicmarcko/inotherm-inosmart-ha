"""Unit tests verifying Inosmart BLE protocol framing, RC6/SHA-1 crypto, and structs."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import pathlib
import subprocess
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import inosmart_ble  # noqa: E402

proto = inosmart_ble._load_submodule("protocol")


class TestInosmartCrypto(unittest.TestCase):
    def test_imei_validation_and_generation(self) -> None:
        for _ in range(10):
            imei = proto.generate_synthetic_imei()
            self.assertEqual(len(imei), 15)
            self.assertTrue(imei.isdigit())
            self.assertNotEqual(imei[0], "0")
            self.assertEqual(proto.validate_imei(imei), imei)

        with self.assertRaises(ValueError):
            proto.validate_imei("12345")
        self.assertEqual(proto.replace_leading_zeros("0012345", "1"), "1112345")

    def test_rc6_block_roundtrip_and_encrypt16_behavior(self) -> None:
        key = bytes.fromhex("00112233445566778899AABBCCDDEEFF")
        cipher = proto.RC6(key)
        block = bytes.fromhex("0123456789ABCDEF0011223344556677")
        enc = cipher.encrypt_block(block)
        self.assertNotEqual(enc, block)
        self.assertEqual(cipher.decrypt_block(enc), block)

        # Encrypt applies ISO padding (15 bytes -> 16 bytes -> 1 block),
        # while 16 bytes -> 32 bytes (2 blocks); Encrypt16 returns ONLY block 0 (16 bytes)
        data15 = b"123456789012345"
        ct15 = proto.RC6.encrypt(data15, key)
        self.assertEqual(len(ct15), 16)
        self.assertEqual(proto.RC6.decrypt(ct15, key), data15)

        data16 = b"0123456789ABCDEF"
        ct32 = proto.RC6.encrypt(data16, key)
        ct16_only = proto.RC6.encrypt16(data16, key)
        self.assertEqual(len(ct32), 32)
        self.assertEqual(len(ct16_only), 16)
        self.assertEqual(ct16_only, ct32[:16])
        self.assertEqual(proto.RC6.decrypt(ct16_only, key, unpad=False), data16)

    def test_encrypt_rc6_s1_pairing_derivation(self) -> None:
        imei = "359123456789012"
        nfc_key = "AABBCCDD0011223344556677889900FF"  # last 4 hex = 00FF -> 255
        ct_hex = proto.encrypt_rc6_s1(imei, nfc_key)
        self.assertEqual(len(ct_hex), 32)
        decrypted = proto.RC6.decrypt(bytes.fromhex(ct_hex), bytes.fromhex(nfc_key))
        # Mask "255" repeated over 15 digits -> digitwise mod-10 sum
        expected_mask = ("255" * 5)[:15]
        expected_digits = "".join(
            str((int(imei[i]) + int(expected_mask[i])) % 10) for i in range(15)
        ).encode("utf-8")
        self.assertEqual(decrypted, expected_digits)

    def test_ble_key_unlock_gen_exact_vector(self) -> None:
        imei = "359123456789012"
        in_key = "01020304AABBCCDD11223344EEFF0011"
        v0 = 0x01020304
        v1 = 0xAABBCCDD
        v2 = 0x11223344
        v3 = 0xEEFF0011
        v4 = v0 ^ v2
        dec_str = f"{v0}{v1}{v2}{v3}{v4}"
        expected_interleaved = (
            "".join(dec_str[i] + imei[i] for i in range(len(imei)))
            + dec_str[len(imei) :]
        )
        expected_sha1 = hashlib.sha1(expected_interleaved.encode("utf-8")).hexdigest().upper()
        self.assertEqual(proto.ble_key_unlock_gen(in_key, imei), expected_sha1)

    def test_ble_key_gen_admin_session_derivation(self) -> None:
        in_key = "112233445566778899AABBCCDDEEFF00"
        password = "1234"
        session_bytes = bytes([0x12, 0x34, 0x56, 0x78])
        ct_hex, token_hex = proto.ble_key_gen(
            password, in_key, session_bytes=session_bytes
        )
        self.assertEqual(token_hex, "12345678")
        self.assertEqual(len(ct_hex), 32)

        # Independently verify RC6 key schedule from Rutine.BLEKeyGen
        p1 = in_key[:16]
        p2 = in_key[16:]
        p3 = bytes(a ^ b for a, b in zip(bytes.fromhex(p1), bytes.fromhex(p2))).hex().upper()
        pwd_hex = "31323334"
        concat_hex = p3[:8] + pwd_hex + "00" + p1[8:16] + p2[8:16] + p3[8:16] + p1[:8] + p2[:8]
        sha1_hex = hashlib.sha1(bytes.fromhex(concat_hex)).hexdigest().upper()
        rc6_key_hex = "".join(
            sha1_hex[idx - 2 : idx]
            for idx in range(40, 0, -2)
            if idx not in (36, 24, 10, 2)
        )
        decrypted = proto.RC6.decrypt(
            bytes.fromhex(ct_hex), bytes.fromhex(rc6_key_hex), unpad=False
        )
        self.assertEqual(decrypted, bytes.fromhex(in_key[:-8] + token_hex))

    def test_encrypt_imei_for_bt(self) -> None:
        out = proto.encrypt_imei_for_bt("359123456789012", rnd_byte=0xAA)
        # 359123456789012 decimal = 0x000146AF631E6914 -> swap [4:8][12:16][0:4][8:12]
        expected_hex = f"{359123456789012:016X}"
        swapped = expected_hex[4:8] + expected_hex[12:16] + expected_hex[0:4] + expected_hex[8:12]
        expected_xored = int(swapped, 16) ^ int("AA" * 8, 16)
        self.assertEqual(out, f"{expected_xored:016X}")


class TestInosmartPackets(unittest.TestCase):
    def test_header_flags_and_packet_splitting(self) -> None:
        pkt1 = proto.build_get_rlogin_access(
            proto.BleRTask.MAIN_KEYLOCK_OPEN,
            session_id=42,
            admin_session_token="CAFEBABE",
        )
        self.assertEqual(len(pkt1) % 4, 0)
        hdr1 = proto.MsgsHeader.decode(pkt1)
        self.assertEqual(hdr1.message_id, proto.MsgType.GET_RLOGIN_ACCESS)
        self.assertEqual(hdr1.message_len, 12 + 10)
        self.assertEqual(hdr1.flags, "CAFEBABE")

        # 3-byte payload triggers 1 byte padding (16 + 3 = 19 -> 20 bytes)
        pkt2 = proto.build_set_unlocked_entrance(30, int(proto.FreeEntranceIO.MAIN_DOOR_RELAY), session_id=43)
        self.assertEqual(len(pkt2), 20)
        self.assertEqual(pkt2[-1], 0x00)
        hdr2 = proto.MsgsHeader.decode(pkt2)
        self.assertEqual(hdr2.message_len, 15)  # 12 + 3

        # Split concatenated stream arrived in arbitrary BLE notification fragments
        stream = pkt1 + pkt2
        acc = bytearray()
        parsed = []
        for i in range(0, len(stream), 7):
            acc.extend(stream[i : i + 7])
            parsed.extend(proto.split_response_packets(acc))
        self.assertEqual(len(acc), 0)
        self.assertEqual(parsed, [pkt1, pkt2])

    def test_rlogin_responses(self) -> None:
        key_bytes = bytes.fromhex("00112233445566778899AABBCCDDEEFF")
        payload = (
            key_bytes
            + int(proto.BleSysInfo.DENIED).to_bytes(4, "big")
        )
        pkt = proto.build_packet(proto.MsgType.GET_RLOGIN_ACCESS, payload, packet_type=1)
        resp = proto.parse_response(pkt)
        self.assertIsInstance(resp, proto.RLoginAccessResponse)
        self.assertEqual(resp.access_key_hex, "00112233445566778899AABBCCDDEEFF")
        self.assertTrue(resp.is_denied)

        task_payload = bytes([int(proto.BleRTask.LEARN_NEW_DEVICE), 0x17])
        task_pkt = proto.build_packet(proto.MsgType.SET_RLOGIN_TASK, task_payload, packet_type=1)
        task_resp = proto.parse_response(task_pkt)
        self.assertIsInstance(task_resp, proto.RLoginTaskResponse)
        self.assertTrue(task_resp.success)

    def test_device_info_and_system_info_and_logs(self) -> None:
        # DeviceInfo 79 bytes
        dev_payload = (
            (3600).to_bytes(4, "big")
            + b"\x05"
            + b"\x00\x00\x00\x40"
            + int(proto.DataPeripheral.TOUCH | proto.DataPeripheral.MOTOR_HANDLE).to_bytes(4, "big")
            + bytes([1, 6, 0, 152])  # fsw 1.6.152
            + bytes(8)
            + b"\x01\x02\x03\x04" * 3
            + (100).to_bytes(2, "big")
            + (500).to_bytes(2, "big")
            + bytes([proto.Models.MODEL_K_LED, 2])
            + (12).to_bytes(2, "big")
            + bytes([2, 1, 0, 3])
            + bytes([1, 4, 0, 10])
            + bytes(8)
            + b"\x05\x06\x07\x08" * 3
            + bytes([proto.ModelsIO.MODEL_IO_BT_SOLO, 1])
            + bytes([3, 0, 0, 7])
        )
        self.assertEqual(len(dev_payload), 79)
        dev_pkt = proto.build_packet(proto.MsgType.GET_DEVICE_INFO, dev_payload, packet_type=1)
        dev = proto.parse_response(dev_pkt)
        self.assertIsInstance(dev, proto.DeviceInfoResponse)
        self.assertEqual(dev.main_fw_version, "1.6.152")
        self.assertEqual(dev.io_fw_version, "1.4.10")
        self.assertEqual(dev.ext_net_fw_version, "3.0.7")
        self.assertEqual(dev.dev_model_io, proto.ModelsIO.MODEL_IO_BT_SOLO)
        self.assertTrue(dev.requires_admin_session_on_ble)

        # SystemInfo
        sys_payload = (
            bytes([192, 168, 5, 1])
            + (55555).to_bytes(2, "big")
            + bytes([0x83])  # always_on + wpa2
            + b"INO1234\x00"
            + b"secret\x00"
            + bytes([25, 40])  # hled=25, sled=40
            + (100).to_bytes(2, "big")
            + (250).to_bytes(2, "big")  # 2.50s
            + bytes([6, 1, 0])
            + (12345).to_bytes(4, "big")
        )
        sys_pkt = proto.build_packet(proto.MsgType.GET_SYSTEM_INFO, sys_payload, packet_type=1)
        sys_info = proto.parse_response(sys_pkt)
        self.assertIsInstance(sys_info, proto.SystemInfoResponse)
        self.assertEqual(sys_info.ssid, "INO1234")
        self.assertEqual(sys_info.hled, 25)
        self.assertEqual(sys_info.sled, 40)
        self.assertAlmostEqual(sys_info.user_relay_time_sec, 2.5)
        self.assertEqual(sys_info.mainlock_counter, 12345)
        self.assertTrue(sys_info.bluetooth_always_on)

        # SetSystemInfo request length/alignment
        set_sys = proto.build_set_system_info(
            int(proto.SysInfoMask.HLED),
            hled=35,
            admin_session_token="CAFEBABE",
        )
        self.assertEqual(len(set_sys) % 4, 0)
        self.assertEqual(proto.MsgsHeader.decode(set_sys).flags, "CAFEBABE")

        # LogData (1 item, epoch 2000-01-01 + 86400s = 2000-01-02)
        log_payload = (
            (1).to_bytes(2, "big")
            + bytes([proto.LogEventType.OPEN_MAIN_DOOR])
            + (86400).to_bytes(4, "big")
            + b"Marc\x00"
            + (3).to_bytes(2, "big")
            + (0xFFFF).to_bytes(2, "big")
            + bytes([proto.LogEventSource.APP])
        )
        log_pkt = proto.build_packet(proto.MsgType.GET_LOG_DATA, log_payload, packet_type=1)
        logs = proto.parse_response(log_pkt)
        self.assertIsInstance(logs, proto.LogDataResponse)
        self.assertEqual(logs.count, 1)
        self.assertEqual(logs.items[0].text, "Marc")
        self.assertEqual(logs.items[0].user_id, 3)
        self.assertIsNone(logs.items[0].auth_item_id)
        self.assertEqual(
            logs.items[0].timestamp,
            datetime(2000, 1, 2, 0, 0, tzinfo=timezone.utc),
        )

    def test_cli_parser_works_without_bleak(self) -> None:
        parser = inosmart_ble.build_parser()
        ns = parser.parse_args(["unlock"])
        self.assertEqual(ns.command, "unlock")

        res = subprocess.run(
            [sys.executable, str(ROOT / "inosmart_ble.py"), "--help"],
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertIn("Inotherm Inosmart BT 640 BLE CLI", res.stdout)


if __name__ == "__main__":
    unittest.main()
