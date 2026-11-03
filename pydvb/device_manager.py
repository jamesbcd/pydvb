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
class DeviceRemovalFailure:
    """Describe a detached device whose resources could not be fully closed."""

    device: DVBDevice
    error: Exception


@dataclass(frozen=True, slots=True)
class DeviceConnectionResult:
    """Report successful and unsuccessful connection attempts."""

    connected: tuple[DVBDevice, ...]
    unavailable: tuple[DeviceConnectionFailure, ...]
    unsupported: tuple[USBDeviceInfo, ...]
    removed: tuple[DVBDevice, ...] = ()
    removal_failures: tuple[DeviceRemovalFailure, ...] = ()


class DVBDeviceManager:
    """Register DVB drivers and connect to all matching USB devices."""

    def __init__(self, device_types: Iterable[type[DVBDevice]]) -> None:
        """Create a manager for the supplied DVB device driver classes."""
        self._device_types = tuple(device_types)
        self._drivers_by_id = self._build_driver_index()
        self._context: Any | None = None
        self._devices: list[DVBDevice] = []
        self._connected_usb_addresses: set[tuple[int, int, int, int]] = set()
        self._usb_address_by_device: dict[int, tuple[int, int, int, int]] = {}

    def _build_driver_index(self) -> dict[USBDeviceID, type[DVBDevice]]:
        drivers: dict[USBDeviceID, type[DVBDevice]] = {}
        for device_type in self._device_types:
            for device_id in device_type.SUPPORTED_USB_MODELS:
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
        return self._connect(self.enumerate(), requested_ids, only_new=False)

    def connect_new(
        self,
        ids: Collection[USBDeviceID] | None = None,
    ) -> DeviceConnectionResult:
        """Connect to supported devices not already opened by this manager."""
        requested_ids = self.supported_ids() if ids is None else frozenset(ids)
        return self._connect(self.enumerate(), requested_ids, only_new=True)

    def _connect(
        self,
        devices: Iterable[USBDeviceInfo],
        requested_ids: Collection[USBDeviceID],
        *,
        only_new: bool,
    ) -> DeviceConnectionResult:
        """Open matching enumerated devices, optionally skipping existing connections."""
        connected: list[DVBDevice] = []
        unavailable: list[DeviceConnectionFailure] = []
        unsupported: list[USBDeviceInfo] = []
        removed: list[DVBDevice] = []
        removal_failures: list[DeviceRemovalFailure] = []
        devices = tuple(devices)
        present_usb_addresses = {self._usb_address(device) for device in devices}

        for connected_device in tuple(self._devices):
            usb_address = self._usb_address_by_device[id(connected_device)]
            if usb_address in present_usb_addresses:
                continue
            removed.append(connected_device)
            removal_failure = self.remove_device(connected_device)
            if removal_failure is not None:
                removal_failures.append(removal_failure)

        for device in devices:
            if device.device_id not in requested_ids:
                unsupported.append(device)
                continue
            device_type = self._drivers_by_id.get(device.device_id)
            if device_type is None:
                unsupported.append(device)
                continue
            usb_address = self._usb_address(device)
            if only_new and usb_address in self._connected_usb_addresses:
                continue
            try:
                connected_device = device_type.open_usb_device(device)
            except (LibUSBError, OSError, PermissionError, RuntimeError, ValueError) as error:
                unavailable.append(DeviceConnectionFailure(device, error))
            else:
                connected.append(connected_device)
                self._connected_usb_addresses.add(usb_address)
                self._usb_address_by_device[id(connected_device)] = usb_address

        self._devices.extend(connected)
        return DeviceConnectionResult(
            tuple(connected),
            tuple(unavailable),
            tuple(unsupported),
            tuple(removed),
            tuple(removal_failures),
        )

    def remove_device(self, device: DVBDevice) -> DeviceRemovalFailure | None:
        """Forget and close one device known to have been disconnected."""
        device_index = next(
            (
                index
                for index, connected_device in enumerate(self._devices)
                if connected_device is device
            ),
            None,
        )
        if device_index is None:
            return None

        self._devices.pop(device_index)
        usb_address = self._usb_address_by_device.pop(id(device))
        self._connected_usb_addresses.discard(usb_address)
        try:
            device.close()
        except Exception as error:
            return DeviceRemovalFailure(device, error)
        return None

    @staticmethod
    def _usb_address(device: USBDeviceInfo) -> tuple[int, int, int, int]:
        """Return the identity for one device at its current USB bus address."""
        return (
            device.vendor_id,
            device.product_id,
            device.bus_number,
            device.device_address,
        )

    def close(self) -> None:
        """Close connected devices and release the manager's libusb context."""
        try:
            for device in reversed(self._devices):
                device.close()
        finally:
            self._devices.clear()
            self._connected_usb_addresses.clear()
            self._usb_address_by_device.clear()
            if self._context is not None:
                self._context.close()
                self._context = None

    def __enter__(self) -> "DVBDeviceManager":
        return self

    def __exit__(self, _exception_type: object, _exception: object, _traceback: object) -> None:
        self.close()
