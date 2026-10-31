from dataclasses import dataclass
from typing import cast

import pydvb.drivers.af9015.adapter as af9015_adapter
from pydvb.drivers.af9015.adapter import AF9015Adapter
from pydvb.drivers.af9015.demux import AF9015Demuxer
from pydvb.driver_api.adapter import DVBAdapter
from pydvb.drivers.af9015.frontend import DVBFrontendStatus
from pydvb.drivers.af9015.frontend import AF9013Frontend
from pydvb.driver_api.tuner import DVBTuning


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
        self._pids = set()

    def start(self):
        self.calls.append(("start",))

    def stop(self):
        self.calls.append(("stop",))

    def read_stream_once(self, timeout_ms=2000):
        self.calls.append(("read_stream_once", timeout_ms))
        return bytes((0x47, 0, 100, 0x10)) + b"x" * 184

    def clear_hw_pid_filters(self):
        self.calls.append(("clear_hw_pid_filters",))
        self._pids.clear()

    def configure_one_hw_pid_filter(self, pid, enabled=True):
        self.calls.append(("configure_one_hw_pid_filter", pid, enabled))
        if enabled:
            self._pids.add(pid)
        else:
            self._pids.discard(pid)

    def set_hw_pid_filter_enabled(self, enabled=True):
        self.calls.append(("set_hw_pid_filter_enabled", enabled))

    @property
    def hw_filter_pids(self):
        return frozenset(self._pids)


def test_adapter_exposes_components_and_delegates_operations(monkeypatch):
    monkeypatch.setattr(af9015_adapter, "ALLOW_HARDWARE_PID_FILTER", True)
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
        ("clear_hw_pid_filters",),
        ("configure_one_hw_pid_filter", 100, True),
        ("set_hw_pid_filter_enabled", True),
        ("set_hw_pid_filter_enabled", True),
        ("read_stream_once", 9),
        ("stop",),
        ("set_hw_pid_filter_enabled", False),
    ]


def test_af9015_hardware_filter_does_not_filter_stream_again(monkeypatch):
    monkeypatch.setattr(af9015_adapter, "ALLOW_HARDWARE_PID_FILTER", True)
    frontend = FakeFrontend()
    demuxer = EmptyThenDataDemuxer([_ts_packet(100) + _ts_packet(200)])
    adapter = AF9015Adapter(
        cast(AF9013Frontend, frontend),
        cast(AF9015Demuxer, demuxer),
    )

    adapter.configure_pid_filter({100})

    assert adapter.read_stream(100) == _ts_packet(100) + _ts_packet(200)


class EmptyThenDataDemuxer(FakeDemuxer):
    def __init__(self, results):
        super().__init__()
        self.results = iter(results)

    def read_stream_once(self, timeout_ms=2000):
        self.calls.append(("read_stream_once", timeout_ms))
        return next(self.results)


def _ts_packet(pid, payload=b"x"):
    header = bytes((0x47, (pid >> 8) & 0x1F, pid & 0xFF, 0x10))
    return header + payload * (188 - len(header))


def test_af9015_adapter_uses_software_filter_when_hardware_slots_overflow(monkeypatch):
    monkeypatch.setattr(af9015_adapter, "ALLOW_HARDWARE_PID_FILTER", True)
    demuxer = FakeDemuxer()
    adapter = AF9015Adapter(
        cast(AF9013Frontend, FakeFrontend()),
        cast(AF9015Demuxer, demuxer),
    )

    adapter.configure_pid_filter(range(33))

    assert demuxer.calls == [
        ("clear_hw_pid_filters",),
        ("set_hw_pid_filter_enabled", False),
    ]
    assert adapter._filter_pid_stream(_ts_packet(32)) == _ts_packet(32)