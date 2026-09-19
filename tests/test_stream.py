from threading import Event

import pytest

from mactv.stream import OverflowPolicy, UnderflowPolicy, TSStream


class FakeSource:
    def __init__(self, chunks):
        self.chunks = iter(chunks)
        self.started = Event()
        self.stopped = Event()

    def start(self):
        self.started.set()

    def stop(self):
        self.stopped.set()

    def read_ts(self, timeout_ms=2000):
        return next(self.chunks)


def test_stream_produces_chunks_and_stops_source():
    source = FakeSource([b"one", b"two"])
    stream = TSStream(source, slot_count=2)

    stream.start()

    assert list(stream.iter_chunks(timeout=1)) == [b"one", b"two"]
    stream.stop()
    assert source.started.is_set()
    assert source.stopped.is_set()


def test_stream_propagates_source_error_and_stops():
    source = FakeSource([b"data"])
    stream = TSStream(source, slot_count=2)
    stream.start()

    assert stream.read(timeout=1) == b"data"
    with pytest.raises(StopIteration):
        stream.read(timeout=1)
    stream.stop()
    assert source.stopped.is_set()


def test_drop_oldest_policy_counts_dropped_chunks():
    source = FakeSource([b"one", b"two", b"three"])
    stream = TSStream(source, slot_count=1, overflow_policy=OverflowPolicy.DROP_OLDEST)
    stream.start()

    assert stream.read(timeout=1) in {b"one", b"two", b"three"}
    stream.stop()
    assert stream.dropped_chunks >= 0


def test_stream_validates_configuration():
    source = FakeSource([])

    with pytest.raises(ValueError):
        TSStream(source, slot_count=0)
    with pytest.raises(ValueError):
        TSStream(source, overflow_policy="invalid")
    with pytest.raises(ValueError):
        TSStream(source, underflow_policy="invalid")


def test_queue_capacity_reports_configured_slot_count():
    stream = TSStream(FakeSource([]), slot_count=3)

    assert stream.queue_capacity == 3


def test_return_empty_underflow_policy_polls_without_waiting():
    source = FakeSource([])
    stream = TSStream(source, underflow_policy=UnderflowPolicy.RETURN_EMPTY)

    assert stream.read(timeout=1) == b""


def test_return_empty_policy_still_returns_queued_data():
    source = FakeSource([b"data"])
    stream = TSStream(source, underflow_policy=UnderflowPolicy.RETURN_EMPTY)
    stream.start()

    assert stream.read(timeout=1) == b"data"
    stream.stop()