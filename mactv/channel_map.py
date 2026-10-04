
from dataclasses import dataclass
from typing import Iterable

from mactv.driver_api.tuner import DVBTuning
from mactv.stream_scanner import DVBScanResult, DVBService


class ChannelMap:

    def __init__(self):
        self._tunings = []
        self._stream_infos = []

        self._stream_infos_by_lcn: dict[int, DVBScanResult] = {}
        self._stream_infos_by_service_id: dict[int, DVBScanResult] = {}
        self._stream_infos_by_service_name: dict[str, DVBScanResult] = {}
        self._tunings_by_lcn: dict[int, DVBTuning] = {}
        self._tunings_by_service_id: dict[int, DVBTuning] = {}
        self._tunings_by_service_name: dict[str, DVBTuning] = {}
        self._services_by_lcn: dict[int, DVBService] = {}
        self._services_by_id: dict[int, DVBService] = {}
        self._services_by_name: dict[str, DVBService] = {}

    def add_stream_info(self, tuning: DVBTuning, stream_info: DVBScanResult):
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
                    service_name: str | None = None) -> DVBScanResult | None:
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
