"""AF9015 USB control protocol and the PyUSB transport."""

from __future__ import annotations

from enum import IntEnum
from time import monotonic, sleep
from typing import Any, Protocol, cast

CONTROL_OUT = 0x02
CONTROL_IN = 0x81
STREAM_ENDPOINTS = (0x84, 0x85)
USB_SPEED_FULL = 2
BUFFER_LENGTH = 63
HEADER_LENGTH = 8
ACK_LENGTH = 2


class USBSpeed(IntEnum):
    UNKNOWN = 0
    LOW = 1
    FULL = 2
    HIGH_USB2 = 3
    SUPER_USB3_GEN1 = 4
    SUPER_PLUS_USB3_GEN2 = 5


class USBTransport(Protocol):
    def get_speed(self) -> USBSpeed: ...

    def bulk_write(self, endpoint: int, data: bytes, timeout: int) -> None: ...

    def bulk_read(self, endpoint: int, length: int, timeout: int) -> bytes: ...

    def close(self) -> None: ...

    def reconnect(self, timeout: float = 5.0, poll_interval: float = 0.1) -> "USBTransport": ...

class PyUSBTransport:
    """Small adapter around a configured ``usb.core.Device`` object."""

    def __init__(
        self,
        usb_device: Any,
        interface: int = 0,
        vendor_id: int | None = None,
        product_id: int | None = None,
    ) -> None:
        self.usb_device = usb_device
        self.interface = interface
        self.vendor_id = vendor_id
        self.product_id = product_id

    def get_speed(self) -> USBSpeed:
        """Return the USB speed reported by PyUSB."""
        try:
            return USBSpeed(getattr(self.usb_device, "speed", USBSpeed.UNKNOWN))
        except (TypeError, ValueError):
            return USBSpeed.UNKNOWN

    @classmethod
    def find(
        cls,
        vendor_id: int,
        product_id: int,
        interface: int = 0,
        configure: bool = False,
    ) -> "PyUSBTransport":
        try:
            import usb.core
        except ImportError as exc:
            raise RuntimeError("PyUSB is required for hardware access") from exc

        usb_device = cast(Any, usb.core.find(idVendor=vendor_id, idProduct=product_id))
        if usb_device is None:
            raise ValueError(f"USB device {vendor_id:04x}:{product_id:04x} was not found")
        if configure:
            usb_device.set_configuration()
        return cls(usb_device, interface, vendor_id, product_id)

    def bulk_write(self, endpoint: int, data: bytes, timeout: int) -> None:
        print(f"{monotonic():.6f} OUT ep=0x{endpoint:02x} {bytes(data).hex()}", flush=True)
        self.usb_device.write(endpoint, data, timeout=timeout)

    def bulk_read(self, endpoint: int, length: int, timeout: int) -> bytes:
        data = bytes(self.usb_device.read(endpoint, length, timeout=timeout))
        print(f"{monotonic():.6f} IN  ep=0x{endpoint:02x} {data.hex()}", flush=True)
        return data

    def close(self) -> None:
        """Release the libusb handle before a device reset or re-enumeration."""
        try:
            import usb.util
        except ImportError as exc:
            raise RuntimeError("PyUSB is required for USB resource cleanup") from exc
        usb.util.dispose_resources(self.usb_device)

    def reconnect(self, timeout: float = 5.0, poll_interval: float = 0.1) -> "PyUSBTransport":
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
