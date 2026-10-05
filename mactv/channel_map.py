
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Collection

from mactv.driver_api.adapter import DVBAdapter
from mactv.driver_api.tuner import DVBTuning
from mactv.scan_tables import parse_table
from mactv.stream_scanner import DVBScanner, DVBStreamInfo, DVBService, DVBTripletID, ScanPolicy


class DVBChannelMap:
    def __init__(self):
        self._tunings: dict[DVBTripletID, DVBTuning] = {}
        self._stream_infos: dict[DVBTripletID, DVBStreamInfo] = {}
        self._services: dict[DVBTripletID, DVBService] = {}

        self._read_only_tunings = MappingProxyType(self._tunings)
        self._read_only_stream_infos = MappingProxyType(self._stream_infos)
        self._read_only_services = MappingProxyType(self._services)

    def add_stream_info(self, tuning: DVBTuning, stream_info: DVBStreamInfo):
        '''Add a stream info and its corresponding tuning to the channel map.
        
        Raises ValueError if either is None.
        '''
        if tuning is None or stream_info is None:
            raise ValueError("Tuning and stream_info must not be None.")

        for service in stream_info.services:
            triplet_id = service.triplet_id
            if triplet_id is None:
                print(f"No triplet ID for service {service.name} ID={service.service_id}")
                continue
            self._tunings[triplet_id] = tuning
            self._stream_infos[triplet_id] = stream_info
            self._services[triplet_id] = service

    @property
    def tunings(self) -> MappingProxyType[DVBTripletID,DVBTuning]:
        return self._read_only_tunings

    @property
    def stream_infos(self) -> MappingProxyType[DVBTripletID, DVBStreamInfo]:
        return self._read_only_stream_infos

    @property
    def services(self) -> MappingProxyType[DVBTripletID, DVBService]:
        return self._read_only_services

    def services_sorted(self, key=None) -> list[DVBService]:
        '''Return a sorted list of all DVB services in the channel map.
        
        If no key function is provided, services are sorted by logical channel number,
        with services lacking an LCN appearing last, and then by triplet ID.
        '''
        if key is None:
            key = lambda service: (service.logical_channel_number is None,
                                   service.logical_channel_number,
                                   service.triplet_id,
                                   service.triplet_id)
        return sorted(self._services.values(), key=key)

    @property
    def lcns(self) -> list[int | None]:
        '''Return a sorted list of all DVB logical channel numbers (LCNs) in the channel map.'''
        return sorted([service.logical_channel_number for service in self._services.values()],
                      key=lambda lcn: (lcn is None, lcn))

    @property
    def service_names(self) -> list[str | None]:
        '''Return a sorted list of all DVBservice names in the channel map.'''
        return sorted([service.name for service in self._services.values()],
                      key=lambda name: (name is None, name))



def map_from_scan(scan_table: str | Path, adapter: DVBAdapter, prefer_auto: bool = True):
    print(f"Parsing scan table from {scan_table}...", end="", flush=True)
    tunings = parse_table(scan_table, prefer_auto=prefer_auto)
    print("Done.")
    return map_from_tunings(tunings, adapter)

def map_from_tunings(tunings: Collection[DVBTuning], adapter: DVBAdapter):
    channel_map = DVBChannelMap()

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
            finally:
                print("\tStopping stream...", end="", flush=True)
                adapter.stop()
                print("Done.")

    adapter.sleep()
    return channel_map
