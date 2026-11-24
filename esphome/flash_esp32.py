#!/usr/bin/env python3
"""Flash ESPHome Bluetooth Proxy firmware onto an ESP32 and provision Wi-Fi via Improv Serial."""

from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import os
import pathlib
import re
import subprocess
import sys
import time
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

FIRMWARE_BASE_URL = "https://firmware.esphome.io/bluetooth-proxy/esp32-generic"
FIRMWARE_DIR = pathlib.Path(__file__).resolve().parent / "firmware"
MANIFEST_PATH = FIRMWARE_DIR / "manifest.json"
DEFAULT_PORT = "/dev/ttyUSB0"
DEFAULT_SSID: Optional[str] = None

IMPROV_HEADER = b"IMPROV\x01"
IMPROV_TYPE_CURRENT_STATE = 0x01
IMPROV_TYPE_ERROR_STATE = 0x02
IMPROV_TYPE_RPC_COMMAND = 0x03
IMPROV_TYPE_RPC_RESULT = 0x04

IMPROV_CMD_WIFI_SETTINGS = 0x01

IMPROV_STATE_NAMES = {
    0x02: "Authorized",
    0x03: "Provisioning (verbinde mit WLAN...)",
    0x04: "Provisioned (mit WLAN verbunden!)",
}

IMPROV_ERROR_NAMES = {
    0x00: "Kein Fehler",
    0x01: "Ungültiges RPC-Paket",
    0x02: "Unbekannter RPC-Befehl",
    0x03: "WLAN-Verbindung fehlgeschlagen (Hinweis: ESP32 unterstützt nur 2,4-GHz-WLAN!)",
    0x04: "Nicht autorisiert",
}


def check_serial_permissions(port: str) -> None:
    """Verify the port exists and is readable/writable by the current user."""
    if not os.path.exists(port):
        raise SystemExit(
            f"[FEHLER] Serieller Port {port} nicht gefunden. "
            "Ist der ESP32 per USB angeschlossen?"
        )
    if not os.access(port, os.R_OK | os.W_OK):
        raise SystemExit(
            f"[FEHLER] Keine Lese-/Schreibrechte für {port}.\n"
            "Bitte im Terminal einmal ausführen:\n"
            f"  sudo chmod 666 {port} && sudo usermod -aG dialout $USER"
        )


def _esptool_subcommands() -> Tuple[str, str]:
    """Return subcommand names appropriate for the installed esptool version."""
    try:
        import esptool  # type: ignore[import-not-found]

        major = int(str(getattr(esptool, "__version__", "4")).split(".")[0])
    except Exception:
        major = 4
    if major >= 5:
        return ("chip-id", "write-flash")
    return ("chip_id", "write_flash")


def detect_chip_family(port: str, chip_id_cmd: str) -> str:
    """Detect ESP32 chip family using esptool chip-id output."""
    print(f"[1/3] Erkenne ESP32-Chip an {port}...")
    proc = subprocess.run(
        [sys.executable, "-m", "esptool", "--port", port, chip_id_cmd],
        capture_output=True,
        text=True,
        check=False,
    )
    out = (proc.stdout or "") + "\n" + (proc.stderr or "")
    for line in out.splitlines():
        if "Detecting chip type" in line or "Chip is" in line:
            print(f"      {line.strip()}")
    if proc.returncode != 0:
        raise SystemExit(
            f"[FEHLER] Konnte Chip an {port} nicht auslesen:\n{out.strip()}"
        )
    upper = out.upper()
    for family in ("ESP32-S3", "ESP32-C6", "ESP32-C3"):
        if family in upper or family.replace("-", "") in upper:
            return family
    return "ESP32"


def _sha256_file(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(65536):
            digest.update(chunk)
    return digest.hexdigest().lower()


def _download_file(url: str, dest: pathlib.Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(url, timeout=30) as resp, dest.open("wb") as out:
        while chunk := resp.read(65536):
            out.write(chunk)


def ensure_firmware(chip_family: str) -> Tuple[pathlib.Path, int]:
    """Ensure manifest and SHA-256-verified factory binary exist locally."""
    if not MANIFEST_PATH.exists():
        print("[*] Lade ESPHome Bluetooth-Proxy Manifest herunter...")
        _download_file(f"{FIRMWARE_BASE_URL}/manifest.json", MANIFEST_PATH)

    manifest: Dict[str, Any] = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    for build in manifest.get("builds", []):
        if str(build.get("chipFamily", "")).upper() == chip_family.upper():
            part = build["parts"][0]
            rel_path = str(part["path"])
            offset = int(part.get("offset", 0))
            expected_sha = str(part["sha256"]).lower()
            bin_path = FIRMWARE_DIR / pathlib.Path(rel_path).name
            if not bin_path.exists() or _sha256_file(bin_path) != expected_sha:
                url = f"{FIRMWARE_BASE_URL}/{rel_path}"
                print(f"[*] Lade Firmware für {chip_family} herunter ({url})...")
                _download_file(url, bin_path)
            actual_sha = _sha256_file(bin_path)
            if actual_sha != expected_sha:
                raise SystemExit(
                    f"[FEHLER] SHA-256 Abweichung für {bin_path}: "
                    f"erwartet {expected_sha}, erhalten {actual_sha}"
                )
            print(f"      Firmware geprüft ({manifest.get('version')}, SHA-256 OK): {bin_path.name}")
            return bin_path, offset

    raise SystemExit(f"[FEHLER] Keine passende Firmware für Chip-Familie '{chip_family}' im Manifest.")


def flash_firmware(port: str, bin_path: pathlib.Path, offset: int, write_flash_cmd: str) -> None:
    """Flash the verified factory image onto the ESP32, falling back to 115200 baud."""
    print(f"[2/3] Flashe {bin_path.name} an Offset {hex(offset)} auf {port}...")
    for baud in ("460800", "115200"):
        print(f"      Versuche Baudrate {baud}...")
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "esptool",
                "--port",
                port,
                "--baud",
                baud,
                write_flash_cmd,
                hex(offset),
                str(bin_path),
            ],
            check=False,
        )
        if proc.returncode == 0:
            print("[OK] Firmware erfolgreich geflasht.")
            return
    raise SystemExit("[FEHLER] Flashen mit esptool ist fehlgeschlagen (460800 & 115200 Baud).")


def build_improv_wifi_packet(ssid: str, wifi_password: str) -> bytes:
    """Build an Improv Serial v1 RPC packet for WIFI_SETTINGS (0x01)."""
    ssid_b = ssid.encode("utf-8")
    pass_b = wifi_password.encode("utf-8")
    if len(ssid_b) > 255 or len(pass_b) > 255:
        raise ValueError("SSID und WLAN-Passwort dürfen je maximal 255 Bytes lang sein.")
    inner = bytes([len(ssid_b)]) + ssid_b + bytes([len(pass_b)]) + pass_b
    rpc_data = bytes([IMPROV_CMD_WIFI_SETTINGS, len(inner)]) + inner
    frame = IMPROV_HEADER + bytes([IMPROV_TYPE_RPC_COMMAND, len(rpc_data)]) + rpc_data
    checksum = sum(frame) & 0xFF
    return frame + bytes([checksum])


def _parse_rpc_strings(data: bytes) -> List[str]:
    """Extract length-prefixed UTF-8 strings from an Improv RPC response payload."""
    strings: List[str] = []
    pos = 0
    while pos < len(data):
        slen = data[pos]
        pos += 1
        if pos + slen > len(data):
            break
        strings.append(data[pos : pos + slen].decode("utf-8", errors="replace"))
        pos += slen
    return strings


def _consume_improv_packets(buf: bytearray) -> Tuple[List[Tuple[int, bytes]], str]:
    """Extract valid Improv packets from buffer and return remaining decoded log text."""
    packets: List[Tuple[int, bytes]] = []
    text_chunks: List[str] = []

    while True:
        idx = buf.find(IMPROV_HEADER)
        if idx == -1:
            keep = max(0, len(buf) - (len(IMPROV_HEADER) - 1))
            if keep > 0:
                text_chunks.append(bytes(buf[:keep]).decode("utf-8", errors="ignore"))
                del buf[:keep]
            break

        if idx > 0:
            text_chunks.append(bytes(buf[:idx]).decode("utf-8", errors="ignore"))
            del buf[:idx]

        if len(buf) < 10:
            break
        pkt_type = buf[7]
        plen = buf[8]
        total_len = 9 + plen + 1
        if len(buf) < total_len:
            break

        frame = bytes(buf[: total_len - 1])
        checksum = buf[total_len - 1]
        if (sum(frame) & 0xFF) == checksum:
            packets.append((pkt_type, bytes(buf[9 : 9 + plen])))
            del buf[:total_len]
        else:
            # Not a valid Improv packet; discard header byte and resync
            del buf[:1]

    return packets, "".join(text_chunks)


def provision_wifi_improv(port: str, ssid: str, wifi_password: str) -> None:
    """Reset the ESP32 and send Wi-Fi credentials via Improv Serial."""
    import serial  # type: ignore[import-not-found]

    packet = build_improv_wifi_packet(ssid, wifi_password)
    print(f"[3/3] Übertrage WLAN-Zugangsdaten für '{ssid}' per Improv Serial...")

    with serial.Serial(port, baudrate=115200, timeout=0.2) as ser:
        # Clean hardware reset so ESPHome & Improv boot from scratch
        ser.dtr = False
        ser.rts = True
        time.sleep(0.15)
        ser.rts = False

        # Wait ~3 seconds while consuming boot output
        boot_deadline = time.monotonic() + 3.0
        while time.monotonic() < boot_deadline:
            ser.read(1024)

        ser.write(packet + b"\n")
        ser.flush()
        sent_at = time.monotonic()

        buf = bytearray()
        provisioned = False
        saw_improv_activity = False
        resent = False
        rpc_urls: List[str] = []
        found_ips: List[str] = []
        ip_pattern = re.compile(r"\b192\.168\.\d{1,3}\.\d{1,3}\b")

        deadline = sent_at + 25.0
        while time.monotonic() < deadline:
            if not saw_improv_activity and not resent and (time.monotonic() - sent_at) >= 6.0:
                print("      Sende Improv-Paket erneut (NVS-Erststart)...")
                ser.write(packet + b"\n")
                ser.flush()
                resent = True

            chunk = ser.read(512)
            if chunk:
                buf.extend(chunk)
                packets, log_text = _consume_improv_packets(buf)

                for ip in ip_pattern.findall(log_text):
                    if ip not in found_ips:
                        found_ips.append(ip)
                        print(f"      Erkannte IP im Log: {ip}")

                for pkt_type, payload in packets:
                    if pkt_type == IMPROV_TYPE_CURRENT_STATE and payload:
                        state = payload[0]
                        label = IMPROV_STATE_NAMES.get(state, f"State 0x{state:02X}")
                        print(f"      Improv-Status: {label}")
                        if state in (0x03, 0x04):
                            saw_improv_activity = True
                        if state == 0x04:
                            provisioned = True
                    elif pkt_type == IMPROV_TYPE_ERROR_STATE and payload:
                        saw_improv_activity = True
                        err = payload[0]
                        if err != 0:
                            err_msg = IMPROV_ERROR_NAMES.get(err, f"Unbekannter Fehler 0x{err:02X}")
                            raise SystemExit(f"[FEHLER] Improv meldet Fehler: {err_msg}")
                    elif pkt_type == IMPROV_TYPE_RPC_RESULT and len(payload) >= 2:
                        saw_improv_activity = True
                        cmd = payload[0]
                        rlen = payload[1]
                        if cmd == IMPROV_CMD_WIFI_SETTINGS:
                            rpc_urls = _parse_rpc_strings(payload[2 : 2 + rlen])
                            for url in rpc_urls:
                                print(f"      Improv-Antwort URL: {url}")

                if provisioned and (rpc_urls or found_ips):
                    print("[OK] ESP32 ist im WLAN verbunden und als Bluetooth Proxy bereit!")
                    return

        if provisioned:
            print("[OK] Improv meldet Status Provisioned (0x04).")
            return

        raise SystemExit(
            "[WARNUNG/TIMEOUT] Keine vollständige Improv-Bestätigung innerhalb von 25 Sekunden.\n"
            "Prüfe, ob das WLAN '2,4 GHz' aktiv hat und das Passwort stimmt."
        )


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Flasht ESPHome Bluetooth Proxy auf ESP32 und richtet WLAN per Improv Serial ein."
    )
    parser.add_argument("--port", default=DEFAULT_PORT, help=f"Serieller Port (Standard: {DEFAULT_PORT})")
    parser.add_argument("--ssid", default=DEFAULT_SSID, help="2,4-GHz-WLAN SSID für Improv-Einrichtung")
    parser.add_argument("--wifi-password", default=None, help="WLAN-Passwort direkt übergeben")
    parser.add_argument(
        "--ask-password",
        action="store_true",
        help="WLAN-Passwort verdeckt im Terminal abfragen",
    )
    parser.add_argument(
        "--skip-flash",
        action="store_true",
        help="Flashen überspringen und nur WLAN per Improv Serial senden",
    )
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    wifi_password: Optional[str] = args.wifi_password
    if args.ask_password and wifi_password is None:
        if not args.ssid:
            raise SystemExit("[FEHLER] Bitte --ssid angeben, wenn --ask-password genutzt wird.")
        wifi_password = getpass.getpass(f"WLAN-Passwort für '{args.ssid}': ")

    check_serial_permissions(args.port)

    if not args.skip_flash:
        chip_id_cmd, write_flash_cmd = _esptool_subcommands()
        chip_family = detect_chip_family(args.port, chip_id_cmd)
        bin_path, offset = ensure_firmware(chip_family)
        flash_firmware(args.port, bin_path, offset, write_flash_cmd)
    else:
        print("[*] --skip-flash aktiv, überspringe esptool.")

    if wifi_password is not None:
        if not args.ssid:
            raise SystemExit("[FEHLER] Bitte --ssid zusammen mit --wifi-password angeben.")
        provision_wifi_improv(args.port, args.ssid, wifi_password)
    else:
        print(
            "\n[*] Hinweis: Kein WLAN-Passwort übergeben – Improv-Einrichtung übersprungen.\n"
            "    Du kannst das WLAN nachträglich per:\n"
            f"      .venv/bin/python {pathlib.Path(__file__).name} --skip-flash --ssid \"{args.ssid}\" --ask-password\n"
            "    übertragen."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
