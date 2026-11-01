import argparse
import logging
import sys
from types import SimpleNamespace

import pytest

from pydvb import __main__
from pydvb.driver_api.tuner import DVBTuning
from pydvb.stream_scanner import (
    DVBDoubletID,
    DVBService,
    DVBServiceType,
    DVBStreamInfo,
    DVBTripletID,
)
from pydvb.drivers.af9015.frontend import DVBFrontendStatus


def test_parse_lcn_validates_dvb_field_range():
    assert __main__.parse_lcn("0") == 0
    assert __main__.parse_lcn("1023") == 1023
    for value in ("-1", "1024", "not-a-number"):
        with pytest.raises(argparse.ArgumentTypeError):
            __main__.parse_lcn(value)


def test_parse_duration_requires_positive_finite_seconds():
    assert __main__.parse_duration("15") == 15.0
    assert __main__.parse_duration("0.25") == 0.25
    for value in ("0", "-1", "nan", "inf", "not-a-number"):
        with pytest.raises(argparse.ArgumentTypeError):
            __main__.parse_duration(value)


def test_parse_triplet_id_validates_dvb_fields():
    assert __main__.parse_triplet_id("1.2.10") == DVBTripletID(1, 2, 10)
    assert __main__.parse_triplet_id("0.65535.65535") == DVBTripletID(0, 65535, 65535)
    for value in ("1.2", "1.2.3.4", "1.x.3", "-1.2.3", "1.65536.3"):
        with pytest.raises(argparse.ArgumentTypeError):
            __main__.parse_triplet_id(value)


def test_parse_doublet_id_validates_dvb_fields():
    assert __main__.parse_doublet_id("1.2") == DVBDoubletID(1, 2)
    assert __main__.parse_doublet_id("0.65535") == DVBDoubletID(0, 65535)
    for value in ("1", "1.2.3", "1.x", "-1.2", "1.65536"):
        with pytest.raises(argparse.ArgumentTypeError):
            __main__.parse_doublet_id(value)


def install_fake_manager(monkeypatch, device):
    calls = []

    class FakeDeviceManager:
        def __init__(self, _device_types):
            pass

        def connect_all(self, ids=None):
            calls.append(ids)
            return SimpleNamespace(connected=(device,))

        def __enter__(self):
            return self

        def __exit__(self, _exception_type, _exception, _traceback):
            device.close()

    monkeypatch.setattr(__main__, "DVBDeviceManager", FakeDeviceManager)
    return calls


class FakeDevice:
    def __init__(self):
        self.initialize_called = False
        self.init_hardware_called = False
        self.sleep_called = False
        self.tune_called = False
        self.close_called = False
        self.model_name = "Fake DVB Device"
        self.device_id = "fake:0000"
        self.adapters = [object()]
        self.tuned = []

    def is_warm(self):
        return False

    def initialize(self):
        self.initialize_called = True
        self.init_hardware_called = True

    def init_hardware(self):
        self.initialize_called = True
        self.init_hardware_called = True

    def log_transport_stats(self):
        pass

    def sleep(self):
        self.sleep_called = True

    def tune(self, tuning, adapter_index, wait_for_lock) -> DVBFrontendStatus | None:
        self.tune_called = True
        self.tuned.append(tuning)

    def close(self):
        self.close_called = True

    def __enter__(self):
        return self

    def __exit__(self, _exception_type, _exception, _traceback):
        self.close()


class FakeUnlockedRecordingDevice(FakeDevice):
    def tune(self, tuning, adapter_index, wait_for_lock):
        super().tune(tuning, adapter_index, wait_for_lock)
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
        super().tune(tuning, adapter_index, wait_for_lock)
        return DVBFrontendStatus(True, True, True, True, True)


class FakeRecordingAdapter:
    def __init__(self):
        self.pid_filters = []

    def configure_pid_filter(self, pids):
        self.pid_filters.append(frozenset(pids))


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


def test_main_requires_a_command(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["pydvb"])

    with pytest.raises(SystemExit) as error:
        __main__.main()

    assert error.value.code == 2
    assert "the following arguments are required: command" in capsys.readouterr().err


def test_safe_probe_does_not_initialize(monkeypatch, capsys):
    device = FakeDevice()
    install_fake_manager(monkeypatch, device)
    monkeypatch.setattr(sys, "argv", ["pydvb", "discover"])

    assert __main__.main() == 0
    assert not device.initialize_called
    assert "Device state: cold" in capsys.readouterr().out


def test_device_uses_supported_device_discovery(monkeypatch):
    device = FakeDevice()
    calls = install_fake_manager(monkeypatch, device)
    monkeypatch.setattr(sys, "argv", ["pydvb", "discover"])

    assert __main__.main() == 0
    assert calls == [None]


@pytest.mark.parametrize("option", ["--vendor-id", "--product-id"])
def test_device_rejects_removed_device_id_options(monkeypatch, option):
    monkeypatch.setattr(sys, "argv", ["pydvb", "discover", option, "0x1234"])

    with pytest.raises(SystemExit) as error:
        __main__.main()

    assert error.value.code == 2


def test_init_initializes_without_tuning(monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger="pydvb.__main__")
    device = FakeDevice()
    install_fake_manager(monkeypatch, device)
    monkeypatch.setattr(sys, "argv", ["pydvb", "init"])

    assert __main__.main() == 0
    assert device.init_hardware_called
    assert not device.tune_called
    assert "Device is cold; initializing device" in caplog.text


def test_status_initializes_reports_and_exits(monkeypatch, capsys):
    device = FakeStatusDevice()
    install_fake_manager(monkeypatch, device)
    monkeypatch.setattr(sys, "argv", ["pydvb", "status"])

    assert __main__.main() == 0
    assert device.init_hardware_called
    assert not device.tune_called
    output = capsys.readouterr().out
    assert "USB speed: HIGH" in output
    assert "Adapter count: 1" in output


def test_sleep_puts_device_to_sleep_instead_of_tuning(monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger="pydvb.__main__")
    device = FakeDevice()
    install_fake_manager(monkeypatch, device)
    monkeypatch.setattr(sys, "argv", ["pydvb", "sleep"])

    assert __main__.main() == 0
    assert device.init_hardware_called
    assert device.sleep_called
    assert not device.tune_called
    assert "Putting device to sleep" in caplog.text


@pytest.mark.parametrize(
    ("device_class", "expected_code", "expected_output"),
    [
        (FakeLockedRecordingDevice, 0, "Lock result: LOCKED"),
        (FakeUnlockedRecordingDevice, 2, "Lock result: NO LOCK"),
    ],
)
def test_tune_returns_lock_result(monkeypatch, capsys, device_class, expected_code, expected_output):
    device = device_class()
    install_fake_manager(monkeypatch, device)
    monkeypatch.setattr(sys, "argv", ["pydvb", "tune", "--tuning", "177.5/8"])

    assert __main__.main() == expected_code
    assert device.tune_called
    assert expected_output in capsys.readouterr().out


def test_tune_requires_explicit_tuning(monkeypatch):
    device = FakeDevice()
    install_fake_manager(monkeypatch, device)
    monkeypatch.setattr(sys, "argv", ["pydvb", "tune"])

    with pytest.raises(SystemExit) as error:
        __main__.main()

    assert error.value.code == 2


def test_tune_rejects_both_tuning_sources(monkeypatch):
    monkeypatch.setattr(
        sys,
        "argv",
        ["pydvb", "tune", "--tuning", "177.5/8", "--use-table", "au-Brisbane"],
    )

    with pytest.raises(SystemExit) as error:
        __main__.main()

    assert error.value.code == 2


def test_tune_rejects_load_services(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["pydvb", "tune", "--load-services", "services.json"])

    with pytest.raises(SystemExit) as error:
        __main__.main()

    assert error.value.code == 2


def test_load_services_rejects_tuning_source(monkeypatch):
    monkeypatch.setattr(
        sys,
        "argv",
        ["pydvb", "scan", "--tuning", "177.5/8", "--load-services", "services.json"],
    )

    with pytest.raises(SystemExit) as error:
        __main__.main()

    assert error.value.code == 2


def test_tune_passes_combined_tuning_to_device(monkeypatch):
    class CapturingDevice(FakeDevice):
        def tune(self, tuning, adapter_index, wait_for_lock):
            self.last_tuning = tuning
            super().tune(tuning, adapter_index, wait_for_lock)

    device = CapturingDevice()
    install_fake_manager(monkeypatch, device)
    monkeypatch.setattr(sys, "argv", ["pydvb", "tune", "--tuning", "177.5/8"])

    assert __main__.main() == 2
    assert device.last_tuning.frequency_hz == 177_500_000
    assert device.last_tuning.bandwidth_hz == 8_000_000


@pytest.mark.parametrize("flag", ["--frequency", "--bandwidth"])
def test_tune_rejects_removed_tuning_flags(monkeypatch, flag):
    monkeypatch.setattr(sys, "argv", ["pydvb", "tune", flag, "177.5"])

    with pytest.raises(SystemExit) as error:
        __main__.main()

    assert error.value.code == 2


def test_tunable_commands_accept_scan_table_name_or_path(monkeypatch, tmp_path):
    device = FakeDevice()
    parsed_args = []
    scan_path = tmp_path / "brisbane.scan"
    install_fake_manager(monkeypatch, device)
    monkeypatch.setattr(
        __main__,
        "_run_device",
        lambda _device, args: (parsed_args.append(args) or 0),
    )
    monkeypatch.setattr(sys, "argv", ["pydvb", "tune", "--use-table", str(scan_path)])

    assert __main__.main() == 0
    assert parsed_args[0].use_table == str(scan_path)


def install_recording_channel_map(monkeypatch):
    mapped_tuning = DVBTuning(frequency_hz=191_500_000, bandwidth_hz=7_000_000)
    triplet_id = DVBTripletID(1, 2, 10)
    doublet_id = DVBDoubletID(1, 2)
    pids = frozenset((0, 16, 17, 100, 101))
    service = SimpleNamespace(
        name="Test service",
        triplet_id=triplet_id,
        logical_channel_number=7,
        pids=lambda: pids,
    )
    channel_map = SimpleNamespace(
        tunings={triplet_id: mapped_tuning},
        tunings_by_doublet={doublet_id: mapped_tuning},
        triplet_ids={7: triplet_id},
        services={triplet_id: service},
        stream_infos_by_doublet={
            doublet_id: SimpleNamespace(network_name="Test network"),
        },
        lcns=[7],
    )
    calls = []

    def fake_map_from_tunings(tunings, adapter, **_kwargs):
        calls.append((tunings, adapter))
        return channel_map

    monkeypatch.setattr(__main__, "map_from_tunings", fake_map_from_tunings)
    return calls, mapped_tuning, pids


def test_recording_stops_before_usb_read_when_frontend_does_not_lock(monkeypatch, capsys, tmp_path):
    device = FakeUnlockedRecordingDevice()
    install_fake_manager(monkeypatch, device)
    map_calls, mapped_tuning, _pids = install_recording_channel_map(monkeypatch)
    capture_calls = []
    monkeypatch.setattr(
        __main__, "capture_stream", lambda *_args: capture_calls.append(True)
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["pydvb", "record", "--doublet-id", "1.2", "--tuning", "177.5/8",
         str(tmp_path / "capture.ts")],
    )

    assert __main__.main() == 2
    assert len(map_calls) == 1
    assert map_calls[0][0][0].frequency_hz == 177_500_000
    assert map_calls[0][1] is device.adapters[0]
    assert device.tuned == [mapped_tuning]
    assert capture_calls == []
    output = capsys.readouterr().out
    assert "frontend did not lock" in output
    assert "transport stream read returned no data" not in output


def test_recording_closes_device_after_capture(monkeypatch, tmp_path):
    device = FakeLockedRecordingDevice()
    install_fake_manager(monkeypatch, device)
    map_calls, mapped_tuning, _pids = install_recording_channel_map(monkeypatch)
    capture_calls = []
    monkeypatch.setattr(
        __main__,
        "capture_stream",
        lambda adapter, output_path, duration: (
            capture_calls.append((adapter, output_path, duration)) or (10, 1.0)
        ),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["pydvb", "record", "--doublet-id", "1.2", "--duration", "2.5",
         "--tuning", "177.5/8",
         str(tmp_path / "capture.ts")],
    )

    assert __main__.main() == 0
    assert len(map_calls) == 1
    assert map_calls[0][0][0].frequency_hz == 177_500_000
    assert map_calls[0][1] is device.adapters[0]
    assert device.tuned == [mapped_tuning]
    assert capture_calls == [(device.adapters[0], tmp_path / "capture.ts", 2.5)]
    assert device.close_called


def test_recording_selects_lcn_tuning_and_filters_service_pids(monkeypatch, tmp_path):
    device = FakeLockedRecordingDevice()
    adapter = FakeRecordingAdapter()
    device.adapters[0] = adapter
    install_fake_manager(monkeypatch, device)
    map_calls, mapped_tuning, pids = install_recording_channel_map(monkeypatch)
    capture_calls = []
    monkeypatch.setattr(
        __main__,
        "capture_stream",
        lambda capture_adapter, output_path, duration: (
            capture_calls.append((capture_adapter, output_path, duration)) or (10, 1.0)
        ),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["pydvb", "record", "--lcn", "7", "--tuning", "177.5/8",
         str(tmp_path / "capture.ts")],
    )

    assert __main__.main() == 0
    assert len(map_calls) == 1
    assert device.tuned == [mapped_tuning]
    assert adapter.pid_filters == [pids]
    assert capture_calls == [(adapter, tmp_path / "capture.ts", 15.0)]


def test_recording_selects_triplet_id_tuning_and_filters_service_pids(monkeypatch, tmp_path):
    device = FakeLockedRecordingDevice()
    adapter = FakeRecordingAdapter()
    device.adapters[0] = adapter
    install_fake_manager(monkeypatch, device)
    _map_calls, mapped_tuning, pids = install_recording_channel_map(monkeypatch)
    capture_calls = []
    monkeypatch.setattr(
        __main__,
        "capture_stream",
        lambda capture_adapter, output_path, duration: (
            capture_calls.append((capture_adapter, output_path, duration)) or (10, 1.0)
        ),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["pydvb", "record", "--triplet-id", "1.2.10", "--tuning", "177.5/8",
         str(tmp_path / "capture.ts")],
    )

    assert __main__.main() == 0
    assert device.tuned == [mapped_tuning]
    assert adapter.pid_filters == [pids]
    assert capture_calls == [(adapter, tmp_path / "capture.ts", 15.0)]


def test_recording_selects_doublet_id_tuning_for_full_multiplex(monkeypatch, tmp_path):
    device = FakeLockedRecordingDevice()
    adapter = FakeRecordingAdapter()
    device.adapters[0] = adapter
    install_fake_manager(monkeypatch, device)
    _map_calls, mapped_tuning, _pids = install_recording_channel_map(monkeypatch)
    capture_calls = []
    monkeypatch.setattr(
        __main__,
        "capture_stream",
        lambda capture_adapter, output_path, duration: (
            capture_calls.append((capture_adapter, output_path, duration)) or (10, 1.0)
        ),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["pydvb", "record", "--doublet-id", "1.2", "--tuning", "177.5/8",
         str(tmp_path / "capture.ts")],
    )

    assert __main__.main() == 0
    assert device.tuned == [mapped_tuning]
    assert adapter.pid_filters == []
    assert capture_calls == [(adapter, tmp_path / "capture.ts", 15.0)]


def test_recording_unknown_doublet_id_fails_before_tuning_or_capture(
    monkeypatch, tmp_path, capsys
):
    device = FakeLockedRecordingDevice()
    install_fake_manager(monkeypatch, device)
    install_recording_channel_map(monkeypatch)
    capture_calls = []
    monkeypatch.setattr(
        __main__, "capture_stream", lambda *_args: capture_calls.append(True)
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["pydvb", "record", "--doublet-id", "1.3", "--tuning", "177.5/8",
         str(tmp_path / "capture.ts")],
    )

    assert __main__.main() == 2
    assert device.tuned == []
    assert capture_calls == []
    assert "doublet ID 1.3 was not found" in capsys.readouterr().out


def test_recording_doublet_id_is_exclusive_with_service_selectors(monkeypatch, tmp_path):
    monkeypatch.setattr(
        sys,
        "argv",
        ["pydvb", "record", "--doublet-id", "1.2", "--lcn", "7", "--tuning",
         "177.5/8", str(tmp_path / "capture.ts")],
    )

    with pytest.raises(SystemExit) as error:
        __main__.main()

    assert error.value.code == 2


def test_recording_requires_a_service_selector(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(
        sys,
        "argv",
        ["pydvb", "record", "--tuning", "177.5/8", str(tmp_path / "capture.ts")],
    )

    with pytest.raises(SystemExit) as error:
        __main__.main()

    assert error.value.code == 2
    assert "one of the arguments --lcn --triplet-id --doublet-id is required" in (
        capsys.readouterr().err
    )


def test_recording_unknown_triplet_id_fails_before_tuning_or_capture(
    monkeypatch, tmp_path, capsys
):
    device = FakeLockedRecordingDevice()
    install_fake_manager(monkeypatch, device)
    install_recording_channel_map(monkeypatch)
    capture_calls = []
    monkeypatch.setattr(
        __main__, "capture_stream", lambda *_args: capture_calls.append(True)
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["pydvb", "record", "--triplet-id", "1.2.11", "--tuning", "177.5/8",
         str(tmp_path / "capture.ts")],
    )

    assert __main__.main() == 2
    assert device.tuned == []
    assert capture_calls == []
    assert "triplet ID 1.2.11 was not found" in capsys.readouterr().out


def test_recording_unknown_lcn_fails_before_tuning_or_capture(monkeypatch, tmp_path, capsys):
    device = FakeLockedRecordingDevice()
    install_fake_manager(monkeypatch, device)
    map_calls, _mapped_tuning, _pids = install_recording_channel_map(monkeypatch)
    capture_calls = []
    monkeypatch.setattr(
        __main__, "capture_stream", lambda *_args: capture_calls.append(True)
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["pydvb", "record", "--lcn", "8", "--tuning", "177.5/8",
         str(tmp_path / "capture.ts")],
    )

    assert __main__.main() == 2
    assert len(map_calls) == 1
    assert device.tuned == []
    assert capture_calls == []
    assert "LCN 8 was not found" in capsys.readouterr().out


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

    install_fake_manager(monkeypatch, device)
    monkeypatch.setattr(
        __main__,
        "map_from_tunings",
        lambda tunings, adapter, **_kwargs: (calls.append((tunings, adapter)) or channel_map),
    )
    monkeypatch.setattr(sys, "argv", ["pydvb", "scan", "--tuning", "177.5/8"])

    assert __main__.main() == 0
    assert len(calls) == 1
    tunings, adapter = calls[0]
    assert tunings
    assert all(isinstance(tuning, DVBTuning) for tuning in tunings)
    assert adapter is device.adapters[0]
    output = capsys.readouterr().out
    assert "Example TV" in output
    assert "network" in output
    assert device.close_called


def test_scan_loads_services_without_tuning_or_scan_table(monkeypatch, tmp_path):
    device = FakeDevice()
    services_path = tmp_path / "services.json"
    channel_map = SimpleNamespace(services_sorted=lambda: [])
    install_fake_manager(monkeypatch, device)
    monkeypatch.setattr(
        __main__.serialize,
        "load",
        lambda path: channel_map if path == services_path else pytest.fail("unexpected path"),
    )
    monkeypatch.setattr(
        __main__, "parse_table", lambda *_args, **_kwargs: pytest.fail("unexpected table parse")
    )
    monkeypatch.setattr(
        __main__,
        "map_from_tunings",
        lambda *_args, **_kwargs: pytest.fail("unexpected live scan"),
    )
    monkeypatch.setattr(
        sys, "argv", ["pydvb", "scan", "--load-services", str(services_path)]
    )

    assert __main__.main() == 0
    assert device.close_called


def test_known_scan_tables_prints_names_without_opening_device(monkeypatch, capsys):
    calls = []

    monkeypatch.setattr(
        __main__,
        "known_table_names",
        lambda: (calls.append(True) or ["au-Adelaide", "au-Brisbane"]),
    )
    monkeypatch.setattr(
        __main__,
        "DVBDeviceManager",
        lambda *_args: pytest.fail("known scan tables should not open a device"),
    )
    monkeypatch.setattr(sys, "argv", ["pydvb", "known-tables"])

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
        __main__,
        "DVBDeviceManager",
        lambda *_args: pytest.fail("parse scan should not open a device"),
    )
    monkeypatch.setattr(sys, "argv", ["pydvb", "parse-table", "au-Brisbane"])

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
    install_fake_manager(monkeypatch, device)
    monkeypatch.setattr(sys, "argv", ["pydvb", "discover", *arguments])

    assert __main__.main() == 0
    expected_level = logging.DEBUG if expected_log else logging.NOTSET
    assert logging.getLogger("pydvb.usb").level == expected_level
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
    [
        ("177.5/6", (177_500_000, 6_000_000)),
        ("650/7", (650_000_000, 7_000_000)),
        ("226.5/8", (226_500_000, 8_000_000)),
    ],
)
def test_parse_tuning_accepts_frequency_and_bandwidth_mhz(value, expected):
    assert __main__.parse_tuning(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        "177.5",
        "177.5/7/8",
        "/7",
        "0/7",
        "nan/7",
        "1e308/7",
        "not-a-frequency/7",
        "177.5/5",
        "177.5/7.5",
    ],
)
def test_parse_tuning_rejects_invalid_values(value):
    with pytest.raises(__main__.argparse.ArgumentTypeError):
        __main__.parse_tuning(value)


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


def test_capture_stream_stops_at_duration_and_sleeps_after_stopping(monkeypatch, tmp_path):
    class FakeClock:
        current = 0.0

        def monotonic(self):
            return self.current

    clock = FakeClock()

    class TimedAdapter(FakeStreamAdapter):
        def __init__(self):
            super().__init__([b"abcdefghij"])
            self.timeouts = []

        def read_stream(self, timeout_ms=2000):
            self.events.append("read")
            self.timeouts.append(timeout_ms)
            if len(self.timeouts) == 1:
                clock.current += 0.4
                return next(self.chunks)
            clock.current += timeout_ms / 1000
            raise TimeoutError

    monkeypatch.setattr(__main__, "monotonic", clock.monotonic)
    adapter = TimedAdapter()
    output_path = tmp_path / "capture.ts"

    bytes_written, elapsed = __main__.capture_stream(
        adapter, output_path, duration=0.75, read_timeout=5.0
    )

    assert bytes_written == 10
    assert output_path.read_bytes() == b"abcdefghij"
    assert elapsed == pytest.approx(0.75)
    assert adapter.timeouts == [750, 350]
    assert adapter.events[0] == "start"
    assert adapter.events[-2:] == ["stop", "sleep"]
    assert adapter.events.count("read") == 2


def test_capture_stream_sleeps_after_stopping_when_read_fails(tmp_path):
    adapter = FakeStreamAdapter([b"data"])
    output_path = tmp_path / "capture.ts"

    with pytest.raises(StopIteration):
        __main__.capture_stream(adapter, output_path, duration=15.0)

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
        __main__.capture_stream(adapter, output_path, duration=15.0, read_timeout=0)

    assert adapter.events[0] == "start"
    assert "stop" in adapter.events
    assert adapter.events[-1] == "sleep"
    assert adapter.events.count("read") >= 1


def test_capture_stream_keeps_partial_recording_on_keyboard_interrupt(tmp_path, caplog):
    caplog.set_level(logging.WARNING, logger="pydvb.__main__")
    class InterruptAdapter(FakeStreamAdapter):
        def read_stream(self, timeout_ms=2000):
            self.events.append("read")
            if self.events.count("read") == 1:
                return next(self.chunks)
            raise KeyboardInterrupt

    adapter = InterruptAdapter([b"partial stream"])
    output_path = tmp_path / "capture.ts"

    bytes_written, elapsed = __main__.capture_stream(adapter, output_path, duration=15.0)

    assert bytes_written == len(b"partial stream")
    assert output_path.read_bytes() == b"partial stream"
    assert elapsed >= 0
    assert adapter.events[-2:] == ["stop", "sleep"]
    assert "Capture interrupted by user" in caplog.text