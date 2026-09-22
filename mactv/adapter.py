"""Logical DVB adapter composition for AF9015 receivers."""

from __future__ import annotations

from dataclasses import dataclass

from .demux import AF9015Demuxer
from .driverapi.adapter import DVBAdapter
from .frontend import AF9013Frontend


@dataclass
class AF9015Adapter(DVBAdapter):
    """Coordinate one logical DVB adapter exposed by an AF9015 device.

    An adapter owns the transport-stream demuxer and composes it with one
    AF9013 frontend. The frontend provides access to the attached tuner.
    """

    frontend: AF9013Frontend
    demuxer: AF9015Demuxer