#!/usr/bin/env python3
"""
Read a DualShock 4 battery level from HID input reports.

The DS4 does not expose a normal Bluetooth LE GATT Battery Service. Over
Bluetooth it reports battery in the status byte of the full 0x11 HID input
report. Over USB it reports the same status byte in the full 0x01 report.

This follows the report offsets and battery mapping used by Linux
hid-playstation:
  - Bluetooth 0x11 report: status[0] is byte 32
  - USB full 0x01 report: status[0] is byte 30
  - low nibble 0..10 is battery in 10% buckets, reported as bucket midpoint
"""

from __future__ import annotations

import argparse
import time
import zlib
from collections import Counter
from dataclasses import dataclass
from typing import Iterable


SONY_VENDOR_ID = 0x054C
DS4_PRODUCT_IDS = {
    0x05C4: "DualShock 4 v1",
    0x09CC: "DualShock 4 v2",
    0x0BA0: "DualShock 4 USB wireless adapter",
}

BT_INPUT_REPORT = 0x11
BT_INPUT_REPORT_SIZE = 78
BT_STATUS0_OFFSET = 32
USB_INPUT_REPORT = 0x01
USB_INPUT_REPORT_SIZE = 64
USB_STATUS0_OFFSET = 30

STATUS0_BATTERY_MASK = 0x0F
STATUS0_CABLE_STATE = 0x10
BATTERY_STATUS_FULL = 11

PS_INPUT_CRC32_SEED = 0xA1
PS_OUTPUT_CRC32_SEED = 0xA2


@dataclass(frozen=True)
class BatteryReading:
    percent: int
    state: str
    raw_status: int
    report_id: int
    report_size: int


def dualshock4_devices() -> list[dict]:
    return [
        device
        for device in hid.enumerate()
        if device.get("product_id") in DS4_PRODUCT_IDS
        and device.get("vendor_id") == SONY_VENDOR_ID
    ]


def device_label(device: dict) -> str:
    product_id = device.get("product_id")
    product = DS4_PRODUCT_IDS.get(product_id, "DualShock 4")
    serial = device.get("serial_number") or "no serial"
    path = device.get("path")
    path_text = path.decode(errors="replace") if isinstance(path, bytes) else str(path)
    return f"{product} ({SONY_VENDOR_ID:04x}:{product_id:04x}, {serial}, {path_text})"


def ds4_crc32(seed: int, payload: Iterable[int]) -> int:
    return zlib.crc32(bytes([seed, *payload])) & 0xFFFFFFFF


def valid_bt_input_crc(report: list[int]) -> bool:
    if len(report) != BT_INPUT_REPORT_SIZE:
        return False

    expected = int.from_bytes(bytes(report[-4:]), "little")
    actual = ds4_crc32(PS_INPUT_CRC32_SEED, report[:-4])
    return actual == expected


def make_bt_enable_report() -> bytes:
    """Build a no-op Bluetooth output report with a valid CRC.

    A DS4 paired over Bluetooth can initially send only short 0x01 reports,
    which do not contain battery status. A valid 0x11 output report asks it to
    use the full Bluetooth report stream without changing LED or rumble state.
    """

    report = bytearray(BT_INPUT_REPORT_SIZE)
    report[0] = 0x11
    report[1] = 0xC0  # HID output data is present, report includes CRC32.
    report[2] = 0x00
    crc = ds4_crc32(PS_OUTPUT_CRC32_SEED, report[:-4])
    report[-4:] = crc.to_bytes(4, "little")
    return bytes(report)


def decode_status(report: list[int]) -> BatteryReading | None:
    report_id = report[0] if report else None

    if report_id == BT_INPUT_REPORT and len(report) >= BT_INPUT_REPORT_SIZE:
        trimmed = report[:BT_INPUT_REPORT_SIZE]
        if not valid_bt_input_crc(trimmed):
            return None
        return status_to_reading(
            trimmed[BT_STATUS0_OFFSET],
            report_id=BT_INPUT_REPORT,
            report_size=BT_INPUT_REPORT_SIZE,
        )

    if report_id == USB_INPUT_REPORT and len(report) >= USB_STATUS0_OFFSET + 1:
        # Bluetooth minimal 0x01 reports are only 10 bytes and stop before status.
        return status_to_reading(
            report[USB_STATUS0_OFFSET],
            report_id=USB_INPUT_REPORT,
            report_size=len(report),
        )

    return None


def status_to_reading(raw_status: int, report_id: int, report_size: int) -> BatteryReading:
    battery_data = raw_status & STATUS0_BATTERY_MASK
    cable_connected = bool(raw_status & STATUS0_CABLE_STATE)

    if cable_connected:
        if battery_data < 10:
            percent = battery_data * 10 + 5
            state = "charging"
        elif battery_data == 10:
            percent = 100
            state = "charging"
        elif battery_data == BATTERY_STATUS_FULL:
            percent = 100
            state = "full"
        else:
            percent = 0
            state = "unknown"
    else:
        percent = battery_data * 10 + 5 if battery_data < 10 else 100
        state = "discharging"

    return BatteryReading(
        percent=percent,
        state=state,
        raw_status=raw_status,
        report_id=report_id,
        report_size=report_size,
    )


def open_device(device_info: dict) -> hid.device:
    dev = hid.device()
    dev.open_path(device_info["path"])
    dev.set_nonblocking(True)
    return dev


def collect_readings(device_info: dict, seconds: float) -> list[BatteryReading]:
    dev = open_device(device_info)
    try:
        try:
            dev.write(make_bt_enable_report())
        except OSError:
            # USB devices and already-initialized Bluetooth devices may reject
            # this. Reading reports is still useful.
            pass

        deadline = time.monotonic() + seconds
        readings: list[BatteryReading] = []

        while time.monotonic() < deadline:
            report = dev.read(BT_INPUT_REPORT_SIZE, timeout_ms=50)
            if not report:
                continue

            reading = decode_status(report)
            if reading is not None:
                readings.append(reading)

        return readings
    finally:
        dev.close()


def choose_reading(readings: list[BatteryReading]) -> tuple[BatteryReading, int]:
    counts = Counter((r.percent, r.state, r.raw_status, r.report_id) for r in readings)
    (percent, state, raw_status, report_id), count = counts.most_common(1)[0]
    for reading in readings:
        if (
            reading.percent == percent
            and reading.state == state
            and reading.raw_status == raw_status
            and reading.report_id == report_id
        ):
            return reading, count

    raise RuntimeError("internal error choosing battery reading")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read DualShock 4 battery level.")
    parser.add_argument(
        "--seconds",
        type=float,
        default=3.0,
        help="How long to listen for HID reports. Default: 3.0",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="List detected DualShock 4 HID devices and exit.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    try:
        global hid
        import hid
    except ModuleNotFoundError:
        print("The Python 'hid' module is not installed.")
        print("Install it with: python -m pip install hidapi")
        return 1

    devices = dualshock4_devices()

    print("DualShock 4 Battery Reader")
    print()

    if not devices:
        print("No DualShock 4 HID device found.")
        print("Expected Sony VID 054c with product 05c4, 09cc, or 0ba0.")
        return 1

    if args.list:
        for index, device in enumerate(devices, start=1):
            print(f"{index}. {device_label(device)}")
        return 0

    last_error: Exception | None = None
    for device in devices:
        print(f"Trying {device_label(device)}")
        try:
            readings = collect_readings(device, args.seconds)
        except OSError as exc:
            print(f"  Could not open/read this HID interface: {exc}")
            last_error = exc
            continue

        if not readings:
            print("  No full battery report received.")
            continue

        reading, agreed = choose_reading(readings)
        confidence = round(agreed * 100 / len(readings))

        print()
        print(f"Battery Level: {reading.percent}% ({reading.state})")
        print(f"Samples:       {agreed} of {len(readings)} agreed ({confidence}%)")
        print(f"Report:        0x{reading.report_id:02x}, {reading.report_size} bytes")
        print(f"Status byte:   0x{reading.raw_status:02x}")
        return 0

    print()
    print("Could not read a DS4 battery report.")
    print("Make sure the controller is connected and awake, then press a button and rerun.")
    print("If DS4Windows, Steam Input, or another app has exclusive HID access, close it first.")
    if last_error:
        print(f"Last HID error: {last_error}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
