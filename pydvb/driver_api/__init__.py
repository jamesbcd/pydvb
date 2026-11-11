"""Generic interfaces and shared building blocks for DVB device drivers.

The package is layered around a device-owned control domain: :class:`DVBDevice`
composes logical :class:`DVBAdapter` objects, and each adapter coordinates a
:class:`DVBFrontend` with a :class:`DVBDemuxer`. Frontends and tuners can share
the device's reentrant control lock so hardware operations are serialized
across adapters. Demuxers keep stream lifecycle and blocking reads on separate
synchronization, allowing shutdown to wait for active reads without holding up
unrelated device control.

Device-specific implementations provide the hardware-facing frontend,
demuxer, tuner, and register I/O. :class:`RegisterMap` builds common
multi-byte access and masked updates on that I/O, while the locking helpers
support safe composition. These types define reusable behavior and contracts;
they do not communicate with a particular chipset by themselves.
"""

from .adapter import DVBAdapter
from .demux import DVBDemuxer
from .device import DVBDevice
from .frontend import DVBFrontend, DVBFrontendStatus
from .registers import RegisterIO, RegisterMap

__all__ = ["DVBAdapter", "DVBDemuxer", "DVBDevice", "DVBFrontend", "DVBFrontendStatus", "RegisterIO", "RegisterMap"]
