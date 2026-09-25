from types import SimpleNamespace

import pytest

from mactv import DVBDescriptor, DVBElementaryStream
from mactv.dvb import DVBScanner, DVBService, ScanPolicy
from mactv.driver_api.adapter import DVBAdapter


def mpeg_crc(data):
    crc = 0xFFFFFFFF
    for byte in data:
        crc ^= byte << 24
        for _ in range(8):
            crc = (
                ((crc << 1) ^ 0x04C11DB7) & 0xFFFFFFFF
                if crc & 0x80000000
                else (crc << 1) & 0xFFFFFFFF
            )
    return crc


def section(table_id, body):
    length = len(body) + 4
    data = bytes((table_id, 0xB0 | (length >> 8), length & 0xFF)) + body
    return data + mpeg_crc(data).to_bytes(4, "big")


def descriptor(tag, value):
    return bytes((tag, len(value))) + value


def packet(pid, payload, continuity=0):
    header = bytes((0x47, (0x40 if continuity == 0 else 0) | (pid >> 8), pid & 0xFF, 0x10 | continuity))
    return header + payload + b"\xff" * (188 - 4 - len(payload))


def table_packet(pid, data):
    return packet(pid, b"\x00" + data)


def test_scanner_discovers_service_metadata_from_split_packets():
    pat = section(
        0x00,
        b"\x12\x34\xC1\x00\x00"
        + b"\x00\x01\xE1\x00",
    )
    pmt = section(
        0x02,
        b"\x00\x01\xC1\x00\x00\xE1\x01\xF0\x00"
        + b"\x1B\xE1\x01\xF0\x07"
        + descriptor(0x52, b"video")
        + b"\x03\xE1\x02\xF0\x05"
        + descriptor(0x0A, b"eng")
        + b"\x06\xE1\x03\xF0\x05"
        + descriptor(0x59, b"sub"),
    )
    service_descriptor = descriptor(0x48, b"\x01\x07Network\x09ExampleTV")
    sdt = section(
        0x42,
        b"\x12\x34\xC1\x00\x00\x00\x01\xff"
            + bytes((0x00, 0x01, 0xFF, 0xF0 | (len(service_descriptor) >> 8), len(service_descriptor)))
        + service_descriptor,
    )
    lcn_descriptor = descriptor(0x83, b"\x00\x01\xC0\x05")
    nit = section(
        0x40,
        b"\x00\x01\xC1\x00\x00"
        + bytes((0xF0, len(descriptor(0x40, b"Brisbane"))))
        + descriptor(0x40, b"Brisbane")
        + bytes((0xF0, 6 + len(lcn_descriptor)))
        + b"\x12\x34\x00\x01\xF0"
        + bytes((len(lcn_descriptor),))
        + lcn_descriptor,
    )
    short_event = b"eng" + bytes((5,)) + b"News!" + bytes((4,)) + b"Live"
    eit = section(
        0x4E,
        b"\x00\x01\xC1\x00\x00\x12\x34\x00\x01\x01\x00\x00"
        + b"\x00\x02\xff\xff\xff\xff\xff\x01\x30"
        + bytes((0x40 | (len(descriptor(0x4D, short_event)) >> 8), len(descriptor(0x4D, short_event))))
        + descriptor(0x4D, short_event),
    )
    stream = b"".join(
        (
            table_packet(0x0000, pat),
            table_packet(0x0100, pmt),
            table_packet(0x0011, sdt),
            table_packet(0x0010, nit),
            table_packet(0x0012, eit),
        )
    )

    scanner = DVBScanner()
    scanner.feed(stream[:73])
    scanner.feed(stream[73:])
    result = scanner.result()

    assert result.complete
    assert result.transport_stream_id == 0x1234
    assert result.network_name == "Brisbane"
    assert len(result.services) == 1
    service = result.services[0]
    assert service.name == "ExampleTV"
    assert service.provider_name == "Network"
    assert service.logical_channel_number == 5
    assert service.pmt_pid == 0x100
    assert service.elementary_streams == (
        DVBElementaryStream(
            pid=0x101,
            stream_type=0x1B,
            descriptors=(DVBDescriptor(tag=0x52, data=b"video"),),
        ),
        DVBElementaryStream(
            pid=0x102,
            stream_type=0x03,
            descriptors=(DVBDescriptor(tag=0x0A, data=b"eng"),),
        ),
        DVBElementaryStream(
            pid=0x103,
            stream_type=0x06,
            descriptors=(DVBDescriptor(tag=0x59, data=b"sub"),),
        ),
    )
    assert service.current_event is not None
    assert service.current_event.name == "News!"
    assert service.current_event.text == "Live"


def test_scanner_replaces_elementary_streams_when_pmt_changes():
    pat = section(0x00, b"\x00\x01\xC1\x00\x00\x00\x01\xE1\x00")
    first_pmt = section(
        0x02,
        b"\x00\x01\xC1\x00\x00\xE1\x01\xF0\x00"
        + b"\x1B\xE1\x01\xF0\x00"
        + b"\x03\xE1\x02\xF0\x00",
    )
    second_pmt = section(
        0x02,
        b"\x00\x01\xC1\x00\x00\xE1\x01\xF0\x00"
        + b"\x06\xE1\x03\xF0\x00",
    )

    scanner = DVBScanner()
    scanner.feed(table_packet(0, pat))
    scanner.feed(table_packet(0x100, first_pmt))
    scanner.feed(table_packet(0x100, second_pmt))

    assert scanner.result().services[0].elementary_streams == (
        DVBElementaryStream(pid=0x103, stream_type=0x06),
    )


def test_scanner_reports_invalid_crc_and_can_skip_crc_validation():
    data = bytearray(section(0x00, b"\x00\x01\xC1\x00\x00"))
    data[-1] ^= 1

    scanner = DVBScanner()
    scanner.feed(table_packet(0, bytes(data)))
    assert scanner.result().warnings == ("invalid CRC for table 0x00",)

    scanner = DVBScanner(verify_crc=False)
    scanner.feed(table_packet(0, bytes(data)))
    assert scanner.result().transport_stream_id == 1


def test_scanner_ignores_transport_error_indicator_but_accepts_payload_start():
    pat = section(0x00, b"\x00\x01\xC1\x00\x00")
    packet_data = bytearray(table_packet(0, pat))
    packet_data[1] |= 0x80

    scanner = DVBScanner()
    scanner.feed(bytes(packet_data))
    assert scanner.result().transport_stream_id is None

    scanner.feed(table_packet(0, pat))
    assert scanner.result().transport_stream_id == 1


def test_scanner_skips_irrelevant_packet_payloads():
    scanner = DVBScanner()
    scanner.feed(packet(0x0100, b"not a section"))

    assert scanner.result().packets_seen == 1
    assert scanner._section_buffers == {}


def test_scanner_rejects_an_unstarted_adapter():
    adapter = DVBAdapter(SimpleNamespace(), SimpleNamespace(running=False))

    with pytest.raises(RuntimeError, match="start the adapter"):
        DVBScanner().scan(adapter)


def test_scanner_completion_policy_can_require_decoded_pmts():
    scanner = DVBScanner()
    scanner._pmt_pids[1] = 100
    scanner._services[1] = DVBService(service_id=1, pmt_pid=100)
    scanner._seen_tables.add(0x42)

    assert scanner.complete(ScanPolicy.MIN_METADATA)
    assert not scanner.complete(ScanPolicy.SERVICE_PIDS)

    scanner._decoded_pmt_pids[1] = 100
    assert scanner.complete(ScanPolicy.SERVICE_PIDS)


def test_scanner_reads_a_started_adapter_until_complete():
    adapter = DVBAdapter(SimpleNamespace(), SimpleNamespace(running=True))
    chunks = iter((b"first",))
    adapter.read_stream = lambda _timeout_ms=2000: next(chunks)
    scanner = DVBScanner()

    def feed(_chunk):
        scanner._pmt_pids[1] = 2
        scanner._seen_tables.add(0x42)

    scanner.feed = feed

    result = scanner.scan(adapter)

    assert result.complete
