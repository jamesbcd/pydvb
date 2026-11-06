"""Decode DVB service information from an MPEG transport stream."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from collections.abc import Iterable
from enum import Enum, IntEnum, auto
from typing import Any, Callable, NamedTuple

from construct import Bit, BitStruct, BitsInteger

from pydvb.driver_api.adapter import DVBAdapter
from pydvb.serialize import Context, serializable, standard_serializer


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
    """Define the amount of metadata required for a scan to be complete."""

    MIN_METADATA = auto() # Require only the minimum metadata to consider the scan complete
    ALL_SERVICE_METADATA = auto() # Require all service PIDs and logical channel numbers

@serializable
@dataclass
class DVBEvent:
    """Metadata for a single DVB event (programme)."""
    event_id: int
    start_time: datetime | None = None
    duration: timedelta | None = None
    name: str | None = None
    text: str | None = None
    running_status: int | None = None


@serializable
@dataclass
class DVBDescriptor:
    """Raw descriptor attached to a DVB service or elementary stream.
    
    A descriptor is a structured piece of metadata attached to a DVB service or
    elementary stream. It contains a tag identifying the type of descriptor and
    the associated data.
    """
    tag: int
    data: bytes


@serializable
@dataclass
class DVBElementaryStream:
    """Elementary stream entry advertised by a service's Program Map Table (PMT).
    
    An elementary stream represents a single media stream (audio, video, or data)
    within a DVB service. It is identified by a PID and may have associated descriptors.
    """
    pid: int
    stream_type: int
    descriptors: tuple[DVBDescriptor, ...] = ()

    def _serialize(self, ctx: Context) -> dict:
        output = standard_serializer(self, ctx)
        # No need to serialize the descriptors.
        output["descriptors"] = []
        return output


@serializable
class DVBServiceType(IntEnum):
    """Service type values from the DVB service descriptor."""
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
    """Broad media category for a DVB service."""

    TV = "TV"
    RADIO = "RADIO"
    OTHER = "OTHER"


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
    '''Represents a DVB doublet that globally and uniquely identifies a DVB stream.'''
    original_network_id: int
    transport_stream_id: int

    @classmethod
    def from_int(cls, value: int) -> "DVBDoubletID":
        """Create a DVBDoubletID from a single integer."""
        onid = (value >> 16) & 0xFFFF
        tsid = value & 0xFFFF
        return cls(original_network_id=onid, transport_stream_id=tsid)

    @classmethod
    def from_dotted(cls, value: str) -> "DVBDoubletID":
        """Create a DVBDoubletID from a dotted string in the format ONID.TSID"""
        onid, tsid = map(int, value.split("."))
        return cls(original_network_id=onid, transport_stream_id=tsid)

    # Compact aliases for convenience
    @property
    def onid(self) -> int:
        return self.original_network_id

    @property
    def tsid(self) -> int:
        return self.transport_stream_id

    def triplet_id(self, service_id: int) -> DVBTripletID:
        """Return a DVBTripletID for this doublet and the given service ID."""
        return DVBTripletID(
            original_network_id=self.original_network_id,
            transport_stream_id=self.transport_stream_id,
            service_id=service_id,
        )

    def __int__(self) -> int:
        '''Converts the DVBDoubletID to a single integer.'''
        return (self.original_network_id << 16) | self.transport_stream_id

    def __index__(self) -> int:
        '''Allows bin(), hex(), oct() and other functions that require a number to work with this object.'''
        return int(self)

    def dotted(self) -> str:
        '''Return the DVB doublet as a dotted string in the format ONID.TSID'''
        return f"{self.original_network_id}.{self.transport_stream_id}"

    def __str__(self) -> str:
        return self.dotted()

    def __format__(self, format_spec: str) -> str:
        return format(self.dotted(), format_spec)

    def _serialize(self, ctx: Context) -> str:
        return self.dotted()

    @classmethod
    def _deserialize(cls, data: object, ctx: Context) -> "DVBDoubletID":
        return cls.from_dotted(str(data))


@serializable
class DVBTripletID(NamedTuple):
    '''Represents a DVB triplet that globally and uniquely identifies a DVB service (i.e. channel).'''
    original_network_id: int
    transport_stream_id: int
    service_id: int

    @classmethod
    def from_int(cls, value: int) -> "DVBTripletID":
        """Create a DVBTripletID from a single integer."""
        onid = (value >> 32) & 0xFFFF
        tsid = (value >> 16) & 0xFFFF
        sid = value & 0xFFFF
        return cls(original_network_id=onid, transport_stream_id=tsid, service_id=sid)

    @classmethod
    def from_dotted(cls, value: str) -> "DVBTripletID":
        """Create a DVBTripletID from a dotted string in the format ONID.TSID.SID"""
        onid, tsid, sid = map(int, value.split("."))
        return cls(original_network_id=onid, transport_stream_id=tsid, service_id=sid)

    # Compact aliases for convenience
    @property
    def onid(self) -> int:
        return self.original_network_id

    @property
    def tsid(self) -> int:
        return self.transport_stream_id

    @property
    def sid(self) -> int:
        return self.service_id

    @property
    def doublet_id(self) -> DVBDoubletID:
        return DVBDoubletID(original_network_id=self.original_network_id, transport_stream_id=self.transport_stream_id)

    def __int__(self) -> int:
        '''Converts the DVBTriplet to a single integer.'''
        return (self.original_network_id << 32) | (self.transport_stream_id << 16) | self.service_id

    def __index__(self) -> int:
        '''Allows bin(), hex(), oct() and other functions that require a number to work with this object.'''
        return int(self)

    def dotted(self) -> str:
        '''Return the DVB triplet as a dotted string in the format ONID.TSID.SID'''
        return f"{self.original_network_id}.{self.transport_stream_id}.{self.service_id}"

    def __str__(self) -> str:
        return self.dotted()

    def __format__(self, format_spec: str) -> str:
        return format(self.dotted(), format_spec)

    def _serialize(self, ctx: Context) -> str:
        return self.dotted()

    @classmethod
    def _deserialize(cls, data: object, ctx: Context) -> "DVBTripletID":
        return cls.from_dotted(str(data))


@serializable
@dataclass
class DVBService:
    """Metadata for a single DVB service (channel)."""
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

    def _serialize(self, ctx: Context) -> dict:
        output = standard_serializer(self, ctx)
        # No need to serialize the current event.
        output["current_event"] = None
        return output


@serializable
@dataclass
class DVBStreamInfo:
    """Accumulated result of scanning a DVB transport stream."""
    original_network_id: int | None = None
    transport_stream_id: int | None = None
    network_name: str | None = None
    services: tuple[DVBService, ...] = ()
    packets_seen: int = 0
    complete: bool = False
    warnings: tuple[str, ...] = ()

    @property
    def doublet_id(self) -> DVBDoubletID | None:
        """Return a DVBDoubletID for this stream, or None if the ONID or TSID is missing."""
        if self.original_network_id is not None and self.transport_stream_id is not None:
            return DVBDoubletID(
                original_network_id=self.original_network_id,
                transport_stream_id=self.transport_stream_id,
            )
        return None

    def _serialize(self, ctx: Context) -> dict:
        output = standard_serializer(self, ctx)
        # No need to serialize warnings
        output["warnings"] = []
        return output


class DVBScanner:
    """Incrementally discover services and programme metadata from TS bytes.

    ``feed`` accepts arbitrary byte chunks. The scanner is independent of USB
    and can consume a file or an adapter read loop. It does not start or stop
    the source supplied by the caller.
    """

    def __init__(self, *, verify_crc: bool = True) -> None:
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
    ) -> DVBStreamInfo:
        """Consume a transport-stream source and return the accumulated result.

        Args:
            source: An iterable of byte chunks, or a started DVB adapter.
            policy: The completion policy that determines when scanning stops.
            read_timeout_ms: The timeout in milliseconds for adapter reads.
            on_service_count: Called with the number of discovered services after
                a chunk adds one or more services.

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
            previous_count = len(self._services)
            self.feed(data)
            service_count = len(self._services)
            if on_service_count is not None and service_count > previous_count:
                on_service_count(service_count)

        if isinstance(source, DVBAdapter):
            if not source.demuxer.running:
                raise RuntimeError("start the adapter before scanning")
            while not self.complete(policy):
                feed_and_report(source.read_stream(read_timeout_ms))
        else:
            for chunk in source:
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
        """Return a snapshot of the scanner state."""
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
        
        This method can be called multiple times as more TS data becomes available.
        Partial packets are buffered internally until complete.

        It is usually more convenient to call ``scan()`` with an iterable of byte chunks
        instead of repeatedly calling ``feed()``.
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
        """Feed a Transport Stream (TS) packet to the scanner."""

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
            payload_start: A boolean indicating if this payload starts a new section.
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
        """Extract complete sections from the buffer for a specific PID."""
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
        """Decode a complete section for a specific PID."""
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
        elif pid == 0x12 and 0x4E <= table_id <= 0x6F: # EIT (Event Information Table)
            self._decode_eit(section)
        elif pid in self._pmt_pids.values() and table_id == 0x02: # PMT (Program Map Table)
            self._decode_pmt(pid, section)

    def _decode_pat(self, section: bytes) -> None:
        """Decode a Program Association Table (PAT) section."""
        self._transport_stream_id = int.from_bytes(section[3:5], "big")
        end = _section_end(section)
        for offset in range(8, end, 4):
            service_id = int.from_bytes(section[offset : offset + 2], "big")
            pid = ((section[offset + 2] & 0x1F) << 8) | section[offset + 3]
            if service_id:
                self._pmt_pids[service_id] = pid
                self._services.setdefault(service_id, DVBService(service_id)).pmt_pid = pid

    def _decode_pmt(self, pid: int, section: bytes) -> None:
        """Decode a Program Map Table (PMT) section."""
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
        """Decode a Service Description Table (SDT) section."""
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
        """Decode a Network Information Table (NIT) section."""
        self._original_network_id = int.from_bytes(section[3:5], "big")
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
            descriptor_length = ((section[offset + 4] & 0x0F) << 8) | section[offset + 5]
            self._decode_lcn_descriptors(section[offset + 6 : offset + 6 + descriptor_length])
            offset += 6 + descriptor_length

    def _decode_bat(self, section: bytes) -> None:
        """Decode local channel numbers from a Bouquet Association Table."""
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
        """Decode LCN descriptors without replacing an LCN found in the NIT."""
        for tag, value in _descriptors(data):
            if tag != 0x83:
                continue
            for index in range(0, len(value) - 3, 4):
                service_id = int.from_bytes(value[index : index + 2], "big")
                service = self._services.setdefault(service_id, DVBService(service_id))
                if service.logical_channel_number is None:
                    service.logical_channel_number = int.from_bytes(value[index + 2 : index + 4], "big") & 0x03FF

    def _decode_eit(self, section: bytes) -> None:
        """Decode an Event Information Table (EIT) section."""
        if len(section) < 26:
            self._warnings.append("truncated EIT section")
            return
        service_id = int.from_bytes(section[3:5], "big")
        service = self._services.setdefault(service_id, DVBService(service_id))
        event_id = int.from_bytes(section[14:16], "big")
        start_time = _decode_dvb_time(section[16:21])
        duration = _decode_duration(section[21:24])
        running_status = section[24] >> 5
        descriptor_length = ((section[24] & 0x0F) << 8) | section[25]
        name = text = None
        for tag, value in _descriptors(section[26 : 26 + descriptor_length]):
            if tag == 0x4D and len(value) >= 4:
                name_length = value[3]
                name = _decode_text(value[4 : 4 + name_length])
                text_offset = 4 + name_length
                if text_offset < len(value):
                    text = _decode_text(value[text_offset + 1 : text_offset + 1 + value[text_offset]])
        event = DVBEvent(event_id, start_time, duration, name, text, running_status)
        if running_status in (2, 3) or service.current_event is None:
            service.current_event = event


def _descriptors(data: bytes):
    """Yield (tag, value) pairs for each descriptor in the given data."""
    offset = 0
    while offset + 2 <= len(data):
        length = data[offset + 1]
        if offset + 2 + length > len(data):
            return
        yield data[offset], data[offset + 2 : offset + 2 + length]
        offset += 2 + length


def _section_end(section: bytes) -> int:
    """Return the index of the end of the section."""
    return 3 + (int.from_bytes(section[1:3], "big") & 0x0FFF) - 4


def _decode_text(data: bytes) -> str:
    """Decode DVB text data, handling the DVB text encoding prefix if present."""
    if data.startswith(b"\x10\x00"):
        data = data[2:]
    return data.decode("utf-8", errors="replace").strip()


def _decode_bcd(value: int) -> int:
    """Decode a single byte of Binary-Coded Decimal (BCD) into an integer."""
    return (value >> 4) * 10 + (value & 0x0F)


def _decode_dvb_time(data: bytes) -> datetime | None:
    """Decode a 5-byte DVB time field into a datetime object."""
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
    """Decode a 3-byte DVB duration field into a timedelta object."""
    if len(data) != 3 or data == b"\xff" * 3:
        return None
    return timedelta(hours=_decode_bcd(data[0]), minutes=_decode_bcd(data[1]), seconds=_decode_bcd(data[2]))


def _mpeg_crc32(data: bytes) -> int:
    """Compute the MPEG-2 CRC32 of the given data."""
    crc = 0xFFFFFFFF
    for byte in data:
        crc ^= byte << 24
        for _ in range(8):
            crc = ((crc << 1) ^ 0x04C11DB7) & 0xFFFFFFFF if crc & 0x80000000 else (crc << 1) & 0xFFFFFFFF
    return crc