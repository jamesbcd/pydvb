from dataclasses import dataclass
from typing import cast

from mactv.adapter import AF9015Adapter
from mactv.demux import AF9015Demuxer
from mactv.driverapi.adapter import DVBAdapter
from mactv.frontend import FrontendStatus
from mactv.frontend import AF9013Frontend
from mactv.tuner import Tuning


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
        return FrontendStatus(True, True, True, True, True)

    def read_status(self):
        self.calls.append(("read_status",))
        return FrontendStatus(False, False, False, False, False)


class FakeDemuxer:
    def __init__(self):
        self.calls = []

    def start(self):
        self.calls.append(("start",))

    def stop(self):
        self.calls.append(("stop",))

    def read_ts(self, timeout_ms=2000):
        self.calls.append(("read_ts", timeout_ms))
        return b"ts"

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
    tuning = Tuning(650_000_000)
    assert adapter.tune(tuning).lock
    assert adapter.tune(tuning, wait_for_lock=False).lock is False
    adapter.start()
    adapter.set_pid_filter(100)
    adapter.enable_pid_filter()
    assert adapter.read_ts(10) == b"ts"
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
        ("set_pid_filter", 100, True),
        ("enable_pid_filter", True),
        ("read_ts", 10),
        ("stop",),
    ]