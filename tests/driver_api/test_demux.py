from threading import Event, Thread

import pytest

from pydvb.driver_api.demux import DVBDemuxer


class FakeDemuxer(DVBDemuxer):
    def __init__(self, chunks=(), block_read=False):
        super().__init__(endpoint=0x84, packet_count=4)
        self.chunks = iter(chunks)
        self.block_read = block_read
        self.read_started = Event()
        self.release_read = Event()

    def _configure_stream(self):
        pass

    def _start_stream(self):
        pass

    def _stop_stream(self):
        pass

    def _read_stream_raw(self, length, timeout_ms):
        if self.block_read:
            self.read_started.set()
            self.release_read.wait(timeout=1)
            return b""
        return next(self.chunks)


def test_read_stream_discards_unaligned_preamble_and_returns_complete_packets():
    packet = b"\x47" + b"a" * 187
    preamble = b"\x47" + b"noise" * 60
    demuxer = FakeDemuxer([preamble + packet * 4])
    demuxer.start()

    assert demuxer.read_stream_once() == packet * 4


def test_read_stream_buffers_partial_packets():
    packet = b"\x47" + b"a" * 187
    demuxer = FakeDemuxer([packet * 4, packet[:10], packet[10:]])
    demuxer.start()

    assert demuxer.read_stream_once() == packet * 4
    assert demuxer.read_stream_once() == b""
    assert demuxer.read_stream_once() == packet


def test_stop_waits_for_in_flight_read_and_discards_late_data():
    demuxer = FakeDemuxer(block_read=True)
    demuxer.start()
    read_errors = []

    def read_stream():
        try:
            demuxer.read_stream_once()
        except RuntimeError as error:
            read_errors.append(error)

    reader = Thread(target=read_stream)
    reader.start()
    assert demuxer.read_started.wait(timeout=1)

    stopped = Event()
    stopper = Thread(target=lambda: (demuxer.stop(), stopped.set()))
    stopper.start()
    assert not stopped.wait(timeout=0.05)

    demuxer.release_read.set()
    reader.join(timeout=1)
    stopper.join(timeout=1)

    assert not reader.is_alive()
    assert not stopper.is_alive()
    assert read_errors and str(read_errors[0]) == "demuxer is not started"