"""Discovery and connection management for supported DVB USB devices."""

from __future__ import annotations

from collections.abc import Collection, Iterable
from dataclasses import dataclass
from typing import Any

from .driver_api.device import DVBDevice
from .drivers import discover_device_types
from .usb import LibUSBError, USBDeviceID, USBDeviceInfo, enumerate_usb_devices, usb1


@dataclass(frozen=True, slots=True)
class DeviceConnectionFailure:
    """Describe a supported USB device that could not be opened.

    Attributes:
        device: Enumerated USB device for which opening was attempted.
        error: Exception raised while opening the device.
    """

    device: USBDeviceInfo
    error: Exception


@dataclass(frozen=True, slots=True)
class DeviceRemovalFailure:
    """Describe a detached device whose resources could not be fully closed.

    Attributes:
        device: Previously connected device that was removed from the manager.
        error: Exception raised while closing the device.
    """

    device: DVBDevice
    error: Exception


@dataclass(frozen=True, slots=True)
class DeviceConnectionResult:
    """Report the outcomes of a device discovery and connection pass.

    Attributes:
        connected: Devices opened during this pass.
        unavailable: Supported devices that could not be opened, with errors.
        unsupported: Enumerated devices excluded by the requested or registered
            USB IDs.
        removed: Previously connected devices no longer present during this pass.
        removal_failures: Errors raised while closing devices reported as removed.
    """

    connected: tuple[DVBDevice, ...]
    unavailable: tuple[DeviceConnectionFailure, ...]
    unsupported: tuple[USBDeviceInfo, ...]
    removed: tuple[DVBDevice, ...] = ()
    removal_failures: tuple[DeviceRemovalFailure, ...] = ()


class DVBDeviceManager:
    """Discover, open, track, and close devices supported by DVB drivers.

    If no driver classes are supplied, the manager discovers the built-in
    drivers. USB device identity is tracked by vendor ID, product ID, bus
    number, and device address; a device moving to a different address is
    therefore treated as a different connection.

    Attributes:
        _device_types: Driver classes registered with this manager.
        _drivers_by_id: Mapping from each supported USB ID to its driver class.
        _context: Lazily created libusb context used for enumeration.
        _devices: Devices opened and currently tracked by the manager.
        _connected_usb_addresses: USB identities currently represented in
            ``_devices``.
        _usb_address_by_device: USB identity for each tracked device, keyed by
            the identity of its Python object.
    """

    def __init__(
        self,
        device_types: Iterable[type[DVBDevice]] | None = None,
    ) -> None:
        """Create a manager using built-in drivers or supplied driver classes.

        Args:
            device_types: Driver classes to register. If omitted, discovers
                built-in drivers.

        Raises:
            ValueError: If different driver classes claim the same USB ID.
        """
        self._device_types = (
            discover_device_types() if device_types is None else tuple(device_types)
        )
        self._drivers_by_id = self._build_driver_index()
        self._context: Any | None = None
        self._devices: list[DVBDevice] = []
        self._connected_usb_addresses: set[tuple[int, int, int, int]] = set()
        self._usb_address_by_device: dict[int, tuple[int, int, int, int]] = {}

    def _build_driver_index(self) -> dict[USBDeviceID, type[DVBDevice]]:
        """Build the USB ID lookup and reject conflicting driver claims.

        Returns:
            A mapping from supported USB IDs to the driver class that opens
            them.

        Raises:
            ValueError: If two distinct driver classes claim the same USB ID.
        """
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
        """Return a snapshot of devices currently tracked by the manager.

        Returns:
            An immutable tuple in connection order.
        """
        return tuple(self._devices)

    def supported_ids(self) -> frozenset[USBDeviceID]:
        """Return the USB IDs advertised by registered drivers.

        Returns:
            An immutable set of all registered USB IDs.
        """
        return frozenset(self._drivers_by_id)

    def enumerate(self) -> tuple[USBDeviceInfo, ...]:
        """Enumerate currently attached USB devices.

        Creates and retains the libusb context on the first call; ``close()``
        releases it.

        Returns:
            The devices returned by the USB enumeration helper.
        """
        if self._context is None:
            self._context = usb1.USBContext()
        return enumerate_usb_devices(self._context)

    def connect_all(
        self,
        ids: Collection[USBDeviceID] | None = None,
    ) -> DeviceConnectionResult:
        """Attempt to open all currently attached devices matching the IDs.

        Args:
            ids: Optional allowlist of USB IDs. Defaults to all IDs supported
                by the registered drivers.

        Returns:
            Results for connections, opening failures, unsupported devices,
            and devices removed since the previous pass.

        Note:
            Existing connections are not skipped, so repeated calls may
            attempt to open the same still-attached device again.
        """
        requested_ids = self.supported_ids() if ids is None else frozenset(ids)
        return self._connect(self.enumerate(), requested_ids, only_new=False)

    def connect_new(
        self,
        ids: Collection[USBDeviceID] | None = None,
    ) -> DeviceConnectionResult:
        """Connect to matching devices not already tracked as connected.

        Args:
            ids: Optional allowlist of USB IDs. Defaults to all IDs supported
                by the registered drivers.

        Returns:
            Results for newly opened devices, opening failures, unsupported
            devices, and devices removed since the previous pass.
        """
        requested_ids = self.supported_ids() if ids is None else frozenset(ids)
        return self._connect(self.enumerate(), requested_ids, only_new=True)

    def _connect(
        self,
        devices: Iterable[USBDeviceInfo],
        requested_ids: Collection[USBDeviceID],
        *,
        only_new: bool,
    ) -> DeviceConnectionResult:
        """Reconcile tracked connections and open matching enumerated devices.

        Previously tracked devices absent from the enumeration are removed
        first. Devices whose IDs are outside ``requested_ids`` or lack a
        registered driver are reported as unsupported. Opening exceptions in
        the expected USB, operating-system, and device-validation categories
        are recorded as unavailable rather than aborting the pass.

        Args:
            devices: Snapshot of currently enumerated USB devices.
            requested_ids: USB IDs allowed in this connection pass.
            only_new: If true, skip devices whose USB identity is already
                connected; otherwise attempt to open every matching device.

        Returns:
            The outcomes of this connection and removal pass.
        """
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
        """Forget and close a device, usually after it has disappeared.

        An unknown device is ignored. A known device is removed from manager
        tracking before ``close()`` is called, so a close error does not leave
        it marked as connected.

        Args:
            device: Device instance to remove.

        Returns:
            A ``DeviceRemovalFailure`` if closing raises, otherwise ``None``.
        """
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
        """Return the identity for a device at its current USB bus address.

        Args:
            device: Enumerated USB device to identify.

        Returns:
            A tuple of vendor ID, product ID, bus number, and device address.
        """
        return (
            device.vendor_id,
            device.product_id,
            device.bus_number,
            device.device_address,
        )

    def close(self) -> None:
        """Close tracked devices and release the libusb context.

        Attempts to close devices in reverse connection order. If a device
        raises while closing, iteration stops; tracking state is still cleared
        and release of the context is attempted. Cleanup exceptions propagate.
        """
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
        """Return this manager for use as a context manager."""
        return self

    def __exit__(self, _exception_type: object, _exception: object, _traceback: object) -> None:
        """Close managed devices and the libusb context when leaving a block.

        The exception details from the ``with`` block are accepted to satisfy
        the context-manager protocol; cleanup behavior is the same whether or
        not the block raised.
        """
        self.close()
