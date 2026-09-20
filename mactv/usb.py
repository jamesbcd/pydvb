"""AF9015 USB control protocol and the PyUSB transport."""

from __future__ import annotations

from enum import IntEnum
from time import monotonic, sleep
from typing import Any, Protocol, cast

LOG = False
CADENCE_LOG = False

CONTROL_OUT = 0x02
CONTROL_IN = 0x81
STREAM_ENDPOINTS = (0x84, 0x85)
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

    def log_cadence_summary(self) -> None: ...

    def reconnect(self, timeout: float = 5.0, poll_interval: float = 0.1) -> "USBTransport": ...

class PyUSBTransport:
    """Implement the AF9015 USB transport using a configured PyUSB device."""

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
        self._last_stream_read_end: float | None = None
        self._cadence_reads = 0
        self._cadence_errors = 0
        self._cadence_bytes = 0
        self._cadence_total_read_seconds = 0.0
        self._cadence_min_read_seconds: float | None = None
        self._cadence_max_read_seconds: float | None = None
        self._cadence_min_gap_seconds: float | None = None
        self._cadence_max_gap_seconds: float | None = None

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
        if LOG:
            print(f"{monotonic():.6f} OUT ep=0x{endpoint:02x} {bytes(data).hex()}", flush=True)
        self.usb_device.write(endpoint, data, timeout=timeout)

    def bulk_read(self, endpoint: int, length: int, timeout: int) -> bytes:
        read_started = monotonic()
        try:
            data = bytes(self.usb_device.read(endpoint, length, timeout=timeout))
        except Exception as error:
            if CADENCE_LOG and endpoint in STREAM_ENDPOINTS:
                self._cadence_errors += 1
            raise
        read_finished = monotonic()
        if CADENCE_LOG and endpoint in STREAM_ENDPOINTS:
            gap = (
                read_started - self._last_stream_read_end
                if self._last_stream_read_end is not None
                else None
            )
            read_seconds = read_finished - read_started
            self._cadence_reads += 1
            self._cadence_bytes += len(data)
            self._cadence_total_read_seconds += read_seconds
            self._cadence_min_read_seconds = (
                read_seconds
                if self._cadence_min_read_seconds is None
                else min(self._cadence_min_read_seconds, read_seconds)
            )
            self._cadence_max_read_seconds = (
                read_seconds
                if self._cadence_max_read_seconds is None
                else max(self._cadence_max_read_seconds, read_seconds)
            )
            if gap is not None:
                self._cadence_min_gap_seconds = (
                    gap
                    if self._cadence_min_gap_seconds is None
                    else min(self._cadence_min_gap_seconds, gap)
                )
                self._cadence_max_gap_seconds = (
                    gap
                    if self._cadence_max_gap_seconds is None
                    else max(self._cadence_max_gap_seconds, gap)
                )
            self._last_stream_read_end = read_finished
        if LOG:
            print(f"{read_finished:.6f} IN  ep=0x{endpoint:02x} {data.hex()}", flush=True)
        return data

    def log_cadence_summary(self) -> None:
        """Print aggregate stream-read timing without logging each transfer."""
        if not CADENCE_LOG:
            return
        if not self._cadence_reads:
            print(
                f"USB cadence: reads=0 errors={self._cadence_errors}",
                flush=True,
            )
            return
        average_read = self._cadence_total_read_seconds / self._cadence_reads
        min_read = self._cadence_min_read_seconds
        max_read = self._cadence_max_read_seconds
        min_gap = self._cadence_min_gap_seconds
        max_gap = self._cadence_max_gap_seconds
        assert min_read is not None and max_read is not None
        gap_text = (
            "n/a"
            if min_gap is None or max_gap is None
            else f"{min_gap * 1000:.3f}/{max_gap * 1000:.3f}"
        )
        print(
            f"USB cadence: reads={self._cadence_reads} errors={self._cadence_errors} "
            f"bytes={self._cadence_bytes} "
            f"read_ms(avg/min/max)={average_read * 1000:.3f}/"
            f"{min_read * 1000:.3f}/{max_read * 1000:.3f} "
            f"gap_ms(min/max)={gap_text}",
            flush=True,
        )

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
