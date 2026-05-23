#!/usr/bin/env python3
"""
Read Xbox Wireless Controller battery using Windows WinRT Bluetooth LE GATT APIs.
Fixed version that works with the current winrt projection.
"""

import asyncio
from winrt.windows.devices.bluetooth import BluetoothLEDevice
from winrt.windows.devices.bluetooth.genericattributeprofile import (
    GattCommunicationStatus,
)
from winrt.windows.devices.enumeration import DeviceInformation

# Xbox controller identifiers (from your device manager)
XBOX_VID = "045E"
XBOX_PID = "0B13"

# Standard GATT Battery Service
BATTERY_SERVICE_UUID = "0000180f-0000-1000-8000-00805f9b34fb"
BATTERY_LEVEL_UUID = "00002a19-0000-1000-8000-00805f9b34fb"


def find_xbox_device_ids():
    """Find all device IDs that look like the Xbox controller."""
    print("Enumerating devices (this may take a second)...")

    devices = DeviceInformation.find_all_async().get()
    print(f"Total devices found: {len(devices)}")

    candidates = []
    for dev in devices:
        dev_id = (dev.id or "").lower()
        name = (dev.name or "").lower()

        if (XBOX_VID.lower() in dev_id or XBOX_PID.lower() in dev_id or
            "xbox" in name or "wireless controller" in name):
            candidates.append((dev.id, dev.name))

    # Deduplicate by ID
    seen = set()
    unique = []
    for dev_id, name in candidates:
        if dev_id not in seen:
            seen.add(dev_id)
            unique.append((dev_id, name))

    return unique


async def read_battery(device_id: str):
    print(f"\nTrying device: {device_id}")

    try:
        ble_device = await BluetoothLEDevice.from_id_async(device_id)
        if not ble_device:
            print("  Could not open as BluetoothLEDevice")
            return None

        print(f"  Opened: {ble_device.name} (connection: {ble_device.connection_status})")

        # Get all GATT services
        result = await ble_device.get_gatt_services_async()
        if result.status != GattCommunicationStatus.SUCCESS:
            print(f"  Failed to enumerate services (status={result.status})")
            return None

        for service in result.services:
            if BATTERY_SERVICE_UUID in str(service.uuid).lower():
                print(f"  Found Battery Service!")

                chars = await service.get_characteristics_async()
                if chars.status != GattCommunicationStatus.SUCCESS:
                    continue

                for char in chars.characteristics:
                    if BATTERY_LEVEL_UUID in str(char.uuid).lower():
                        read = await char.read_value_async()
                        if read.status == GattCommunicationStatus.SUCCESS:
                            data = read.value
                            if data and data.length > 0:
                                # Correct way to read bytes from WinRT IBuffer
                                from winrt.windows.storage.streams import DataReader
                                reader = DataReader.from_buffer(data)
                                level = reader.read_byte()
                                print(f"  ✅ Battery Level: {level}%")
                                return level
                        else:
                            print(f"  Read failed with status {read.status}")

        print("  No readable battery characteristic found on this device.")
        return None

    except Exception as e:
        print(f"  Error: {e}")
        return None


async def main():
    print("=== Xbox Controller Battery via WinRT GATT ===\n")

    candidates = find_xbox_device_ids()

    if not candidates:
        print("\nNo devices matching Xbox / 045E:0B13 found.")
        print("The controller might not be paired over Bluetooth LE.")
        return

    print(f"\nFound {len(candidates)} candidate device(s):")
    for dev_id, name in candidates:
        print(f"  - {name or '(no name)'}")
        print(f"    {dev_id}")

    for dev_id, name in candidates:
        # Only try devices that look like the Xbox controller
        if "xbox" in name.lower() or "0b13" in dev_id.lower():
            level = await read_battery(dev_id)
            if level is not None:
                print(f"\n>>> SUCCESS: Xbox controller battery = {level}%\n")
                return

    print("\nScanned all Xbox candidates but could not read battery level.")
    print("The controller may only expose battery via HID reports (not GATT),")
    print("or it may need to be disconnected + reconnected.")


if __name__ == "__main__":
    asyncio.run(main())
