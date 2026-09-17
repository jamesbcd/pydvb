import pytest

from mactv.bridge import AF9015Bridge
from mactv.demux import TSDemuxer


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


def test_pid_filter_programs_af9013_slot():
    transport = FakeTransport()
    demuxer = TSDemuxer(AF9015Bridge(transport), demodulator_address=0x1D)

    demuxer.set_pid_filter(0x123)

    assert demuxer.pids == frozenset({0x123})
    assert [packet[3:5] + packet[8:] for _, packet in transport.writes] == [
        b"\xD5\x05\x23\x01",
        b"\xD5\x04\x20",
    ]
    assert all(packet[0] == 0x22 for _, packet in transport.writes)
    assert all(packet[2] == 0x3A for _, packet in transport.writes)


def test_pid_filter_reuses_freed_slot_and_controls_filter():
    transport = FakeTransport()
    demuxer = TSDemuxer(AF9015Bridge(transport))

    demuxer.enable_pid_filter()
    demuxer.set_pid_filter(100)
    demuxer.set_pid_filter(100, enabled=False)
    demuxer.set_pid_filter(200)

    assert demuxer.pid_filter_enabled
    assert demuxer.pids == frozenset({200})
    assert transport.writes[0][1][3:5] == b"\xD5\x03"
    assert transport.writes[0][1][8:] == b"\x01"
    assert transport.writes[-1][1][3:5] == b"\xD5\x04"
    assert transport.writes[-1][1][8:] == b"\x20"


def test_pid_filter_rejects_more_than_32_slots():
    demuxer = TSDemuxer(AF9015Bridge(FakeTransport()))

    for pid in range(32):
        demuxer.set_pid_filter(pid)

    with pytest.raises(ValueError, match="32 slots"):
        demuxer.set_pid_filter(32)


def test_full_speed_stream_uses_five_packets():
    transport = FakeTransport()
    demuxer = TSDemuxer(AF9015Bridge(transport), full_speed=True)

    assert demuxer.packet_count == 5

    demuxer.start()

    buffer_writes = [packet for _, packet in transport.writes if packet[3:5] == b"\xdd\x0c"]
    assert buffer_writes[-1][8:] == b"\x10"


def test_virtual_whole_stream_pid_is_accepted_without_a_slot():
    transport = FakeTransport()
    demuxer = TSDemuxer(AF9015Bridge(transport))

    demuxer.set_pid_filter(0x2000)

    assert demuxer.pids == frozenset()
    assert transport.writes == []


def test_demuxer_get_config_dict_contains_configuration_and_is_a_snapshot():
    demuxer = TSDemuxer(AF9015Bridge(FakeTransport()), full_speed=True)

    config = demuxer.get_config_dict()

    assert config == {
        "adapter": 0,
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