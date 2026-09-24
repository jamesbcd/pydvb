"""Generic adapter protocol for DVB receivers."""

from __future__ import annotations

from dataclasses import dataclass, field
from threading import RLock
from time import monotonic
from typing import Any

@dataclass
class DVBAdapter:
    """Coordinate a frontend and demuxer for one logical DVB adapter."""

    frontend: Any
    demuxer: Any
    _control_lock: RLock = field(default_factory=RLock, init=False, repr=False)

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
            data = self.demuxer.read_stream_once(remaining_ms)
            if data:
                return data
            if monotonic() >= deadline:
                raise TimeoutError("timed out waiting for transport-stream data")

    def set_pid_filter(self, pid: int, enabled: bool = True) -> None:
        with self._control_lock:
            self.demuxer.set_pid_filter(pid, enabled)

    def enable_pid_filter(self, enabled: bool = True) -> None:
        with self._control_lock:
            self.demuxer.enable_pid_filter(enabled)
