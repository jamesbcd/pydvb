"""Logical DVB adapter composition for AF9015 receivers.

The hardware PID operations correspond to ``af9015_pid_filter_ctrl`` and
``af9015_pid_filter`` in ``linux/af9015.c`` (lines 1075 and 1088).
"""

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

    Locking:
        The inherited adapter control operations use the device-shared
        control lock. Hardware PID-filter bookkeeping additionally uses the
        demuxer's PID lock; stream lifecycle and reads use separate demux
        synchronization.

    Attributes:
        frontend: AF9013 demodulator and tuner controller for this adapter.
        demuxer: USB transport-stream reader and hardware PID-slot manager.
        _using_hardware_pid_filter: Whether the active PID list is programmed
            in the demodulator instead of the base class software filter.
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
                    self.demuxer.configure_one_hw_pid_filter(pid)
                self.demuxer.set_hw_pid_filter_enabled(enabled and bool(validated))
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
        """Enable or disable whichever PID filter currently holds the list.

        Hardware filtering is used only when the configured list fits the
        AF9015's 32 slots; otherwise this delegates to the base software filter.
        Linux's corresponding hardware control is
        ``af9015_pid_filter_ctrl`` in ``linux/af9015.c:1075``.
        """
        if not ALLOW_HARDWARE_PID_FILTER:
            # Use the software filter of the base class.
            return super().set_pid_filter_enabled(enabled)

        with self._control_lock:
            if self._using_hardware_pid_filter:
                pids = self.demuxer.hw_filter_pids
                self.demuxer.set_hw_pid_filter_enabled(enabled and bool(pids))
                super().set_pid_filter_enabled(False)
            else:
                # We're using software PID filter, so delegate to the base class.
                super().set_pid_filter_enabled(enabled)