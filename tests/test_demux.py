from threading import Event, Thread

import pytest

from mactv.drivers.af9015.bridge import AF9015Bridge
from mactv.drivers.af9015.demux import AF9015Demuxer


class FakeTransport:
    def __init__(self):
        self.writes = []

    def bulk_write(self, endpoint, data, timeout):
        self.writes.append((endpoint, bytes(data)))

    def bulk_read(self, endpoint, length, timeout):
        return b"\x00\x00\x00"

    def close(self):
        pass

    def reconnect(self, timeout=5.0, poll_interval=0.1):
        return self


class ControlRegisterTransport(FakeTransport):
    def bulk_read(self, endpoint, length, timeout):
        if endpoint == 0x81 and length == 3:
            return b"\x00\x00\x80"
        return super().bulk_read(endpoint, length, timeout)


class StreamTransport(FakeTransport):
    def __init__(self, chunks):
        super().__init__()
        self.chunks = iter(chunks)

    def bulk_read(self, endpoint, length, timeout):
        if endpoint == 0x81:
            return bytes(length)
        return next(self.chunks)


class BlockingStreamTransport(FakeTransport):
    def __init__(self):
        super().__init__()
        self.read_started = Event()
        self.release_read = Event()

    def bulk_read(self, endpoint, length, timeout):
        if endpoint == 0x84:
            self.read_started.set()
            self.release_read.wait(timeout=1)
            return b""
        return super().bulk_read(endpoint, length, timeout)


def test_pid_filter_programs_af9013_slot():
    transport = FakeTransport()
    demuxer = AF9015Demuxer(AF9015Bridge(transport), demodulator_address=0x1D)

    demuxer.configure_hw_pid_filter(0x123)

    assert demuxer.hw_filter_pids == frozenset({0x123})
    assert [packet[3:5] + packet[8:] for _, packet in transport.writes] == [
        b"\xD5\x05\x23\x01",
        b"\xD5\x04\x20",
    ]
    assert all(packet[0] == 0x22 for _, packet in transport.writes)
    assert all(packet[2] == 0x3A for _, packet in transport.writes)


def test_pid_filter_reuses_freed_slot_and_controls_filter():
    transport = FakeTransport()
    demuxer = AF9015Demuxer(AF9015Bridge(transport))

    demuxer.set_hw_pid_filter_enabled()
    demuxer.configure_hw_pid_filter(100)
    demuxer.configure_hw_pid_filter(100, enabled=False)
    demuxer.configure_hw_pid_filter(200)

    assert demuxer.hw_pid_filter_enabled
    assert demuxer.hw_filter_pids == frozenset({200})
    control_writes = [
        packet
        for _, packet in transport.writes
        if packet[3:5] == b"\xD5\x03" and packet[7] == 1 and len(packet) > 8
    ]
    assert control_writes[0][8:] == b"\x01"
    assert transport.writes[-1][1][3:5] == b"\xD5\x04"
    assert transport.writes[-1][1][8:] == b"\x20"


def test_pid_filter_control_preserves_unrelated_register_bits():
    transport = ControlRegisterTransport()
    demuxer = AF9015Demuxer(AF9015Bridge(transport))

    demuxer.set_hw_pid_filter_enabled()

    assert transport.writes[-1][1][3:5] == b"\xD5\x03"
    assert transport.writes[-1][1][8:] == b"\x81"


def test_clear_all_pid_filters_disables_configured_slots():
    transport = FakeTransport()
    demuxer = AF9015Demuxer(AF9015Bridge(transport))

    demuxer.configure_hw_pid_filter(100)
    demuxer.configure_hw_pid_filter(200)
    demuxer.set_hw_pid_filter_enabled()
    writes_before_clear = len(transport.writes)

    demuxer.clear_hw_pid_filters()

    assert demuxer.hw_filter_pids == frozenset()
    assert demuxer.hw_pid_filter_enabled
    assert [
        packet[3:5] + packet[8:]
        for _, packet in transport.writes[writes_before_clear:]
    ] == [b"\xD5\x04\x00", b"\xD5\x04\x01"]


def test_clear_all_pid_filters_is_noop_when_empty():
    transport = FakeTransport()
    demuxer = AF9015Demuxer(AF9015Bridge(transport))

    demuxer.clear_hw_pid_filters()

    assert transport.writes == []


def test_pid_filter_rejects_more_than_32_slots():
    demuxer = AF9015Demuxer(AF9015Bridge(FakeTransport()))

    for pid in range(32):
        demuxer.configure_hw_pid_filter(pid)

    with pytest.raises(ValueError, match="32 slots"):
        demuxer.configure_hw_pid_filter(32)


def test_pid_filter_rejects_values_above_hardware_pid_range():
    demuxer = AF9015Demuxer(AF9015Bridge(FakeTransport()))

    with pytest.raises(ValueError, match="0x1FFF"):
        demuxer.configure_hw_pid_filter(0x2001)


def test_full_speed_stream_uses_five_packets():
    transport = FakeTransport()
    demuxer = AF9015Demuxer(AF9015Bridge(transport), full_speed=True)

    assert demuxer.packet_count == 5

    demuxer.start()

    buffer_writes = [packet for _, packet in transport.writes if packet[3:5] == b"\xdd\x0c"]
    assert buffer_writes[-1][8:] == b"\x10"


def test_read_ts_discards_unaligned_preamble_and_returns_complete_packets():
    packet = b"\x47" + b"a" * 187
    preamble = b"\x47" + b"noise" * 60
    transport = StreamTransport([preamble + packet * 4])
    demuxer = AF9015Demuxer(AF9015Bridge(transport))
    demuxer.start()

    assert demuxer.read_stream_once() == packet * 4


def test_read_ts_buffers_partial_packets():
    packet = b"\x47" + b"a" * 187
    transport = StreamTransport([packet * 4, packet[:10], packet[10:]])
    demuxer = AF9015Demuxer(AF9015Bridge(transport))
    demuxer.start()

    assert demuxer.read_stream_once() == packet * 4
    assert demuxer.read_stream_once() == b""
    assert demuxer.read_stream_once() == packet


def test_stop_waits_for_in_flight_read_and_discards_late_data():
    transport = BlockingStreamTransport()
    demuxer = AF9015Demuxer(AF9015Bridge(transport))
    demuxer.start()
    read_error = []

    def read_stream():
        try:
            demuxer.read_stream_once()
        except RuntimeError as error:
            read_error.append(error)

    reader = Thread(target=read_stream)
    reader.start()
    assert transport.read_started.wait(timeout=1)

    stopped = Event()
    stopper = Thread(target=lambda: (demuxer.stop(), stopped.set()))
    stopper.start()
    assert not stopped.wait(timeout=0.05)

    transport.release_read.set()
    reader.join(timeout=1)
    stopper.join(timeout=1)

    assert not reader.is_alive()
    assert not stopper.is_alive()
    assert read_error and str(read_error[0]) == "demuxer is not started"


def test_pid_filter_rejects_linux_whole_stream_sentinel():
    demuxer = AF9015Demuxer(AF9015Bridge(FakeTransport()))

    with pytest.raises(ValueError, match="0x1FFF"):
        demuxer.configure_hw_pid_filter(0x2000)


def test_demuxer_get_config_dict_contains_configuration_and_is_a_snapshot():
    demuxer = AF9015Demuxer(AF9015Bridge(FakeTransport()), full_speed=True)

    config = demuxer.get_config_dict()

    assert config == {
        "adapter_index": 0,
        "dual_mode": False,
        "demodulator_address": 0x1C,
        "endpoint": demuxer.endpoint,
        "buffer_size": 64,
        "packet_count": 5,
        "running": False,
        "configured": False,
        "pid_filter_enabled": False,
        "pids": frozenset(),
        "pid_slots": {},
    }

    config["buffer_size"] = 1
    config["pid_slots"][100] = 0
    assert demuxer.get_config_dict()["buffer_size"] == 64
    assert demuxer.get_config_dict()["pid_slots"] == {}