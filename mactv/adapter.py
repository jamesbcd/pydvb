"""Logical DVB adapter composition for AF9015 receivers."""

from __future__ import annotations

from dataclasses import dataclass, field
from threading import RLock

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
    # Lock to protect adapter control operations
    _control_lock: RLock = field(default_factory=RLock, init=False, repr=False)

    @property
    def adapter_index(self) -> int:
        return self.frontend.adapter_index

    @property
    def tuner(self) -> Tuner | None:
        return self.frontend.tuner

    def init_hardware(self) -> None:
        """Initialize the frontend hardware.

        Threading:
            Safe to call concurrently with other adapter control operations;
            control operations are serialized and this method may block on
            USB transactions. It should not be called during active capture.
        """
        with self._control_lock:
            self.frontend.init_hardware()

    def sleep(self) -> None:
        """Put the frontend into its low-power state.

        Threading:
            Safe to call concurrently with other adapter control operations;
            control operations are serialized and this method may block on
            USB transactions. It should not be called during active capture.
        """
        with self._control_lock:
            self.frontend.sleep()

    def tune(self, tuning: Tuning, wait_for_lock: bool = True) -> FrontendStatus:
        """Tune the frontend as one adapter control transaction.

        Threading:
            Safe to call concurrently with other adapter control operations;
            control operations are serialized. Tuning is rejected while the
            demuxer is running. Waiting for lock may block for up to the
            frontend lock timeout while holding the control lock.
        """
        with self._control_lock:
            if getattr(self.demuxer, "running", False):
                raise RuntimeError("stop the adapter before tuning")
            return self.frontend.tune(tuning, wait_for_lock)

    def read_status(self) -> FrontendStatus:
        """Read frontend lock and reception status.

        Threading:
            Safe to call concurrently with other adapter control operations;
            control operations are serialized. This method may block on USB
            transactions.
        """
        with self._control_lock:
            return self.frontend.read_status()

    def start(self) -> None:
        """Start transport-stream delivery.

        Threading:
            Safe to call concurrently with other adapter control operations;
            control operations are serialized. This method may block on USB
            transactions or an in-flight demuxer read.
        """
        with self._control_lock:
            self.demuxer.start()

    def stop(self) -> None:
        """Stop transport-stream delivery.

        Threading:
            Safe to call concurrently with other adapter control operations;
            control operations are serialized. This method may wait for an
            in-flight USB read, bounded by that read's timeout.
        """
        with self._control_lock:
            self.demuxer.stop()

    def read_ts(self, timeout_ms: int = 2000) -> bytes:
        """Read a transport-stream chunk from the demuxer.

        Threading:
            The demuxer permits only one active read. This method does not
            hold the adapter control lock while USB blocks, allowing ``stop``
            to initiate shutdown; concurrent reads are rejected.
        """
        return self.demuxer.read_ts(timeout_ms)

    def set_pid_filter(self, pid: int, enabled: bool = True) -> None:
        """Enable or disable one hardware PID filter slot.

        Threading:
            Safe to call concurrently with other adapter control operations;
            control operations are serialized and this method may block on
            USB transactions.
        """
        with self._control_lock:
            self.demuxer.set_pid_filter(pid, enabled)

    def enable_pid_filter(self, enabled: bool = True) -> None:
        """Enable or disable AF9013 PID filtering.

        Threading:
            Safe to call concurrently with other adapter control operations;
            control operations are serialized and this method may block on
            USB transactions.
        """
        with self._control_lock:
            self.demuxer.enable_pid_filter(enabled)