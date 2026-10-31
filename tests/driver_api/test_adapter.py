import pytest

from pydvb.driver_api.adapter import DVBAdapter


class EmptyThenDataDemuxer:
    def __init__(self, results):
        self.results = iter(results)
        self.calls = []

    def read_stream_once(self, timeout_ms=2000):
        self.calls.append(("read_stream_once", timeout_ms))
        return next(self.results)

    def stop(self):
        pass


def _ts_packet(pid, payload=b"x"):
    header = bytes((0x47, (pid >> 8) & 0x1F, pid & 0xFF, 0x10))
    return header + payload * (188 - len(header))


def test_adapter_retries_empty_demux_reads_until_data_arrives():
    demuxer = EmptyThenDataDemuxer([b"", b"ts"])
    adapter = DVBAdapter(object(), demuxer)

    assert adapter.read_stream(100) == b"ts"
    assert [call[0] for call in demuxer.calls] == [
        "read_stream_once",
        "read_stream_once",
    ]
    assert demuxer.calls[1][1] <= demuxer.calls[0][1]


def test_adapter_read_stream_times_out_after_empty_demux_reads():
    demuxer = EmptyThenDataDemuxer([b""] * 2)
    adapter = DVBAdapter(object(), demuxer)

    with pytest.raises(TimeoutError, match="transport-stream data"):
        adapter.read_stream(0)


def test_adapter_software_pid_filter_replaces_collection_atomically():
    demuxer = EmptyThenDataDemuxer([_ts_packet(100) + _ts_packet(200)])
    adapter = DVBAdapter(object(), demuxer)

    adapter.configure_pid_filter({100})

    assert adapter.read_stream(100) == _ts_packet(100)


def test_adapter_software_pid_filter_can_be_disabled():
    demuxer = EmptyThenDataDemuxer([_ts_packet(100) + _ts_packet(200)])
    adapter = DVBAdapter(object(), demuxer)
    adapter.configure_pid_filter({100})
    adapter.set_pid_filter_enabled(False)

    assert adapter.read_stream(100) == _ts_packet(100) + _ts_packet(200)


def test_adapter_stop_disables_software_pid_filter():
    adapter = DVBAdapter(object(), EmptyThenDataDemuxer([]))
    adapter.configure_pid_filter({100})

    adapter.stop()

    assert adapter._sw_pid_filter_enabled is False


def test_adapter_software_pid_filter_returns_empty_for_no_matches():
    adapter = DVBAdapter(object(), EmptyThenDataDemuxer([]))
    adapter.configure_pid_filter({300})

    assert adapter._filter_pid_stream(_ts_packet(100) + _ts_packet(200)) == b""


def test_adapter_software_pid_filter_ignores_incomplete_tail():
    adapter = DVBAdapter(object(), EmptyThenDataDemuxer([]))
    adapter.configure_pid_filter({100})
    packet = _ts_packet(100)

    assert adapter._filter_pid_stream(packet + b"tail") == packet


def test_adapter_rejects_invalid_pid_filter_values():
    adapter = DVBAdapter(object(), EmptyThenDataDemuxer([]))

    with pytest.raises(ValueError, match="0x1FFF"):
        adapter.configure_pid_filter({0x2000})