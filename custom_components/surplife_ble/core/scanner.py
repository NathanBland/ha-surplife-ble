"""Bleak discovery helpers that preserve BLEDevice objects."""

from __future__ import annotations

from typing import Any

from bleak import BleakScanner
from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData

from .models import DeviceAdvertisement


def advertisement_snapshot(
    device: BLEDevice, advertisement: AdvertisementData
) -> DeviceAdvertisement:
    """Convert Bleak's platform data into a JSON-safe snapshot."""
    return DeviceAdvertisement(
        identifier=device.address,
        name=device.name,
        rssi=advertisement.rssi,
        local_name=advertisement.local_name,
        service_uuids=sorted(uuid.lower() for uuid in advertisement.service_uuids),
        manufacturer_data={
            manufacturer_id: payload.hex()
            for manufacturer_id, payload in advertisement.manufacturer_data.items()
        },
        service_data={
            uuid.lower(): payload.hex() for uuid, payload in advertisement.service_data.items()
        },
        tx_power=advertisement.tx_power,
    )


async def discover_devices(
    duration: float = 10.0,
) -> list[tuple[BLEDevice, AdvertisementData, DeviceAdvertisement]]:
    """Actively scan and return live Bleak objects alongside snapshots."""
    if duration <= 0:
        raise ValueError("scan duration must be positive")
    discovered: dict[str, tuple[BLEDevice, AdvertisementData]] = await BleakScanner.discover(
        timeout=duration,
        return_adv=True,
        scanning_mode="active",
    )
    rows = [
        (device, advertisement, advertisement_snapshot(device, advertisement))
        for device, advertisement in discovered.values()
    ]
    return sorted(rows, key=lambda row: row[2].rssi, reverse=True)


async def find_device(
    identifier: str,
    *,
    scan_timeout: float = 10.0,
) -> tuple[BLEDevice, AdvertisementData | None]:
    """Scan once, retain the matching BLEDevice, and avoid implicit connect scans."""
    normalized = identifier.casefold()
    discovered: dict[str, tuple[BLEDevice, AdvertisementData]] = await BleakScanner.discover(
        timeout=scan_timeout,
        return_adv=True,
        scanning_mode="active",
    )
    for device, advertisement in discovered.values():
        if device.address.casefold() == normalized:
            return device, advertisement
    raise LookupError(
        f"BLE device {identifier!r} was not found during a {scan_timeout:g}s active scan"
    )


def bleak_details(device: BLEDevice) -> dict[str, Any]:
    """Return diagnostic backend details without requiring them for behavior."""
    details = getattr(device, "details", None)
    return {"repr": repr(details)} if details is not None else {}
