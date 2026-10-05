import sys

import pytest

from mactv import __main__
from mactv.driver_api.tuner import DVBTuning
from mactv.stream_scanner import DVBService, DVBServiceType, DVBStreamInfo, DVBTripletID
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


class FakeStatusDevice(FakeDevice):
    def __init__(self):
        super().__init__()
        transport = type("Transport", (), {
            "get_speed": lambda _self: type("Speed", (), {"name": "HIGH"})(),
        })()
        adapter = type("Adapter", (), {
            "frontend": type("Frontend", (), {
                "get_config_dict": lambda _self: {"frequency": 177_500_000},
            })(),
            "demuxer": type("Demuxer", (), {
                "get_config_dict": lambda _self: {"running": False},
            })(),
            "tuner": None,
            "read_status": lambda _self: DVBFrontendStatus(False, False, False, False, False),
        })()
        self.adapters = [adapter]
        self.adapter_count = 1
        self.bridge = type("Bridge", (), {"transport": transport})()


def test_safe_probe_does_not_initialize(monkeypatch, capsys):
    device = FakeDevice()
    monkeypatch.setattr(__main__.AF9015Device, "open", lambda vendor_id, product_id: device)
    monkeypatch.setattr(sys, "argv", ["mactv", "device"])

    assert __main__.main() == 0
    assert not device.initialize_called
    assert "Device state: cold" in capsys.readouterr().out


def test_device_accepts_device_ids(monkeypatch):
    device = FakeDevice()
    calls = []

    def open_device(vendor_id, product_id):
        calls.append((vendor_id, product_id))
        return device

    monkeypatch.setattr(__main__.AF9015Device, "open", open_device)
    monkeypatch.setattr(
        sys,
        "argv",
        ["mactv", "device", "--vendor-id", "0x1234", "--product-id", "0x5678"],
    )

    assert __main__.main() == 0
    assert calls == [(0x1234, 0x5678)]


def test_init_initializes_without_tuning(monkeypatch, capsys):
    device = FakeDevice()
    monkeypatch.setattr(__main__.AF9015Device, "open", lambda vendor_id, product_id: device)
    monkeypatch.setattr(sys, "argv", ["mactv", "init"])

    assert __main__.main() == 0
    assert device.initialize_called
    assert not device.tune_called
    assert "Device is cold; initializing" in capsys.readouterr().out


def test_status_initializes_reports_and_exits(monkeypatch, capsys):
    device = FakeStatusDevice()
    monkeypatch.setattr(__main__.AF9015Device, "open", lambda vendor_id, product_id: device)
    monkeypatch.setattr(sys, "argv", ["mactv", "status"])

    assert __main__.main() == 0
    assert device.initialize_called
    assert not device.tune_called
    output = capsys.readouterr().out
    assert "USB speed: HIGH" in output
    assert "Adapter count: 1" in output


def test_sleep_puts_device_to_sleep_instead_of_tuning(monkeypatch, capsys):
    device = FakeDevice()
    monkeypatch.setattr(__main__.AF9015Device, "open", lambda vendor_id, product_id: device)
    monkeypatch.setattr(sys, "argv", ["mactv", "sleep"])

    assert __main__.main() == 0
    assert device.initialize_called
    assert device.sleep_called
    assert not device.tune_called
    assert "Putting device to sleep" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("device_class", "expected_code", "expected_output"),
    [
        (FakeLockedRecordingDevice, 0, "Lock result: LOCKED"),
        (FakeUnlockedRecordingDevice, 2, "Lock result: NO LOCK"),
    ],
)
def test_tune_returns_lock_result(monkeypatch, capsys, device_class, expected_code, expected_output):
    device = device_class()
    monkeypatch.setattr(__main__.AF9015Device, "open", lambda vendor_id, product_id: device)
    monkeypatch.setattr(sys, "argv", ["mactv", "tune"])

    assert __main__.main() == expected_code
    assert device.tune_called
    assert expected_output in capsys.readouterr().out


def test_tunable_commands_accept_scan_table_name_or_path(monkeypatch, tmp_path):
    device = FakeDevice()
    parsed_args = []
    scan_path = tmp_path / "brisbane.scan"
    monkeypatch.setattr(__main__.AF9015Device, "open", lambda vendor_id, product_id: device)
    monkeypatch.setattr(
        __main__,
        "_run_device",
        lambda _device, args: (parsed_args.append(args) or 0),
    )
    monkeypatch.setattr(sys, "argv", ["mactv", "tune", "--use-table", str(scan_path)])

    assert __main__.main() == 0
    assert parsed_args[0].use_table == str(scan_path)


def test_recording_stops_before_usb_read_when_frontend_does_not_lock(monkeypatch, capsys, tmp_path):
    device = FakeUnlockedRecordingDevice()
    monkeypatch.setattr(__main__.AF9015Device, "open", lambda vendor_id, product_id: device)
    monkeypatch.setattr(sys, "argv", ["mactv", "record", str(tmp_path / "capture.ts")])

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
    monkeypatch.setattr(sys, "argv", ["mactv", "record", str(tmp_path / "capture.ts")])

    assert __main__.main() == 0
    assert device.close_called


def test_scan_maps_tunings_and_prints_services(monkeypatch, capsys):
    device = FakeLockedRecordingDevice()
    triplet_id = DVBTripletID(1, 2, 10)
    service = DVBService(
        service_id=10,
        triplet_id=triplet_id,
        name="Example TV",
        logical_channel_number=7,
        service_type=DVBServiceType.DIGITAL_TELEVISION,
    )
    result = DVBStreamInfo(1, 2, "network", (service,), 3, True)
    channel_map = type(
        "ChannelMap",
        (),
        {
            "services_sorted": lambda _self: [service],
            "stream_infos": {triplet_id: result},
        },
    )()
    calls = []

    monkeypatch.setattr(__main__.AF9015Device, "open", lambda vendor_id, product_id: device)
    monkeypatch.setattr(
        __main__,
        "map_from_tunings",
        lambda tunings, adapter: (calls.append((tunings, adapter)) or channel_map),
    )
    monkeypatch.setattr(sys, "argv", ["mactv", "scan"])

    assert __main__.main() == 0
    assert len(calls) == 1
    tunings, adapter = calls[0]
    assert len(tunings) == 1
    assert isinstance(tunings[0], DVBTuning)
    assert tunings[0].frequency_hz == 226_500_000
    assert adapter is device.adapters[0]
    output = capsys.readouterr().out
    assert "Example TV" in output
    assert "network" in output
    assert device.close_called


def test_known_scan_tables_prints_names_without_opening_device(monkeypatch, capsys):
    calls = []

    monkeypatch.setattr(
        __main__,
        "known_table_names",
        lambda: (calls.append(True) or ["au-Adelaide", "au-Brisbane"]),
    )
    monkeypatch.setattr(
        __main__.AF9015Device,
        "open",
        lambda *_args: pytest.fail("known scan tables should not open a device"),
    )
    monkeypatch.setattr(sys, "argv", ["mactv", "known-tables"])

    assert __main__.main() == 0
    assert calls == [True]
    assert capsys.readouterr().out == "au-Adelaide\nau-Brisbane\n"


def test_parse_scan_prints_tunings_without_opening_device(monkeypatch, capsys):
    calls = []
    tunings = ["tuning"]

    monkeypatch.setattr(
        __main__,
        "parse_table",
        lambda scan: (calls.append(scan) or tunings),
    )
    monkeypatch.setattr(
        __main__.AF9015Device,
        "open",
        lambda *_args: pytest.fail("parse scan should not open a device"),
    )
    monkeypatch.setattr(sys, "argv", ["mactv", "parse-table", "au-Brisbane"])

    assert __main__.main() == 0
    assert calls == ["au-Brisbane"]
    assert capsys.readouterr().out == "['tuning']\n"


@pytest.mark.parametrize(
    ("arguments", "expected_log", "expected_stats"),
    [
        ([], False, False),
        (["--usb-log"], True, False),
        (["--usb-stats"], False, True),
        (["--usb-log", "--usb-stats"], True, True),
    ],
)
def test_usb_options_are_opt_in(monkeypatch, arguments, expected_log, expected_stats):
    device = FakeDevice()
    monkeypatch.setattr(__main__.AF9015Device, "open", lambda vendor_id, product_id: device)
    monkeypatch.setattr(sys, "argv", ["mactv", "device", *arguments])

    assert __main__.main() == 0
    assert __main__.usb.LOG is expected_log
    assert __main__.usb.RECORD_STATS is expected_stats


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