from dataclasses import dataclass
from typing import cast

import pytest

from mactv.drivers.af9015.adapter import AF9015Adapter
from mactv.drivers.af9015.demux import AF9015Demuxer
from mactv.driver_api.adapter import DVBAdapter
from mactv.drivers.af9015.frontend import DVBFrontendStatus
from mactv.drivers.af9015.frontend import AF9013Frontend
from mactv.driver_api.tuner import DVBTuning


@dataclass
class FakeTuner:
    pass


class FakeFrontend:
    def __init__(self):
        self.adapter_index = 1
        self.tuner = FakeTuner()
        self.calls = []

    def init_hardware(self):
        self.calls.append(("init_hardware",))

    def sleep(self):
        self.calls.append(("sleep",))

    def tune(self, tuning, wait_for_lock=True):
        self.calls.append(("tune", tuning))
        return self.wait_for_lock() if wait_for_lock else self.read_status()

    def wait_for_lock(self, timeout=None, poll_interval=0.05):
        self.calls.append(("wait_for_lock", timeout, poll_interval))
        return DVBFrontendStatus(True, True, True, True, True)

    def read_status(self):
        self.calls.append(("read_status",))
        return DVBFrontendStatus(False, False, False, False, False)


class FakeDemuxer:
    PID_FILTER_COUNT = 32

    def __init__(self):
        self.calls = []

    def start(self):
        self.calls.append(("start",))

    def stop(self):
        self.calls.append(("stop",))

    def read_stream_once(self, timeout_ms=2000):
        self.calls.append(("read_stream_once", timeout_ms))
        return bytes((0x47, 0, 100, 0x10)) + b"x" * 184

    def clear_all_pid_filters(self):
        self.calls.append(("clear_all_pid_filters",))

    def set_pid_filter(self, pid, enabled=True):
        self.calls.append(("set_pid_filter", pid, enabled))

    def enable_pid_filter(self, enabled=True):
        self.calls.append(("enable_pid_filter", enabled))


def test_adapter_exposes_components_and_delegates_operations():
    frontend = FakeFrontend()
    demuxer = FakeDemuxer()
    adapter = AF9015Adapter(cast(AF9013Frontend, frontend), cast(AF9015Demuxer, demuxer))

    assert isinstance(adapter, DVBAdapter)
    adapter.init_hardware()
    tuning = DVBTuning(650_000_000)
    assert adapter.tune(tuning).lock
    assert adapter.tune(tuning, wait_for_lock=False).lock is False
    adapter.start()
    adapter.configure_pid_filter({100})
    adapter.set_pid_filter_enabled()
    assert adapter.read_stream(10) == bytes((0x47, 0, 100, 0x10)) + b"x" * 184
    adapter.stop()
    adapter.sleep()

    assert adapter.adapter_index == 1
    assert adapter.tuner is frontend.tuner
    assert frontend.calls == [
        ("init_hardware",),
        ("tune", tuning),
        ("wait_for_lock", None, 0.05),
        ("tune", tuning),
        ("read_status",),
        ("sleep",),
    ]
    assert demuxer.calls == [
        ("start",),
        ("clear_all_pid_filters",),
        ("set_pid_filter", 100, True),
        ("enable_pid_filter", True),
        ("enable_pid_filter", True),
        ("read_stream_once", 9),
        ("stop",),
    ]


class EmptyThenDataDemuxer(FakeDemuxer):
    def __init__(self, results):
        super().__init__()
        self.results = iter(results)

    def read_stream_once(self, timeout_ms=2000):
        self.calls.append(("read_stream_once", timeout_ms))
        return next(self.results)


def test_adapter_retries_empty_demux_reads_until_data_arrives():
    frontend = FakeFrontend()
    demuxer = EmptyThenDataDemuxer([b"", b"ts"])
    adapter = DVBAdapter(frontend, demuxer)

    assert adapter.read_stream(100) == b"ts"
    assert [call[0] for call in demuxer.calls] == [
        "read_stream_once",
        "read_stream_once",
    ]
    assert demuxer.calls[1][1] <= demuxer.calls[0][1]


def test_adapter_read_stream_times_out_after_empty_demux_reads():
    frontend = FakeFrontend()
    demuxer = EmptyThenDataDemuxer([b""] * 2)
    adapter = DVBAdapter(frontend, demuxer)

    with pytest.raises(TimeoutError, match="transport-stream data"):
        adapter.read_stream(0)


def _ts_packet(pid, payload=b"x"):
    header = bytes((0x47, (pid >> 8) & 0x1F, pid & 0xFF, 0x10))
    return header + payload * (188 - len(header))


def test_adapter_software_pid_filter_replaces_collection_atomically():
    frontend = FakeFrontend()
    demuxer = EmptyThenDataDemuxer([_ts_packet(100) + _ts_packet(200)])
    adapter = DVBAdapter(frontend, demuxer)

    adapter.configure_pid_filter({100})

    assert adapter.read_stream(100) == _ts_packet(100)


def test_adapter_software_pid_filter_can_be_disabled():
    frontend = FakeFrontend()
    demuxer = EmptyThenDataDemuxer([_ts_packet(100) + _ts_packet(200)])
    adapter = DVBAdapter(frontend, demuxer)
    adapter.configure_pid_filter({100})
    adapter.set_pid_filter_enabled(False)

    assert adapter.read_stream(100) == _ts_packet(100) + _ts_packet(200)


def test_adapter_rejects_invalid_pid_filter_values():
    adapter = DVBAdapter(FakeFrontend(), FakeDemuxer())

    with pytest.raises(ValueError, match="0x1FFF"):
        adapter.configure_pid_filter({0x2000})


def test_af9015_adapter_uses_software_filter_when_hardware_slots_overflow():
    demuxer = FakeDemuxer()
    adapter = AF9015Adapter(
        cast(AF9013Frontend, FakeFrontend()),
        cast(AF9015Demuxer, demuxer),
    )

    adapter.configure_pid_filter(range(33))

    assert demuxer.calls == [
        ("clear_all_pid_filters",),
        ("enable_pid_filter", False),
    ]
    assert adapter._filter_pid_stream(_ts_packet(32)) == _ts_packet(32)