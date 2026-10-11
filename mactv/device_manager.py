"""Discovery and connection management for supported DVB USB devices."""

from __future__ import annotations

from collections.abc import Collection, Iterable
from dataclasses import dataclass
from typing import Any

from .driver_api.device import DVBDevice
from .usb import LibUSBError, USBDeviceID, USBDeviceInfo, enumerate_usb_devices, usb1


@dataclass(frozen=True, slots=True)
class DeviceConnectionFailure:
    """Describe a supported USB device that could not be opened."""

    device: USBDeviceInfo
    error: Exception


@dataclass(frozen=True, slots=True)
class DeviceConnectionResult:
    """Report successful and unsuccessful connection attempts."""

    connected: tuple[DVBDevice, ...]
    unavailable: tuple[DeviceConnectionFailure, ...]
    unsupported: tuple[USBDeviceInfo, ...]


class DVBDeviceManager:
    """Register DVB drivers and connect to all matching USB devices."""

    def __init__(self, device_types: Iterable[type[DVBDevice]]) -> None:
        """Create a manager for the supplied DVB device driver classes."""
        self._device_types = tuple(device_types)
        self._drivers_by_id = self._build_driver_index()
        self._context: Any | None = None
        self._devices: list[DVBDevice] = []

    def _build_driver_index(self) -> dict[USBDeviceID, type[DVBDevice]]:
        drivers: dict[USBDeviceID, type[DVBDevice]] = {}
        for device_type in self._device_types:
            for device_id in device_type.SUPPORTED_USB_IDS:
                existing = drivers.get(device_id)
                if existing is not None and existing is not device_type:
                    raise ValueError(
                        f"USB ID {device_id.vendor_id:04x}:{device_id.product_id:04x} "
                        f"is claimed by both {existing.__name__} and {device_type.__name__}"
                    )
                drivers[device_id] = device_type
        return drivers

    @property
    def devices(self) -> tuple[DVBDevice, ...]:
        """Return devices successfully connected by this manager."""
        return tuple(self._devices)

    def supported_ids(self) -> frozenset[USBDeviceID]:
        """Return the USB IDs advertised by registered drivers."""
        return frozenset(self._drivers_by_id)

    def enumerate(self) -> tuple[USBDeviceInfo, ...]:
        """Enumerate USB devices using the manager's libusb context."""
        if self._context is None:
            self._context = usb1.USBContext()
        return enumerate_usb_devices(self._context)

    def connect_all(
        self,
        ids: Collection[USBDeviceID] | None = None,
    ) -> DeviceConnectionResult:
        """Connect to every available supported device matching optional IDs."""
        requested_ids = self.supported_ids() if ids is None else frozenset(ids)
        connected: list[DVBDevice] = []
        unavailable: list[DeviceConnectionFailure] = []
        unsupported: list[USBDeviceInfo] = []

        for device in self.enumerate():
            if device.device_id not in requested_ids:
                unsupported.append(device)
                continue
            device_type = self._drivers_by_id.get(device.device_id)
            if device_type is None:
                unsupported.append(device)
                continue
            try:
                connected.append(device_type.open_usb_device(device))
            except (LibUSBError, OSError, PermissionError, RuntimeError, ValueError) as error:
                unavailable.append(DeviceConnectionFailure(device, error))

        self._devices.extend(connected)
        return DeviceConnectionResult(tuple(connected), tuple(unavailable), tuple(unsupported))

    def close(self) -> None:
        """Close connected devices and release the manager's libusb context."""
        try:
            for device in reversed(self._devices):
                device.close()
        finally:
            self._devices.clear()
            if self._context is not None:
                self._context.close()
                self._context = None

    def __enter__(self) -> "DVBDeviceManager":
        return self

    def __exit__(self, _exception_type: object, _exception: object, _traceback: object) -> None:
        self.close()
