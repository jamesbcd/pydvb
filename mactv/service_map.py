
from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType
from typing import Callable, Collection, cast

from mactv.driver_api.adapter import DVBAdapter
from mactv.driver_api.tuner import DVBTuning
from mactv.scan_tables import parse_table
from mactv.serialize import Context, serializable, standard_serializer
from mactv.stream_scanner import DVBScanner, DVBStreamInfo, DVBService, DVBDoubletID, DVBTripletID, ScanPolicy


@serializable
class DVBServiceMap:
    def __init__(self):
        # For ease of use, we use dictionaries of triplet IDs to tunings, stream infos, and services.
        # But for compact serialization, we also stores lists of the tunings and stream infos, and only
        # serialize these lists. Serializating the dictionaries would produce many duplicate references
        # to the same objects, which is unnecessary.
        self._tuning_list: list[DVBTuning] = []
        self._stream_info_list: list[DVBStreamInfo] = []

        # Dictionaries mapping triplet IDs to tunings, stream infos, and services.
        self._tuning_dict: dict[DVBTripletID, DVBTuning] = {}
        self._stream_info_dict: dict[DVBTripletID, DVBStreamInfo] = {}
        self._service_dict: dict[DVBTripletID, DVBService] = {}

        # Dictionaries mapping doublet IDs to tunings and stream infos.
        self._doublet_tuning_dict: dict[DVBDoubletID, DVBTuning] = {}
        self._doublet_stream_info_dict: dict[DVBDoubletID, DVBStreamInfo] = {}

        # Dictionary mapping logical channel number to triplet ID
        self._tripletID_dict: dict[int, DVBTripletID] = {}

        self._initialize_read_only_views()

    def _initialize_read_only_views(self):
        # Read-only views of the tunings, stream infos, and services, keyed by
        # DVBTripletIDs for convenience.
        self._read_only_tunings = MappingProxyType(self._tuning_dict)
        self._read_only_stream_infos = MappingProxyType(self._stream_info_dict)
        self._read_only_services = MappingProxyType(self._service_dict)
        self._read_only_tripletIDs = MappingProxyType(self._tripletID_dict)

        # Read-only views of the tunings and stream infos, keyed by DVBDoubletIDs for convenience.
        self._read_only_doublet_tunings = MappingProxyType(self._doublet_tuning_dict)
        self._read_only_doublet_stream_infos = MappingProxyType(self._doublet_stream_info_dict)

    def add_stream_info(self, tuning: DVBTuning, stream_info: DVBStreamInfo):
        '''Add a stream info and its corresponding tuning to the channel map.
        
        Raises ValueError if either is None.
        '''
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
                print(f"No triplet ID for service {service.name} ID={service.service_id}")
                continue
            self._tuning_dict[triplet_id] = tuning
            self._stream_info_dict[triplet_id] = stream_info
            self._service_dict[triplet_id] = service
            if service.logical_channel_number is not None:
                self._tripletID_dict[service.logical_channel_number] = triplet_id

    @property
    def tunings(self) -> Mapping[DVBTripletID, DVBTuning]:
        '''Return a read-only map of all DVBTunings, keyed by service DVBTripletID.'''
        return self._read_only_tunings

    @property
    def stream_infos(self) -> Mapping[DVBTripletID, DVBStreamInfo]:
        '''Return a read-only map of all DVBStreamInfos, keyed by serviceDVBTripletID.'''
        return self._read_only_stream_infos

    @property
    def services(self) -> Mapping[DVBTripletID, DVBService]:
        '''Return a read-only map of all DVBServices, keyed by serviceDVBTripletID.'''
        return self._read_only_services

    @property
    def triplet_ids(self) -> Mapping[int, DVBTripletID]:
        '''Return a read-only map of all DVBTripletIDs, keyed by logical channel number.'''
        return self._read_only_tripletIDs

    @property
    def tunings_by_doublet(self) -> Mapping[DVBDoubletID, DVBTuning]:
        '''Return a read-only map of all DVBTunings, keyed by DVBDoubletID.'''
        return self._read_only_doublet_tunings

    @property
    def stream_infos_by_doublet(self) -> Mapping[DVBDoubletID, DVBStreamInfo]:
        '''Return a read-only map of all DVBStreamInfos, keyed by DVBDoubletID.'''
        return self._read_only_doublet_stream_infos

    def services_sorted(self, key=None) -> list[DVBService]:
        '''Return a sorted list of all DVB services in the channel map.
        
        If no key function is provided, services are sorted by logical channel number,
        with services lacking an LCN appearing last, and then by triplet ID.
        '''
        if key is None:
            key = lambda service: (service.logical_channel_number is None,
                                   service.logical_channel_number,
                                   service.triplet_id is None,
                                   service.triplet_id)
        return sorted(self.services.values(), key=key)

    @property
    def lcns(self) -> list[int | None]:
        '''Return a sorted list of all DVB logical channel numbers (LCNs) in the channel map.'''
        return sorted([service.logical_channel_number for service in self.services.values()],
                      key=lambda lcn: (lcn is None, lcn))

    @property
    def service_names(self) -> list[str | None]:
        '''Return a sorted list of all DVB service names in the channel map.'''
        return sorted([service.name for service in self.services.values()],
                      key=lambda name: (name is None, name))

    def _serialize(self, ctx: Context) -> dict:
        return {
            "_tuning_list": self._tuning_list,
            "_stream_info_list": self._stream_info_list,
        }

    @classmethod
    def _deserialize(cls, data: object, ctx: Context) -> "DVBServiceMap":
        instance = cls()
        data = cast(dict, data)
        # Reconstruct internal dictionaries from the serialized lists.
        for tuning, stream_info in zip(data["_tuning_list"], data["_stream_info_list"]):
            instance.add_stream_info(tuning, stream_info)
        # Reconstruct the read-only views from the internal dictionaries.
        instance._initialize_read_only_views()
        return instance


def map_from_scan(scan_table: str | Path, adapter: DVBAdapter, prefer_auto: bool = True):
    print(f"Parsing scan table from {scan_table}...", end="", flush=True)
    tunings = parse_table(scan_table, prefer_auto=prefer_auto)
    print("Done.")
    return map_from_tunings(tunings, adapter)

def map_from_tunings(
    tunings: Collection[DVBTuning],
    adapter: DVBAdapter,
    *,
    on_scan_error: Callable[[Exception], None] | None = None,
) -> DVBServiceMap:
    """Tune each channel, scan locked streams, and put the adapter to sleep.

    ``on_scan_error`` is called with a scanning exception before stream cleanup,
    allowing callers to collect diagnostics while the frontend remains awake.
    """
    channel_map = DVBServiceMap()

    try:
        for index, tuning in enumerate(tunings):
            print(f"Tuning {index+1} of {len(tunings)}: Frequency {tuning.frequency_hz / 1_000_000} MHz" +
                    f" with bandwidth {tuning.bandwidth_hz / 1_000_000} MHz: ", end="", flush=True)
            status = adapter.tune(tuning, wait_for_lock=True)
            print(f"Lock result: {'LOCKED' if status and status.lock else 'NO LOCK'}")

            if status and status.lock:
                scanner = DVBScanner()
                try:
                    print("\tStarting stream...", end="", flush=True)
                    adapter.start()
                    print("Done.")
                    print("\tScanning for services...", end="", flush=True)
                    stream_info = scanner.scan(adapter, policy=ScanPolicy.ALL_SERVICE_METADATA)
                    print("Done.")
                    channel_map.add_stream_info(tuning, stream_info)
                except Exception as error:
                    if on_scan_error is not None:
                        try:
                            on_scan_error(error)
                        except Exception as diagnostic_error:
                            raise error from diagnostic_error
                    raise
                finally:
                    print("\tStopping stream...", end="", flush=True)
                    adapter.stop()
                    print("Done.")
    except BaseException as scan_error:
        try:
            adapter.sleep()
        except Exception as sleep_error:
            raise scan_error from sleep_error
        raise
    else:
        adapter.sleep()
    return channel_map
