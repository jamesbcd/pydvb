
from dataclasses import dataclass

from mactv.driver_api.tuner import DVBTuning
from mactv.stream_scanner import DVBScanResult, DVBService


@dataclass
class ChannelMap:
    tunings: list[DVBTuning] = []
    streams: list[DVBScanResult] = []
    streams_by_lcn: dict[int, DVBScanResult] = {}
    streams_by_service_id: dict[int, DVBScanResult] = {}
    streams_by_service_name: dict[str, DVBScanResult] = {}
    tunings_by_lcn: dict[int, DVBTuning] = {}
    tunings_by_service_id: dict[int, DVBTuning] = {}
    tunings_by_service_name: dict[str, DVBTuning] = {}
    services_by_lcn: dict[int, DVBService] = {}
    services_by_id: dict[int, DVBService] = {}
    services_by_name: dict[str, DVBService] = {}
