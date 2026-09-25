"""Logical DVB adapter composition for AF9015 receivers."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field

from .demux import AF9015Demuxer
from ...driver_api.adapter import DVBAdapter
from .frontend import AF9013Frontend


ALLOW_HARDWARE_PID_FILTER = True


@dataclass
class AF9015Adapter(DVBAdapter):
    """Coordinate one logical DVB adapter exposed by an AF9015 device.

    An adapter owns the transport-stream demuxer and composes it with one
    AF9013 frontend. The frontend provides access to the attached tuner.
    """

    frontend: AF9013Frontend
    demuxer: AF9015Demuxer
    _using_hardware_pid_filter: bool = field(
        default=ALLOW_HARDWARE_PID_FILTER,
        init=False,
        repr=False,
    )

    def configure_pid_filter(self, pids: Iterable[int], enabled: bool = True) -> None:
        """Atomically configure the PID allow-list, and enable or disable PID filtering.

        We override the base class method to provide hardware PID filtering when available.
        AF9015 hardware has 32 PID slots. Larger allow-lists use the unfiltered hardware
        stream and the software filter of the base class.
        """
        if not ALLOW_HARDWARE_PID_FILTER:
            # Use the software filter of the base class.
            return super().configure_pid_filter(pids, enabled)

        validated = self._validate_pids(pids)
        with self._control_lock:
            self.demuxer.clear_hw_pid_filters()
            if len(validated) <= self.demuxer.PID_FILTER_COUNT:
                for pid in sorted(validated):
                    self.demuxer.configure_hw_pid_filter(pid)
                self.demuxer.set_hw_pid_filter_enabled(enabled and bool(validated))
                print(f"Hardware PID filter enabled: {enabled and bool(pids)}")
                self._using_hardware_pid_filter = True
                # Disable software filtering
                super().set_pid_filter_enabled(False)
            else:
                # The PID list is too large for the hardware PID filter, so we fall back
                # to the software filter.
                self.demuxer.set_hw_pid_filter_enabled(False)
                self._using_hardware_pid_filter = False
                # Delegate to the base class to configure the software PID filter.
                super().configure_pid_filter(validated, enabled)

    def set_pid_filter_enabled(self, enabled: bool = True) -> None:
        """Enable or disable the PID filtering.

        We override the base class method to provide hardware PID filtering when available.
        AF9015 hardware has 32 PID slots. Larger allow-lists use the unfiltered hardware
        stream and the software filter of the base class
        """
        if not ALLOW_HARDWARE_PID_FILTER:
            # Use the software filter of the base class.
            return super().set_pid_filter_enabled(enabled)

        with self._control_lock:
            if self._using_hardware_pid_filter:
                pids = self.demuxer.hw_filter_pids
                self.demuxer.set_hw_pid_filter_enabled(enabled and bool(pids))
                print(f"Hardware PID filter enabled: {enabled and bool(pids)}")
                super().set_pid_filter_enabled(False)
            else:
                # We're using software PID filter, so delegate to the base class.
                super().set_pid_filter_enabled(enabled)