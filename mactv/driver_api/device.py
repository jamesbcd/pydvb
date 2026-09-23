"""Generic device lifecycle and adapter operations for DVB receivers."""

from __future__ import annotations

from abc import ABC, abstractmethod
from threading import RLock
from typing import Any

from mactv.driver_api.adapter import DVBAdapter
from mactv.usb import USBTransport


class DVBDevice(ABC):
    """Provide common lifecycle and adapter operations for a DVB device."""

    @abstractmethod
    def init_hardware(self) -> None:
        """Initialize device hardware and configure its adapters."""
        ...

    def __init__(
        self,
        transport: USBTransport,
        vendor_id: int | None = None,
        product_id: int | None = None,
    ) -> None:
        self.transport = transport
        self.vendor_id = vendor_id
        self.product_id = product_id
        self._hardware_initialized = False
        self._control_lock = RLock()
        self.adapters: list[DVBAdapter] = []

    def _reset_device_state(self) -> None:
        self.adapters = []
        self._hardware_initialized = False

    def _require_initialized(self) -> None:
        if not self._hardware_initialized:
            raise RuntimeError("call init_hardware() before using configured adapters")

    def _get_adapter(self, adapter_index: int) -> DVBAdapter:
        if not 0 <= adapter_index < len(self.adapters):
            raise ValueError(f"adapter_index {adapter_index} is not configured")
        return self.adapters[adapter_index]

    def __enter__(self) -> DVBDevice:
        return self

    def __exit__(self, _exception_type: object, _exception: object, _traceback: object) -> None:
        self.close()

    @property
    def adapter_count(self) -> int:
        with self._control_lock:
            self._require_initialized()
            return len(self.adapters) or 1

    def close(self) -> None:
        with self._control_lock:
            try:
                for adapter in self.adapters:
                    adapter.stop()
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

    def read_ts(self, timeout_ms: int = 2000, adapter_index: int = 0) -> bytes:
        """Read a transport-stream chunk from one adapter.

        Threading:
            The adapter permits one active read. This method snapshots the
            adapter under the device lock, then releases that lock while USB
            blocks so another device thread can initiate shutdown.
        """
        with self._control_lock:
            self._require_initialized()
            adapter = self._get_adapter(adapter_index)
        return adapter.read_ts(timeout_ms)

    def set_pid_filter(self, pid: int, enabled: bool = True, adapter_index: int = 0) -> None:
        """Enable or disable one adapter's hardware PID filter slot.

        Threading:
            Safe to call concurrently with other device control operations;
            control operations across all adapters are serialized and may
            block on USB I/O.
        """
        with self._control_lock:
            self._require_initialized()
            self._get_adapter(adapter_index).set_pid_filter(pid, enabled)

    def enable_pid_filter(self, enabled: bool = True, adapter_index: int = 0) -> None:
        """Enable or disable one adapter's PID filtering.

        Threading:
            Safe to call concurrently with other device control operations;
            control operations across all adapters are serialized and may
            block on USB I/O.
        """
        with self._control_lock:
            self._require_initialized()
            self._get_adapter(adapter_index).enable_pid_filter(enabled)
