import sys

import pytest

from mactv import __main__
from mactv.scanner import DVBScanResult
from mactv.drivers.af9015.frontend import DVBFrontendStatus


class FakeDevice:
    def __init__(self):
        self.initialize_called = False
        self.sleep_called = False
        self.tune_called = False
        self.close_called = False

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

    def close(self):
        self.close_called = True

    def __enter__(self):
        return self

    def __exit__(self, _exception_type, _exception, _traceback):
        self.close()


class FakeUnlockedRecordingDevice(FakeDevice):
    def tune(self, tuning, adapter_index, wait_for_lock):
        self.tune_called = True
        return DVBFrontendStatus(False, False, False, False, False)


class FakeLockedRecordingDevice(FakeDevice):
    def __init__(self):
        super().__init__()
        self.adapters = [object()]
        self.bridge = type("Bridge", (), {
            "transport": type("Transport", (), {
                "log_stats": lambda _self: None,
            })(),
        })()

    def tune(self, tuning, adapter_index, wait_for_lock):
        self.tune_called = True
        return DVBFrontendStatus(True, True, True, True, True)


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


def test_recording_closes_device_after_capture(monkeypatch, tmp_path):
    device = FakeLockedRecordingDevice()
    monkeypatch.setattr(__main__.AF9015Device, "open", lambda vendor_id, product_id: device)
    monkeypatch.setattr(
        __main__,
        "capture_stream",
        lambda adapter, output_path: (10, 1.0),
    )
    monkeypatch.setattr(sys, "argv", ["mactv", "--record", str(tmp_path / "capture.ts")])

    assert __main__.main() == 0
    assert device.close_called


def test_scan_stream_prints_result_and_closes_device(monkeypatch, capsys):
    device = FakeLockedRecordingDevice()
    result = DVBScanResult(1, 2, "network", (), 3, True)
    monkeypatch.setattr(__main__.AF9015Device, "open", lambda vendor_id, product_id: device)
    monkeypatch.setattr(__main__, "scan_stream", lambda adapter: result)
    monkeypatch.setattr(sys, "argv", ["mactv", "--scan-stream"])

    assert __main__.main() == 0
    assert "DVBScanResult" in capsys.readouterr().out
    assert device.close_called


@pytest.mark.parametrize(
    ("arguments", "expected"),
    [([], False), (["--usb-log"], True)],
)
def test_usb_logging_is_opt_in(monkeypatch, arguments, expected):
    device = FakeDevice()
    monkeypatch.setattr(__main__.AF9015Device, "open", lambda vendor_id, product_id: device)
    monkeypatch.setattr(sys, "argv", ["mactv", "--safe", *arguments])

    assert __main__.main() == 0
    assert __main__.usb.LOG is expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [("177.5", 177_500_000), ("650", 650_000_000)],
)
def test_parse_frequency_accepts_mhz(value, expected):
    assert __main__.parse_frequency(value) == expected


def test_parse_frequency_rejects_non_positive_values():
    with pytest.raises(__main__.argparse.ArgumentTypeError):
        __main__.parse_frequency("0")


@pytest.mark.parametrize(
    ("value", "expected"),
    [("127.0.0.1:8080", ("127.0.0.1", 8080)), ("localhost:0", ("localhost", 0))],
)
def test_parse_server_address(value, expected):
    assert __main__.parse_server_address(value) == expected


def test_parse_server_address_rejects_invalid_values():
    with pytest.raises(__main__.argparse.ArgumentTypeError):
        __main__.parse_server_address("localhost:not-a-port")


def test_serve_default_address_is_loopback():
    assert __main__.DEFAULT_SERVER_ADDRESS == ("127.0.0.1", 8080)


class FakeStreamAdapter:
    def __init__(self, chunks):
        self.chunks = iter(chunks)
        self.events = []

    def start(self):
        self.events.append("start")

    def read_stream(self, timeout_ms=2000):
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
    assert adapter.events[0] == "start"
    assert adapter.events[-2:] == ["stop", "sleep"]
    assert adapter.events.count("read") >= 3


def test_capture_stream_sleeps_after_stopping_when_read_fails(tmp_path):
    adapter = FakeStreamAdapter([b"data"])
    output_path = tmp_path / "capture.ts"

    with pytest.raises(StopIteration):
        __main__.capture_stream(adapter, output_path, max_bytes=10)

    assert adapter.events[0] == "start"
    assert adapter.events[-2:] == ["stop", "sleep"]
    assert adapter.events.count("read") >= 2


def test_scan_stream_stops_after_scanner_is_complete(monkeypatch):
    adapter = FakeStreamAdapter([b"first", b"second"])
    scan_calls = []
    scanner = type("Scanner", (), {
        "scan": lambda _scanner, source, read_timeout_ms: (
            scan_calls.append((source, read_timeout_ms))
            or DVBScanResult(1, 2, "network", (), 1, True)
        ),
    })()
    monkeypatch.setattr(__main__, "DVBScanner", lambda: scanner)

    result = __main__.scan_stream(adapter)

    assert result.complete
    assert scan_calls == [(adapter, 5000)]
    assert adapter.events == ["start", "stop", "sleep"]


def test_capture_stream_fails_after_read_timeout(tmp_path):
    class TimeoutAdapter(FakeStreamAdapter):
        def read_stream(self, timeout_ms=2000):
            self.events.append("read")
            raise TimeoutError

    adapter = TimeoutAdapter(())
    output_path = tmp_path / "capture.ts"

    with pytest.raises(IOError, match="transport stream read returned no data"):
        __main__.capture_stream(adapter, output_path, max_bytes=4, read_timeout=0)

    assert adapter.events[0] == "start"
    assert "stop" in adapter.events
    assert adapter.events[-1] == "sleep"
    assert adapter.events.count("read") >= 1