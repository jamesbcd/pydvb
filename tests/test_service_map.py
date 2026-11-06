from types import SimpleNamespace

import pytest

from pydvb import service_map
from pydvb.driver_api.tuner import DVBTuning
from pydvb.stream_scanner import DVBStreamInfo


class FakeAdapter:
    def __init__(self, *, locked=True):
        self.events = []
        self.locked = locked

    def tune(self, _tuning, wait_for_lock=True):
        self.events.append(("tune", wait_for_lock))
        return SimpleNamespace(lock=self.locked)

    def start(self):
        self.events.append(("start",))

    def stop(self):
        self.events.append(("stop",))

    def sleep(self):
        self.events.append(("sleep",))


def test_map_from_tunings_sleeps_after_success(monkeypatch):
    adapter = FakeAdapter()

    class FakeScanner:
        def scan(self, _adapter, policy):
            assert policy is service_map.ScanPolicy.ALL_SERVICE_METADATA
            return DVBStreamInfo()

    monkeypatch.setattr(service_map, "DVBScanner", FakeScanner)

    result = service_map.map_from_tunings([DVBTuning(177_500_000)], adapter)

    assert isinstance(result, service_map.DVBServiceMap)
    assert adapter.events == [
        ("tune", True),
        ("start",),
        ("stop",),
        ("sleep",),
    ]


def test_map_from_tunings_reports_cumulative_service_count(monkeypatch):
    adapter = FakeAdapter()
    counts = []

    class FakeScanner:
        scans = 0

        def scan(self, _adapter, policy, *, on_service_count=None):
            assert policy is service_map.ScanPolicy.ALL_SERVICE_METADATA
            self.scans += 1
            assert on_service_count is not None
            on_service_count(1)
            return DVBStreamInfo(
                services=(service_map.DVBService(service_id=self.scans),)
            )

    monkeypatch.setattr(service_map, "DVBScanner", FakeScanner)

    service_map.map_from_tunings(
        [DVBTuning(177_500_000), DVBTuning(191_500_000)],
        adapter,
        on_service_count=counts.append,
    )

    assert counts == [1, 2]


def test_map_from_scan_forwards_service_count_callback(monkeypatch):
    tuning = DVBTuning(177_500_000)
    callback = lambda _count: None
    forwarded_callbacks = []
    monkeypatch.setattr(
        service_map,
        "parse_table",
        lambda *_args, **_kwargs: SimpleNamespace(
            tunings=[tuning],
            skipped_blocks=0,
            failed_blocks=0,
        ),
    )

    def fake_map_from_tunings(tunings, _adapter, *, on_service_count=None):
        assert tunings == [tuning]
        forwarded_callbacks.append(on_service_count)
        return service_map.DVBServiceMap()

    monkeypatch.setattr(service_map, "map_from_tunings", fake_map_from_tunings)

    result = service_map.map_from_scan(
        "scan-table",
        FakeAdapter(),
        on_service_count=callback,
    )

    assert isinstance(result, service_map.DVBServiceMap)
    assert forwarded_callbacks == [callback]


def test_map_from_tunings_reports_scan_error_and_sleeps(monkeypatch):
    adapter = FakeAdapter()
    scan_error = TimeoutError("no stream data")
    diagnostics = []

    class FakeScanner:
        def scan(self, _adapter, policy):
            raise scan_error

    monkeypatch.setattr(service_map, "DVBScanner", FakeScanner)

    with pytest.raises(TimeoutError, match="no stream data") as raised:
        service_map.map_from_tunings(
            [DVBTuning(177_500_000)],
            adapter,
            on_scan_error=diagnostics.append,
        )

    assert raised.value is scan_error
    assert diagnostics == [scan_error]
    assert adapter.events == [
        ("tune", True),
        ("start",),
        ("stop",),
        ("sleep",),
    ]


def test_map_from_tunings_keeps_scan_error_if_sleep_fails(monkeypatch):
    adapter = FakeAdapter()
    scan_error = TimeoutError("no stream data")
    sleep_error = RuntimeError("could not sleep tuner")

    def fail_sleep():
        raise sleep_error

    adapter.sleep = fail_sleep

    class FakeScanner:
        def scan(self, _adapter, policy):
            raise scan_error

    monkeypatch.setattr(service_map, "DVBScanner", FakeScanner)

    with pytest.raises(TimeoutError, match="no stream data") as raised:
        service_map.map_from_tunings([DVBTuning(177_500_000)], adapter)

    assert raised.value is scan_error
    assert raised.value.__cause__ is sleep_error
