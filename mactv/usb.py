"""AF9015 USB control protocol and the libusb1 transport."""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass
from enum import IntEnum
from math import isfinite
from queue import Empty, Queue
from threading import Condition, Event, RLock, Thread, current_thread
from time import monotonic, sleep
from typing import Any, Protocol

import usb1 as _usb1

usb1: Any = _usb1
LibUSBError = getattr(_usb1, "USBError", OSError)

LOG = False             # Enable or disable general USB logging.
RECORD_STATS = False    # Enable or disable asynchronous bulk-read statistics.

DEFAULT_MAX_QUEUE_SIZE = 16 # Default maximum size of the asynchronous transfer queue in MiB.
ASYNC_READ_DRAIN_TIMEOUT = 0.1
ASYNC_READ_CANCEL_TIMEOUT = 2.0


@dataclass(frozen=True, slots=True)
class USBDeviceID:
    """Identify a USB device by its vendor and product IDs."""

    vendor_id: int
    product_id: int


@dataclass(frozen=True, slots=True)
class USBDeviceInfo:
    """Describe an enumerated USB device and retain its libusb reference."""

    vendor_id: int
    product_id: int
    bus_number: int
    device_address: int
    device: Any
    context: Any

    @property
    def device_id(self) -> USBDeviceID:
        """Return the vendor/product identity of this device."""
        return USBDeviceID(self.vendor_id, self.product_id)


def enumerate_usb_devices(context: Any) -> tuple[USBDeviceInfo, ...]:
    """Enumerate connected USB devices using the caller-owned context."""
    return tuple(
        USBDeviceInfo(
            device.getVendorID(),
            device.getProductID(),
            device.getBusNumber(),
            device.getDeviceAddress(),
            device,
            context,
        )
        for device in context.getDeviceList()
    )

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

    def pause_async_reads(self, endpoint: int) -> None:
        """Prevent an endpoint's completed asynchronous reads from being resubmitted."""
        ...

    def stop_async_reads(
        self, endpoint: int, drain_timeout: float = ASYNC_READ_DRAIN_TIMEOUT
    ) -> None:
        """Drain and release an endpoint's asynchronous bulk-read transfers."""
        ...

    def close(self) -> None:
        """Release resources held by the transport."""
        ...

    def log_stats(self) -> None:
        """Log aggregate timing information for asynchronous bulk reads."""
        ...

    def reconnect(self, timeout: float = 5.0, poll_interval: float = 0.1) -> "USBTransport":
        """Reconnect to the same USB device after it re-enumerates."""
        ...


class _AsynchronousReadStats:
    """Collect timing and queue statistics from asynchronous bulk-read callbacks."""

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

    Control transfers use libusb's synchronous API. Configured bulk-read
    endpoints use asynchronous transfers so another read is pending while
    the caller consumes the previous result.
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
        owns_context: bool = True,
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
        self._owns_context = owns_context
        self.usb_handle = usb_handle    # Open libusb device handle.
        self.interface = interface      # Interface claimed for USB transfers.
        self.vendor_id = vendor_id      # Device vendor ID used for reconnecting.
        self.product_id = product_id    # Device product ID used for reconnecting.
        self.async_transfer_count = async_transfer_count  # Transfers kept pending per configured endpoint.
        self.async_endpoints = frozenset(async_endpoints) # Endpoints configured for asynchronous transfers.
        self.async_queue_mib = async_queue_mib # Maximum size of the asynchronous transfer queue in MiB.
        self.async_queue_bytes = async_queue_bytes # Maximum size of the asynchronous transfer queue in bytes.
        self._claimed_interface: Any | None = None  # Optional interface context manager.
        self._is_interface_claimed = False  # Whether the interface must be released on close.
        # The event-thread callbacks and caller threads share all per-endpoint
        # state under this lock; the condition also lets shutdown await callbacks.
        self._async_read_lock = RLock()
        self._async_read_condition = Condition(self._async_read_lock)
        self._closed = False            # Prevents new I/O and repeated cleanup.
        self._event_stop = Event()      # Signals the libusb event thread to stop.
        self._event_thread: Thread | None = None  # Processes asynchronous transfer events.
        # Completed data or errors by endpoint.
        # Queues holding completed data or errors for each endpoint.
        self._async_read_queues: dict[int, Queue[bytes | BaseException | None]] = {}
        # Tracks the number of bytes queued for each endpoint in the asynchronous read queues.
        self._async_read_queue_bytes: dict[int, int] = {}
        # Paused asynchronous transfers by endpoint, waiting to be resumed.
        self._paused_async_reads: dict[int, list[Any]] = {}
        # Stores the last error encountered for each endpoint during asynchronous reads.
        self._async_read_errors: dict[int, BaseException] = {}
        # Fixed transfer length for each initialized endpoint.
        self._async_read_lengths: dict[int, int] = {}
        # Pending asynchronous transfers for each endpoint.
        self._async_read_transfers: dict[int, list[Any]] = {}
        # Endpoints that are in the process of stopping asynchronous reads.
        self._async_read_stopping: set[int] = set()
        # Number of asynchronous reads currently in flight for each endpoint.
        self._async_read_inflight: dict[int, int] = {}
        # Number of pending callbacks for each endpoint.
        self._async_read_callbacks: dict[int, int] = {}
        # Statistics for asynchronous reads.
        self._stats = _AsynchronousReadStats()

    @classmethod
    def open_device(
        cls,
        device: USBDeviceInfo,
        interface: int = 0,
        async_transfer_count: int = 8,
        async_endpoints: Collection[int] = (),
        async_queue_mib: float | None = None,
        owns_context: bool = True,
    ) -> "_LibUSB1Transport":
        """Open and claim one enumerated USB device."""
        transport = cls(
            device.context,
            device.device.open(),
            interface,
            device.vendor_id,
            device.product_id,
            async_transfer_count,
            async_endpoints,
            async_queue_mib,
            owns_context=owns_context,
        )
        transport._claim_interface()
        return transport

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
        """Find and open the first device matching a vendor/product pair."""
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
        """Read a bulk packet, using queued async transfers on configured endpoints."""
        self._require_open()
        data = (
            self._async_bulk_read(endpoint, length, timeout) # Asynchronous bulk read
            if endpoint in self.async_endpoints
            else bytes(self.usb_handle.bulkRead(endpoint, length, timeout=timeout)) # Synchronous bulk read
        )
        if LOG:
            print(f"{monotonic():.6f} IN  ep=0x{endpoint:02x} {data.hex()}", flush=True)
        return data

    def _require_open(self) -> None:
        """Raise an error if the transport has already been closed."""
        if self._closed:
            raise RuntimeError("USB transport is closed")

    def _async_bulk_read(self, endpoint: int, length: int, timeout: int) -> bytes:
        """Read the next completed packet from an endpoint's transfer queue."""
        if length <= 0 or timeout < 0:
            raise ValueError("read length must be positive and timeout must be non-negative")
        with self._async_read_lock:
            if endpoint not in self._async_read_queues:
                # Initialize the asynchronous read queue for this endpoint, and
                # start the async read transfers.
                self._start_async_reads(endpoint, length)
            elif self._async_read_lengths[endpoint] != length:
                raise ValueError("endpoint read length cannot change after initialization")
            if endpoint in self._async_read_stopping:
                raise RuntimeError("asynchronous reads for this endpoint are stopping")
            # While under the async read lock, we check the queue and any read errors.
            queue = self._async_read_queues[endpoint]
            read_error = self._async_read_errors.get(endpoint)

        if read_error is not None:
            raise read_error
        try:
            #
            item = queue.get(timeout=timeout / 1000 if timeout else 0)
        except Empty:
            raise TimeoutError("timed out waiting for asynchronous USB data") from None
        if item is None:
            # None in the queue means the transport has been closed
            raise RuntimeError("USB transport is closed")
        if isinstance(item, BaseException):
            # If the item is an exception, raise it to propagate the error.
            raise item
        with self._async_read_lock:
            # Account for bytes leaving the queue before deciding whether a paused
            # transfer can be submitted again.
            self._async_read_queue_bytes[endpoint] -= len(item)
            self._resume_paused_async_reads(endpoint)
        return item

    def _start_async_reads(self, endpoint: int, length: int) -> None:
        """Start the event thread and prime an endpoint's asynchronous reads."""
        queue: Queue[bytes | BaseException | None] = Queue()
        transfers: list[Any] = []
        self._async_read_queues[endpoint] = queue
        self._async_read_queue_bytes[endpoint] = 0
        self._paused_async_reads[endpoint] = []
        self._async_read_lengths[endpoint] = length
        self._async_read_transfers[endpoint] = transfers
        self._async_read_inflight[endpoint] = 0
        self._async_read_callbacks[endpoint] = 0
        if self._event_thread is None:
            # Start the event thread if it hasn't been started yet. It will handle USB events asynchronously
            self._event_thread = Thread(target=self._run_events, name="mactv-libusb-events", daemon=True)
            self._event_thread.start()
        try:
            for _ in range(self.async_transfer_count):
                # Create and configure a new asynchronous transfer for the endpoint.
                transfer = self.usb_handle.getTransfer()
                transfer.setBulk(endpoint, length, callback=self._make_async_read_callback(endpoint))
                transfers.append(transfer)
                self._submit_async_read(endpoint, transfer)
        except Exception as start_error:
            self.pause_async_reads(endpoint)
            try:
                self.stop_async_reads(endpoint, drain_timeout=0)
            except Exception as cleanup_error:
                raise cleanup_error from start_error
            raise

    def _make_async_read_callback(self, endpoint: int):
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
            with self._async_read_condition:
                if endpoint not in self._async_read_queues:
                    return
                # A completed libusb transfer is no longer submitted before its
                # callback finishes, so track callback execution separately.
                self._async_read_inflight[endpoint] -= 1
                self._async_read_callbacks[endpoint] += 1
                queue = self._async_read_queues[endpoint]

            try:
                if status == completed:
                    data = bytes(transfer.getBuffer()[: transfer.getActualLength()])
                    queue.put(data)
                    with self._async_read_lock:
                        self._async_read_queue_bytes[endpoint] += len(data)
                        queue_bytes = self._async_read_queue_bytes[endpoint]
                    self._stats.record_completion(endpoint, len(data), queue.qsize(), queue_bytes)
                    self._resubmit_or_pause(endpoint, transfer)
                elif status != cancelled:
                    self._stats.record_transfer_error(endpoint, status)
                    error = RuntimeError(f"asynchronous USB transfer failed with status {status}")
                    if status in recoverable and not self._closed:
                        self._resubmit_or_pause(endpoint, transfer)
                    else:
                        self._fail_async_reads(endpoint, error)
                else:
                    self._stats.record_cancelled_transfer()
            finally:
                with self._async_read_condition:
                    self._async_read_callbacks[endpoint] -= 1
                    self._async_read_condition.notify_all()

        return callback

    def _submit_async_read(self, endpoint: int, transfer: Any) -> None:
        """Submit while holding the endpoint lock, then account for its callback."""
        transfer.submit()
        self._async_read_inflight[endpoint] += 1

    def _resubmit_or_pause(self, endpoint: int, transfer: Any) -> None:
        """Resubmit a transfer when its endpoint queue is below the byte limit."""
        with self._async_read_lock:
            if self._closed or endpoint in self._async_read_stopping:
                return
            if self._async_read_queue_bytes[endpoint] >= self.async_queue_bytes:
                self._paused_async_reads[endpoint].append(transfer)
                return
            try:
                self._submit_async_read(endpoint, transfer)
            except Exception as error:
                self._stats.record_resubmit_error()
                self._fail_async_reads(endpoint, error)

    def _resume_paused_async_reads(self, endpoint: int) -> None:
        """Resume paused transfers while an endpoint has queue capacity."""
        while (
            self._paused_async_reads[endpoint]
            and not self._closed
            and self._async_read_queue_bytes[endpoint] < self.async_queue_bytes
        ):
            transfer = self._paused_async_reads[endpoint].pop()
            try:
                self._submit_async_read(endpoint, transfer)
            except Exception as error:
                self._stats.record_resubmit_error()
                self._fail_async_reads(endpoint, error)
                return

    def pause_async_reads(self, endpoint: int) -> None:
        """Stop resubmitting transfers before disabling the device-side endpoint."""
        with self._async_read_condition:
            if endpoint in self._async_read_queues:
                self._async_read_stopping.add(endpoint)

    def stop_async_reads(
        self, endpoint: int, drain_timeout: float = ASYNC_READ_DRAIN_TIMEOUT
    ) -> None:
        """Drain briefly, cancel pending reads, then release endpoint state."""
        if not isfinite(drain_timeout) or drain_timeout < 0:
            raise ValueError("drain_timeout must be finite and non-negative")
        with self._async_read_condition:
            if endpoint not in self._async_read_queues:
                return
            self._async_read_stopping.add(endpoint)
            drain_deadline = monotonic() + drain_timeout
            while self._async_read_inflight[endpoint] or self._async_read_callbacks[endpoint]:
                remaining = drain_deadline - monotonic()
                if remaining <= 0:
                    break
                # Condition.wait releases the endpoint lock so callbacks can
                # finish and notify us while the drain deadline is running.
                self._async_read_condition.wait(remaining)
            transfers = tuple(self._async_read_transfers[endpoint])

        cancel_error: Exception | None = None
        # Cancel outside the lock: libusb completes cancellation through the
        # event thread, whose callback needs this same endpoint lock.
        for transfer in transfers:
            if transfer.isSubmitted():
                try:
                    transfer.cancel()
                except Exception as error:
                    if transfer.isSubmitted() and cancel_error is None:
                        cancel_error = error

        cancel_deadline = monotonic() + ASYNC_READ_CANCEL_TIMEOUT
        with self._async_read_condition:
            while self._async_read_inflight[endpoint] or self._async_read_callbacks[endpoint]:
                remaining = cancel_deadline - monotonic()
                if remaining <= 0:
                    break
                self._async_read_condition.wait(remaining)
            still_submitted = any(transfer.isSubmitted() for transfer in transfers)
            if self._async_read_inflight[endpoint] or self._async_read_callbacks[endpoint] or still_submitted:
                raise TimeoutError(
                    f"timed out cancelling asynchronous USB reads on endpoint 0x{endpoint:02x}"
                ) from cancel_error

            for transfer in transfers:
                transfer.close()
            for mapping in (
                self._async_read_queues,
                self._async_read_queue_bytes,
                self._paused_async_reads,
                self._async_read_errors,
                self._async_read_lengths,
                self._async_read_transfers,
                self._async_read_inflight,
                self._async_read_callbacks,
            ):
                mapping.pop(endpoint, None)
            self._async_read_stopping.discard(endpoint)

    def _fail_async_reads(self, endpoint: int, error: BaseException) -> None:
        """Stop an endpoint after a fatal transfer or resubmission failure."""
        with self._async_read_lock:
            if (
                endpoint in self._async_read_errors
                or endpoint in self._async_read_stopping
                or endpoint not in self._async_read_queues
            ):
                return
            self._async_read_errors[endpoint] = error
            self._async_read_queues[endpoint].put(error)
            transfers = tuple(self._async_read_transfers[endpoint])
        for transfer in transfers:
            if transfer.isSubmitted():
                try:
                    transfer.cancel()
                except Exception:
                    pass

    def _run_events(self) -> None:
        """Process libusb events until the transport is closed."""
        while not self._event_stop.is_set() or any(
            transfer.isSubmitted()
            for transfers in self._async_read_transfers.values()
            for transfer in transfers
        ):
            try:
                self.context.handleEvents()
            except usb1.USBErrorInterrupted:
                pass
            except Exception as error:
                if not self._event_stop.is_set():
                    for queue in self._async_read_queues.values():
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
        for transfers in self._async_read_transfers.values():
            for transfer in transfers:
                if transfer.isSubmitted():
                    try:
                        transfer.cancel()
                    except Exception:
                        pass
        for queue in self._async_read_queues.values():
            queue.put(None) # Signal that the transport has been closed.
        if self._event_thread is not None and self._event_thread is not current_thread():
            self._event_thread.join(timeout=2)
        for transfers in self._async_read_transfers.values():
            for transfer in transfers:
                if not transfer.isSubmitted():
                    transfer.close()
        if self._is_interface_claimed:
            if self._claimed_interface is not None and hasattr(self._claimed_interface, "__exit__"):
                self._claimed_interface.__exit__(None, None, None)
            else:
                self.usb_handle.releaseInterface(self.interface)
        self.usb_handle.close()
        if self._owns_context:
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


def open_usb_transport(
    device: USBDeviceInfo,
    async_endpoints: Collection[int] = (),
    async_queue_mib: float | None = None,
) -> USBTransport:
    """Open an enumerated USB device using its caller-owned context."""
    return _LibUSB1Transport.open_device(
        device,
        async_endpoints=async_endpoints,
        async_queue_mib=async_queue_mib,
        owns_context=False,
    )


def default_transport_factory(
    vendor_id: int,
    product_id: int,
    async_endpoints: Collection[int] = (),
    async_queue_mib: float | None = None,
) -> USBTransport:
    """Create the default libusb transport for a USB device."""
    context = usb1.USBContext()
    try:
        devices = enumerate_usb_devices(context)
        for device in devices:
            if device.device_id == USBDeviceID(vendor_id, product_id):
                return _LibUSB1Transport.open_device(
                    device,
                    async_endpoints=async_endpoints,
                    async_queue_mib=async_queue_mib,
                )
    except Exception:
        context.close()
        raise
    context.close()
    raise ValueError(f"USB device {vendor_id:04x}:{product_id:04x} was not found")
