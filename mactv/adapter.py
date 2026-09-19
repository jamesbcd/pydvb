"""Logical DVB adapter composition for AF9015 receivers."""

from __future__ import annotations

from dataclasses import dataclass

from .demux import TSDemuxer
from .frontend import AF9013Frontend, FrontendStatus
from .tuner import Tuner, Tuning


@dataclass
class AF9015Adapter:
    """Coordinate one logical DVB adapter exposed by an AF9015 device.

    An adapter owns the transport-stream demuxer and composes it with one
    AF9013 frontend. The frontend provides access to the attached tuner.
    """

    frontend: AF9013Frontend
    demuxer: TSDemuxer

    @property
    def adapter_index(self) -> int:
        return self.frontend.adapter_index

    @property
    def tuner(self) -> Tuner | None:
        return self.frontend.tuner

    def init_hardware(self) -> None:
        self.frontend.init_hardware()

    def sleep(self) -> None:
        self.frontend.sleep()

    def tune(self, tuning: Tuning, wait_for_lock: bool = True) -> FrontendStatus:
        return self.frontend.tune(tuning, wait_for_lock)

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