
from dataclasses import dataclass
from pathlib import Path

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

    def stream_info(self, *, lcn: int | None = None, service_id: int | None = None,
                    service_name: str | None = None) -> DVBStreamInfo | None:
        if [lcn, service_id, service_name].count(None) != 2:
            raise ValueError("Exactly one of lcn, service_id, or service_name must be provided.")
        if lcn is not None:
            return self._stream_infos_by_lcn.get(lcn)
        if service_id is not None:
            return self._stream_infos_by_service_id.get(service_id)
        if service_name is not None:
            return self._stream_infos_by_service_name.get(service_name)
        return None

    def tuning(self, *, lcn: int | None = None, service_id: int | None = None,
               service_name: str | None = None) -> DVBTuning | None:
        if [lcn, service_id, service_name].count(None) != 2:
            raise ValueError("Exactly one of lcn, service_id, or service_name must be provided.")
        if lcn is not None:
            return self._tunings_by_lcn.get(lcn)
        if service_id is not None:
            return self._tunings_by_service_id.get(service_id)
        if service_name is not None:
            return self._tunings_by_service_name.get(service_name)
        return None

    def service(self, *, lcn: int | None = None, service_id: int | None = None,
                service_name: str | None = None) -> DVBService | None:
        if [lcn, service_id, service_name].count(None) != 2:
            raise ValueError("Exactly one of lcn, service_id, or service_name must be provided.")
        if lcn is not None:
            return self._services_by_lcn.get(lcn)
        if service_id is not None:
            return self._services_by_id.get(service_id)
        if service_name is not None:
            return self._services_by_name.get(service_name)
        return None


def scan_for_map(scan_table: str | Path, adapter: DVBAdapter, prefer_auto: bool = True,
                 read_timeout: float = 5.0):
    tunings = parse_scan(scan_table, prefer_auto=prefer_auto)
    channel_map = ChannelMap()

    for tuning in tunings:
        print(f"Tuning to frequency {tuning.frequency_hz / 1_000_000} MHz" +
                f" with bandwidth {tuning.bandwidth_hz / 1_000_000} MHz: ", end="", flush=True)
        status = adapter.tune(tuning, wait_for_lock=True)
        print(f"Lock result: {'LOCKED' if status and status.lock else 'NO LOCK'}")

        scanner = DVBScanner()
        try:
            adapter.start()
            stream_info = scanner.scan(
                adapter,
                policy=ScanPolicy.ALL_SERVICE_METADATA,
            )
            channel_map.add_stream_info(tuning, stream_info)
        finally:
            adapter.stop()

    adapter.sleep()
    return channel_map
