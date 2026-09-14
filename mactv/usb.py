"""AF9015 USB control protocol and the PyUSB transport."""

from __future__ import annotations

from time import monotonic, sleep
from typing import Any, Protocol, cast

CONTROL_OUT = 0x02
CONTROL_IN = 0x81
STREAM_ENDPOINTS = (0x84, 0x85)
USB_SPEED_FULL = 1
BUFFER_LENGTH = 63
HEADER_LENGTH = 8
ACK_LENGTH = 2


class UsbTransport(Protocol):
    def bulk_write(self, endpoint: int, data: bytes, timeout: int) -> None: ...

    def bulk_read(self, endpoint: int, length: int, timeout: int) -> bytes: ...

    def close(self) -> None: ...

    def reconnect(self, timeout: float = 5.0, poll_interval: float = 0.1) -> "UsbTransport": ...

class PyUsbTransport:
    """Small adapter around a configured ``usb.core.Device`` object."""

    def __init__(
        self,
        device: Any,
        interface: int = 0,
        vendor_id: int | None = None,
        product_id: int | None = None,
    ) -> None:
        self.device = device
        self.interface = interface
        self.vendor_id = vendor_id
        self.product_id = product_id

    @property
    def is_full_speed(self) -> bool:
        """Whether PyUSB reports the device as USB full speed."""
        return getattr(self.device, "speed", None) == USB_SPEED_FULL

    @classmethod
    def find(
        cls,
        vendor_id: int,
        product_id: int,
        interface: int = 0,
        configure: bool = False,
    ) -> "PyUsbTransport":
        try:
            import usb.core
        except ImportError as exc:
            raise RuntimeError("PyUSB is required for hardware access") from exc

        device = cast(Any, usb.core.find(idVendor=vendor_id, idProduct=product_id))
        if device is None:
            raise ValueError(f"USB device {vendor_id:04x}:{product_id:04x} was not found")
        if configure:
            device.set_configuration()
        return cls(device, interface, vendor_id, product_id)

    def bulk_write(self, endpoint: int, data: bytes, timeout: int) -> None:
        self.device.write(endpoint, data, timeout=timeout)

    def bulk_read(self, endpoint: int, length: int, timeout: int) -> bytes:
        return bytes(self.device.read(endpoint, length, timeout=timeout))

    def close(self) -> None:
        """Release the libusb handle before a device reset or re-enumeration."""
        try:
            import usb.util
        except ImportError as exc:
            raise RuntimeError("PyUSB is required for USB resource cleanup") from exc
        usb.util.dispose_resources(self.device)

    def reconnect(self, timeout: float = 5.0, poll_interval: float = 0.1) -> "PyUsbTransport":
        """Release this handle and wait for the same USB device to reappear."""
        if self.vendor_id is None or self.product_id is None:
            raise RuntimeError("reconnect requires a transport created by PyUsbTransport.find()")
        if timeout <= 0 or poll_interval <= 0:
            raise ValueError("timeout and poll_interval must be positive")

        self.close()
        deadline = monotonic() + timeout
        last_error: Exception | None = None
        while monotonic() < deadline:
            try:
                return type(self).find(self.vendor_id, self.product_id, self.interface)
            except (OSError, ValueError, RuntimeError) as error:
                last_error = error
                sleep(poll_interval)
        raise TimeoutError("USB device did not re-enumerate") from last_error
