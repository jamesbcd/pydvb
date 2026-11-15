
"""Build and query indexes of DVB services discovered on tuned multiplexes.

The service map associates each discovered service with its tuning and
transport-stream metadata. It can be populated directly from tunings or from a
DVB-T scan table, and it serializes the underlying tuning/stream pairs rather
than the derived lookup indexes.
"""

from collections.abc import Mapping
import logging
from pathlib import Path
from threading import Event
from types import MappingProxyType
from typing import Callable, Collection, cast

from simple_serialize import serializable

from pydvb.driver_api.adapter import DVBAdapter
from pydvb.driver_api.tuner import DVBTuning
from pydvb.scan_tables import parse_table
from pydvb.stream_scanner import (
    DVBScanner,
    DVBStreamInfo,
    DVBService,
    DVBDoubletID,
    DVBTripletID,
    ScanPolicy,
)


logger = logging.getLogger(__name__)


@serializable(select=["_tuning_list", "_stream_info_list"])
class DVBServiceMap:
    """Index discovered DVB services by service, multiplex, and channel number.

    The tuning and stream-info lists are the canonical stored data. Lookup
    dictionaries are derived from those lists as stream information is added
    or deserialized. Public properties expose live, read-only views of the
    dictionaries.
    """

    def __init__(self):
        """Create empty service indexes and their read-only views."""
        #: Canonical tuning entries, kept in the same order as
        #: ``_stream_info_list`` for efficientserialization and reconstruction.
        #: (Serializing the dictionaries themselves results in excessive redundant
        #: data and very large serialization files.)
        self._tuning_list: list[DVBTuning] = []
        #: Canonical stream metadata entries, paired positionally with
        #: ``_tuning_list``.
        self._stream_info_list: list[DVBStreamInfo] = []

        #: Tuning associated with each service's DVB triplet identifier.
        self._tuning_dict: dict[DVBTripletID, DVBTuning] = {}
        #: Stream metadata associated with each service's triplet identifier.
        self._stream_info_dict: dict[DVBTripletID, DVBStreamInfo] = {}
        #: Service records indexed by their triplet identifiers.
        self._service_dict: dict[DVBTripletID, DVBService] = {}

        #: Tuning associated with each multiplex's DVB doublet identifier.
        self._doublet_tuning_dict: dict[DVBDoubletID, DVBTuning] = {}
        #: Stream metadata associated with each multiplex's doublet identifier.
        self._doublet_stream_info_dict: dict[DVBDoubletID, DVBStreamInfo] = {}

        #: Service triplet identifier indexed by logical channel number.
        self._tripletID_dict: dict[int, DVBTripletID] = {}

        self._initialize_read_only_views()

    def _initialize_read_only_views(self):
        """Create live read-only views over the mutable lookup dictionaries.

        The views reflect later dictionary updates; they are not snapshots.
        This method is also called after deserialization to bind the views to
        the reconstructed dictionaries.
        """
        #: Read-only live view of ``_tuning_dict``.
        self._read_only_tunings = MappingProxyType(self._tuning_dict)
        #: Read-only live view of ``_stream_info_dict``.
        self._read_only_stream_infos = MappingProxyType(self._stream_info_dict)
        #: Read-only live view of ``_service_dict``.
        self._read_only_services = MappingProxyType(self._service_dict)
        #: Read-only live view of ``_tripletID_dict``.
        self._read_only_tripletIDs = MappingProxyType(self._tripletID_dict)

        #: Read-only live view of ``_doublet_tuning_dict``.
        self._read_only_doublet_tunings = MappingProxyType(self._doublet_tuning_dict)
        #: Read-only live view of ``_doublet_stream_info_dict``.
        self._read_only_doublet_stream_infos = MappingProxyType(self._doublet_stream_info_dict)

    def add_stream_info(self, tuning: DVBTuning, stream_info: DVBStreamInfo):
        """Add a tuning and its scanned stream information to the map.

        The pair is always appended to the canonical lists. Available
        multiplex identifiers and service triplet identifiers are then added
        to their lookup indexes. Services without a triplet identifier are
        logged and omitted from service-level indexes; an available doublet
        identifier is still indexed independently.

        Args:
            tuning: Tuning used to receive the multiplex.
            stream_info: Stream metadata and services discovered on it.

        Raises:
            ValueError: If either argument is ``None``.
        """
        if tuning is None or stream_info is None:
            raise ValueError("Tuning and stream_info must not be None.")
        
        self._tuning_list.append(tuning)
        self._stream_info_list.append(stream_info)

        if stream_info.doublet_id is not None:
            self._doublet_tuning_dict[stream_info.doublet_id] = tuning
            self._doublet_stream_info_dict[stream_info.doublet_id] = stream_info

        for service in stream_info.services:
            triplet_id = service.triplet_id
            if triplet_id is None:
                logger.warning(
                    "No triplet ID for service %s ID=%s",
                    service.name,
                    service.service_id,
                )
                continue
            self._tuning_dict[triplet_id] = tuning
            self._stream_info_dict[triplet_id] = stream_info
            self._service_dict[triplet_id] = service
            if service.logical_channel_number is not None:
                self._tripletID_dict[service.logical_channel_number] = triplet_id

    @property
    def tunings(self) -> Mapping[DVBTripletID, DVBTuning]:
        """Return tunings indexed by service triplet identifier.

        The returned mapping is read-only and reflects later additions to the
        service map.
        """
        return self._read_only_tunings

    @property
    def stream_infos(self) -> Mapping[DVBTripletID, DVBStreamInfo]:
        """Return stream metadata indexed by service triplet identifier.

        The returned mapping is read-only and reflects later additions to the
        service map.
        """
        return self._read_only_stream_infos

    @property
    def services(self) -> Mapping[DVBTripletID, DVBService]:
        """Return services indexed by triplet identifier.

        The returned mapping is read-only and reflects later additions to the
        service map.
        """
        return self._read_only_services

    @property
    def triplet_ids(self) -> Mapping[int, DVBTripletID]:
        """Return the read-only logical-channel-number to triplet mapping."""
        return self._read_only_tripletIDs

    @property
    def tunings_by_doublet(self) -> Mapping[DVBDoubletID, DVBTuning]:
        """Return tunings indexed by multiplex doublet identifier.

        The returned mapping is read-only and reflects later additions to the
        service map.
        """
        return self._read_only_doublet_tunings

    @property
    def stream_infos_by_doublet(self) -> Mapping[DVBDoubletID, DVBStreamInfo]:
        """Return stream metadata indexed by multiplex doublet identifier.

        The returned mapping is read-only and reflects later additions to the
        service map.
        """
        return self._read_only_doublet_stream_infos

    def services_sorted(self, key=None) -> list[DVBService]:
        """Return services sorted by a supplied key or their default ordering.

        The default places services with logical channel numbers first,
        ascending by number, and then sorts by triplet identifier, placing
        services without one last. If ``key`` is provided, it is passed
        directly to :func:`sorted` instead.

        Args:
            key: Optional sort-key function applied to each service.
        """
        if key is None:
            key = lambda service: (service.logical_channel_number is None,
                                   service.logical_channel_number,
                                   service.triplet_id is None,
                                   service.triplet_id)
        return sorted(self.services.values(), key=key)

    @property
    def lcns(self) -> list[int | None]:
        """Return service logical channel numbers in ascending order.

        Missing numbers are included and sorted after all numeric values.
        """
        return sorted([service.logical_channel_number for service in self.services.values()],
                      key=lambda lcn: (lcn is None, lcn))

    @property
    def service_names(self) -> list[str | None]:
        """Return service names in ascending order, with missing names last."""
        return sorted([service.name for service in self.services.values()],
                      key=lambda name: (name is None, name))

    @classmethod
    def _deserialize(
        cls, data: object, options: dict[str, object], ctx: object | None
    ) -> "DVBServiceMap":
        """Rebuild the map's lookup indexes from serialized tuning/stream pairs.

        Args:
            data: Serialized representation containing the canonical lists.
            ctx: Deserialization context supplied by the serializer.

        Returns:
            A populated service map with read-only views attached to its
            reconstructed indexes.
        """
        instance = cls()
        data = cast(dict, data)
        # Reconstruct internal dictionaries from the serialized lists.
        for tuning, stream_info in zip(data["_tuning_list"], data["_stream_info_list"]):
            instance.add_stream_info(tuning, stream_info)
        # Reconstruct the read-only views from the internal dictionaries.
        instance._initialize_read_only_views()
        return instance


def map_from_scan(
    scan_table: str | Path,
    adapter: DVBAdapter,
    prefer_auto: bool = True,
    *,
    on_service_count: Callable[[int], None] | None = None,
):
    """Parse a scan table and build a service map.

    The scan table is parsed using :func:`pydvb.scan_tables.parse_table`; its
    tunings are then scanned in order. Unsupported and malformed table blocks
    are reported by the parser and do not prevent scanning the tunings it did
    parse.

    Args:
        scan_table: Local scan-table path or table source accepted by the
            scan-table parser.
        adapter: Adapter used to tune and scan each multiplex.
        prefer_auto: Whether the parser should prefer automatic values for
            omitted tuning parameters.
        on_service_count: Optional callback receiving cumulative service
            discoveries across tunings. A service found on multiple tunings
            is counted once for each tuning.

    Returns:
        The service map built from the tunings in the parsed table.
    """
    logger.info("Parsing scan table from %s", scan_table)
    result = parse_table(scan_table, prefer_auto=prefer_auto)
    logger.info(
        "Parsed %d tunings from %s (%d skipped, %d failed)",
        len(result.tunings),
        scan_table,
        result.skipped_blocks,
        result.failed_blocks,
    )
    if on_service_count is None:
        return map_from_tunings(result.tunings, adapter)
    return map_from_tunings(
        result.tunings,
        adapter,
        on_service_count=on_service_count,
    )

def map_from_tunings(
    tunings: Collection[DVBTuning],
    adapter: DVBAdapter,
    *,
    on_scan_error: Callable[[Exception], None] | None = None,
    on_service_count: Callable[[int], None] | None = None,
    on_mapped_count: Callable[[int], None] | None = None,
    on_tuning_progress: Callable[[int, int, DVBTuning], None] | None = None,
    cancel_event: Event | None = None,
) -> DVBServiceMap:
    """Tune each channel, scan locked streams, and put the adapter to sleep.

    A stream scan is attempted only when tuning reports a lock. Each started
    stream is stopped in a ``finally`` block, and the adapter is put to sleep
    once the scan sequence finishes or fails. If sleeping fails while handling
    another error, the original error remains primary and the sleep error is
    attached as its cause.

    Args:
        tunings: Tuning values to try, processed in iteration order.
        adapter: Adapter used to tune, start, stop, and sleep the receiver.
        on_scan_error: Optional diagnostic callback invoked with an exception
            raised while starting or scanning a locked stream, before stream
            cleanup. If this callback itself raises, the scan exception remains
            primary and the callback exception is attached as its cause.
        on_service_count: Optional callback receiving cumulative discoveries.
            Services found on multiple tunings are counted once per tuning.
        on_mapped_count: Optional callback receiving the number of unique
            channels currently in the service map after a tuning completes.
        on_tuning_progress: Optional callback invoked before each tuning.
        cancel_event: Optional event that stops the scan between tunings or
            stream reads. Results from completed tunings are retained.

    Returns:
        A service map containing metadata from successfully scanned,
        lock-acquired tunings.

    Raises:
        Exception: Propagates tuning, scanning, cleanup, or sleep errors. If
            sleeping fails while another error is active, the active error is
            re-raised with the sleep error as its cause.
    """
    channel_map = DVBServiceMap()
    total_service_count = 0

    try:
        for index, tuning in enumerate(tunings):
            if cancel_event is not None and cancel_event.is_set():
                break
            if on_tuning_progress is not None:
                on_tuning_progress(index + 1, len(tunings), tuning)
            logger.info(
                "Tuning %d of %d: Frequency %s MHz with bandwidth %s MHz",
                index + 1,
                len(tunings),
                tuning.frequency_hz / 1_000_000,
                tuning.bandwidth_hz / 1_000_000,
            )
            status = adapter.tune(tuning, wait_for_lock=True)
            logger.info("Lock result: %s", "LOCKED" if status and status.lock else "NO LOCK")
            if (
                cancel_event is not None
                and cancel_event.is_set()
                and not stream_info.complete
            ):
                break

            if status and status.lock:
                scanner = DVBScanner()
                try:
                    logger.info("Starting stream")
                    adapter.start()
                    logger.info("Scanning for services")
                    service_count_callback = (
                        None
                        if on_service_count is None
                        else lambda count: on_service_count(
                            total_service_count + count
                        )
                    )
                    if cancel_event is None and service_count_callback is None:
                        stream_info = scanner.scan(
                            adapter,
                            policy=ScanPolicy.ALL_SERVICE_METADATA,
                        )
                    elif cancel_event is None:
                        stream_info = scanner.scan(
                            adapter,
                            policy=ScanPolicy.ALL_SERVICE_METADATA,
                            on_service_count=service_count_callback,
                        )
                    elif service_count_callback is None:
                        stream_info = scanner.scan(
                            adapter,
                            policy=ScanPolicy.ALL_SERVICE_METADATA,
                            cancel_event=cancel_event,
                        )
                    else:
                        stream_info = scanner.scan(
                            adapter,
                            policy=ScanPolicy.ALL_SERVICE_METADATA,
                            on_service_count=service_count_callback,
                            cancel_event=cancel_event,
                        )
                    if cancel_event is not None and cancel_event.is_set():
                        break
                    channel_map.add_stream_info(tuning, stream_info)
                    total_service_count += len(stream_info.services)
                    if on_mapped_count is not None:
                        on_mapped_count(len(channel_map.services))
                except Exception as error:
                    if on_scan_error is not None:
                        try:
                            on_scan_error(error)
                        except Exception as diagnostic_error:
                            raise error from diagnostic_error
                    raise
                finally:
                    logger.info("Stopping stream")
                    adapter.stop()
    except BaseException as scan_error:
        try:
            adapter.sleep()
        except Exception as sleep_error:
            raise scan_error from sleep_error
        raise
    else:
        adapter.sleep()
    return channel_map
