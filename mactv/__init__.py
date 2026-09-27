"""Python interfaces for AF9015-based DVB-T USB receivers.
Currently only implements support for the Leadtek Winfast DTV Dongle Gold.
"""

from .driver_api.demux import DVBDemuxer
from .driver_api.device import DVBDevice
from .driver_api.frontend import DVBFrontend
from .driver_api.tuner import (
    CodeRate,
    DVBTuner,
    GuardInterval,
    Hierarchy,
    Modulation,
    DVBTuning,
    TransmissionMode,
)
from .drivers.af9015.adapter import AF9015Adapter
from .drivers.af9015.bridge import AF9015Bridge
from .drivers.af9015.demux import AF9015Demuxer
from .drivers.af9015.device import AF9015Device
from .drivers.af9015.frontend import AF9013Frontend, DVBFrontendStatus
from .drivers.af9015.tuner import TDA18271Tuner
from .scanner import (
    DVBDescriptor,
    DVBElementaryStream,
    DVBEvent,
    DVBScanResult,
    DVBScanner,
    DVBService,
    DVBServiceMetatype,
    DVBServiceType,
)
from .server import LocalTS_HTTPServer
from .usb import USBSpeed, USBTransport, default_transport_factory

__all__ = [
    "AF9015Bridge",
    "AF9015Adapter",
    "AF9013Frontend",
    "AF9015Device",
    "DVBDevice",
    "DVBFrontend",
    "CodeRate",
    "DVBDemuxer",
    "GuardInterval",
    "Hierarchy",
    "DVBFrontendStatus",
    "Modulation",
    "USBTransport",
    "default_transport_factory",
    "USBSpeed",
    "DVBTuner",
    "TDA18271Tuner",
    "AF9015Demuxer",
    "DVBTuning",
    "TransmissionMode",
    "LocalTS_HTTPServer",
    "DVBEvent",
    "DVBDescriptor",
    "DVBElementaryStream",
    "DVBScanResult",
    "DVBScanner",
    "DVBService",
    "DVBServiceMetatype",
    "DVBServiceType",
]
