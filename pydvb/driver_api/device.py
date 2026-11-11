"""Base device lifecycle, firmware resolution, and adapter dispatch.

Concrete devices open a USB transport, initialize chipset hardware, and
populate the logical adapters they expose. The device owns one reentrant
control lock shared by those adapters and their frontend/tuner components.
Stream reads release that lock while waiting for data; demuxer synchronization
then coordinates a concurrent stop or close.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable, Mapping
from hashlib import sha256
from os import replace
from pathlib import Path
from tempfile import NamedTemporaryFile
from threading import RLock
from typing import Any, ClassVar

import requests

from pydvb.driver_api.adapter import DVBAdapter
from pydvb.usb import USBDeviceID, USBDeviceInfo, USBTransport


class DVBDevice(ABC):
    """Provide common lifecycle and adapter operations for a DVB device.

    Class attributes describe supported USB models and optional firmware
    acquisition settings for each concrete device implementation.

    Attributes:
        transport: Open USB transport used by the concrete driver.
        device_id: USB vendor/product identity, if known.
        _hardware_initialized: Whether subclass hardware setup has completed.
        _control_lock: Device-wide reentrant lock shared with configured
            adapters, frontends, and tuners.
        adapters: Configured logical adapters owned by this device.
    """

    # Device-specific metadata used by discovery and firmware resolution.
    SUPPORTED_USB_MODELS: ClassVar[Mapping[USBDeviceID, str]] = {}
    FIRMWARE_URLS: ClassVar[tuple[str, ...]] = ()
    FIRMWARE_FILENAME: ClassVar[str | None] = None
    FIRMWARE_SHA256: ClassVar[str | None] = None
    FIRMWARE_CACHE_DIR: ClassVar[Path]

    @classmethod
    @abstractmethod
    def open_usb_device(cls, device: USBDeviceInfo) -> "DVBDevice":
        """Open one enumerated USB device using this driver's implementation."""
        ...

    @abstractmethod
    def is_warm(self) -> bool:
        """Return whether the device firmware is already running."""
        ...

    @abstractmethod
    def log_transport_stats(self) -> None:
        """Log implementation-specific USB transport statistics."""
        ...

    @abstractmethod
    def init_hardware(self) -> None:
        """Initialize device hardware and configure its adapters."""
        ...

    def __init__(
        self,
        transport: USBTransport,
        device_id: USBDeviceID | None = None,
    ) -> None:
        """Create a device wrapper around an open transport.

        Args:
            transport: Open USB transport owned and closed by this device.
            device_id: Optional USB identity used for model lookup.
        """
        self.transport = transport
        self.device_id = device_id
        self._hardware_initialized = False
        # The device owns this lock. Configured adapters, frontends, and
        # tuners share it; stream reads deliberately use separate demux locks.
        self._control_lock = RLock()
        self.adapters: list[DVBAdapter] = []

    @property
    def model_name(self) -> str | None:
        """Return the registered model name for this device's USB ID, if known."""
        if self.device_id is None:
            return None
        return type(self).SUPPORTED_USB_MODELS.get(self.device_id)

    def resolve_firmware_path(self) -> Path:
        """Return verified cached firmware, downloading and caching it when needed.

        Concrete device classes configure ``FIRMWARE_FILENAME``,
        ``FIRMWARE_SHA256``, ``FIRMWARE_URLS``, and ``FIRMWARE_CACHE_DIR``.
        """
        filename = type(self).FIRMWARE_FILENAME
        expected_hash = type(self).FIRMWARE_SHA256
        if not filename or not expected_hash:
            raise RuntimeError("device firmware filename and SHA-256 are not configured")
        if Path(filename).name != filename:
            raise ValueError("firmware filename must not include a directory")
        if len(expected_hash) != 64 or any(character not in "0123456789abcdefABCDEF" for character in expected_hash):
            raise ValueError("firmware SHA-256 must contain exactly 64 hexadecimal characters")
        expected_hash = expected_hash.lower()

        cache_path = type(self).FIRMWARE_CACHE_DIR / filename
        if self._matches_firmware_hash(cache_path, expected_hash):
            return cache_path

        errors: list[str] = []
        for url in type(self).FIRMWARE_URLS:
            temporary_path: Path | None = None
            try:
                with requests.get(url, timeout=15) as response:
                    response.raise_for_status()
                    data = response.content
                if sha256(data).hexdigest() != expected_hash:
                    raise ValueError("downloaded firmware SHA-256 does not match the expected value")

                cache_path.parent.mkdir(parents=True, exist_ok=True)
                with NamedTemporaryFile(dir=cache_path.parent, prefix=f".{filename}.", delete=False) as temporary_file:
                    temporary_path = Path(temporary_file.name)
                    temporary_file.write(data)
                replace(temporary_path, cache_path)
                return cache_path
            except (OSError, requests.RequestException, ValueError) as error:
                errors.append(f"{url}: {error}")
            finally:
                if temporary_path is not None:
                    temporary_path.unlink(missing_ok=True)

        detail = "; ".join(errors) if errors else "no firmware URLs are configured"
        raise IOError(f"could not obtain verified firmware: {detail}")

    @staticmethod
    def _matches_firmware_hash(path: Path, expected_hash: str) -> bool:
        """Return whether ``path`` exists and matches the expected SHA-256."""
        try:
            return sha256(path.read_bytes()).hexdigest() == expected_hash
        except OSError:
            return False

    def _reset_device_state(self) -> None:
        """Clear adapter configuration and mark hardware uninitialized."""
        self.adapters = []
        self._hardware_initialized = False

    def _share_control_lock_with_adapter(self, adapter: DVBAdapter) -> None:
        """Give an adapter and its frontend the device-owned control lock.

        This is the shared frontend/control domain for all logical adapters
        belonging to one physical device, matching Linux's per-device
        ``fe_mutex`` rather than creating one lock per frontend.

        Only device composition code should call this private method.
        Application code should use the device or adapter control methods.
        """
        adapter.bind_to_control_lock(self._control_lock)

    def _share_control_lock_with_all_adapters(self) -> None:
        """Wire this device's lock into all configured adapters.

        Device subclasses call this after constructing adapter objects;
        application code should not call it or rewire locks directly.
        """
        for adapter in self.adapters:
            self._share_control_lock_with_adapter(adapter)

    def _require_initialized(self) -> None:
        """Raise unless hardware setup has completed."""
        if not self._hardware_initialized:
            raise RuntimeError("call init_hardware() before using configured adapters")

    def _get_adapter(self, adapter_index: int) -> DVBAdapter:
        """Return the configured adapter at ``adapter_index``."""
        if not 0 <= adapter_index < len(self.adapters):
            raise ValueError(f"adapter_index {adapter_index} is not configured")
        return self.adapters[adapter_index]

    def __enter__(self) -> DVBDevice:
        """Return this device for use as a context manager."""
        return self

    def __exit__(self, _exception_type: object, _exception: object, _traceback: object) -> None:
        """Close the device when leaving its context."""
        self.close()

    @property
    def adapter_count(self) -> int:
        """Return the configured adapter count after hardware initialization."""
        with self._control_lock:
            self._require_initialized()
            return len(self.adapters) or 1

    def close(self) -> None:
        """Stop streams, sleep initialized frontends, then release the transport."""
        with self._control_lock:
            try:
                for adapter in self.adapters:
                    adapter.stop()
                if self._hardware_initialized:
                    self.sleep()
            finally:
                self.transport.close()

    def sleep(self) -> None:
        """Put all configured frontends into their low-power state.

        Threading:
            Safe to call concurrently with other device control operations;
            all frontend sleep operations are serialized across adapters and
            may block on USB I/O.
        """
        with self._control_lock:
            self._require_initialized()
            for adapter in self.adapters:
                adapter.sleep()

    def tune(self, tuning: Any, adapter_index: int = 0, wait_for_lock: bool = True) -> Any:
        """Tune one adapter's frontend as a unit.

        Threading:
            Safe to call concurrently with other device control operations;
            control operations across all adapters are serialized. Tuning is
            rejected while the selected adapter is streaming.
        """
        with self._control_lock:
            self._require_initialized()
            adapter = self._get_adapter(adapter_index)
            if adapter.tuner is None:
                raise ValueError("adapter_index has no supported tuner")
            return adapter.tune(tuning, wait_for_lock)

    def read_status(self, adapter_index: int = 0) -> Any:
        """Read one adapter's frontend status.

        Threading:
            Safe to call concurrently with other device control operations;
            device control operations are serialized and this may block on
            USB I/O.
        """
        with self._control_lock:
            self._require_initialized()
            return self._get_adapter(adapter_index).read_status()

    def start(self, adapter_index: int = 0) -> None:
        """Start transport-stream delivery for one adapter.

        Threading:
            Safe to call concurrently with other device control operations;
            lifecycle and control operations across all adapters are
            serialized. This may wait for an in-flight read.
        """
        with self._control_lock:
            self._require_initialized()
            self._get_adapter(adapter_index).start()

    def stop(self, adapter_index: int = 0) -> None:
        """Stop transport-stream delivery for one adapter.

        Threading:
            Safe to call concurrently with other device control operations;
            lifecycle and control operations across all adapters are
            serialized. This waits for an in-flight read.
        """
        with self._control_lock:
            self._require_initialized()
            self._get_adapter(adapter_index).stop()

    def read_stream(self, timeout_ms: int = 2000, adapter_index: int = 0) -> bytes:
        """Read non-empty transport-stream data from one adapter.

        Threading:
            The adapter permits one active read. This method snapshots the
            adapter under the device lock, then releases that lock while USB
            blocks so another device thread can initiate shutdown.
        """
        with self._control_lock:
            self._require_initialized()
            adapter = self._get_adapter(adapter_index)
        return adapter.read_stream(timeout_ms)

    def configure_pid_filter(
        self,
        pids: Iterable[int],
        enabled: bool = True,
        adapter_index: int = 0,
    ) -> None:
        """Atomically configure one adapter's software PID allow-list.

        Threading:
            Safe to call concurrently with other device control operations;
            control operations across all adapters are serialized and may
            block on USB I/O.
        """
        with self._control_lock:
            self._require_initialized()
            self._get_adapter(adapter_index).configure_pid_filter(pids, enabled)

    def set_pid_filter_enabled(self, enabled: bool = True, adapter_index: int = 0) -> None:
        """Enable or disable one adapter's PID allow-list.

        Threading:
            Safe to call concurrently with other device control operations;
            control operations across all adapters are serialized and may
            block on USB I/O.
        """
        with self._control_lock:
            self._require_initialized()
            self._get_adapter(adapter_index).set_pid_filter_enabled(enabled)
