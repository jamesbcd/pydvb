"""AF9015 USB control protocol and the libusb1 transport."""

from __future__ import annotations

from collections.abc import Collection
from enum import IntEnum
from math import isfinite
from queue import Empty, Queue
from threading import Event, RLock, Thread, current_thread
from time import monotonic, sleep
from typing import Any, Protocol

import usb1 as _usb1

usb1: Any = _usb1

LOG = False             # Enable or disable general USB logging.
RECORD_STATS = False    # Enable or disable logging of stream read statistics.

DEFAULT_MAX_QUEUE_SIZE = 16 # Default maximum size of the asynchronous transfer queue in MiB.

class USBSpeed(IntEnum):
    """USB link-speed values reported by the transport backends."""
    UNKNOWN = 0
    LOW = 1
    FULL = 2
    HIGH_USB2 = 3
    SUPER_USB3_GEN1 = 4
    SUPER_PLUS_USB3_GEN2 = 5


class USBTransport(Protocol):
    """Common interface implemented by supported USB transport backends."""

    def get_speed(self) -> USBSpeed:
        """Return the negotiated USB link speed."""
        ...

    def bulk_write(self, endpoint: int, data: bytes, timeout: int) -> None:
        """Write bytes to a USB bulk endpoint."""
        ...

    def bulk_read(self, endpoint: int, length: int, timeout: int) -> bytes:
        """Read bytes from a USB bulk endpoint."""
        ...

    def close(self) -> None:
        """Release resources held by the transport."""
        ...

    def log_stats(self) -> None:
        """Log aggregate timing information for stream reads."""
        ...

    def reconnect(self, timeout: float = 5.0, poll_interval: float = 0.1) -> "USBTransport":
        """Reconnect to the same USB device after it re-enumerates."""
        ...


class _AsynchronousReadStats:
    """Collect timing and queue statistics from asynchronous stream callbacks."""

    def __init__(self) -> None:
        self._last_completion: float | None = None # Time of the last completed asynchronous transfer
        self._completions = 0 # Number of completed asynchronous transfers
        self._completion_counts: dict[int, int] = {}
        self._zero_length_completions = 0
        self._bytes = 0 # Total bytes transferred in completed asynchronous transfers
        self._total_interval_seconds = 0.0 # Cumulative time between consecutive completions
        self._min_interval_seconds: float | None = None # Minimum interval between consecutive completions
        self._max_interval_seconds: float | None = None # Maximum interval between consecutive completions
        self._min_queue_depth: int | None = None # Minimum queue depth observed at completion
        self._max_queue_depth: int | None = None # Maximum queue depth observed at completion
        self._min_queue_bytes: int | None = None # Minimum queued bytes observed at completion
        self._max_queue_bytes: int | None = None # Maximum queued bytes observed at completion
        self._transfer_errors = 0 # Count of non-cancelled asynchronous transfer errors
        self._transfer_error_statuses: dict[tuple[int, int], int] = {}
        self._cancelled_transfers = 0
        self._resubmit_errors = 0 # Count of failed transfer resubmissions

    def record_completion(
        self, endpoint: int, byte_count: int, queue_depth: int, queue_bytes: int
    ) -> None:
        """Record one completed transfer after its data enters the queue."""
        if not RECORD_STATS:
            return
        now = monotonic()
        if self._last_completion is not None:
            interval = now - self._last_completion
            self._total_interval_seconds += interval
            self._min_interval_seconds = (
                interval
                if self._min_interval_seconds is None
                else min(self._min_interval_seconds, interval)
            )
            self._max_interval_seconds = (
                interval
                if self._max_interval_seconds is None
                else max(self._max_interval_seconds, interval)
            )
        self._last_completion = now
        self._completions += 1
        self._completion_counts[endpoint] = self._completion_counts.get(endpoint, 0) + 1
        if byte_count == 0:
            self._zero_length_completions += 1
        self._bytes += byte_count
        self._min_queue_depth = (
            queue_depth
            if self._min_queue_depth is None
            else min(self._min_queue_depth, queue_depth)
        )
        self._max_queue_depth = (
            queue_depth
            if self._max_queue_depth is None
            else max(self._max_queue_depth, queue_depth)
        )
        self._min_queue_bytes = (
            queue_bytes
            if self._min_queue_bytes is None
            else min(self._min_queue_bytes, queue_bytes)
        )
        self._max_queue_bytes = (
            queue_bytes
            if self._max_queue_bytes is None
            else max(self._max_queue_bytes, queue_bytes)
        )

    def record_transfer_error(self, endpoint: int, status: int) -> None:
        """Record a non-cancelled asynchronous transfer error and status."""
        if RECORD_STATS:
            self._transfer_errors += 1
            key = (endpoint, status)
            self._transfer_error_statuses[key] = self._transfer_error_statuses.get(key, 0) + 1

    def record_cancelled_transfer(self) -> None:
        """Record a transfer cancelled during transport shutdown."""
        if RECORD_STATS:
            self._cancelled_transfers += 1

    def record_resubmit_error(self) -> None:
        """Record a failed transfer resubmission."""
        if RECORD_STATS:
            self._resubmit_errors += 1

    def log_summary(self) -> None:
        """Print aggregate asynchronous transfer statistics."""
        if not RECORD_STATS:
            return
        if not self._completions:
            error_statuses = ",".join(
                f"0x{endpoint:02x}:{status}={count}"
                for (endpoint, status), count in sorted(self._transfer_error_statuses.items())
            ) or "none"
            print(
                "USB async summary: completions=0 transfer_errors="
                f"{self._transfer_errors} cancelled={self._cancelled_transfers} "
                f"transfer_error_statuses={error_statuses} "
                f"resubmit_errors={self._resubmit_errors}",
                flush=True,
            )
            return
        interval_text = "n/a" 
        if self._min_interval_seconds is not None and self._max_interval_seconds is not None:
            average_interval = self._total_interval_seconds / (self._completions - 1)
            interval_text = (
                f"{average_interval * 1000:.3f}/"
                f"{self._min_interval_seconds * 1000:.3f}/"
                f"{self._max_interval_seconds * 1000:.3f}"
            )
        assert (
            self._min_queue_depth is not None
            and self._max_queue_depth is not None
            and self._min_queue_bytes is not None
            and self._max_queue_bytes is not None
        )
        completion_counts = ",".join(
            f"0x{endpoint:02x}={count}"
            for endpoint, count in sorted(self._completion_counts.items())
        )
        error_statuses = ",".join(
            f"0x{endpoint:02x}:{status}={count}"
            for (endpoint, status), count in sorted(self._transfer_error_statuses.items())
        ) or "none"
        print(
            f"USB async summary: completions={self._completions} bytes={self._bytes} "
            f"completion_intervals_ms(avg/min/max)={interval_text} "
            f"queue_depth(min/max)={self._min_queue_depth}/{self._max_queue_depth} "
            f"queue_size_mib(min/max)={self._min_queue_bytes / 1048576:.3f}/"
            f"{self._max_queue_bytes / 1048576:.3f} "
            f"completed_by_endpoint={completion_counts} "
            f"zero_length={self._zero_length_completions} "
            f"cancelled={self._cancelled_transfers} "
            f"transfer_errors={self._transfer_errors} "
            f"transfer_error_statuses={error_statuses} "
            f"resubmit_errors={self._resubmit_errors}",
            flush=True,
        )


class _LibUSB1Transport:
    """Implement the AF9015 transport with python-libusb1.

    Control transfers use libusb's synchronous API. Stream endpoints use a
    pool of asynchronous transfers so the next USB read is already pending
    while the caller consumes the previous one.
    """

    def __init__(
        self,
        context: Any,
        usb_handle: Any,
        interface: int = 0,
        vendor_id: int | None = None,
        product_id: int | None = None,
        async_transfer_count: int = 6,
        async_endpoints: Collection[int] = (),
        async_queue_mib: float | None = None, # Max size in MiB of the asynchronous transfer queue. None = Default
    ) -> None:
        """Create a transport around an open python-libusb1 handle."""
        if async_transfer_count <= 0:
            raise ValueError("async_transfer_count must be positive")
        if async_queue_mib is None:
            async_queue_mib = DEFAULT_MAX_QUEUE_SIZE
        if not isfinite(async_queue_mib) or async_queue_mib <= 0:
            raise ValueError("async_queue_mib must be positive")
        async_queue_bytes = int(async_queue_mib * 1024 * 1024)
        if async_queue_bytes <= 0:
            raise ValueError("async_queue_mib is too small")
        self.context = context          # libusb context that owns the device handle.
        self.usb_handle = usb_handle    # Open libusb device handle.
        self.interface = interface      # Interface claimed for USB transfers.
        self.vendor_id = vendor_id      # Device vendor ID used for reconnecting.
        self.product_id = product_id    # Device product ID used for reconnecting.
        self.async_transfer_count = async_transfer_count  # Transfers kept pending per stream endpoint.
        self.async_endpoints = frozenset(async_endpoints)
        self.async_queue_mib = async_queue_mib
        self.async_queue_bytes = async_queue_bytes
        self._claimed_interface: Any | None = None  # Optional interface context manager.
        self._is_interface_claimed = False  # Whether the interface must be released on close.
        self._stream_lock = RLock()     # Protects stream endpoint state.
        self._closed = False            # Prevents new I/O and repeated cleanup.
        self._event_stop = Event()      # Signals the libusb event thread to stop.
        self._event_thread: Thread | None = None  # Processes asynchronous transfer events.
        # Completed data or errors by endpoint.
        self._stream_queues: dict[int, Queue[bytes | BaseException | None]] = {}
        self._stream_queue_bytes: dict[int, int] = {}   # Tracks the number of bytes queued for each endpoint.
        self._paused_transfers: dict[int, list[Any]] = {} # Paused asynchronous transfers by endpoint.
        self._stream_errors: dict[int, BaseException] = {} # Stores the last error encountered for each endpoint.
        self._stream_lengths: dict[int, int] = {}  # Fixed transfer length by initialized endpoint.
        self._stream_transfers: dict[int, list[Any]] = {}  # Pending asynchronous transfers by endpoint.
        self._stats = _AsynchronousReadStats()

    @classmethod
    def find(
        cls,
        vendor_id: int,
        product_id: int,
        interface: int = 0,
        async_transfer_count: int = 8,
        async_endpoints: Collection[int] = (),
        async_queue_mib: float | None = None,
    ) -> "_LibUSB1Transport":
        """Find, open, and claim a device by vendor/product ID."""
        context = usb1.USBContext()
        try:
            handle = context.openByVendorIDAndProductID(
                vendor_id, product_id, skip_on_error=True
            )
            if handle is None:
                raise ValueError(f"USB device {vendor_id:04x}:{product_id:04x} was not found")
            transport = cls(
                context,
                handle,
                interface,
                vendor_id,
                product_id,
                async_transfer_count,
                async_endpoints,
                async_queue_mib,
            )
            transport._claim_interface()
            return transport
        except Exception:
            context.close()
            raise

    def _claim_interface(self) -> None:
        """Claim the configured interface and retain its release context."""
        claim = self.usb_handle.claimInterface(self.interface)
        self._claimed_interface = claim
        if hasattr(claim, "__enter__"):
            claim.__enter__()
        self._is_interface_claimed = True

    def get_speed(self) -> USBSpeed:
        """Return the USB speed reported by libusb."""
        try:
            speed = self.usb_handle.getDevice().getDeviceSpeed()
            speed_map = {
                usb1.SPEED_LOW: USBSpeed.LOW,
                usb1.SPEED_FULL: USBSpeed.FULL,
                usb1.SPEED_HIGH: USBSpeed.HIGH_USB2,
                usb1.SPEED_SUPER: USBSpeed.SUPER_USB3_GEN1,
                usb1.SPEED_SUPER_PLUS: USBSpeed.SUPER_PLUS_USB3_GEN2,
            }
            return speed_map.get(speed, USBSpeed.UNKNOWN)
        except (AttributeError, ImportError, OSError, TypeError, ValueError):
            return USBSpeed.UNKNOWN

    def bulk_write(self, endpoint: int, data: bytes, timeout: int) -> None:
        """Write a bulk packet using libusb's synchronous API."""
        if LOG:
            print(f"{monotonic():.6f} OUT ep=0x{endpoint:02x} {bytes(data).hex()}", flush=True)
        self._require_open()
        self.usb_handle.bulkWrite(endpoint, bytes(data), timeout=timeout)

    def bulk_read(self, endpoint: int, length: int, timeout: int) -> bytes:
        """Read a bulk packet, using queued async transfers for stream endpoints."""
        self._require_open()
        data = (
            self._stream_read(endpoint, length, timeout)
            if endpoint in self.async_endpoints
            else bytes(self.usb_handle.bulkRead(endpoint, length, timeout=timeout))
        )
        if LOG:
            print(f"{monotonic():.6f} IN  ep=0x{endpoint:02x} {data.hex()}", flush=True)
        return data

    def _require_open(self) -> None:
        """Raise an error if the transport has already been closed."""
        if self._closed:
            raise RuntimeError("USB transport is closed")

    def _stream_read(self, endpoint: int, length: int, timeout: int) -> bytes:
        """Read the next completed packet from an endpoint's transfer queue."""
        if length <= 0 or timeout < 0:
            raise ValueError("stream read length must be positive and timeout must be non-negative")
        with self._stream_lock:
            if endpoint not in self._stream_queues:
                self._start_stream_endpoint(endpoint, length)
            elif self._stream_lengths[endpoint] != length:
                raise ValueError("stream endpoint read length cannot change after initialization")
            queue = self._stream_queues[endpoint]
            stream_error = self._stream_errors.get(endpoint)

        if stream_error is not None:
            raise stream_error
        try:
            item = queue.get(timeout=timeout / 1000 if timeout else 0)
        except Empty:
            raise TimeoutError("timed out waiting for asynchronous USB data") from None
        if item is None: # None in the queue means the transport has been closed
            raise RuntimeError("USB transport is closed")
        if isinstance(item, BaseException):
            raise item
        with self._stream_lock:
            # We've consumed some bytes from the stream, queue, so decrement the queued byte count.
            self._stream_queue_bytes[endpoint] -= len(item)
            self._resume_paused_transfers(endpoint)
        return item

    def _start_stream_endpoint(self, endpoint: int, length: int) -> None:
        """Start the event thread and prime async transfers for an endpoint."""
        queue: Queue[bytes | BaseException | None] = Queue()
        transfers: list[Any] = []
        self._stream_queues[endpoint] = queue
        self._stream_queue_bytes[endpoint] = 0
        self._paused_transfers[endpoint] = []
        self._stream_lengths[endpoint] = length
        self._stream_transfers[endpoint] = transfers
        if self._event_thread is None:
            # Start the event thread if it hasn't been started yet. It will handle USB events asynchronously
            self._event_thread = Thread(target=self._run_events, name="mactv-libusb-events", daemon=True)
            self._event_thread.start()
        for _ in range(self.async_transfer_count):
            # Allocate and configure an asynchronous transfer for the endpoint.
            transfer = self.usb_handle.getTransfer()
            transfer.setBulk(endpoint, length, callback=self._make_callback(endpoint))
            transfers.append(transfer)
            transfer.submit()

    def _make_callback(self, endpoint: int):
        """Create the callback that queues and resubmits endpoint transfers."""
        def callback(transfer: Any) -> None:
            """Queue a completed transfer and resubmit it when appropriate."""
            status = transfer.getStatus()
            completed = usb1.TRANSFER_COMPLETED
            cancelled = usb1.TRANSFER_CANCELLED
            recoverable = {
                candidate_status
                for name in ("TRANSFER_ERROR", "TRANSFER_TIMED_OUT", "TRANSFER_OVERFLOW")
                if (candidate_status := getattr(usb1, name, None)) is not None
            }
            queue = self._stream_queues.get(endpoint)
            if queue is None:
                return
            if status == completed:
                # Transfer completed successfully; queue the data for consumption.
                data = bytes(transfer.getBuffer()[: transfer.getActualLength()])
                queue.put(data)
                with self._stream_lock:
                    # We've added data to the stream queue, so increment the queued byte count.
                    self._stream_queue_bytes[endpoint] += len(data)
                    queue_bytes = self._stream_queue_bytes[endpoint]
                self._stats.record_completion(endpoint, len(data), queue.qsize(), queue_bytes)
                self._resubmit_or_pause(endpoint, transfer)
            elif status != cancelled:
                self._stats.record_transfer_error(endpoint, status)
                error = RuntimeError(f"asynchronous USB transfer failed with status {status}")
                if status in recoverable and not self._closed:
                    self._resubmit_or_pause(endpoint, transfer)
                else:
                    self._fail_stream_endpoint(endpoint, error)
            else:
                self._stats.record_cancelled_transfer()

        return callback

    def _resubmit_or_pause(self, endpoint: int, transfer: Any) -> None:
        """Resubmit a transfer when its endpoint queue is below the byte limit."""
        with self._stream_lock:
            if self._closed:
                return
            if self._stream_queue_bytes[endpoint] >= self.async_queue_bytes:
                self._paused_transfers[endpoint].append(transfer)
                return
            try:
                transfer.submit()
            except Exception as error:
                self._stats.record_resubmit_error()
                self._fail_stream_endpoint(endpoint, error)

    def _resume_paused_transfers(self, endpoint: int) -> None:
        """Resume paused transfers while an endpoint has queue capacity."""
        while (
            self._paused_transfers[endpoint]
            and not self._closed
            and self._stream_queue_bytes[endpoint] < self.async_queue_bytes
        ):
            transfer = self._paused_transfers[endpoint].pop()
            try:
                transfer.submit()
            except Exception as error:
                self._stats.record_resubmit_error()
                self._fail_stream_endpoint(endpoint, error)
                return

    def _fail_stream_endpoint(self, endpoint: int, error: BaseException) -> None:
        """Stop an endpoint after a fatal transfer or resubmission failure."""
        if endpoint in self._stream_errors:
            # If the endpoint has already failed, do not attempt to fail it again.
            return
        self._stream_errors[endpoint] = error
        queue = self._stream_queues.get(endpoint)
        if queue is not None:
            queue.put(error)
        for transfer in self._stream_transfers.get(endpoint, []):
            if transfer.isSubmitted():
                try:
                    transfer.cancel()
                except Exception:
                    pass

    def _run_events(self) -> None:
        """Process libusb events until the transport is closed."""
        while not self._event_stop.is_set() or any(
            transfer.isSubmitted()
            for transfers in self._stream_transfers.values()
            for transfer in transfers
        ):
            try:
                self.context.handleEvents()
            except usb1.USBErrorInterrupted:
                pass
            except Exception as error:
                if not self._event_stop.is_set():
                    for queue in self._stream_queues.values():
                        queue.put(error)
                return

    def log_stats(self) -> None:
        """Print aggregate asynchronous transfer statistics."""
        self._stats.log_summary()

    def close(self) -> None:
        """Cancel transfers and release the interface, handle, and context."""
        if self._closed:
            return
        self._closed = True
        self._event_stop.set()
        for transfers in self._stream_transfers.values():
            for transfer in transfers:
                if transfer.isSubmitted():
                    try:
                        transfer.cancel()
                    except Exception:
                        pass
        for queue in self._stream_queues.values():
            queue.put(None) # Signal that the transport has been closed.
        if self._event_thread is not None and self._event_thread is not current_thread():
            self._event_thread.join(timeout=2)
        for transfers in self._stream_transfers.values():
            for transfer in transfers:
                if not transfer.isSubmitted():
                    transfer.close()
        if self._is_interface_claimed:
            if self._claimed_interface is not None and hasattr(self._claimed_interface, "__exit__"):
                self._claimed_interface.__exit__(None, None, None)
            else:
                self.usb_handle.releaseInterface(self.interface)
        self.usb_handle.close()
        self.context.close()

    def reconnect(self, timeout: float = 5.0, poll_interval: float = 0.1) -> "_LibUSB1Transport":
        """Release this handle and wait for the same USB device to reappear."""
        if self.vendor_id is None or self.product_id is None:
            raise RuntimeError("reconnect requires a transport created by the default transport factory")
        if timeout <= 0 or poll_interval <= 0:
            raise ValueError("timeout and poll_interval must be positive")

        self.close()
        deadline = monotonic() + timeout
        last_error: Exception | None = None
        while monotonic() < deadline:
            try:
                return type(self).find(
                    self.vendor_id,
                    self.product_id,
                    self.interface,
                    self.async_transfer_count,
                    self.async_endpoints,
                    self.async_queue_mib,
                )
            except (OSError, ValueError, RuntimeError) as error:
                last_error = error
                sleep(poll_interval)
        raise TimeoutError("USB device did not re-enumerate") from last_error


def default_transport_factory(
    vendor_id: int,
    product_id: int,
    async_endpoints: Collection[int] = (),
    async_queue_mib: float | None = None,
) -> USBTransport:
    """Create the default libusb transport for a USB device."""
    return _LibUSB1Transport.find(
        vendor_id,
        product_id,
        async_endpoints=async_endpoints,
        async_queue_mib=async_queue_mib,
    )
