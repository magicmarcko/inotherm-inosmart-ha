#!/usr/bin/env python3
"""Standalone CLI & client for Inotherm Inosmart (BT 640) doors over BLE."""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import pathlib
import sys
import types
from typing import Any, Dict

_ROOT = pathlib.Path(__file__).resolve().parent
_COMP_DIR = _ROOT / "custom_components" / "inosmart"


def _load_submodule(subname: str, package_name: str = "_inosmart_standalone"):
    """Load protocol.py / client.py directly without executing HA's __init__.py."""
    if package_name not in sys.modules:
        pkg = types.ModuleType(package_name)
        pkg.__path__ = [str(_COMP_DIR)]  # type: ignore[attr-defined]
        sys.modules[package_name] = pkg
    full_name = f"{package_name}.{subname}"
    if full_name in sys.modules:
        return sys.modules[full_name]
    spec = importlib.util.spec_from_file_location(full_name, _COMP_DIR / f"{subname}.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[full_name] = mod
    spec.loader.exec_module(mod)
    return mod


_proto = _load_submodule("protocol")
_cli_mod = _load_submodule("client")

BleRTask = _proto.BleRTask
FreeEntranceIO = _proto.FreeEntranceIO
LogEventType = _proto.LogEventType
generate_synthetic_imei = _proto.generate_synthetic_imei
InosmartBleClient = _cli_mod.InosmartBleClient
discover_inosmart_doors = _cli_mod.discover_inosmart_doors

DEFAULT_CONFIG_PATH = pathlib.Path.home() / ".config" / "inosmart" / "config.json"


def load_config(path: pathlib.Path) -> Dict[str, Any]:
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {}


def save_config(path: pathlib.Path, cfg: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")


async def _connect_and_maybe_admin(
    args: argparse.Namespace, cfg: Dict[str, Any]
) -> InosmartBleClient:
    address = args.address or cfg.get("address")
    if not address:
        raise SystemExit(
            "No BLE address given. Run `inosmart_ble.py scan` and pass `--address XX:XX:XX:XX:XX:XX`."
        )
    imei = args.imei or cfg.get("imei")
    if not imei:
        imei = generate_synthetic_imei()
        cfg["imei"] = imei
        save_config(args.config, cfg)
        print(f"[info] Generated & saved synthetic IMEI: {imei}")

    client = InosmartBleClient(address, imei, timeout=args.timeout)
    for attempt in range(1, 6):
        try:
            await client.connect()
            break
        except Exception as err:
            print(f"[retry {attempt}/5] Verbindung fehlgeschlagen: {type(err).__name__}")
            await client.disconnect()
            if attempt == 5:
                raise
            await asyncio.sleep(2)
    password = getattr(args, "password", None)
    if password is not None:
        await client.admin_login(password)
    return client


async def async_main(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)

    if args.command == "scan":
        doors = await discover_inosmart_doors(timeout=args.scan_timeout)
        if not doors:
            print("No Inosmart doors found.")
            return 1
        for d in doors:
            print(f"{d.address}\t{d.name}\trssi={d.rssi}")
        return 0

    if args.command == "pair":
        address = args.address or cfg.get("address")
        if not address:
            print("Scanning for Inosmart door...")
            doors = await discover_inosmart_doors(timeout=args.scan_timeout)
            if not doors:
                raise SystemExit("No Inosmart door found in BLE range.")
            address = doors[0].address
            print(f"Found door: {doors[0].name} ({address})")

        imei = args.imei or cfg.get("imei") or generate_synthetic_imei()
        cfg["address"] = address
        cfg["imei"] = imei
        save_config(args.config, cfg)
        print(f"Pairing with door {address} using IMEI {imei} ...")
        print("(Ensure door LEDs are solid white: Button 1 + Admin finger on control unit)")
        async with InosmartBleClient(address, imei, timeout=args.timeout) as client:
            resp = await client.pair_phone()
            print(f"Paired successfully! err=0x{resp.err:02X}")
        return 0

    task_map = {
        "unlock": BleRTask.MAIN_KEYLOCK_OPEN,
        "ready2open": BleRTask.MAIN_KEYLOCK_READY2OPEN,
        "relay2": BleRTask.RELAY2_OPEN,
        "gw-relay1": BleRTask.GW_RELAY1_OPEN,
        "gw-relay2": BleRTask.GW_RELAY2_OPEN,
        "gw-both": BleRTask.GW_RELAY1_RELAY2_OPEN,
    }
    if args.command in task_map:
        client = await _connect_and_maybe_admin(args, cfg)
        try:
            resp = await client.run_unlock_task(task_map[args.command])
            print(f"{args.command} OK (err=0x{resp.err:02X})")
        finally:
            await client.disconnect()
        return 0

    client = await _connect_and_maybe_admin(args, cfg)
    try:
        if args.command == "info":
            dev = await client.get_device_info()
            print(json.dumps(dev.__dict__, indent=2, default=str))
            try:
                sys_info = await client.get_system_info()
                print("--- System Info ---")
                print(json.dumps(sys_info.__dict__, indent=2, default=str))
            except Exception as err:
                print(f"[note] get_system_info skipped ({err}) - try --password after Button 3")
        elif args.command == "state":
            st = await client.get_system_state()
            print(f"state={st.state} state_ext={st.state_ext}")
        elif args.command == "clock":
            clk = await client.get_sys_clock()
            print(f"datetime={clk.device_datetime.isoformat()} temp={clk.temperature_c}C")
        elif args.command == "set-clock":
            await client.set_sys_clock()
            print("Clock synchronized.")
        elif args.command == "set-led":
            ack = await client.set_leds(hled=args.hled, sled=args.sled)
            print(f"set-led ack cmd={ack.cmd} err=0x{ack.err:02X}")
        elif args.command == "set-handle-led":
            ack = await client.set_motor_handle_leds(args.led1, args.led2)
            print(f"set-handle-led ack cmd={ack.cmd} err=0x{ack.err:02X}")
        elif args.command == "free-passage":
            io_mask = 0
            if args.main_door:
                io_mask |= int(FreeEntranceIO.MAIN_DOOR_RELAY)
            if args.relay2:
                io_mask |= int(FreeEntranceIO.USER_RELAY)
            if not io_mask:
                io_mask = int(FreeEntranceIO.MAIN_DOOR_RELAY)
            ack = await client.set_unlocked_entrance(args.minutes, io_mask)
            print(f"free-passage minutes={args.minutes} io_mask={io_mask} err=0x{ack.err:02X}")
        elif args.command == "logs":
            logs = await client.get_logs(count=args.count, event_type=int(LogEventType.ALL))
            for item in logs.items:
                print(
                    f"{item.timestamp.isoformat()} type={item.event_type} "
                    f"src={item.source} uid={item.user_id} text={item.text!r}"
                )
    finally:
        await client.disconnect()
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Inotherm Inosmart BT 640 BLE CLI")
    p.add_argument("--address", help="Door BLE MAC/UUID address")
    p.add_argument("--imei", help="15-digit client IMEI override")
    p.add_argument("--config", type=pathlib.Path, default=DEFAULT_CONFIG_PATH)
    p.add_argument("--timeout", type=float, default=12.0)
    p.add_argument("--scan-timeout", type=float, default=6.0)

    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("scan", help="Scan for Inosmart BLE doors")
    sub.add_parser("pair", help="Pair synthetic IMEI (door LEDs must be white: Button 1 + Admin)")

    for name, help_txt in (
        ("unlock", "Open main door lock (MAIN_KEYLOCK_OPEN = 0)"),
        ("ready2open", "Trigger day/ready2open mode (MAIN_KEYLOCK_READY2OPEN = 1)"),
        ("relay2", "Trigger Relay 2 (RELAY2_OPEN = 2)"),
        ("gw-relay1", "Trigger GW Relay 1 (GW_RELAY1_OPEN = 3)"),
        ("gw-relay2", "Trigger GW Relay 2 (GW_RELAY2_OPEN = 4)"),
        ("gw-both", "Trigger both GW Relays (GW_RELAY1_RELAY2_OPEN = 5)"),
    ):
        sub.add_parser(name, help=help_txt)

    for name, help_txt in (
        ("info", "Read device info & system info"),
        ("state", "Read current enrollment/standby state"),
        ("clock", "Read device clock & temperature"),
        ("set-clock", "Sync door RTC to current UTC time"),
    ):
        sp = sub.add_parser(name, help=help_txt)
        sp.add_argument("--password", help="Admin password (requires Button 3 or BT Demo mode)")

    sp_led = sub.add_parser("set-led", help="Set integrated (HLED 0..60) / external (SLED 0..75) LED")
    sp_led.add_argument("--password", required=True)
    sp_led.add_argument("--hled", type=int)
    sp_led.add_argument("--sled", type=int)

    sp_mled = sub.add_parser("set-handle-led", help="Set motor handle LEDs")
    sp_mled.add_argument("--password", required=True)
    sp_mled.add_argument("--led1", type=int, required=True)
    sp_mled.add_argument("--led2", type=int, required=True)

    sp_fp = sub.add_parser("free-passage", help="Set Free Passage (Freier Durchgang)")
    sp_fp.add_argument("--password", required=True)
    sp_fp.add_argument("--minutes", type=int, required=True, help="0 to disable")
    sp_fp.add_argument("--main-door", action="store_true")
    sp_fp.add_argument("--relay2", action="store_true")

    sp_logs = sub.add_parser("logs", help="Read recent door event logs")
    sp_logs.add_argument("--password", required=True)
    sp_logs.add_argument("--count", type=int, default=20)
    return p


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        return asyncio.run(async_main(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
