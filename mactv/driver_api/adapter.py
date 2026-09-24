"""Generic adapter protocol for DVB receivers."""

from __future__ import annotations

from dataclasses import dataclass, field
from collections.abc import Iterable
from threading import RLock
from time import monotonic
from typing import Any

@dataclass
class DVBAdapter:
    """Coordinate a frontend and demuxer for one logical DVB adapter."""

    frontend: Any
    demuxer: Any
    _control_lock: RLock = field(default_factory=RLock, init=False, repr=False)
    _pid_filter_lock: RLock = field(default_factory=RLock, init=False, repr=False)
    _pid_filter_pids: frozenset[int] = field(default_factory=frozenset, init=False, repr=False)
    _pid_filter_enabled: bool = field(default=False, init=False, repr=False)

    @property
    def adapter_index(self) -> int:
        return self.frontend.adapter_index

    @property
    def tuner(self) -> object | None:
        return self.frontend.tuner

    def init_hardware(self) -> None:
        with self._control_lock:
            self.frontend.init_hardware()

    def sleep(self) -> None:
        with self._control_lock:
            self.frontend.sleep()

    def tune(self, tuning: Any, wait_for_lock: bool = True) -> Any:
        with self._control_lock:
            if getattr(self.demuxer, "running", False):
                raise RuntimeError("stop the adapter before tuning")
            return self.frontend.tune(tuning, wait_for_lock)

    def read_status(self) -> Any:
        with self._control_lock:
            return self.frontend.read_status()

    def start(self) -> None:
        with self._control_lock:
            self.demuxer.start()

    def stop(self) -> None:
        with self._control_lock:
            self.demuxer.stop()

    def read_stream(self, timeout_ms: int = 2000) -> bytes:
        """Read non-empty transport-stream data within a total timeout.

        The demuxer may return empty chunks while synchronizing or buffering
        packets. Those chunks are retried here so adapter callers receive
        usable stream data or a timeout error.
        """
        if timeout_ms < 0:
            raise ValueError("timeout_ms must be non-negative")
        deadline = monotonic() + timeout_ms / 1000
        while True:
            remaining_ms = max(0, int((deadline - monotonic()) * 1000))
            data = self._filter_pid_stream(self.demuxer.read_stream_once(remaining_ms))
            if data:
                return data
            if monotonic() >= deadline:
                raise TimeoutError("timed out waiting for transport-stream data")

    def configure_pid_filter(self, pids: Iterable[int], enabled: bool = True) -> None:
        """Atomically configure the software PID allow-list."""
        with self._control_lock:
            self._set_software_pid_filter(self._validate_pids(pids), enabled)

    def set_pid_filter_enabled(self, enabled: bool = True) -> None:
        """Enable or disable the configured software PID allow-list."""
        with self._control_lock:
            with self._pid_filter_lock:
                self._pid_filter_enabled = enabled

    @staticmethod
    def _validate_pids(pids: Iterable[int]) -> frozenset[int]:
        pid_set = frozenset(pids)
        if any(not isinstance(pid, int) or not 0 <= pid <= 0x1FFF for pid in pid_set):
            raise ValueError("PID must be between 0 and 0x1FFF")
        return pid_set

    def _set_software_pid_filter(self, pids: frozenset[int], enabled: bool) -> None:
        with self._pid_filter_lock:
            self._pid_filter_pids = pids
            self._pid_filter_enabled = enabled

    def _filter_pid_stream(self, data: bytes) -> bytes:
        with self._pid_filter_lock:
            pids = self._pid_filter_pids
            enabled = self._pid_filter_enabled
        if not enabled or not pids:
            return data
        # Filter the transport-stream packets based on the allowed PIDs.
        # Each transport-stream packet is 188 bytes long. The PID is located in the
        # second and third bytes of the packet header. Only packets with PIDs in the
        # allowed set are included in the returned stream.
        return b"".join(
            packet
            for offset in range(0, len(data), 188)
            if len(packet := data[offset : offset + 188]) == 188
            and ((packet[1] & 0x1F) << 8) | packet[2] in pids
        )
