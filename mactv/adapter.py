"""Logical DVB adapter composition for AF9015 receivers."""

from __future__ import annotations

from dataclasses import dataclass

from .demux import TSDemuxer
from .frontend import AF9013Frontend, DEFAULT_LOCK_TIMEOUT, FrontendStatus
from .tuner import Tuner, Tuning


@dataclass
class AF9015Adapter:
    """One logical DVB adapter exposed by an AF9015 device."""

    frontend: AF9013Frontend
    demuxer: TSDemuxer

    @property
    def adapter(self) -> int:
        return self.frontend.adapter

    @property
    def tuner(self) -> Tuner | None:
        return self.frontend.tuner

    def init_hardware(self) -> None:
        self.frontend.init_hardware()

    def sleep(self) -> None:
        self.frontend.sleep()

    def tune(self, tuning: Tuning) -> None:
        self.frontend.tune(tuning)

    def wait_for_lock(
        self,
        timeout: float | None = DEFAULT_LOCK_TIMEOUT,
        poll_interval: float = 0.05,
    ) -> FrontendStatus:
        return self.frontend.wait_for_lock(timeout, poll_interval)

    def read_status(self) -> FrontendStatus:
        return self.frontend.read_status()

    def start(self) -> None:
        self.demuxer.start()

    def stop(self) -> None:
        self.demuxer.stop()

    def read_ts(self, timeout_ms: int = 2000) -> bytes:
        return self.demuxer.read_ts(timeout_ms)

    def set_pid_filter(self, pid: int, enabled: bool = True) -> None:
        self.demuxer.set_pid_filter(pid, enabled)

    def enable_pid_filter(self, enabled: bool = True) -> None:
        self.demuxer.enable_pid_filter(enabled)