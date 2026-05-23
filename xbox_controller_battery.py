#!/usr/bin/env python3
"""
Read Xbox controller battery level from the standard Bluetooth LE GATT Battery
Service on Windows.

This script only considers currently connected Bluetooth LE Xbox controllers.
That avoids stale paired-device entries reporting misleading values when no
controller is actually connected.
"""

from __future__ import annotations

import argparse
import asyncio
import ctypes
import time
from dataclasses import dataclass


MICROSOFT_VENDOR_ID = "045e"
BATTERY_SERVICE_UUID = "0000180f-0000-1000-8000-00805f9b34fb"
BATTERY_LEVEL_UUID = "00002a19-0000-1000-8000-00805f9b34fb"


@dataclass(frozen=True)
class XboxController:
    device_id: str
    name: str


class XINPUT_VIBRATION(ctypes.Structure):
    _fields_ = [
        ("wLeftMotorSpeed", ctypes.c_ushort),
        ("wRightMotorSpeed", ctypes.c_ushort),
    ]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Read Xbox controller battery level over Bluetooth LE."
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="List detected connected Xbox Bluetooth LE controllers and exit.",
    )
    return parser.parse_args()


def controller_label(controller: XboxController) -> str:
    return f"{controller.name} ({controller.device_id})"


def pulse_xinput_controller(duration_seconds: float = 0.25) -> bool:
    """Pulse the first connected XInput controller for a very short time."""

    try:
        xinput = ctypes.WinDLL("xinput1_4.dll")
    except OSError:
        try:
            xinput = ctypes.WinDLL("xinput9_1_0.dll")
        except OSError:
            return False

    set_state = xinput.XInputSetState
    set_state.argtypes = [ctypes.c_uint, ctypes.POINTER(XINPUT_VIBRATION)]
    set_state.restype = ctypes.c_uint

    stop = XINPUT_VIBRATION(0, 0)
    pulse = XINPUT_VIBRATION(0x4000, 0x4000)

    for user_index in range(4):
        result = set_state(user_index, ctypes.byref(pulse))
        if result != 0:
            continue

        time.sleep(duration_seconds)
        set_state(user_index, ctypes.byref(stop))
        return True

    return False


def looks_like_xbox_controller(device_id: str, name: str) -> bool:
    device_id_lower = device_id.lower()
    name_lower = name.lower()

    if "xbox" in name_lower:
        return True

    return MICROSOFT_VENDOR_ID in device_id_lower and "controller" in name_lower


def list_connected_xbox_controllers() -> list[XboxController]:
    from winrt.windows.devices.bluetooth import BluetoothConnectionStatus, BluetoothLEDevice
    from winrt.windows.devices.enumeration import DeviceInformation

    devices = DeviceInformation.find_all_async().get()

    controllers: list[XboxController] = []
    seen_devices: set[str] = set()

    for device in devices:
        device_id = device.id or ""
        name = device.name or "Xbox Controller"
        if not device_id:
            continue

        if not looks_like_xbox_controller(device_id, name):
            continue

        try:
            ble_device = BluetoothLEDevice.from_id_async(device_id).get()
        except Exception:
            continue

        if not ble_device:
            continue

        if ble_device.connection_status != BluetoothConnectionStatus.CONNECTED:
            continue

        canonical_id = ble_device.device_id or str(ble_device.bluetooth_address)
        if canonical_id in seen_devices:
            continue

        seen_devices.add(canonical_id)
        controllers.append(XboxController(device_id=device_id, name=ble_device.name or name))

    return controllers


async def read_battery_level(controller: XboxController) -> int | None:
    from winrt.windows.devices.bluetooth import BluetoothLEDevice
    from winrt.windows.devices.bluetooth.genericattributeprofile import (
        GattCommunicationStatus,
    )
    from winrt.windows.storage.streams import DataReader

    ble_device = await BluetoothLEDevice.from_id_async(controller.device_id)
    if not ble_device:
        return None

    services = await ble_device.get_gatt_services_async()
    if services.status != GattCommunicationStatus.SUCCESS:
        return None

    for service in services.services:
        if str(service.uuid).lower() != BATTERY_SERVICE_UUID:
            continue

        characteristics = await service.get_characteristics_async()
        if characteristics.status != GattCommunicationStatus.SUCCESS:
            continue

        for characteristic in characteristics.characteristics:
            if str(characteristic.uuid).lower() != BATTERY_LEVEL_UUID:
                continue

            read_result = await characteristic.read_value_async()
            if read_result.status != GattCommunicationStatus.SUCCESS:
                return None

            buffer = read_result.value
            if not buffer or buffer.length < 1:
                return None

            reader = DataReader.from_buffer(buffer)
            return reader.read_byte()

    return None


async def async_main(args: argparse.Namespace) -> int:
    print("Xbox Controller Battery Reader")
    print()

    controllers = list_connected_xbox_controllers()

    if not controllers:
        print("No Xbox controller found.")
        print("Make sure the controller is connected over Bluetooth and awake, then rerun.")
        return 1

    if args.list:
        for index, controller in enumerate(controllers, start=1):
            print(f"{index}. {controller_label(controller)}")
        return 0

    last_error: Exception | None = None

    for controller in controllers:
        print(f"Trying {controller_label(controller)}")
        try:
            level = await read_battery_level(controller)
        except Exception as exc:
            print(f"  Could not read GATT battery service: {exc}")
            last_error = exc
            continue

        if level is None:
            print("  No readable battery report found.")
            continue

        print()
        print(f"Battery Level: {level}%")
        print("Source:        Bluetooth LE GATT Battery Service")
        if pulse_xinput_controller():
            print("Controller:    Brief rumble pulse sent")
        else:
            print("Controller:    Detected, but no XInput rumble pulse was available")
        return 0

    print()
    print("Could not read an Xbox controller battery level.")
    print("Make sure the controller is connected over Bluetooth and awake, then rerun.")
    print("Some Xbox connections expose battery through a non-GATT path that this script cannot read.")
    if last_error:
        print(f"Last GATT error: {last_error}")
    return 1


def main() -> int:
    args = parse_args()

    try:
        import winrt  # noqa: F401
    except ModuleNotFoundError:
        print("The Python 'winrt' module is not installed.")
        print("Install it with: python -m pip install winrt")
        return 1

    return asyncio.run(async_main(args))


if __name__ == "__main__":
    raise SystemExit(main())
