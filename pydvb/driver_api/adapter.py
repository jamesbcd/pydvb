"""Adapter-level coordination of DVB frontend control and stream delivery.

An adapter serializes frontend and demux lifecycle operations through its
control lock, while the demuxer manages its own blocking read lifecycle.
Software PID filtering is applied to demuxed, packet-aligned transport-stream
data unless a device-specific adapter replaces that behavior with hardware
filtering.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from collections.abc import Iterable
from threading import RLock
from time import monotonic
from typing import Any

from .demux import DVBDemuxer
from .frontend import DVBFrontend


logger = logging.getLogger(__name__)


@dataclass
class DVBAdapter:
    """Coordinate a frontend and demuxer for one logical DVB adapter.

    The adapter's control lock is normally replaced by its owning device's
    lock. The demuxer retains separate locks for stream lifecycle and PID
    bookkeeping.

    Attributes:
        frontend: Frontend that performs tuning and reports reception status.
        demuxer: Stream demuxer supplying transport-stream packets.
        _control_lock: Reentrant lock shared with the owning device when
            composed; serializes hardware-control operations.
        _sw_pid_filter_lock: Protects the software PID filter configuration
            while stream data is being processed.
        _sw_pid_filter_pids: Immutable allow-list of 13-bit transport PIDs.
        _sw_pid_filter_enabled: Whether the software allow-list is applied.
        _first_sw_filter_call: Whether the next filtering pass should log the
            current software-filter state.
    """

    frontend: DVBFrontend
    demuxer: DVBDemuxer
    _control_lock: RLock = field(default_factory=RLock, init=False, repr=False)
    _sw_pid_filter_lock: RLock = field(default_factory=RLock, init=False, repr=False)
    _sw_pid_filter_pids: frozenset[int] = field(default_factory=frozenset, init=False, repr=False)
    _sw_pid_filter_enabled: bool = field(default=False, init=False, repr=False)
    _first_sw_filter_call = False

    def bind_to_control_lock(self, control_lock: RLock) -> None:
        """Bind to a device-owned lock and pass it to the frontend.

        The owning device calls this while composing its adapter graph.
        Application code should use adapter operations, not replace this
        lock after configuration.
        """
        self._control_lock = control_lock
        bind_frontend_to_lock = getattr(self.frontend, "bind_to_control_lock", None)
        if bind_frontend_to_lock is not None:
            bind_frontend_to_lock(control_lock)

    @property
    def adapter_index(self) -> int:
        """Return the index assigned to this adapter by its frontend."""
        return self.frontend.adapter_index

    @property
    def tuner(self) -> object | None:
        """Return the frontend's tuner, or ``None`` if it has no tuner."""
        return self.frontend.tuner

    def init_hardware(self) -> None:
        """Initialize frontend hardware while holding the control lock."""
        with self._control_lock:
            self.frontend.init_hardware()

    def sleep(self) -> None:
        """Put the frontend into its low-power state under the control lock."""
        with self._control_lock:
            self.frontend.sleep()

    def tune(self, tuning: Any, wait_for_lock: bool = True) -> Any:
        """Tune the frontend, rejecting changes while stream delivery is active.

        Args:
            tuning: Device/frontend-specific tuning configuration.
            wait_for_lock: Whether the frontend should wait for reception lock.

        Returns:
            The frontend's tuning result.

        Raises:
            RuntimeError: If the demuxer is currently running.
        """
        with self._control_lock:
            if getattr(self.demuxer, "running", False):
                raise RuntimeError("stop the adapter before tuning")
            return self.frontend.tune(tuning, wait_for_lock)

    def read_status(self) -> Any:
        """Return the frontend's current reception status."""
        with self._control_lock:
            return self.frontend.read_status()

    def start(self) -> None:
        """Start transport-stream delivery through the demuxer."""
        with self._control_lock:
            self.demuxer.start()

    def stop(self) -> None:
        """Stop stream delivery and disable software PID filtering."""
        with self._control_lock:
            self.demuxer.stop()
            self.set_pid_filter_enabled(False)

    def read_stream(self, timeout_ms: int = 2000) -> bytes:
        """Read non-empty transport-stream data within a total timeout.

        The demuxer may return empty chunks while synchronizing or buffering
        packets. It returns only complete, 188-byte packets; empty chunks are
        retried here so adapter callers receive usable stream data or a
        timeout error.

        Args:
            timeout_ms: Total time allowed for a non-empty result.

        Returns:
            Non-empty transport-stream bytes, filtered when configured.

        Raises:
            ValueError: If ``timeout_ms`` is negative.
            TimeoutError: If no non-empty data arrives before the deadline.
        """
        if timeout_ms < 0:
            raise ValueError("timeout_ms must be non-negative")
        deadline = monotonic() + timeout_ms / 1000
        while True:
            remaining_ms = max(0, int((deadline - monotonic()) * 1000))
            data = self.demuxer.read_stream_once(remaining_ms)
            if data:
                data = self._filter_pid_stream(data)
            if data:
                return data
            if monotonic() >= deadline:
                raise TimeoutError("timed out waiting for transport-stream data")

    def configure_pid_filter(self, pids: Iterable[int], enabled: bool = True) -> None:
        """Atomically configure the PID allow-list, and enable or disable PID filtering.

        This base class provides software PID filtering functionality.
        Subclasses may override this method to provide hardware PID filtering.

        Args:
            pids: Iterable of transport-stream PIDs in the inclusive range
                0 through ``0x1FFF``.
            enabled: Whether filtering should be enabled after configuration.

        Raises:
            ValueError: If any PID is outside the valid range.
        """
        with self._control_lock:
            with self._sw_pid_filter_lock:
                self._sw_pid_filter_pids = self._validate_pids(pids)
                self._sw_pid_filter_enabled = enabled
                self._first_sw_filter_call = enabled

    def set_pid_filter_enabled(self, enabled: bool = True) -> None:
        """Enable or disable the PID filtering.

        This base class provides software PID filtering functionality.
        Subclasses may override this method to provide hardware PID filtering.

        Args:
            enabled: Whether to apply the currently configured PID allow-list.
        """
        with self._control_lock:
            with self._sw_pid_filter_lock:
                self._sw_pid_filter_enabled = enabled
                self._first_sw_filter_call = enabled

    @staticmethod
    def _validate_pids(pids: Iterable[int]) -> frozenset[int]:
        """Validate and freeze a collection of 13-bit transport PIDs."""
        pid_set = frozenset(pids)
        if any(not isinstance(pid, int) or not 0 <= pid <= 0x1FFF for pid in pid_set):
            raise ValueError("PID must be between 0 and 0x1FFF")
        return pid_set

    def _filter_pid_stream(self, data: bytes) -> bytes:
        """Apply the software filter and return packets with an allowed PID.

        ``data`` is expected to be packet-aligned and to contain only complete
        packets, as provided by :meth:`DVBDemuxer.read_stream_once`. Any
        incomplete tail is ignored defensively; packet reassembly belongs to
        the demuxer, not this software PID filter.
        """
        with self._sw_pid_filter_lock:
            pids = self._sw_pid_filter_pids
            enabled = self._sw_pid_filter_enabled
            first_filter_call = self._first_sw_filter_call
            if first_filter_call:
                self._first_sw_filter_call = False
        if not enabled or not pids:
            # We're not filtering.
            if first_filter_call:
                logger.debug("Software PID filter inactive")
            return data
        # We are filtering.
        if first_filter_call:
            logger.debug("Software PID filter active with %d PIDs: %s", len(pids), sorted(pids))
        packet_size = 188
        # For efficiency We use memoryviews to reduce unnecessary copying of the original data.
        data_view = memoryview(data)
        filtered_packets = [] # A list of memoryview objects for packets that pass the PID filter
        complete_packet_bytes = len(data) - len(data) % packet_size
        for offset in range(0, complete_packet_bytes, packet_size):
            packet_pid = ((data_view[offset + 1] & 0x1F) << 8) | data_view[offset + 2]
            if packet_pid in pids:
                filtered_packets.append(data_view[offset : offset + packet_size])

        # We convert the list of memoryview objects back into a single bytes object before returning.
        return b"".join(filtered_packets)
