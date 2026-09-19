import sys

import pytest

from mactv import __main__
from mactv.frontend import FrontendStatus


class FakeDevice:
    def __init__(self):
        self.initialize_called = False
        self.sleep_called = False
        self.tune_called = False

    @property
    def is_warm(self):
        return False

    def initialize(self):
        self.initialize_called = True

    def init_hardware(self):
        self.initialize_called = True

    def sleep(self):
        self.sleep_called = True

    def tune(self, tuning, adapter_index, wait_for_lock):
        self.tune_called = True


class FakeUnlockedRecordingDevice(FakeDevice):
    def tune(self, tuning, adapter_index, wait_for_lock):
        self.tune_called = True
        return FrontendStatus(False, False, False, False, False)


def test_safe_probe_does_not_initialize(monkeypatch, capsys):
    device = FakeDevice()
    monkeypatch.setattr(__main__.AF9015Device, "open", lambda vendor_id, product_id: device)
    monkeypatch.setattr(sys, "argv", ["mactv", "--safe"])

    assert __main__.main() == 0
    assert not device.initialize_called
    assert "Device state: cold" in capsys.readouterr().out


def test_sleep_puts_device_to_sleep_instead_of_tuning(monkeypatch, capsys):
    device = FakeDevice()
    monkeypatch.setattr(__main__.AF9015Device, "open", lambda vendor_id, product_id: device)
    monkeypatch.setattr(sys, "argv", ["mactv", "--sleep"])

    assert __main__.main() == 0
    assert device.initialize_called
    assert device.sleep_called
    assert not device.tune_called
    assert "Device is sleeping" in capsys.readouterr().out


def test_recording_stops_before_usb_read_when_frontend_does_not_lock(monkeypatch, capsys, tmp_path):
    device = FakeUnlockedRecordingDevice()
    monkeypatch.setattr(__main__.AF9015Device, "open", lambda vendor_id, product_id: device)
    monkeypatch.setattr(sys, "argv", ["mactv", "--record", str(tmp_path / "capture.ts")])

    assert __main__.main() == 2
    output = capsys.readouterr().out
    assert "frontend did not lock" in output
    assert "transport stream read returned no data" not in output


@pytest.mark.parametrize(
    ("arguments", "expected"),
    [([], False), (["--usb-log"], True)],
)
def test_usb_logging_is_opt_in(monkeypatch, arguments, expected):
    device = FakeDevice()
    monkeypatch.setattr(__main__.AF9015Device, "open", lambda vendor_id, product_id: device)
    monkeypatch.setattr(sys, "argv", ["mactv", "--safe", *arguments])

    assert __main__.main() == 0
    assert __main__.usb_logging.LOG is expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [("177.5", 177_500_000), ("650", 650_000_000)],
)
def test_parse_frequency_accepts_mhz(value, expected):
    assert __main__.parse_frequency(value) == expected


def test_parse_frequency_rejects_non_positive_values():
    with pytest.raises(__main__.argparse.ArgumentTypeError):
        __main__.parse_frequency("0")


class FakeStreamAdapter:
    def __init__(self, chunks):
        self.chunks = iter(chunks)
        self.events = []

    def start(self):
        self.events.append("start")

    def read_ts(self):
        self.events.append("read")
        return next(self.chunks)

    def stop(self):
        self.events.append("stop")

    def sleep(self):
        self.events.append("sleep")


def test_capture_stream_respects_maximum_and_sleeps_after_stopping(tmp_path):
    adapter = FakeStreamAdapter([b"abcd", b"efgh", b"ijkl"])
    output_path = tmp_path / "capture.ts"

    bytes_written, elapsed = __main__.capture_stream(adapter, output_path, max_bytes=10)

    assert bytes_written == 10
    assert output_path.read_bytes() == b"abcdefghij"
    assert elapsed >= 0
    assert adapter.events == ["start", "read", "read", "read", "stop", "sleep"]


def test_capture_stream_sleeps_after_stopping_when_read_fails(tmp_path):
    adapter = FakeStreamAdapter([b"data"])
    output_path = tmp_path / "capture.ts"

    with pytest.raises(StopIteration):
        __main__.capture_stream(adapter, output_path, max_bytes=10)

    assert adapter.events == ["start", "read", "read", "stop", "sleep"]