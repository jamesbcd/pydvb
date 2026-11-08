from types import SimpleNamespace

import pytest

from pydvb import DVBDescriptor, DVBElementaryStream
from pydvb.stream_scanner import (
    DVBScanner,
    DVBEventScanner,
    DVBDoubletID,
    DVBService,
    DVBServiceMetatype,
    DVBServiceType,
    DVBStreamInfo,
    DVBTripletID,
    ScanPolicy,
)
from pydvb.driver_api.adapter import DVBAdapter
import pydvb.stream_scanner as scanner_module


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


def extended_event_descriptor(number, last_number, text, language=b"eng", items=b""):
    value = (
        bytes(((number << 4) | last_number,))
        + language
        + bytes((len(items),))
        + items
        + bytes((len(text),))
        + text
    )
    return descriptor(0x4E, value)


@pytest.mark.parametrize(
    ("raw", "expected"),
    (
        (b"Caf\xc2e", "Café"),
        (b"\x01\xb0", "А"),
        (b"\x10\x00\x01Caf\xe9", "Café"),
        (b"\x11\x00\xe9", "é"),
        (b"\x15Caf\xc3\xa9", "Café"),
    ),
)
def test_dvb_text_uses_default_and_selected_encodings(raw, expected):
    assert scanner_module._decode_text(raw) == expected


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
        b"\x32\x29\xC1\x00\x00"
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
    assert result.original_network_id == 1
    assert result.network_name == "Brisbane"
    assert len(result.services) == 1
    service = result.services[0]
    assert service.triplet_id == DVBTripletID(1, 0x1234, 1)
    assert service.name == "ExampleTV"
    assert service.service_type is DVBServiceType.DIGITAL_TELEVISION
    assert service.service_metatype is DVBServiceMetatype.TV
    assert service.provider_name == "Network"
    assert service.logical_channel_number == 5
    assert service.pmt_pid == 0x100
    assert service.pids() == frozenset(
        (0x0000, 0x0001, 0x0010, 0x0011, 0x0012, 0x0014, 0x100, 0x101, 0x102, 0x103)
    )
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


def test_scanner_prefers_running_event_over_other_eit_statuses():
    scanner = DVBScanner()

    def eit_event(event_id, running_status, name):
        short_event = b"eng" + bytes((len(name),)) + name + b"\x00"
        event_descriptor = descriptor(0x4D, short_event)
        body = (
            b"\x00\x01\xC1\x00\x00\x12\x34\x00\x01\x01\x00"
            + event_id.to_bytes(2, "big")
            + b"\xff" * 5
            + b"\x01\x00\x00"
            + bytes((running_status << 5, len(event_descriptor)))
            + event_descriptor
        )
        scanner._decode_eit(section(0x4E, body))

    eit_event(1, 2, b"Starting soon")
    eit_event(2, 4, b"Running now")
    eit_event(3, 3, b"Pausing")

    service = scanner.result().services[0]
    assert service.current_event is not None
    assert service.current_event.event_id == 2
    assert service.current_event.name == "Running now"
    assert service.current_event.running_status == 4


def test_scanner_uses_extended_event_text_over_short_event_text():
    short_event = b"eng" + b"\x04Name" + b"\x05Short"
    descriptors = (
        descriptor(0x4D, short_event)
        + extended_event_descriptor(1, 1, b"description")
        + extended_event_descriptor(0, 1, b"Long ")
    )
    event = (
        b"\x00\x01"
        + b"\xff" * 5
        + b"\x01\x00\x00"
        + bytes((0x80 | (len(descriptors) >> 8), len(descriptors)))
        + descriptors
    )
    body = b"\x00\x01\xc1\x00\x00\x12\x34\x00\x01\x01\x00" + event

    scanner = DVBScanner()
    scanner._decode_eit(section(0x4E, body))

    result = scanner.result().services[0].current_event
    assert result is not None
    assert result.name == "Name"
    assert result.text == "Long description"


def test_scanner_uses_extended_event_text_without_short_event_descriptor():
    items = b"\x04Type\x05Drama"
    descriptors = extended_event_descriptor(0, 0, b"Long description", items=items)
    event = (
        b"\x00\x01"
        + b"\xff" * 5
        + b"\x01\x00\x00"
        + bytes((0x80 | (len(descriptors) >> 8), len(descriptors)))
        + descriptors
    )
    body = b"\x00\x01\xc1\x00\x00\x12\x34\x00\x01\x01\x00" + event

    scanner = DVBScanner()
    scanner._decode_eit(section(0x4E, body))

    result = scanner.result().services[0].current_event
    assert result is not None
    assert result.name is None
    assert result.text == "Long description"


def test_scanner_discovers_next_event_from_present_following_eit():
    scanner = DVBScanner()

    def eit_event(event_id, running_status, name):
        short_event = b"eng" + bytes((len(name),)) + name + b"\x00"
        event_descriptor = descriptor(0x4D, short_event)
        return (
            event_id.to_bytes(2, "big")
            + b"\xff" * 5
            + b"\x01\x00\x00"
            + bytes((running_status << 5, len(event_descriptor)))
            + event_descriptor
        )

    body = (
        b"\x00\x01\xC1\x00\x00\x12\x34\x00\x01\x01\x00"
        + eit_event(1, 4, b"Current")
        + eit_event(2, 1, b"Next")
    )
    scanner._decode_eit(section(0x4E, body))
    other_transport_body = (
        b"\x00\x01\xC1\x00\x00\x56\x78\x00\x02\x01\x00"
        + eit_event(3, 4, b"Other current")
        + eit_event(4, 1, b"Other next")
    )
    scanner._decode_section(0x12, section(0x4F, other_transport_body))

    service = scanner.result().services[0]
    assert service.current_event is not None
    assert service.current_event.name == "Current"
    assert service.next_event is not None
    assert service.next_event.event_id == 2
    assert service.next_event.name == "Next"


def test_scanner_discovers_next_event_from_separate_following_eit_section():
    scanner = DVBScanner()

    def eit_section(section_number, event_id, running_status, name):
        short_event = b"eng" + bytes((len(name),)) + name + b"\x00"
        event_descriptor = descriptor(0x4D, short_event)
        event = (
            event_id.to_bytes(2, "big")
            + b"\xff" * 5
            + b"\x01\x00\x00"
            + bytes((running_status << 5, len(event_descriptor)))
            + event_descriptor
        )
        body = (
            b"\x00\x01\xc1"
            + bytes((section_number, 1))
            + b"\x12\x34\x00\x01\x01\x00"
            + event
        )
        return section(0x4E, body)

    scanner._decode_eit(eit_section(0, 1, 4, b"Current"))
    scanner._decode_eit(eit_section(1, 2, 1, b"Next"))
    scanner._decode_eit(eit_section(0, 1, 4, b"Current"))

    service = scanner.result().services[0]
    assert service.current_event is not None
    assert service.current_event.name == "Current"
    assert service.next_event is not None
    assert service.next_event.event_id == 2
    assert service.next_event.name == "Next"


def test_event_scanner_only_decodes_selected_present_following_services():
    def eit_packet(service_id, event_id, name, original_network_id=1):
        short_event = b"eng" + bytes((len(name),)) + name + b"\x00"
        event_descriptor = descriptor(0x4D, short_event)
        event = (
            event_id.to_bytes(2, "big")
            + b"\xff" * 5
            + b"\x01\x00\x00"
            + b"\x80"
            + bytes((len(event_descriptor),))
            + event_descriptor
        )
        body = (
            service_id.to_bytes(2, "big")
            + b"\xc1\x00\x00\x12\x34"
            + original_network_id.to_bytes(2, "big")
            + b"\x00\x4e"
            + event
        )
        return table_packet(0x12, section(0x4E, body))

    triplet_id = DVBTripletID(1, 0x1234, 1)
    service = DVBService(service_id=1, triplet_id=triplet_id)
    mismatched_onid_id = DVBTripletID(12841, 0x1234, 3)
    mismatched_onid_service = DVBService(
        service_id=3,
        triplet_id=mismatched_onid_id,
    )
    scanner = DVBEventScanner(
        DVBStreamInfo(services=(service, mismatched_onid_service))
    )
    scanner.feed(table_packet(0, section(0, b"\x12\x34\xc1\x00\x00\x00\x02\xe1\x00")))
    scanner.feed(eit_packet(2, 20, b"Ignored"))
    scanner.feed(eit_packet(1, 10, b"Selected"))
    scanner.feed(eit_packet(3, 30, b"ONID fallback", original_network_id=4116))

    assert service.current_event is not None
    assert service.current_event.triplet_id == triplet_id
    assert service.current_event.name == "Selected"
    assert service.next_event is None
    assert mismatched_onid_service.current_event is not None
    assert mismatched_onid_service.current_event.name == "ONID fallback"
    assert scanner.eit_packets_seen == 3
    assert scanner.eit_sections_seen == 3
    assert scanner.matched_eit_services == {
        triplet_id,
        DVBTripletID(4116, 0x1234, 3),
    }
    assert scanner.unmatched_eit_services == {DVBTripletID(1, 0x1234, 2)}
    assert scanner.onid_mismatch_eit_services == {DVBTripletID(4116, 0x1234, 3)}
    assert scanner.event_updates == 2


def test_scanner_scan_reports_service_count_increases():
    first_pat = section(
        0x00,
        b"\x12\x34\xC1\x00\x00"
        + b"\x00\x01\xE1\x00",
    )
    second_pat = section(
        0x00,
        b"\x12\x34\xC1\x00\x00"
        + b"\x00\x01\xE1\x00"
        + b"\x00\x02\xE1\x01",
    )
    counts = []

    result = DVBScanner().scan(
        (
            table_packet(0x0000, first_pat),
            table_packet(0x0000, first_pat),
            table_packet(0x0000, second_pat),
        ),
        on_service_count=counts.append,
    )

    assert counts == [1, 2]
    assert len(result.services) == 2


def test_result_sets_triplet_ids_for_each_service():
    pat = section(
        0x00,
        b"\x12\x34\xC1\x00\x00"
        + b"\x00\x01\xE1\x00"
        + b"\x00\x02\xE1\x01",
    )
    sdt = section(
        0x42,
        b"\x12\x34\xC1\x00\x00\x00\x01\xff"
        + b"\x00\x01\xff\xf0\x00"
        + b"\x00\x02\xff\xf0\x00",
    )
    scanner = DVBScanner()
    scanner.feed(table_packet(0x0000, pat))
    scanner.feed(table_packet(0x0011, sdt))

    result = scanner.result()

    assert [service.triplet_id for service in result.services] == [
        DVBTripletID(1, 0x1234, 1),
        DVBTripletID(1, 0x1234, 2),
    ]


def test_stream_info_doublet_id_falls_back_to_first_identified_service():
    service = DVBService(service_id=3, triplet_id=DVBTripletID(1, 2, 3))

    assert DVBStreamInfo(services=(service,)).doublet_id == DVBDoubletID(1, 2)
    assert DVBStreamInfo(
        original_network_id=4,
        transport_stream_id=5,
        services=(service,),
    ).doublet_id == DVBDoubletID(4, 5)
    assert DVBStreamInfo().doublet_id is None


def test_dvb_service_type_uses_int_compatible_standard_codes():
    assert DVBServiceType.DIGITAL_TELEVISION == 0x01
    assert DVBServiceType.DIGITAL_RADIO_SOUND == 0x02
    assert DVBServiceType.DVB_MULTIMEDIA_HOME_PLATFORM == 0x10
    assert DVBServiceType(0x01) is DVBServiceType.DIGITAL_TELEVISION


def test_every_dvb_service_type_has_a_metatype():
    assert {
        service_type.metatype
        for service_type in DVBServiceType
    } == {
        DVBServiceMetatype.TV,
        DVBServiceMetatype.RADIO,
        DVBServiceMetatype.OTHER,
    }
    assert DVBServiceType.DIGITAL_RADIO_SOUND.metatype is DVBServiceMetatype.RADIO
    assert DVBServiceType.DATA_BROADCAST.metatype is DVBServiceMetatype.OTHER


def test_scanner_uses_bat_lcn_when_nit_does_not_advertise_one():
    pat = section(0x00, b"\x12\x34\xC1\x00\x00\x00\x01\xE1\x00")
    lcn_descriptor = descriptor(0x83, b"\x00\x01\xC0\x07")
    bat = section(
        0x4A,
        b"\x00\x01\xC1\x00\x00"
        + b"\xF0\x00"
        + bytes((0xF0, 6 + len(lcn_descriptor)))
        + b"\x12\x34\x00\x01"
        + bytes((0xF0 | (len(lcn_descriptor) >> 8), len(lcn_descriptor)))
        + lcn_descriptor,
    )

    scanner = DVBScanner()
    scanner.feed(table_packet(0x0000, pat))
    scanner.feed(table_packet(0x0011, bat))

    assert scanner.result().services[0].logical_channel_number == 7


def test_service_pids_omit_missing_optional_pids():
    service = DVBService(
        service_id=1,
        elementary_streams=(DVBElementaryStream(pid=0x101, stream_type=0x1B),),
    )

    assert service.pids() == frozenset((0x0000, 0x0001, 0x0010, 0x0011, 0x0012, 0x0014, 0x101))


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


def test_scanner_completion_policy_can_require_all_service_metadata():
    scanner = DVBScanner()
    scanner._pmt_pids[1] = 100
    scanner._services[1] = DVBService(service_id=1, pmt_pid=100)
    scanner._seen_tables.add(0x42)

    assert scanner.complete(ScanPolicy.MIN_METADATA)
    assert not scanner.complete(ScanPolicy.ALL_SERVICE_METADATA)

    scanner._decoded_pmt_pids[1] = 100
    assert not scanner.complete(ScanPolicy.ALL_SERVICE_METADATA)

    scanner._services[1].logical_channel_number = 7
    assert scanner.complete(ScanPolicy.ALL_SERVICE_METADATA)


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
