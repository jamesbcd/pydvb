"""Logical DVB adapter composition for AF9015 receivers."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from .demux import AF9015Demuxer
from ...driver_api.adapter import DVBAdapter
from .frontend import AF9013Frontend


@dataclass
class AF9015Adapter(DVBAdapter):
    """Coordinate one logical DVB adapter exposed by an AF9015 device.

    An adapter owns the transport-stream demuxer and composes it with one
    AF9013 frontend. The frontend provides access to the attached tuner.
    """

    frontend: AF9013Frontend
    demuxer: AF9015Demuxer

    def configure_pid_filter(self, pids: Iterable[int], enabled: bool = True) -> None:
        """Atomically configure hardware or software PID filtering.

        AF9015 hardware has 32 PID slots. Larger allow-lists use the
        unfiltered hardware stream and the generic adapter software filter.
        """
        normalized = self._validate_pids(pids)
        with self._control_lock:
            self.demuxer.clear_all_pid_filters()
            if len(normalized) <= self.demuxer.PID_FILTER_COUNT:
                for pid in sorted(normalized):
                    self.demuxer.set_pid_filter(pid)
                self.demuxer.enable_pid_filter(enabled and bool(normalized))
                self._hardware_pid_filter_mode = True
            else:
                self.demuxer.enable_pid_filter(False)
                self._hardware_pid_filter_mode = False
            self._set_software_pid_filter(normalized, enabled)

    def set_pid_filter_enabled(self, enabled: bool = True) -> None:
        """Enable or disable the configured AF9015 PID filter."""
        with self._control_lock:
            with self._pid_filter_lock:
                pids = self._pid_filter_pids
            if getattr(self, "_hardware_pid_filter_mode", True):
                self.demuxer.enable_pid_filter(enabled and bool(pids))
            with self._pid_filter_lock:
                self._pid_filter_enabled = enabled