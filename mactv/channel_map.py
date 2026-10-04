
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator

from mactv.driver_api.adapter import DVBAdapter
from mactv.driver_api.tuner import DVBTuning
from mactv.scan_tables import parse_scan
from mactv.stream_scanner import DVBScanner, DVBStreamInfo, DVBService, ScanPolicy


class ChannelMap:
    def __init__(self):
        self._tunings: list[DVBTuning] = []
        self._stream_infos: list[DVBStreamInfo] = []

        self._stream_infos_by_lcn: dict[int, DVBStreamInfo] = {}
        self._stream_infos_by_service_id: dict[int, DVBStreamInfo] = {}
        self._stream_infos_by_service_name: dict[str, DVBStreamInfo] = {}
        self._tunings_by_lcn: dict[int, DVBTuning] = {}
        self._tunings_by_service_id: dict[int, DVBTuning] = {}
        self._tunings_by_service_name: dict[str, DVBTuning] = {}
        self._services_by_lcn: dict[int, DVBService] = {}
        self._services_by_id: dict[int, DVBService] = {}
        self._services_by_name: dict[str, DVBService] = {}

    def add_stream_info(self, tuning: DVBTuning, stream_info: DVBStreamInfo):
        '''Add a stream info and its corresponding tuning to the channel map.
        
        Raises ValueError if either is None.
        '''
        self._tunings.append(tuning)
        self._stream_infos.append(stream_info)

        if tuning is None or stream_info is None:
            raise ValueError("Tuning and stream_info must not be None.")

        for service in stream_info.services:
            lcn = service.logical_channel_number
            service_id = service.service_id
            service_name = service.name

            if lcn is not None:
                self._stream_infos_by_lcn[lcn] = stream_info
                self._tunings_by_lcn[lcn] = tuning
                self._services_by_lcn[lcn] = service
            if service_id is not None:
                self._stream_infos_by_service_id[service_id] = stream_info
                self._tunings_by_service_id[service_id] = tuning
                self._services_by_id[service_id] = service
            if service_name is not None:
                self._stream_infos_by_service_name[service_name] = stream_info
                self._tunings_by_service_name[service_name] = tuning
                self._services_by_name[service_name] = service

    def lcns(self) -> list[int]:
        '''Return a sorted list of all DVB logical channel numbers (LCNs) in the channel map.'''
        return sorted(self._stream_infos_by_lcn.keys())

    def service_ids(self) -> list[int]:
        '''Return a sorted list of all DVB service IDs in the channel map.'''
        return sorted(self._stream_infos_by_service_id.keys())

    def service_names(self) -> list[str]:
        '''Return a sorted list of all DVBservice names in the channel map.'''
        return sorted(self._stream_infos_by_service_name.keys())

    def stream_infos(self) -> list[DVBStreamInfo]:
        '''Return a list of all DVBStreamInfos in the channel map.'''
        return self._stream_infos[:]

    def stream_info(self, *, lcn: int | None = None, service_id: int | None = None,
                    service_name: str | None = None) -> DVBStreamInfo | None:
        '''Return the DVBStreamInfo corresponding to the given LCN, service ID, or service name.

        Exactly one of lcn, service_id, or service_name must be provided.
        '''
        if [lcn, service_id, service_name].count(None) != 2:
            raise ValueError("Exactly one of lcn, service_id, or service_name must be provided.")
        if lcn is not None:
            return self._stream_infos_by_lcn.get(lcn)
        if service_id is not None:
            return self._stream_infos_by_service_id.get(service_id)
        if service_name is not None:
            return self._stream_infos_by_service_name.get(service_name)
        return None

    def tunings(self) -> list[DVBTuning]:
        '''Return a list of all DVBTunings in the channel map.'''
        return self._tunings[:]

    def tuning(self, *, lcn: int | None = None, service_id: int | None = None,
               service_name: str | None = None) -> DVBTuning | None:
        '''Return the DVBTuning corresponding to the given LCN, service ID, or service name.

        Exactly one of lcn, service_id, or service_name must be provided.
        '''
        if [lcn, service_id, service_name].count(None) != 2:
            raise ValueError("Exactly one of lcn, service_id, or service_name must be provided.")
        if lcn is not None:
            return self._tunings_by_lcn.get(lcn)
        if service_id is not None:
            return self._tunings_by_service_id.get(service_id)
        if service_name is not None:
            return self._tunings_by_service_name.get(service_name)
        return None

    def services(self) -> list[DVBService]:
        '''Return a list of all DVB services in the channel map.'''
        # We manually compile the list of services, to ensure we include any services
        # missing a LCN, service ID, or service name.
        services: list[DVBService] = []
        for service_info in self._stream_infos:
            services.extend(service_info.services)
        return services

    def service(self, *, lcn: int | None = None, service_id: int | None = None,
                service_name: str | None = None) -> DVBService | None:
        '''Return the DVBService corresponding to the given LCN, service ID, or service name.

        Exactly one of lcn, service_id, or service_name must be provided.
        '''
        if [lcn, service_id, service_name].count(None) != 2:
            raise ValueError("Exactly one of lcn, service_id, or service_name must be provided.")
        if lcn is not None:
            return self._services_by_lcn.get(lcn)
        if service_id is not None:
            return self._services_by_id.get(service_id)
        if service_name is not None:
            return self._services_by_name.get(service_name)
        return None

    def iter_streams_and_services(self) -> Iterator[tuple[DVBStreamInfo, DVBService]]:
        '''Yield pairs of (DVBStreamInfo, DVBService) for all services in the channel map.'''
        for stream_info in self._stream_infos:
            for service in stream_info.services:
                yield (stream_info, service)


def map_from_scan(scan_table: str | Path, adapter: DVBAdapter, prefer_auto: bool = True):
    print(f"Parsing scan table from {scan_table}...", end="", flush=True)
    tunings = parse_scan(scan_table, prefer_auto=prefer_auto)
    print("Done.")
    return map_from_tunings(tunings, adapter)

def map_from_tunings(tunings: Iterable[DVBTuning], adapter: DVBAdapter):
    channel_map = ChannelMap()

    for tuning in tunings:
        print(f"Tuning to frequency {tuning.frequency_hz / 1_000_000} MHz" +
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
