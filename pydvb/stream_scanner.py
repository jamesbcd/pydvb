"""Decode DVB service and programme information from MPEG transport streams.

The module contains value objects for DVB metadata, incremental scanners for
transport-stream packets and present/following events, and private helpers for
decoding DVB sections, descriptors, text, timestamps, and CRCs. Scanning is
independent of the source: :class:`DVBScanner` accepts arbitrary byte chunks
or a started adapter, while :class:`DVBEventScanner` filters event updates to
services in an existing stream description.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from enum import Enum, IntEnum, auto
import logging
from typing import Any, Callable, NamedTuple
from threading import Event, Lock
import unicodedata

from construct import Bit, BitStruct, BitsInteger
from simple_serialize import serializable, standard_serializer

from pydvb.driver_api.adapter import DVBAdapter


logger = logging.getLogger(__name__)

TS_PACKET_SIZE = 188    # Standard size of an MPEG transport stream packet in bytes
TS_SYNC_BYTE = 0x47     # Sync byte value for MPEG transport stream packets

# Standard PIDs for DVB tables that are typically scanned.
# These PIDs correspond to the Program Association Table (PAT), Network Information Table (NIT),
# Service Description Table (SDT), and Event Information Table (EIT) respectively.
STREAM_TABLE_PIDS = frozenset((
        0x0000,    # PAT (Program Association Table)
        0x0010,    # NIT (Network Information Table)
        0x0011,    # SDT (Service Description Table)
        0x0012,    # EIT (Event Information Table)
    ))

# PSI/SI PIDs useful when presenting one service to a transport-stream client.
SERVICE_METADATA_PIDS = frozenset((0x0000, 0x0001, 0x0010, 0x0011, 0x0012, 0x0014))

# MPEG transport stream packet header structure for DVB tables.
TS_PACKET_HEADER: Any = BitStruct(
    "sync_byte" / BitsInteger(8),
    "transport_error_indicator" / Bit,      # type: ignore
    "payload_unit_start_indicator" / Bit,   # type: ignore
    "transport_priority" / Bit,             # type: ignore
    "pid" / BitsInteger(13),            # Packet Identifier (PID) for the TS packet
    "transport_scrambling_control" / BitsInteger(2),
    "adaptation_field_control" / BitsInteger(2),
    "continuity_counter" / BitsInteger(4),
)

# MPEG transport stream section header structure for DVB tables.
SECTION_HEADER: Any = BitStruct(
    "table_id" / BitsInteger(8),
    "section_syntax_indicator" / Bit,   # type: ignore
    "private_indicator" / Bit,          # type: ignore
    "reserved" / BitsInteger(2),
    "section_length" / BitsInteger(12),
)

class ScanPolicy(Enum):
    """Select the metadata threshold that marks a scan as complete.

    ``MIN_METADATA`` waits for PAT service-to-PMT references and SDT service
    metadata. ``ALL_SERVICE_METADATA`` additionally waits for every advertised
    PMT and an LCN for every discovered service.
    """

    MIN_METADATA = auto() # Require only the minimum metadata to consider the scan complete
    ALL_SERVICE_METADATA = auto() # Require all service PIDs and logical channel numbers

@serializable
@dataclass
class DVBEvent:
    """Metadata for one DVB event (programme).

    Attributes:
        event_id: Event identifier from the EIT.
        start_time: UTC start time, or ``None`` when the broadcast time is
            unspecified or invalid.
        duration: Event duration, or ``None`` when it is unspecified or invalid.
        name: Short event name, if advertised.
        text: Event description, if advertised.
        running_status: DVB running-status value from the EIT.
        triplet_id: ONID, TSID, and service ID identifying the event's service.
    """
    event_id: int
    start_time: datetime | None = None
    duration: timedelta | None = None
    name: str | None = None
    text: str | None = None
    running_status: int | None = None
    triplet_id: DVBTripletID | None = None


@serializable
@dataclass
class DVBDescriptor:
    """Raw descriptor attached to a DVB service or elementary stream.
    
    A descriptor is structured metadata attached to a DVB service or elementary
    stream. Its tag identifies the descriptor type; its data contains the
    descriptor payload without the tag and length bytes.

    Attributes:
        tag: Descriptor type code.
        data: Raw descriptor payload bytes.
    """
    tag: int
    data: bytes


@serializable(exclude=["descriptors"]) # No need to serialize the raw descriptors
@dataclass
class DVBElementaryStream:
    """Elementary stream entry advertised by a service's Program Map Table (PMT).
    
    An elementary stream represents one media stream (audio, video, or data)
    within a DVB service. It is identified by a PID and may have descriptors.

    Attributes:
        pid: Packet identifier carrying this elementary stream.
        stream_type: PMT stream-type code.
        descriptors: Descriptors advertised for this stream. Serialization
            intentionally omits these raw descriptor payloads.
    """
    pid: int
    stream_type: int
    descriptors: tuple[DVBDescriptor, ...] = ()


@serializable
class DVBServiceType(IntEnum):
    """Service-type codes defined by the DVB service descriptor."""
    # See p100 of https://www.etsi.org/deliver/etsi_en/300400_300499/300468/01.19.00_20/en_300468v011900a.pdf
    RESERVED_0x00 = 0x00
    DIGITAL_TELEVISION = 0x01
    DIGITAL_RADIO_SOUND = 0x02
    TELETEXT = 0x03
    NVOD_REFERENCE = 0x04
    NVOD_TIME_SHIFTED = 0x05
    MOSAIC = 0x06
    FM_RADIO = 0x07
    DVB_SRM = 0x08
    RESERVED_0x09 = 0x09
    ADVANCED_CODEC_DIGITAL_RADIO_SOUND = 0x0A
    H264_AVC_MOSAIC = 0x0B
    DATA_BROADCAST = 0x0C
    RCS_MAP = 0x0E
    RCS_FLS = 0x0F
    DVB_MULTIMEDIA_HOME_PLATFORM = 0x10
    HD_DIGITAL_TELEVISION = 0x11
    RESERVED_0x12 = 0x12
    RESERVED_0x13 = 0x13
    RESERVED_0x14 = 0x14
    RESERVED_0x15 = 0x15
    H264_AVC_SD_DTV = 0x16
    H264_AVC_SD_NVOD_TIME_SHIFTED = 0x17
    H264_AVC_SD_NVOD_REFERENCE = 0x18
    H264_AVC_HD_DTV = 0x19
    H264_AVC_HD_NVOD_TIME_SHIFTED = 0x1A
    H264_AVC_HD_NVOD_REFERENCE = 0x1B
    H264_AVC_STEREOSCOPIC_HD_DTV = 0x1C
    H264_AVC_STEREOSCOPIC_HD_NVOD_TIME_SHIFTED = 0x1D
    H264_AVC_STEREOSCOPIC_HD_NVOD_REFERENCE = 0x1E
    HEVC_DTV = 0x1F
    HEVC_UHD_DTV = 0x20
    VVC_DTV = 0x21
    AVS3_DTV = 0x22

    @property
    def metatype(self) -> "DVBServiceMetatype":
        """Return the broad media category for this DVB service type."""
        return _DVB_SERVICE_METATYPES[self]


@serializable
class DVBServiceMetatype(str, Enum):
    """Broad media category inferred from a DVB service-type code."""

    TV = "TV"
    RADIO = "RADIO"
    OTHER = "OTHER"


# Map each DVB service type to its broad media category.
_DVB_SERVICE_METATYPES = {
    DVBServiceType.RESERVED_0x00: DVBServiceMetatype.OTHER,
    DVBServiceType.DIGITAL_TELEVISION: DVBServiceMetatype.TV,
    DVBServiceType.DIGITAL_RADIO_SOUND: DVBServiceMetatype.RADIO,
    DVBServiceType.TELETEXT: DVBServiceMetatype.OTHER,
    DVBServiceType.NVOD_REFERENCE: DVBServiceMetatype.TV,
    DVBServiceType.NVOD_TIME_SHIFTED: DVBServiceMetatype.TV,
    DVBServiceType.MOSAIC: DVBServiceMetatype.TV,
    DVBServiceType.FM_RADIO: DVBServiceMetatype.RADIO,
    DVBServiceType.DVB_SRM: DVBServiceMetatype.TV,
    DVBServiceType.RESERVED_0x09: DVBServiceMetatype.OTHER,
    DVBServiceType.ADVANCED_CODEC_DIGITAL_RADIO_SOUND: DVBServiceMetatype.RADIO,
    DVBServiceType.H264_AVC_MOSAIC: DVBServiceMetatype.TV,
    DVBServiceType.DATA_BROADCAST: DVBServiceMetatype.OTHER,
    DVBServiceType.RCS_MAP: DVBServiceMetatype.OTHER,
    DVBServiceType.RCS_FLS: DVBServiceMetatype.OTHER,
    DVBServiceType.DVB_MULTIMEDIA_HOME_PLATFORM: DVBServiceMetatype.OTHER,
    DVBServiceType.HD_DIGITAL_TELEVISION: DVBServiceMetatype.TV,
    DVBServiceType.RESERVED_0x12: DVBServiceMetatype.OTHER,
    DVBServiceType.RESERVED_0x13: DVBServiceMetatype.OTHER,
    DVBServiceType.RESERVED_0x14: DVBServiceMetatype.OTHER,
    DVBServiceType.RESERVED_0x15: DVBServiceMetatype.OTHER,
    DVBServiceType.H264_AVC_SD_DTV: DVBServiceMetatype.TV,
    DVBServiceType.H264_AVC_SD_NVOD_TIME_SHIFTED: DVBServiceMetatype.TV,
    DVBServiceType.H264_AVC_SD_NVOD_REFERENCE: DVBServiceMetatype.TV,
    DVBServiceType.H264_AVC_HD_DTV: DVBServiceMetatype.TV,
    DVBServiceType.H264_AVC_HD_NVOD_TIME_SHIFTED: DVBServiceMetatype.TV,
    DVBServiceType.H264_AVC_HD_NVOD_REFERENCE: DVBServiceMetatype.TV,
    DVBServiceType.H264_AVC_STEREOSCOPIC_HD_DTV: DVBServiceMetatype.TV,
    DVBServiceType.H264_AVC_STEREOSCOPIC_HD_NVOD_TIME_SHIFTED: DVBServiceMetatype.TV,
    DVBServiceType.H264_AVC_STEREOSCOPIC_HD_NVOD_REFERENCE: DVBServiceMetatype.TV,
    DVBServiceType.HEVC_DTV: DVBServiceMetatype.TV,
    DVBServiceType.HEVC_UHD_DTV: DVBServiceMetatype.TV,
    DVBServiceType.VVC_DTV: DVBServiceMetatype.TV,
    DVBServiceType.AVS3_DTV: DVBServiceMetatype.TV,
}

@serializable
class DVBDoubletID(NamedTuple):
    """Identify a transport stream by its original-network and stream IDs. This should
    globally and uniquely identify a transport stream within the DVB network.

    Attributes:
        original_network_id: DVB original network identifier (ONID).
        transport_stream_id: DVB transport stream identifier (TSID).

    The integer representation packs ONID in the upper 16 bits and TSID in
    the lower 16 bits. The dotted representation is ``ONID.TSID``.
    """
    original_network_id: int
    transport_stream_id: int

    @classmethod
    def from_int(cls, value: int) -> "DVBDoubletID":
        """Unpack ONID and TSID from a 32-bit-style integer."""
        onid = (value >> 16) & 0xFFFF
        tsid = value & 0xFFFF
        return cls(original_network_id=onid, transport_stream_id=tsid)

    @classmethod
    def from_dotted(cls, value: str) -> "DVBDoubletID":
        """Parse an ``ONID.TSID`` string into a doublet."""
        onid, tsid = map(int, value.split("."))
        return cls(original_network_id=onid, transport_stream_id=tsid)

    @property
    def onid(self) -> int:
        """Return ``original_network_id`` using its standard abbreviation."""
        return self.original_network_id

    @property
    def tsid(self) -> int:
        """Return ``transport_stream_id`` using its standard abbreviation."""
        return self.transport_stream_id

    def triplet_id(self, service_id: int) -> DVBTripletID:
        """Add a service ID to form a globally identifying DVB triplet."""
        return DVBTripletID(
            original_network_id=self.original_network_id,
            transport_stream_id=self.transport_stream_id,
            service_id=service_id,
        )

    def __int__(self) -> int:
        """Pack ONID and TSID into one integer, with ONID in the high bits."""
        return (self.original_network_id << 16) | self.transport_stream_id

    def __index__(self) -> int:
        """Allow integer-formatting functions such as ``hex()`` to use this ID."""
        return int(self)

    def dotted(self) -> str:
        """Return the ID in ``ONID.TSID`` form."""
        return f"{self.original_network_id}.{self.transport_stream_id}"

    def __str__(self) -> str:
        """Return the dotted representation."""
        return self.dotted()

    def __format__(self, format_spec: str) -> str:
        """Format the dotted representation with the requested string format."""
        return format(self.dotted(), format_spec)

    def _serialize(self, options: dict[str, object], ctx: object | None) -> str:
        """Serialize the ID using its dotted representation."""
        return self.dotted()

    @classmethod
    def _deserialize(
        cls, data: object, options: dict[str, object], ctx: object | None
    ) -> "DVBDoubletID":
        """Deserialize a dotted ID, converting the input to a string first."""
        return cls.from_dotted(str(data))


@serializable
class DVBTripletID(NamedTuple):
    """Identify a service by its ONID, TSID, and service ID. This should globally and
    uniquely identify a service within the DVB network.

    Attributes:
        original_network_id: DVB original network identifier (ONID).
        transport_stream_id: DVB transport stream identifier (TSID).
        service_id: DVB service identifier (SID).

    The integer representation packs these three 16-bit identifiers in order.
    The dotted representation is ``ONID.TSID.SID``.
    """
    original_network_id: int
    transport_stream_id: int
    service_id: int

    @classmethod
    def from_int(cls, value: int) -> "DVBTripletID":
        """Unpack ONID, TSID, and SID from an integer."""
        onid = (value >> 32) & 0xFFFF
        tsid = (value >> 16) & 0xFFFF
        sid = value & 0xFFFF
        return cls(original_network_id=onid, transport_stream_id=tsid, service_id=sid)

    @classmethod
    def from_dotted(cls, value: str) -> "DVBTripletID":
        """Parse an ``ONID.TSID.SID`` string into a triplet."""
        onid, tsid, sid = map(int, value.split("."))
        return cls(original_network_id=onid, transport_stream_id=tsid, service_id=sid)

    @property
    def onid(self) -> int:
        """Return ``original_network_id`` using its standard abbreviation."""
        return self.original_network_id

    @property
    def tsid(self) -> int:
        """Return ``transport_stream_id`` using its standard abbreviation."""
        return self.transport_stream_id

    @property
    def sid(self) -> int:
        """Return ``service_id`` using its standard abbreviation."""
        return self.service_id

    @property
    def doublet_id(self) -> DVBDoubletID:
        """Return the stream-identifying ONID and TSID portion of this ID."""
        return DVBDoubletID(original_network_id=self.original_network_id, transport_stream_id=self.transport_stream_id)

    def __int__(self) -> int:
        """Pack ONID, TSID, and SID into one integer, in that bit order."""
        return (self.original_network_id << 32) | (self.transport_stream_id << 16) | self.service_id

    def __index__(self) -> int:
        """Allow integer-formatting functions such as ``hex()`` to use this ID."""
        return int(self)

    def dotted(self) -> str:
        """Return the ID in ``ONID.TSID.SID`` form."""
        return f"{self.original_network_id}.{self.transport_stream_id}.{self.service_id}"

    def __str__(self) -> str:
        """Return the dotted representation."""
        return self.dotted()

    def __format__(self, format_spec: str) -> str:
        """Format the dotted representation with the requested string format."""
        return format(self.dotted(), format_spec)

    def _serialize(self, options: dict[str, object], ctx: object | None) -> str:
        """Serialize the ID using its dotted representation."""
        return self.dotted()

    @classmethod
    def _deserialize(
        cls, data: object, options: dict[str, object], ctx: object | None
    ) -> "DVBTripletID":
        """Deserialize a dotted ID, converting the input to a string first."""
        return cls.from_dotted(str(data))


@serializable(exclude=["current_event", "next_event"]) # No need to serialize EIT events
@dataclass
class DVBService:
    """Metadata for a single DVB service (channel).

    Attributes:
        service_id: DVB service identifier (SID).
        triplet_id: Globally identifying ONID/TSID/SID tuple, when known.
        name: Broadcast service name, if advertised.
        provider_name: Broadcast provider name, if advertised.
        logical_channel_number: LCN, if present in a NIT or BAT descriptor.
        service_type: DVB service-type code; unknown codes remain integers.
        service_metatype: Broad TV, radio, or other category.
        pmt_pid: PID carrying this service's PMT.
        pcr_pid: PID carrying its program clock reference, if known.
        elementary_streams: Audio, video, and data streams advertised by its PMT.
        current_event: Current event from present/following EIT, if available.
        next_event: Next event from present/following EIT, if available.
    """
    service_id: int
    triplet_id: DVBTripletID | None = None
    name: str | None = None
    provider_name: str | None = None
    logical_channel_number: int | None = None
    service_type: DVBServiceType | int | None = None
    service_metatype: DVBServiceMetatype = DVBServiceMetatype.OTHER
    pmt_pid: int | None = None  # PMT = Program Map Table PID. PID = Packet Identifier
    pcr_pid: int | None = None  # PCR = Program Clock Reference PID. PID = Packet Identifier
    elementary_streams: tuple[DVBElementaryStream, ...] = ()
    current_event: DVBEvent | None = None
    next_event: DVBEvent | None = None

    def pids(self) -> frozenset[int]:
        """Return the transport-stream PIDs required to describe this service."""
        return frozenset(
            SERVICE_METADATA_PIDS
            | frozenset(
                pid
                for pid in (
                    self.pmt_pid,
                    self.pcr_pid,
                    *(stream.pid for stream in self.elementary_streams),
                )
                if pid is not None
            )
        )


@serializable(exclude=["warnings"]) # No need to serialize long lists of warnings
@dataclass
class DVBStreamInfo:
    """Snapshot of metadata accumulated while scanning a transport stream.

    Attributes:
        original_network_id: ONID from the stream's SDT or NIT, if known.
        transport_stream_id: TSID from the stream's PAT or SDT, if known.
        network_name: Network name from the NIT, if advertised.
        services: Discovered services in service-ID order.
        packets_seen: Number of complete TS packets passed to packet processing.
        complete: Whether the selected scan policy was met when snapshotted.
        warnings: Non-fatal parsing issues encountered during scanning.
    """
    original_network_id: int | None = None
    transport_stream_id: int | None = None
    network_name: str | None = None
    services: tuple[DVBService, ...] = ()
    packets_seen: int = 0
    complete: bool = False
    warnings: tuple[str, ...] = ()

    @property
    def doublet_id(self) -> DVBDoubletID | None:
        """Return this stream's ID, inferring it from a service if necessary."""
        if self.original_network_id is not None and self.transport_stream_id is not None:
            return DVBDoubletID(
                original_network_id=self.original_network_id,
                transport_stream_id=self.transport_stream_id,
            )
        return next(
            (
                service.triplet_id.doublet_id
                for service in self.services
                if service.triplet_id is not None
            ),
            None,
        )


class DVBScanner:
    """Incrementally discover services and programme metadata from TS bytes.

    ``feed`` accepts arbitrary byte chunks. The scanner is independent of USB
    and can consume a file or an adapter read loop. It does not start or stop
    the source supplied by the caller.

    Attributes:
        verify_crc: Whether complete table sections must pass MPEG-2 CRC
            validation before decoding.
        _buffer: Incomplete or not-yet-processed transport-stream packet bytes.
        _section_buffers: Partially assembled section bytes, keyed by PID.
        _continuity: Most recently accepted continuity counter for each PID.
        _services: Mutable service records accumulated during the scan, keyed
            by service ID.
        _pmt_pids: Current PAT mapping from service IDs to PMT PIDs.
        _decoded_pmt_pids: PMT PID most recently decoded for each service.
        _seen_tables: Table IDs for sections that passed CRC validation.
        _warnings: Non-fatal parsing warnings accumulated during the scan.
        _packets_seen: Number of complete packets removed from the input buffer.
        _transport_stream_id: TSID most recently read from a PAT or SDT.
        _original_network_id: ONID most recently read from an SDT or matching NIT.
        _network_name: Network name, if found in a NIT.
    """

    def __init__(self, *, verify_crc: bool = True) -> None:
        """Create an empty scanner with optional section CRC validation.

        Args:
            verify_crc: If true, discard table sections whose MPEG-2 CRC
                remainder is non-zero.
        """
        self.verify_crc = verify_crc
        self._buffer = bytearray()              # Buffer for accumulating TS packet bytes
        self._section_buffers: dict[int, bytearray] = {} # Buffers for accumulating section data keyed by PID
        self._continuity: dict[int, int] = {} # Continuity counters for each PID
        self._services: dict[int, DVBService] = {} # Discovered services keyed by service ID
        self._pmt_pids: dict[int, int] = {}     # Mapping from service ID to PMT (Program Map Table) PID
        self._decoded_pmt_pids: dict[int, int] = {} # PMT PIDs decoded for each service
        self._seen_tables: set[int] = set()     # Set of table IDs that have been seen
        self._warnings: list[str] = []          # Accumulated warnings during scanning
        self._packets_seen = 0                  # Count of TS packets processed
        self._transport_stream_id: int | None = None # Transport stream ID from the PAT
        self._original_network_id: int | None = None # Original network ID from the SDT
        self._network_name: str | None = None   # Network name from the SDT

    def scan(
        self,
        source: Iterable[bytes] | DVBAdapter,
        policy: ScanPolicy = ScanPolicy.MIN_METADATA,
        read_timeout_ms: int = 2000,
        *,
        on_service_count: Callable[[int], None] | None = None,
        cancel_event: Event | None = None,
    ) -> DVBStreamInfo:
        """Consume a transport-stream source and return the accumulated result.

        Args:
            source: An iterable of byte chunks, or a started DVB adapter.
            policy: The completion policy that determines when scanning stops.
            read_timeout_ms: The timeout in milliseconds for adapter reads.
            on_service_count: Called with the number of discovered services after
                a chunk adds one or more services.
            cancel_event: Optional event that stops scanning between input
                chunks or adapter reads.

        Returns:
            A DVBStreamInfo object containing the accumulated scan results.

        Raises:
            ValueError: If ``read_timeout_ms`` is negative.
            RuntimeError: If ``source`` is a DVB adapter that has not started
                its stream.
        """
        if read_timeout_ms < 0:
            raise ValueError("read_timeout_ms must be non-negative")

        def feed_and_report(data: bytes) -> None:
            """Feed a chunk and report only increases in discovered services."""
            previous_count = len(self._services)
            self.feed(data)
            service_count = len(self._services)
            if on_service_count is not None and service_count > previous_count:
                on_service_count(service_count)

        if isinstance(source, DVBAdapter):
            if not source.demuxer.running:
                raise RuntimeError("start the adapter before scanning")
            while not self.complete(policy):
                if cancel_event is not None and cancel_event.is_set():
                    break
                data = source.read_stream(read_timeout_ms)
                if cancel_event is not None and cancel_event.is_set():
                    break
                feed_and_report(data)
        else:
            for chunk in source:
                if cancel_event is not None and cancel_event.is_set():
                    break
                feed_and_report(chunk)
                if self.complete(policy):
                    break
        return self.result(policy)

    def complete(
        self,
        policy: ScanPolicy = ScanPolicy.MIN_METADATA,
    ) -> bool:
        """Return whether the selected scan completion condition is satisfied.

        ``MIN_METADATA`` requires the PAT PMT references and SDT service
        metadata. ``ALL_SERVICE_METADATA`` additionally requires every PMT
        advertised by the current PAT to have been decoded and a logical
        channel number on every discovered service.
        """
        # PMT = Program Map Table (contains information about the programs and their associated PIDs)
        # SDT = Service Description Table (contains information about the services. e.g.names and provider names)
        # The scanner checks for the presence of these tables and PIDs to determine completeness.
        # The scanner is considered complete if it has seen the PMT PIDs,
        # the SDT table (0x42), and all services have a PMT PID.
        minimum_metadata = bool(self._pmt_pids) and 0x42 in self._seen_tables and all(
            service.pmt_pid is not None for service in self._services.values()
        )
        if policy is ScanPolicy.MIN_METADATA:
            return minimum_metadata
        if policy is ScanPolicy.ALL_SERVICE_METADATA:
            return minimum_metadata and all(
                self._decoded_pmt_pids.get(service_id) == pmt_pid
                for service_id, pmt_pid in self._pmt_pids.items()
            ) and all(
                service.logical_channel_number is not None
                for service in self._services.values()
            )
        raise ValueError(f"unsupported scan completion policy: {policy!r}")

    def result(
        self,
        policy: ScanPolicy = ScanPolicy.MIN_METADATA,
    ) -> DVBStreamInfo:
        """Return a detached snapshot of accumulated scan state.

        Services are ordered by service ID and their elementary-stream
        descriptors are copied. The snapshot's ``complete`` flag is evaluated
        using ``policy``; the scanner remains usable and is not reset.
        """
        original_network_id = self._original_network_id
        transport_stream_id = self._transport_stream_id
        services = tuple(
            DVBService(
                service_id=service.service_id,
                triplet_id=(
                    DVBTripletID(
                        original_network_id=original_network_id,
                        transport_stream_id=transport_stream_id,
                        service_id=service.service_id,
                    )
                    if original_network_id is not None and transport_stream_id is not None
                    else None
                ),
                name=service.name,
                provider_name=service.provider_name,
                logical_channel_number=service.logical_channel_number,
                service_type=service.service_type,
                service_metatype=service.service_metatype,
                pmt_pid=service.pmt_pid,
                pcr_pid=service.pcr_pid,
                elementary_streams=tuple(
                    DVBElementaryStream(
                        pid=stream.pid,
                        stream_type=stream.stream_type,
                        descriptors=tuple(
                            DVBDescriptor(tag=descriptor.tag, data=bytes(descriptor.data))
                            for descriptor in stream.descriptors
                        ),
                    )
                    for stream in service.elementary_streams
                ),
                current_event=service.current_event,
                next_event=service.next_event,
            )
            for service in sorted(self._services.values(), key=lambda item: item.service_id)
        )
        return DVBStreamInfo(
            transport_stream_id=transport_stream_id,
            original_network_id=original_network_id,
            network_name=self._network_name,
            services=services,
            packets_seen=self._packets_seen,
            complete=self.complete(policy),
            warnings=tuple(self._warnings),
        )

    def feed(self, data: bytes) -> None:
        """Consume zero or more complete or partial transport-stream packets.
        
        This method can be called multiple times as new data becomes available.
        Partial packets remain buffered until a later call supplies enough bytes.
        When packet alignment is lost, bytes are discarded up to the next sync
        byte; if no sync byte is present in the current buffer, that buffer is
        cleared. Complete packets are dispatched immediately.

        It is usually more convenient to call ``scan()`` with an iterable of
        byte chunks instead of repeatedly calling ``feed()``.
        """
        self._buffer.extend(data)
        while True:
            if len(self._buffer) < TS_PACKET_SIZE:
                # Not enough data for a complete TS packet, wait for more data
                return
            if self._buffer[0] != TS_SYNC_BYTE:
                # TS sync byte not found at the start of the buffer, search for the next one
                try:
                    offset = self._buffer.index(TS_SYNC_BYTE)
                except ValueError:
                    # Sync byte not found, so clear the buffer and wait for more data
                    self._buffer.clear()
                    return
                # Remove the bytes before the sync byte and continue processing
                del self._buffer[:offset]
                if len(self._buffer) < TS_PACKET_SIZE:
                    return
            packet = bytes(self._buffer[:TS_PACKET_SIZE]) # Extract a complete TS packet from the buffer
            del self._buffer[:TS_PACKET_SIZE] # Remove the processed TS packet from the buffer
            self._packets_seen += 1
            self._feed_packet(packet)

    def _feed_packet(self, packet: bytes) -> None:
        """Filter and dispatch one complete TS packet, feeding it to the scanner.

        Only table PIDs and currently advertised PMT PIDs are considered.
        Packets marked with a transport error or without payload are ignored.
        An unexpected continuity counter on a continuation packet invalidates
        that PID's partial section before the payload is assembled.
        """

        # Perform quick extraction of the PID from the TS packet header, and confirm
        # whether this PID is one of the standard table PIDs or a PMT PID that we are tracking.
        # Otherwise we ignore this packet. Without ignoring irrelevant packets, the scanner
        # struggles to keep up with the stream.
        pid = ((packet[1] & 0x1F) << 8) | packet[2]
        if pid not in STREAM_TABLE_PIDS and pid not in self._pmt_pids.values():
            return
        header = TS_PACKET_HEADER.parse(packet[:4])
        if header.transport_error_indicator:
            return
        adaptation_control = header.adaptation_field_control
        # Only process packets with payload (adaptation_field_control 1 or 3)
        if adaptation_control not in (1, 3):
            return
        offset = 4
        if adaptation_control == 3:
            # Adaptation field is present, so skip it to reach the payload
            offset += 1 + packet[4] # Skip the adaptation field length and the adaptation field itself
        if offset >= TS_PACKET_SIZE:
            # No payload available after skipping the adaptation field, so return
            return
        continuity = header.continuity_counter
        previous = self._continuity.get(pid)
        if (
            previous is not None
            and (previous + 1) % 16 != continuity
            and not header.payload_unit_start_indicator
        ):
            # Continuity error detected, so discard any partially collected section for this PID
            self._section_buffers.pop(pid, None)
        self._continuity[pid] = continuity
        self._feed_payload(pid, packet[offset:], header.payload_unit_start_indicator)

    def _feed_payload(self, pid: int, payload: bytes, payload_start: bool) -> None:
        """Feed a payload for a specific PID to the section assembler.

        Args:
            pid: The Packet Identifier (PID) of the TS packet.
            payload: The payload data for the specific PID.
            payload_start: Whether the payload begins a new section. When true,
                its first byte is the pointer to the start of that section; any
                pointed-to prefix first finishes a previously buffered section.
        """
        section_buffer = self._section_buffers.setdefault(pid, bytearray())
        if payload_start:
            if not payload:
                return
            pointer = payload[0]
            prefix = payload[1 : 1 + pointer]
            section_buffer.extend(prefix)
            self._extract_sections(pid, section_buffer)
            section_buffer.clear()
            payload = payload[1 + pointer :]
        section_buffer.extend(payload)
        self._extract_sections(pid, section_buffer)

    def _extract_sections(self, pid: int, buffer: bytearray) -> None:
        """Decode complete sections from a PID buffer, retaining any partial tail.

        Invalid section lengths clear the buffer. With CRC verification enabled,
        sections with a non-zero MPEG-2 CRC remainder are dropped and recorded
        as warnings.
        """
        while len(buffer) >= 3:
            header = SECTION_HEADER.parse(bytes(buffer[:3]))
            total_length = 3 + header.section_length
            if header.section_length < 4 or total_length > 4096:
                buffer.clear()
                return
            if len(buffer) < total_length:
                return
            section = bytes(buffer[:total_length])
            del buffer[:total_length]
            if self.verify_crc and _mpeg_crc32(section) != 0:
                self._warnings.append(f"invalid CRC for table 0x{section[0]:02x}")
                continue
            self._decode_section(pid, section)

    def _decode_section(self, pid: int, section: bytes) -> None:
        """Record a section's table ID and dispatch supported PID/table pairs."""
        table_id = section[0]
        self._seen_tables.add(table_id)
        if pid == 0 and table_id == 0x00: # PAT (Program Association Table)
            self._decode_pat(section)
        elif pid == 0x11 and table_id in (0x42, 0x46): # SDT (Service Description Table)
            self._decode_sdt(section)
        elif pid == 0x11 and table_id == 0x4A: # BAT (Bouquet Association Table)
            self._decode_bat(section)
        elif pid == 0x10 and table_id in (0x40, 0x41): # NIT (Network Information Table)
            self._decode_nit(section)
        elif pid == 0x12 and table_id == 0x4E: # EIT present/following for the actual transport stream
            self._decode_eit(section)
        elif pid in self._pmt_pids.values() and table_id == 0x02: # PMT (Program Map Table)
            self._decode_pmt(pid, section)

    def _decode_pat(self, section: bytes) -> None:
        """Read the TSID and service-to-PMT PID entries from a PAT section."""
        self._transport_stream_id = int.from_bytes(section[3:5], "big")
        end = _section_end(section)
        for offset in range(8, end, 4):
            service_id = int.from_bytes(section[offset : offset + 2], "big")
            pid = ((section[offset + 2] & 0x1F) << 8) | section[offset + 3]
            if service_id:
                self._pmt_pids[service_id] = pid
                self._services.setdefault(service_id, DVBService(service_id)).pmt_pid = pid

    def _decode_pmt(self, pid: int, section: bytes) -> None:
        """Replace a service's PMT-derived PCR and elementary-stream metadata."""
        service_id = int.from_bytes(section[3:5], "big")
        service = self._services.setdefault(service_id, DVBService(service_id))
        end = _section_end(section)
        service.pcr_pid = ((section[8] & 0x1F) << 8) | section[9]
        info_length = ((section[10] & 0x0F) << 8) | section[11]
        offset = 12 + info_length
        elementary_streams: list[DVBElementaryStream] = []
        while offset + 5 <= end:
            stream_type = section[offset]
            stream_pid = ((section[offset + 1] & 0x1F) << 8) | section[offset + 2]
            descriptor_length = ((section[offset + 3] & 0x0F) << 8) | section[offset + 4]
            descriptor_data = section[offset + 5 : offset + 5 + descriptor_length]
            elementary_streams.append(
                DVBElementaryStream(
                    pid=stream_pid,
                    stream_type=stream_type,
                    descriptors=tuple(
                        DVBDescriptor(tag=tag, data=bytes(value))
                        for tag, value in _descriptors(descriptor_data)
                    ),
                )
            )
            offset += 5 + descriptor_length
        service.elementary_streams = tuple(elementary_streams)
        self._decoded_pmt_pids[service_id] = pid

    def _decode_sdt(self, section: bytes) -> None:
        """Read stream identifiers and advertised service descriptor metadata.

        Known service descriptor codes map to :class:`DVBServiceType`; unknown
        codes are retained as integers and classified as ``OTHER``.
        """
        self._transport_stream_id = int.from_bytes(section[3:5], "big")
        self._original_network_id = int.from_bytes(section[8:10], "big")
        end = _section_end(section)
        offset = 11
        while offset + 5 <= end:
            service_id = int.from_bytes(section[offset : offset + 2], "big")
            descriptor_length = ((section[offset + 3] & 0x0F) << 8) | section[offset + 4]
            service = self._services.setdefault(service_id, DVBService(service_id))
            service_data = section[offset + 5 : offset + 5 + descriptor_length]
            for tag, value in _descriptors(service_data):
                if tag == 0x48 and len(value) >= 2:
                    try:
                        service.service_type = DVBServiceType(value[0])
                        service.service_metatype = service.service_type.metatype
                    except ValueError:
                        service.service_type = value[0]
                        service.service_metatype = DVBServiceMetatype.OTHER
                    provider_length = value[1]
                    service.provider_name = _decode_text(value[2 : 2 + provider_length])
                    name_offset = 2 + provider_length
                    if name_offset < len(value):
                        service.name = _decode_text(value[name_offset + 1 : name_offset + 1 + value[name_offset]])
            offset += 5 + descriptor_length

    def _decode_nit(self, section: bytes) -> None:
        """Read the network name and transport descriptors from a NIT.

        The ONID is taken from the NIT transport entry only when that entry's
        TSID matches the scanner's current transport stream. LCN descriptors
        are decoded from the listed transport entries.
        """
        end = _section_end(section)
        network_length = ((section[8] & 0x0F) << 8) | section[9]
        for tag, value in _descriptors(section[10 : 10 + network_length]):
            if tag == 0x40:
                self._network_name = _decode_text(value)
        offset = 10 + network_length
        if offset + 2 > end:
            return
        transport_length = ((section[offset] & 0x0F) << 8) | section[offset + 1]
        offset += 2
        limit = min(offset + transport_length, end)
        while offset + 6 <= limit:
            transport_stream_id = int.from_bytes(section[offset : offset + 2], "big")
            original_network_id = int.from_bytes(section[offset + 2 : offset + 4], "big")
            descriptor_length = ((section[offset + 4] & 0x0F) << 8) | section[offset + 5]
            if (
                section[0] == 0x40
                and transport_stream_id == self._transport_stream_id
            ):
                self._original_network_id = original_network_id
            self._decode_lcn_descriptors(section[offset + 6 : offset + 6 + descriptor_length])
            offset += 6 + descriptor_length

    def _decode_bat(self, section: bytes) -> None:
        """Decode LCN descriptors from transport entries in a BAT.

        BAT-derived LCNs only fill services that do not already have a number,
        so an LCN previously found in a NIT takes precedence.
        """
        if len(section) < 12:
            self._warnings.append("truncated BAT section")
            return
        bouquet_length = ((section[8] & 0x0F) << 8) | section[9]
        offset = 10 + bouquet_length
        end = _section_end(section)
        if offset + 2 > end:
            return
        transport_length = ((section[offset] & 0x0F) << 8) | section[offset + 1]
        offset += 2
        limit = min(offset + transport_length, end)
        while offset + 6 <= limit:
            descriptor_length = ((section[offset + 4] & 0x0F) << 8) | section[offset + 5]
            descriptor_end = min(offset + 6 + descriptor_length, limit)
            self._decode_lcn_descriptors(section[offset + 6 : descriptor_end])
            offset += 6 + descriptor_length

    def _decode_lcn_descriptors(self, data: bytes) -> None:
        """Read service LCNs from descriptor 0x83 without replacing an existing LCN."""
        for tag, value in _descriptors(data):
            if tag != 0x83:
                continue
            for index in range(0, len(value) - 3, 4):
                service_id = int.from_bytes(value[index : index + 2], "big")
                service = self._services.setdefault(service_id, DVBService(service_id))
                if service.logical_channel_number is None:
                    service.logical_channel_number = int.from_bytes(value[index + 2 : index + 4], "big") & 0x03FF

    def _decode_eit(self, section: bytes) -> None:
        """Decode event names, descriptions, times, and running status from EIT.

        Extended-event descriptor text is reassembled in descriptor-number
        order and preferred over short-event text when it is non-empty.
        Present/following sections update the current and next event records.
        """
        if len(section) < 26:
            self._warnings.append("truncated EIT section")
            return
        service_id = int.from_bytes(section[3:5], "big")
        triplet_id = DVBTripletID(
            original_network_id=int.from_bytes(section[10:12], "big"),
            transport_stream_id=int.from_bytes(section[8:10], "big"),
            service_id=service_id,
        )
        service = self._services.setdefault(service_id, DVBService(service_id))
        events: list[DVBEvent] = []
        offset = 14
        section_end = _section_end(section)
        while offset + 12 <= section_end:
            event_id = int.from_bytes(section[offset : offset + 2], "big")
            start_time = _decode_dvb_time(section[offset + 2 : offset + 7])
            duration = _decode_duration(section[offset + 7 : offset + 10])
            running_status = section[offset + 10] >> 5
            descriptor_length = ((section[offset + 10] & 0x0F) << 8) | section[offset + 11]
            descriptor_start = offset + 12
            descriptor_end = descriptor_start + descriptor_length
            if descriptor_end > section_end:
                break
            name = text = None
            extended_event_language: bytes | None = None
            extended_event_parts: dict[int, bytes] = {}
            for tag, value in _descriptors(section[descriptor_start:descriptor_end]):
                if tag == 0x4D and len(value) >= 4:
                    name_length = value[3]
                    name = _decode_text(value[4 : 4 + name_length])
                    text_offset = 4 + name_length
                    if text_offset < len(value):
                        text = _decode_text(value[text_offset + 1 : text_offset + 1 + value[text_offset]])
                elif tag == 0x4E:
                    extended_event = _parse_extended_event_descriptor(value)
                    if extended_event is None:
                        continue
                    language, descriptor_number, _, descriptor_text = extended_event
                    if extended_event_language is None:
                        extended_event_language = language
                    if language == extended_event_language:
                        extended_event_parts[descriptor_number] = descriptor_text
            if extended_event_parts:
                extended_text = _decode_text(
                    b"".join(
                        extended_event_parts[number]
                        for number in sorted(extended_event_parts)
                    )
                )
                if extended_text:
                    text = extended_text
            event = DVBEvent(
                event_id,
                start_time,
                duration,
                name,
                text,
                running_status,
                triplet_id,
            )
            events.append(event)
            current_event = service.current_event
            if (
                current_event is None
                or running_status == 4
                or (running_status in (2, 3) and current_event.running_status != 4)
            ):
                service.current_event = event
            offset = descriptor_end

        if section[0] == 0x4E: # 0x4E indicates an EIT present/following section
            if section[6] == 1: # Current event present
                service.next_event = events[0] if events else None
            elif len(events) > 1: # Next event present
                service.next_event = events[1]
            elif section[7] == 0: # No next event present
                service.next_event = None


class DVBEventScanner(DVBScanner):
    """Scan present/following events for services in an existing stream info.

    Only EIT packets are passed to the base scanner, and events for services
    outside ``stream_info`` are ignored. Services are matched by the full
    ONID/TSID/SID triplet first, then by TSID and SID if the broadcast ONID
    differs from the stream map.

    Attributes:
        eit_packets_seen: EIT-PID packets passed to the base scanner.
        eit_sections_seen: EIT sections presented to this scanner's decoder.
        matched_eit_services: Broadcast triplets matched to a known service.
        unmatched_eit_services: Broadcast triplets with no known TSID/SID match.
        onid_mismatch_eit_services: Matched broadcast triplets that required
            the TSID/SID fallback because their ONID differed.
        event_updates: Number of times a service's current/next event pair changed.
        _on_event_ready: Optional callback invoked when a service is ready and
            its event pair becomes available or changes.
        _event_lock: Protects the event results and readiness-section sets.
        _event_results: Current and next event snapshots keyed by mapped triplet.
        _current_sections: Services for which a current-section EIT was received.
        _following_sections: Services for which following-event information is
            known, including cases where the current EIT declares no next event.
        _existing_services: Exact broadcast-triplet to stream-info-triplet map.
        _existing_services_by_ts_service: Fallback map keyed by TSID and SID.
    """

    def __init__(
        self,
        stream_info: DVBStreamInfo,
        *,
        on_event_ready: Callable[
            [DVBTripletID, DVBEvent | None, DVBEvent | None], None
        ] | None = None,
        verify_crc: bool = True,
    ) -> None:
        """Create a scanner that reports events only for services in ``stream_info``.

        EIT sections for services outside this stream info are ignored.

        Args:
            stream_info: Existing stream metadata defining the services to watch.
            on_event_ready: Optional callback receiving a service triplet and
                its current and next events after the service becomes ready or
                its event pair changes.
            verify_crc: Whether to discard sections that fail MPEG-2 CRC checks.
        """
        super().__init__(verify_crc=verify_crc)
        self.eit_packets_seen = 0
        self.eit_sections_seen = 0
        self.matched_eit_services: set[DVBTripletID] = set()
        self.unmatched_eit_services: set[DVBTripletID] = set()
        self.onid_mismatch_eit_services: set[DVBTripletID] = set()
        self.event_updates = 0
        self._on_event_ready = on_event_ready
        self._event_lock = Lock()
        self._event_results: dict[
            DVBTripletID, tuple[DVBEvent | None, DVBEvent | None]
        ] = {}
        self._current_sections: set[DVBTripletID] = set()
        self._following_sections: set[DVBTripletID] = set()
        doublet_id = stream_info.doublet_id
        # Map broadcast IDs to the IDs in stream_info, including ONID fallback matches.
        self._existing_services: dict[DVBTripletID, DVBTripletID] = {}
        self._existing_services_by_ts_service: dict[
            tuple[int, int], tuple[DVBTripletID, DVBTripletID]
        ] = {}
        for service in stream_info.services:
            triplet_id = service.triplet_id
            if triplet_id is None and doublet_id is not None:
                triplet_id = doublet_id.triplet_id(service.service_id)
            if triplet_id is not None:
                self._event_results[triplet_id] = (None, None)
                self._existing_services[triplet_id] = triplet_id
                self._existing_services_by_ts_service[
                    (triplet_id.transport_stream_id, triplet_id.service_id)
                ] = (triplet_id, triplet_id)

    @property
    def events(self) -> dict[DVBTripletID, tuple[DVBEvent | None, DVBEvent | None]]:
        """Return a snapshot of current and next events keyed by service triplet."""
        with self._event_lock:
            return dict(self._event_results)

    @property
    def events_complete(self) -> bool:
        """Return whether present/following event information is ready for all services."""
        with self._event_lock:
            return self._ready_services.issuperset(self._event_results)

    @property
    def pending_services(self) -> frozenset[DVBTripletID]:
        """Return services that have not yet received complete present/following EIT data."""
        with self._event_lock:
            return frozenset(self._event_results.keys() - self._ready_services)

    @property
    def _ready_services(self) -> set[DVBTripletID]:
        """Return services with both current and following EIT information."""
        return self._current_sections & self._following_sections

    def _feed_packet(self, packet: bytes) -> None:
        """Count and pass only EIT-PID packets to the base packet processor."""
        pid = ((packet[1] & 0x1F) << 8) | packet[2]
        if pid == 0x12:
            self.eit_packets_seen += 1
            super()._feed_packet(packet)

    def _decode_eit(self, section: bytes) -> None:
        """Match an EIT section, update its event snapshot, and notify if ready.

        An exact triplet match is preferred; a TSID/SID fallback is logged when
        ONIDs differ. Readiness requires current and following-section evidence
        for the service. Callbacks run after the event lock is released and
        only when a ready service is new or its event pair has changed.
        """
        self.eit_sections_seen += 1
        if len(section) < 12:
            return
        service_id = int.from_bytes(section[3:5], "big")
        triplet_id = DVBTripletID(
            original_network_id=int.from_bytes(section[10:12], "big"),
            transport_stream_id=int.from_bytes(section[8:10], "big"),
            service_id=service_id,
        )
        service_triplet_id = self._existing_services.get(triplet_id)
        if service_triplet_id is None:
            mapped_service = self._existing_services_by_ts_service.get(
                (triplet_id.transport_stream_id, triplet_id.service_id)
            )
            if mapped_service is None:
                if triplet_id not in self.unmatched_eit_services:
                    self.unmatched_eit_services.add(triplet_id)
                    logger.info(
                        "Received present/following EIT for unmapped service %s",
                        triplet_id,
                    )
                return
            service_triplet_id, _ = mapped_service
            if triplet_id not in self.onid_mismatch_eit_services:
                self.onid_mismatch_eit_services.add(triplet_id)
                logger.warning(
                    "EIT service %s matched to service-map entry %s by TSID and service ID",
                    triplet_id,
                    service_triplet_id,
                )

        self.matched_eit_services.add(triplet_id)
        decoded_service = self._services.get(service_id)
        previous_current_event = (
            decoded_service.current_event if decoded_service is not None else None
        )
        super()._decode_eit(section)
        decoded_service = self._services.get(service_id)
        if decoded_service is None:
            return
        if section[0] == 0x4E and section[6] == 1:
            decoded_service.current_event = previous_current_event

        current_event = self._event_for_triplet(
            decoded_service.current_event, service_triplet_id
        )
        next_event = self._event_for_triplet(
            decoded_service.next_event, service_triplet_id
        )
        callback_events: tuple[DVBEvent | None, DVBEvent | None] | None = None
        with self._event_lock:
            previous_events = self._event_results[service_triplet_id]
            current_events = (current_event, next_event)
            changed = previous_events != current_events
            self._event_results[service_triplet_id] = current_events
            if changed:
                self.event_updates += 1

            was_ready = service_triplet_id in self._ready_services
            if len(section) >= 26 and section[0] == 0x4E:
                if section[6] == 0:
                    self._current_sections.add(service_triplet_id)
                    if section[7] == 0 or next_event is not None:
                        self._following_sections.add(service_triplet_id)
                elif section[6] == 1:
                    self._following_sections.add(service_triplet_id)
            is_ready = service_triplet_id in self._ready_services
            if is_ready and (not was_ready or changed):
                callback_events = current_events

        if changed:
            logger.info(
                "Updated events for service %s: now=%r next=%r",
                service_triplet_id,
                current_event.name if current_event is not None else None,
                next_event.name if next_event is not None else None,
            )
        if callback_events is not None and self._on_event_ready is not None:
            self._on_event_ready(service_triplet_id, *callback_events)

    @staticmethod
    def _event_for_triplet(
        event: DVBEvent | None,
        triplet_id: DVBTripletID,
    ) -> DVBEvent | None:
        """Copy an event with the matched stream-info triplet attached."""
        if event is None:
            return None
        return replace(event, triplet_id=triplet_id)


def _descriptors(data: bytes):
    """Yield ``(tag, payload)`` pairs from a descriptor loop.

    If the loop ends with an incomplete header or payload, iteration stops
    without yielding that malformed descriptor or any trailing bytes.
    """
    offset = 0
    while offset + 2 <= len(data):
        length = data[offset + 1]
        if offset + 2 + length > len(data):
            return
        yield data[offset], data[offset + 2 : offset + 2 + length]
        offset += 2 + length


def _parse_extended_event_descriptor(
    value: bytes,
) -> tuple[bytes, int, int, bytes] | None:
    """Parse language, sequence numbers, and text from descriptor 0x4E.

    Return ``None`` for truncated data or an invalid descriptor-number range.
    The returned text bytes exclude the descriptor's item loop and length
    fields; callers can combine multiple descriptors by sequence number.
    """
    if len(value) < 6:
        return None

    descriptor_number = value[0] >> 4
    last_descriptor_number = value[0] & 0x0F
    if descriptor_number > last_descriptor_number:
        return None

    items_end = 5 + value[4]
    if items_end >= len(value):
        return None
    text_length = value[items_end]
    text_start = items_end + 1
    if text_start + text_length > len(value):
        return None

    return (
        value[1:4],
        descriptor_number,
        last_descriptor_number,
        value[text_start : text_start + text_length],
    )


def _section_end(section: bytes) -> int:
    """Return the exclusive end index of section data before its four-byte CRC."""
    return 3 + (int.from_bytes(section[1:3], "big") & 0x0FFF) - 4


# Maps ISO/IEC 6937 single-byte characters and combining accents to their Unicode equivalents.
_ISO6937_SINGLE_BYTE = (
    "\u00a0\u00a1\u00a2\u00a3\ufffd\u00a5\ufffd\u00a7"
    "\u00a4\u2018\u201c\u00ab\u2190\u2191\u2192\u2193"
    "\u00b0\u00b1\u00b2\u00b3\u00d7\u00b5\u00b6\u00b7"
    "\u00f7\u2019\u201d\u00bb\u00bc\u00bd\u00be\u00bf"
    + "\ufffd" * 16
    + "\u2014\u00b9\u00ae\u00a9\u2122\u266a\u00ac\u00a6"
    + "\ufffd" * 4
    + "\u215b\u215c\u215d\u215e"
    + "\u2126\u00c6\u00d0\u00aa\u0126\ufffd\u0132\u013f"
    + "\u0141\u00d8\u0152\u00ba\u00de\u0166\u014a\u0149"
    + "\u0138\u00e6\u0111\u00f0\u0127\u0131\u0133\u0140"
    + "\u0142\u00f8\u0153\u00df\u00fe\u0167\u014b\u00ad"
)

# Maps ISO/IEC 6937 combining accents to their Unicode equivalents.
_ISO6937_ACCENTS = {
    0xC1: "\u0300",
    0xC2: "\u0301",
    0xC3: "\u0302",
    0xC4: "\u0303",
    0xC5: "\u0304",
    0xC6: "\u0306",
    0xC7: "\u0307",
    0xC8: "\u0308",
    0xCA: "\u030A",
    0xCB: "\u0327",
    0xCD: "\u030B",
    0xCE: "\u0328",
    0xCF: "\u030C",
}

# Maps ISO/IEC 6937 spacing accents to their Unicode equivalents.
_ISO6937_SPACING_ACCENTS = {
    0xC1: "\u00B4",
    0xC5: "\u00AF",
    0xC6: "\u02D8",
    0xC7: "\u02D9",
    0xC8: "\u00A8",
    0xCA: "\u02DA",
    0xCB: "\u00B8",
    0xCD: "\u02DD",
    0xCE: "\u02DB",
    0xCF: "\u02C7",
}

# Maps DVB ISO 8859 selectors to their corresponding Python codec names.
_DVB_ISO8859_SELECTORS = {
    0x01: "iso8859_5",
    0x02: "iso8859_6",
    0x03: "iso8859_7",
    0x04: "iso8859_8",
    0x05: "iso8859_9",
    0x06: "iso8859_10",
    0x07: "iso8859_11",
    0x09: "iso8859_13",
    0x0A: "iso8859_14",
    0x0B: "iso8859_15",
}


def _decode_iso6937(data: bytes) -> str:
    """Decode DVB's default ISO/IEC 6937 text, including combining accents.

    Invalid bytes and dangling accent prefixes become the Unicode replacement
    character; combining sequences are normalized to NFC.
    """
    characters: list[str] = []
    offset = 0
    while offset < len(data):
        value = data[offset]
        accent = _ISO6937_ACCENTS.get(value)
        if accent is not None:
            if offset + 1 >= len(data):
                characters.append("\ufffd")
                offset += 1
                continue
            next_value = data[offset + 1]
            if next_value == 0x20:
                characters.append(_ISO6937_SPACING_ACCENTS.get(value, "\ufffd"))
            else:
                base = _decode_iso6937_byte(next_value)
                if base == "\ufffd":
                    characters.append("\ufffd")
                else:
                    characters.append(unicodedata.normalize("NFC", base + accent))
            offset += 2
            continue
        characters.append(_decode_iso6937_byte(value))
        offset += 1
    return "".join(characters)


def _decode_iso6937_byte(value: int) -> str:
    """Decode one non-accent-prefix ISO/IEC 6937 byte.

    Unassigned control-range bytes map to the Unicode replacement character.
    """
    if value < 0xA0:
        return chr(value)
    if value <= 0xBF or value >= 0xD0:
        return _ISO6937_SINGLE_BYTE[value - 0xA0]
    return "\ufffd"


def _decode_text(data: bytes) -> str:
    """Decode text using a DVB character-table selector or ISO/IEC 6937.

    Supported selectors include ISO-8859 variants, UTF-16BE, EUC-KR, GB2312,
    Big5, and UTF-8. Unsupported selector forms are skipped and decoded using
    the default encoding; decoding errors in selected codecs are replaced.
    Leading and trailing whitespace is removed.
    """
    if not data or data[0] == 0:
        return ""

    encoding = "iso6937"
    selector = data[0]
    if 0x01 <= selector <= 0x0B:
        encoding = _DVB_ISO8859_SELECTORS.get(selector, "iso6937")
        data = data[1:]
    elif selector == 0x10:
        if len(data) >= 3 and data[1] == 0 and 0 < data[2] <= 0x0F:
            part = data[2]
            encoding = f"iso8859_{part}" if part != 12 else "iso6937"
            data = data[3:]
        else:
            data = data[1:]
    elif selector == 0x11:
        encoding = "utf_16_be"
        data = data[1:]
    elif selector == 0x12:
        encoding = "euc_kr"
        data = data[1:]
    elif selector == 0x13:
        encoding = "gb2312"
        data = data[1:]
    elif selector == 0x14:
        encoding = "big5"
        data = data[1:]
    elif selector == 0x15:
        encoding = "utf_8"
        data = data[1:]
    elif 0x0C <= selector <= 0x0F or 0x16 <= selector <= 0x1E:
        data = data[1:]
    elif selector == 0x1F:
        data = data[2:] if len(data) >= 2 else data[1:]

    if encoding == "iso6937":
        text = _decode_iso6937(data)
    else:
        text = data.decode(encoding, errors="replace")
    return text.strip()


def _decode_bcd(value: int) -> int:
    """Convert the high and low decimal nibbles of one byte to an integer."""
    return (value >> 4) * 10 + (value & 0x0F)


def _decode_dvb_time(data: bytes) -> datetime | None:
    """Decode a five-byte MJD/BCD time field as a UTC datetime.

    Return ``None`` for an unspecified all-ones value, wrong-length input, or
    invalid BCD-derived clock fields.
    """
    if len(data) != 5 or data == b"\xff" * 5:
        return None
    mjd = int.from_bytes(data[:2], "big")
    date = datetime(1858, 11, 17, tzinfo=timezone.utc) + timedelta(days=mjd)
    hour, minute, second = (_decode_bcd(value) for value in data[2:5])
    try:
        return date.replace(hour=hour, minute=minute, second=second)
    except ValueError:
        return None


def _decode_duration(data: bytes) -> timedelta | None:
    """Decode three BCD hour/minute/second bytes into a duration.

    Return ``None`` for an unspecified all-ones value or wrong-length input.
    """
    if len(data) != 3 or data == b"\xff" * 3:
        return None
    return timedelta(hours=_decode_bcd(data[0]), minutes=_decode_bcd(data[1]), seconds=_decode_bcd(data[2]))


def _mpeg_crc32(data: bytes) -> int:
    """Compute the non-reflected MPEG-2 CRC-32 remainder for ``data``."""
    crc = 0xFFFFFFFF
    for byte in data:
        crc ^= byte << 24
        for _ in range(8):
            crc = ((crc << 1) ^ 0x04C11DB7) & 0xFFFFFFFF if crc & 0x80000000 else (crc << 1) & 0xFFFFFFFF
    return crc