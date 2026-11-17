import http.client
import logging
from http.server import BaseHTTPRequestHandler
from queue import Queue
from threading import Event, Thread
from time import monotonic, sleep

from pydvb.server import (
    LocalTS_HTTPServer,
    _MultiplexSession,
    _StreamRequestHandler,
    _StreamSubscriber,
)
from pydvb.service_map import DVBServiceMap
from pydvb.stream_scanner import DVBElementaryStream, DVBService, DVBServiceType, DVBStreamInfo, DVBTripletID
from pydvb.driver_api.tuner import DVBTuning


class FakeScanner:
    def scan(self, adapter, policy):
        return type("ScanResult", (), {"services": ()})()


class FakeAdapter:
    def __init__(self):
        self.chunks = Queue()
        self.stopped = Event()
        self.calls = []

    def start(self):
        self.stopped.clear()
        self.calls.append(("start",))

    def tune(self, tuning, wait_for_lock=True):
        self.calls.append(("tune", tuning, wait_for_lock))
        return type("Status", (), {"lock": True})()

    def configure_pid_filter(self, pids):
        self.calls.append(("configure_pid_filter", frozenset(pids)))

    def set_pid_filter_enabled(self, enabled=True):
        self.calls.append(("set_pid_filter_enabled", enabled))

    def read_stream(self, timeout_ms=2000):
        if self.stopped.is_set():
            raise RuntimeError("demuxer is not started")
        try:
            return self.chunks.get(timeout=0.1)
        except Exception as error:
            raise TimeoutError from error

    def stop(self):
        self.calls.append(("stop",))
        self.stopped.set()

    def sleep(self):
        self.calls.append(("sleep",))


class ShutdownRaceAdapter(FakeAdapter):
    def read_stream(self, timeout_ms=2000):
        self.stopped.wait(timeout=1)
        raise RuntimeError("demuxer is not started")


TRIPLET = DVBTripletID(1, 2, 3)
TRIPLET_2 = DVBTripletID(4, 5, 6)
TRIPLET_3 = DVBTripletID(1, 2, 6)
TUNING = DVBTuning(650_000_000)
OTHER_TUNING = DVBTuning(666_000_000)


def make_service_map():
    service = DVBService(
        service_id=3,
        triplet_id=TRIPLET,
        name="Test TV",
        service_type=DVBServiceType.DIGITAL_TELEVISION,
        pmt_pid=100,
        elementary_streams=(DVBElementaryStream(200, 0x1B),),
    )
    service_map = DVBServiceMap()
    service_map.add_stream_info(
        TUNING,
        DVBStreamInfo(
            original_network_id=TRIPLET.original_network_id,
            transport_stream_id=TRIPLET.transport_stream_id,
            services=(service,),
        ),
    )
    return service_map


def make_service_map_with_another_multiplex():
    service_map = make_service_map()
    service = DVBService(
        service_id=6,
        triplet_id=TRIPLET_2,
        name="Other TV",
        service_type=DVBServiceType.DIGITAL_TELEVISION,
        pmt_pid=101,
        elementary_streams=(DVBElementaryStream(201, 0x1B),),
    )
    service_map.add_stream_info(
        OTHER_TUNING,
        DVBStreamInfo(
            original_network_id=TRIPLET_2.original_network_id,
            transport_stream_id=TRIPLET_2.transport_stream_id,
            services=(service,),
        ),
    )
    return service_map


def make_service_map_with_another_service_on_same_multiplex():
    service_map = make_service_map()
    service = DVBService(
        service_id=6,
        triplet_id=TRIPLET_3,
        name="Second Test TV",
        service_type=DVBServiceType.DIGITAL_TELEVISION,
        pmt_pid=101,
        elementary_streams=(DVBElementaryStream(201, 0x1B),),
    )
    service_map.add_stream_info(
        TUNING,
        DVBStreamInfo(
            original_network_id=TRIPLET_3.original_network_id,
            transport_stream_id=TRIPLET_3.transport_stream_id,
            services=(service,),
        ),
    )
    return service_map


def make_ts_packet(pid, payload_byte=0):
    return bytes((0x47, (pid >> 8) & 0x1F, pid & 0xFF)) + bytes((payload_byte,)) * 185


def make_eit_packet(service_id, name):
    short_event = b"eng" + bytes((len(name),)) + name + b"\x00"
    event_descriptor = bytes((0x4D, len(short_event))) + short_event
    event = (
        b"\x00\x01"
        + b"\xff" * 5
        + b"\x01\x00\x00"
        + b"\x80"
        + bytes((len(event_descriptor),))
        + event_descriptor
    )
    body = (
        service_id.to_bytes(2, "big")
        + b"\xc1\x00\x00\x00\x02\x00\x01\x00\x4e"
        + event
    )
    length = len(body) + 4
    data = bytes((0x4E, 0xB0 | (length >> 8), length & 0xFF)) + body
    crc = 0xFFFFFFFF
    for byte in data:
        crc ^= byte << 24
        for _ in range(8):
            crc = (
                ((crc << 1) ^ 0x04C11DB7) & 0xFFFFFFFF
                if crc & 0x80000000
                else (crc << 1) & 0xFFFFFFFF
            )
    section = data + crc.to_bytes(4, "big")
    payload = b"\x00" + section
    return (
        bytes((0x47, 0x40, 0x12, 0x10))
        + payload
        + b"\xff" * (184 - len(payload))
    )


def test_server_streams_requested_service_and_releases_adapter(caplog):
    caplog.set_level(logging.DEBUG, logger="pydvb.server")
    adapter = FakeAdapter()
    server = LocalTS_HTTPServer([adapter], make_service_map(), port=0)
    serving = Thread(target=server.serve_forever)
    serving.start()
    server.start()
    connection = http.client.HTTPConnection(*server.address, timeout=2)
    try:
        connection.request("GET", "/service/1.2.3?token=secret")
        response = connection.getresponse()

        clients = server.client_statuses
        assert len(clients) == 1
        assert clients[0].service_name == "Test TV"
        assert clients[0].service_id == "1.2.3"
        assert clients[0].peer.startswith("127.0.0.1:")

        payload = b"\x47\x00\xc8" + b"\x00" * 185
        adapter.chunks.put(payload)

        assert response.status == 200
        assert response.read(188) == payload
        deadline = monotonic() + 2
        while monotonic() < deadline and server.client_statuses[0].bytes_sent != 188:
            sleep(0.01)
        assert server.client_statuses[0].bytes_sent == 188
    finally:
        connection.close()
        server.shutdown()
        server.close()
        serving.join(timeout=2)

    assert adapter.stopped.is_set()
    assert adapter.calls == [
        ("tune", TUNING, True),
        ("set_pid_filter_enabled", False),
        ("start",),
        ("stop",),
        ("sleep",),
    ]
    messages = [record.getMessage() for record in caplog.records]
    deadline = monotonic() + 2
    while monotonic() < deadline and not any(
        "Client disconnected:" in message for message in messages
    ):
        sleep(0.01)
        messages = [record.getMessage() for record in caplog.records]
    assert any(">>> Client connected:" in message for message in messages)
    assert any("<<< Client disconnected:" in message for message in messages)
    assert messages.index(next(message for message in messages if ">>> Client connected:" in message)) < \
        messages.index(next(message for message in messages if "<<< Client disconnected:" in message))
    assert any(
        "Tuning adapter to 650000000 Hz (bandwidth 8000000 Hz)" in message
        for message in messages
    )
    assert any(
        "Started multiplex stream with ID 1.2 for 1 subscriber(s)" in message
        for message in messages
    )
    assert any("path='/service/1.2.3'" in message for message in messages)
    assert "token=secret" not in "\n".join(messages)


def test_server_updates_service_events_while_streaming(caplog):
    caplog.set_level(logging.INFO, logger="pydvb.stream_scanner")
    caplog.set_level(logging.INFO, logger="pydvb.server")
    adapter = FakeAdapter()
    service_map = make_service_map()
    server = LocalTS_HTTPServer(
        [adapter],
        service_map,
        port=0,
        event_scan_pause_seconds=0.05,
    )
    serving = Thread(target=server.serve_forever)
    serving.start()
    connection = http.client.HTTPConnection(*server.address, timeout=2)
    try:
        connection.request("GET", "/service/1.2.3")
        response = connection.getresponse()
        assert response.status == 200

        packet = make_eit_packet(3, b"Current programme")
        adapter.chunks.put(packet)

        assert response.read(188) == packet
        deadline = monotonic() + 2
        service = service_map.services[TRIPLET]
        while monotonic() < deadline and service.current_event is None:
            sleep(0.01)
        assert service.current_event is not None
        assert service.current_event.name == "Current programme"

        deadline = monotonic() + 2
        while monotonic() < deadline and not any(
            "DVB EIT scan duration for multiplex" in record.getMessage()
            for record in caplog.records
        ):
            sleep(0.01)
        assert any(
            "DVB EIT scan duration for multiplex" in record.getMessage()
            for record in caplog.records
        )

        sleep(0.1)
        next_packet = make_eit_packet(3, b"Later programme")
        adapter.chunks.put(next_packet)
        assert response.read(188) == next_packet
        deadline = monotonic() + 2
        while monotonic() < deadline and service.current_event.name != "Later programme":
            sleep(0.01)
        assert service.current_event.name == "Later programme"
        assert any(
            "Updated events for service 1.2.3" in record.getMessage()
            for record in caplog.records
        )
    finally:
        connection.close()
        server.shutdown()
        server.close()
        serving.join(timeout=2)


def test_server_does_not_scan_events_when_disabled():
    adapter = FakeAdapter()
    service_map = make_service_map()
    server = LocalTS_HTTPServer(
        [adapter],
        service_map,
        port=0,
        scan_dvb_events=False,
    )
    serving = Thread(target=server.serve_forever)
    serving.start()
    connection = http.client.HTTPConnection(*server.address, timeout=2)
    try:
        connection.request("GET", "/service/1.2.3")
        response = connection.getresponse()
        assert response.status == 200

        packet = make_eit_packet(3, b"Current programme")
        adapter.chunks.put(packet)
        assert response.read(188) == packet
        sleep(0.2)

        assert service_map.services[TRIPLET].current_event is None
    finally:
        connection.close()
        server.shutdown()
        server.close()
        serving.join(timeout=2)


def test_server_pauses_after_incomplete_event_scan_timeout(caplog):
    caplog.set_level(logging.INFO, logger="pydvb.server")
    adapter = FakeAdapter()
    server = LocalTS_HTTPServer(
        [adapter],
        make_service_map(),
        port=0,
        event_scan_pause_seconds=0.4,
        event_scan_max_duration_seconds=0.2,
    )
    serving = Thread(target=server.serve_forever)
    serving.start()
    connection = http.client.HTTPConnection(*server.address, timeout=2)
    try:
        connection.request("GET", "/service/1.2.3")
        response = connection.getresponse()
        assert response.status == 200

        deadline = monotonic() + 2
        while monotonic() < deadline and not any(
            "DVB EIT scan timed out for multiplex 1.2" in record.getMessage()
            for record in caplog.records
        ):
            sleep(0.01)

        timeout_records = [
            record
            for record in caplog.records
            if "DVB EIT scan timed out for multiplex 1.2" in record.getMessage()
        ]
        assert len(timeout_records) == 1
        assert "0/1 services ready" in timeout_records[0].getMessage()
        assert "pending services: 1.2.3" in timeout_records[0].getMessage()
        sleep(0.1)
        assert len(
            [
                record
                for record in caplog.records
                if "DVB EIT scan timed out for multiplex 1.2" in record.getMessage()
            ]
        ) == 1
    finally:
        connection.close()
        server.shutdown()
        server.close()
        serving.join(timeout=2)


def test_server_suppresses_demuxer_error_when_stopping_stream(caplog):
    caplog.set_level(logging.DEBUG, logger="pydvb.server")
    adapter = ShutdownRaceAdapter()
    server = LocalTS_HTTPServer([adapter], make_service_map(), port=0)
    serving = Thread(target=server.serve_forever)
    serving.start()
    connection = http.client.HTTPConnection(*server.address, timeout=2)
    try:
        connection.request("GET", "/service/1.2.3")
        assert connection.getresponse().status == 200
    finally:
        connection.close()
        server.shutdown()
        server.close()
        serving.join(timeout=2)

    messages = [record.getMessage() for record in caplog.records]
    deadline = monotonic() + 2
    while monotonic() < deadline and not any(
        "Client disconnected:" in message for message in messages
    ):
        sleep(0.01)
        messages = [record.getMessage() for record in caplog.records]
    assert any("Client disconnected:" in message for message in messages)
    assert not any("Streaming request failed:" in message for message in messages)


def test_server_root_returns_sorted_m3u8_playlist():
    first = DVBService(
        service_id=6,
        triplet_id=DVBTripletID(4, 5, 6),
        name="Unnumbered",
    )
    second = DVBService(
        service_id=3,
        triplet_id=DVBTripletID(1, 2, 3),
        name="Second TV",
        logical_channel_number=20,
    )
    third = DVBService(
        service_id=4,
        triplet_id=DVBTripletID(1, 2, 4),
        name="First TV",
        logical_channel_number=2,
    )
    service_map = DVBServiceMap()
    service_map.add_stream_info(
        TUNING,
        DVBStreamInfo(services=(first, second, third)),
    )
    server = LocalTS_HTTPServer([], service_map, port=0)
    serving = Thread(target=server.serve_forever)
    serving.start()
    connection = http.client.HTTPConnection(*server.address, timeout=2)
    base_url = f"http://{server.address[0]}:{server.address[1]}"
    expected_playlist = (
        "#EXTM3U\n"
        "#EXTINF:-1,002 — First TV\n"
        f"{base_url}/service/1.2.4\n"
        "#EXTINF:-1,020 — Second TV\n"
        f"{base_url}/service/1.2.3\n"
        "#EXTINF:-1,--- — Unnumbered\n"
        f"{base_url}/service/4.5.6\n"
    )
    try:
        connection.request("GET", "/")
        response = connection.getresponse()
        assert response.status == 200
        assert response.getheader("Content-Type") == "audio/x-mpegurl"
        assert response.read().decode("utf-8") == expected_playlist
        connection.request("GET", "/playlist.m3u8")
        playlist_response = connection.getresponse()
        assert playlist_response.status == 200
        assert playlist_response.getheader("Content-Type") == (
            "audio/x-mpegurl"
        )
        assert playlist_response.read().decode("utf-8") == expected_playlist
    finally:
        connection.close()
        server.shutdown()
        server.close()
        serving.join(timeout=2)


def test_server_returns_not_found_for_unknown_endpoint():
    server = LocalTS_HTTPServer([], make_service_map(), port=0)
    serving = Thread(target=server.serve_forever)
    serving.start()
    connection = http.client.HTTPConnection(*server.address, timeout=2)
    try:
        connection.request("GET", "/unknown")
        assert connection.getresponse().status == 404
    finally:
        connection.close()
        server.shutdown()
        server.close()
        serving.join(timeout=2)


def test_server_rejects_malformed_and_unknown_services():
    server = LocalTS_HTTPServer([], make_service_map(), port=0)
    serving = Thread(target=server.serve_forever)
    serving.start()
    connection = http.client.HTTPConnection(*server.address, timeout=2)
    try:
        connection.request("GET", "/service/not-a-triplet")
        assert connection.getresponse().status == 400
        connection.request("GET", "/service/9.9.9")
        assert connection.getresponse().status == 404
    finally:
        connection.close()
        server.shutdown()
        server.close()
        serving.join(timeout=2)


def test_server_returns_unavailable_for_another_multiplex_when_adapter_is_busy():
    adapter = FakeAdapter()
    server = LocalTS_HTTPServer(
        [adapter],
        make_service_map_with_another_multiplex(),
        port=0,
        evict_oldest_on_exhaustion=False,
    )
    serving = Thread(target=server.serve_forever)
    serving.start()
    first = http.client.HTTPConnection(*server.address, timeout=2)
    second = http.client.HTTPConnection(*server.address, timeout=3)
    try:
        first.request("GET", "/service/1.2.3")
        assert first.getresponse().status == 200
        second.request("GET", "/service/4.5.6")
        assert second.getresponse().status == 503
    finally:
        first.close()
        second.close()
        server.shutdown()
        server.close()
        serving.join(timeout=2)


def test_server_waits_for_final_subscriber_before_tuning_another_multiplex():
    adapter = FakeAdapter()
    server = LocalTS_HTTPServer(
        [adapter],
        make_service_map_with_another_multiplex(),
        port=0,
        evict_oldest_on_exhaustion=False,
    )
    serving = Thread(target=server.serve_forever)
    serving.start()
    first = http.client.HTTPConnection(*server.address, timeout=3)
    second = http.client.HTTPConnection(*server.address, timeout=3)
    second_response = []

    def request_second_client():
        second.request("GET", "/service/4.5.6")
        second_response.append(second.getresponse())

    request_thread = Thread(target=request_second_client)
    try:
        first.request("GET", "/service/1.2.3")
        assert first.getresponse().status == 200
        request_thread.start()
        sleep(0.1)
        assert request_thread.is_alive()
        first.close()
        request_thread.join(timeout=3)
        assert not request_thread.is_alive()
        assert second_response[0].status == 200
        assert [call[1] for call in adapter.calls if call[0] == "tune"] == [
            TUNING,
            OTHER_TUNING,
        ]
    finally:
        first.close()
        second.close()
        server.shutdown()
        server.close()
        serving.join(timeout=2)


def test_server_close_interrupts_waiting_adapter_request():
    adapter = FakeAdapter()
    server = LocalTS_HTTPServer(
        [adapter],
        make_service_map_with_another_multiplex(),
        port=0,
        evict_oldest_on_exhaustion=False,
    )
    serving = Thread(target=server.serve_forever)
    serving.start()
    first = http.client.HTTPConnection(*server.address, timeout=3)
    second = http.client.HTTPConnection(*server.address, timeout=3)
    second_response = []

    def request_second_client():
        second.request("GET", "/service/4.5.6")
        second_response.append(second.getresponse())

    request_thread = Thread(target=request_second_client)
    try:
        first.request("GET", "/service/1.2.3")
        assert first.getresponse().status == 200
        request_thread.start()
        sleep(0.1)
        server.close()
        request_thread.join(timeout=1)
        assert not request_thread.is_alive()
        assert second_response[0].status == 503
    finally:
        first.close()
        second.close()
        server.shutdown()
        serving.join(timeout=2)


def test_server_shares_same_multiplex_even_when_eviction_is_enabled():
    adapter = FakeAdapter()
    server = LocalTS_HTTPServer(
        [adapter], make_service_map(), port=0, evict_oldest_on_exhaustion=True
    )
    serving = Thread(target=server.serve_forever)
    serving.start()
    first = http.client.HTTPConnection(*server.address, timeout=2)
    second = http.client.HTTPConnection(*server.address, timeout=2)
    try:
        first.request("GET", "/service/1.2.3")
        assert first.getresponse().status == 200
        session = server._sessions[TRIPLET.doublet_id]
        assert session.stream_info is server.service_map.stream_infos[TRIPLET]

        second.request("GET", "/service/1.2.3")
        response = second.getresponse()
        assert response.status == 200
        assert [call[0] for call in adapter.calls] == [
            "tune",
            "set_pid_filter_enabled",
            "start",
        ]
        assert len(server._active_sessions) == 1
        first.close()
        deadline = monotonic() + 2
        while monotonic() < deadline:
            with server._adapters_condition:
                if len(server._active_sessions[0].subscribers) == 1:
                    break
            sleep(0.01)
        assert len(server._active_sessions) == 1
        assert not adapter.stopped.is_set()
        assert server._available_adapters == []
        second.close()
        deadline = monotonic() + 2
        while monotonic() < deadline and not server._available_adapters:
            sleep(0.01)
        assert server._available_adapters == [adapter]
        assert [call[0] for call in adapter.calls].count("stop") == 1
        assert [call[0] for call in adapter.calls].count("sleep") == 1
    finally:
        first.close()
        second.close()
        server.shutdown()
        server.close()
        serving.join(timeout=2)


def test_server_reuses_idle_session_for_same_multiplex_reconnect(caplog):
    caplog.set_level(logging.INFO, logger="pydvb.server")
    adapter = FakeAdapter()
    server = LocalTS_HTTPServer(
        [adapter],
        make_service_map_with_another_service_on_same_multiplex(),
        port=0,
        multiplex_idle_grace_seconds=2.0,
    )
    serving = Thread(target=server.serve_forever)
    serving.start()
    first = http.client.HTTPConnection(*server.address, timeout=2)
    second = http.client.HTTPConnection(*server.address, timeout=2)
    try:
        first.request("GET", "/service/1.2.3")
        assert first.getresponse().status == 200
        first.close()

        deadline = monotonic() + 2
        while monotonic() < deadline and server.client_statuses:
            sleep(0.01)
        assert server.client_statuses == ()
        session = server._sessions[TRIPLET.doublet_id]
        assert session.idle_deadline is not None
        assert not adapter.stopped.is_set()

        second.request("GET", "/service/1.2.6")
        assert second.getresponse().status == 200
        assert session.idle_deadline is None
        assert len(server._active_sessions) == 1
        assert [call[0] for call in adapter.calls].count("tune") == 1
        assert any(
            "Reusing idle multiplex session 1.2 for service 1.2.6 during grace period"
            in record.getMessage()
            for record in caplog.records
        )
    finally:
        first.close()
        second.close()
        server.shutdown()
        server.close()
        serving.join(timeout=2)


def test_server_releases_session_after_idle_grace_expires():
    adapter = FakeAdapter()
    server = LocalTS_HTTPServer(
        [adapter],
        make_service_map(),
        port=0,
        multiplex_idle_grace_seconds=0.1,
    )
    serving = Thread(target=server.serve_forever)
    serving.start()
    first = http.client.HTTPConnection(*server.address, timeout=2)
    second = http.client.HTTPConnection(*server.address, timeout=2)
    try:
        first.request("GET", "/service/1.2.3")
        assert first.getresponse().status == 200
        first.close()

        deadline = monotonic() + 2
        while monotonic() < deadline and not server._available_adapters:
            sleep(0.01)
        assert server._available_adapters == [adapter]

        second.request("GET", "/service/1.2.3")
        assert second.getresponse().status == 200
        assert [call[0] for call in adapter.calls].count("tune") == 2
    finally:
        first.close()
        second.close()
        server.shutdown()
        server.close()
        serving.join(timeout=2)


def test_server_releases_idle_session_for_different_multiplex():
    adapter = FakeAdapter()
    server = LocalTS_HTTPServer(
        [adapter],
        make_service_map_with_another_multiplex(),
        port=0,
        multiplex_idle_grace_seconds=10.0,
        evict_oldest_on_exhaustion=False,
    )
    serving = Thread(target=server.serve_forever)
    serving.start()
    first = http.client.HTTPConnection(*server.address, timeout=2)
    second = http.client.HTTPConnection(*server.address, timeout=2)
    try:
        first.request("GET", "/service/1.2.3")
        assert first.getresponse().status == 200
        first.close()

        deadline = monotonic() + 2
        while monotonic() < deadline and server.client_statuses:
            sleep(0.01)
        assert server.client_statuses == ()

        second.request("GET", "/service/4.5.6")
        assert second.getresponse().status == 200
        assert [call[1] for call in adapter.calls if call[0] == "tune"] == [
            TUNING,
            OTHER_TUNING,
        ]
    finally:
        first.close()
        second.close()
        server.shutdown()
        server.close()
        serving.join(timeout=2)


def test_server_drops_oldest_filtered_block_when_subscriber_queue_is_full():
    server = LocalTS_HTTPServer([], make_service_map(), client_queue_size=1, port=0)
    subscriber = _StreamSubscriber(pids=frozenset({200}), output=Queue(maxsize=1))
    session = _MultiplexSession(
        adapter=FakeAdapter(),
        tuning=TUNING,
        stream_info=DVBStreamInfo(),
        subscribers={subscriber},
        pid_routes={200: (subscriber,)},
        stop_event=Event(),
        ready_event=Event(),
        done_event=Event(),
    )
    try:
        server._route_stream_chunk(session, make_ts_packet(200, 1))
        server._route_stream_chunk(session, make_ts_packet(200, 2))
        assert subscriber.output.get_nowait() == make_ts_packet(200, 2)
    finally:
        server.close()


def test_server_routes_each_subscriber_only_its_service_pids():
    second_service = DVBService(
        service_id=6,
        triplet_id=TRIPLET_3,
        name="Second TV",
        service_type=DVBServiceType.DIGITAL_TELEVISION,
        pmt_pid=101,
        elementary_streams=(DVBElementaryStream(201, 0x1B),),
    )
    service_map = make_service_map()
    service_map.add_stream_info(
        TUNING,
        DVBStreamInfo(
            original_network_id=TRIPLET_3.original_network_id,
            transport_stream_id=TRIPLET_3.transport_stream_id,
            services=(second_service,),
        ),
    )
    adapter = FakeAdapter()
    server = LocalTS_HTTPServer([adapter], service_map, port=0)
    serving = Thread(target=server.serve_forever)
    serving.start()
    first = http.client.HTTPConnection(*server.address, timeout=2)
    second = http.client.HTTPConnection(*server.address, timeout=2)
    try:
        first.request("GET", "/service/1.2.3")
        first_response = first.getresponse()
        second.request("GET", "/service/1.2.6")
        second_response = second.getresponse()
        assert first_response.status == second_response.status == 200

        adapter.chunks.put(make_ts_packet(200) + make_ts_packet(201) + make_ts_packet(0))

        assert first_response.read(376) == make_ts_packet(200) + make_ts_packet(0)
        assert second_response.read(376) == make_ts_packet(201) + make_ts_packet(0)
        assert [call[1] for call in adapter.calls if call[0] == "tune"] == [TUNING]
        assert [call[0] for call in adapter.calls].count("start") == 1
    finally:
        first.close()
        second.close()
        server.shutdown()
        server.close()
        serving.join(timeout=2)


def test_server_retunes_evicted_adapter_for_different_tuning():
    adapter = FakeAdapter()
    server = LocalTS_HTTPServer(
        [adapter], make_service_map(), port=0, evict_oldest_on_exhaustion=True
    )
    serving = Thread(target=server.serve_forever)
    serving.start()
    first = http.client.HTTPConnection(*server.address, timeout=2)
    second = http.client.HTTPConnection(*server.address, timeout=2)
    try:
        first.request("GET", "/service/1.2.3")
        assert first.getresponse().status == 200
        triplet = DVBTripletID(7, 8, 3)
        service = DVBService(
            service_id=3,
            triplet_id=triplet,
            name="Retuned TV",
            pmt_pid=100,
            elementary_streams=(DVBElementaryStream(200, 0x1B),),
        )
        server.service_map.add_stream_info(
            DVBTuning(666_000_000),
            DVBStreamInfo(
                original_network_id=triplet.original_network_id,
                transport_stream_id=triplet.transport_stream_id,
                services=(service,),
            ),
        )

        second.request("GET", f"/service/{triplet.dotted()}")
        assert second.getresponse().status == 200
        assert [call[0] for call in adapter.calls].count("tune") == 2
    finally:
        first.close()
        second.close()
        server.shutdown()
        server.close()
        serving.join(timeout=2)


def test_server_eviction_stops_all_subscribers_of_the_old_multiplex():
    adapter = FakeAdapter()
    server = LocalTS_HTTPServer(
        [adapter],
        make_service_map_with_another_multiplex(),
        port=0,
        evict_oldest_on_exhaustion=True,
    )
    serving = Thread(target=server.serve_forever)
    serving.start()
    first = http.client.HTTPConnection(*server.address, timeout=3)
    second = http.client.HTTPConnection(*server.address, timeout=3)
    replacement = http.client.HTTPConnection(*server.address, timeout=3)
    try:
        first.request("GET", "/service/1.2.3")
        assert first.getresponse().status == 200
        second.request("GET", "/service/1.2.3")
        assert second.getresponse().status == 200
        old_session = server._sessions[TRIPLET.doublet_id]
        assert len(old_session.subscribers) == 2

        replacement.request("GET", "/service/4.5.6")
        assert replacement.getresponse().status == 200
        assert old_session.done_event.wait(timeout=2)
        assert old_session.subscribers == set()
        assert set(server._sessions) == {TRIPLET_2.doublet_id}
        assert [call[1] for call in adapter.calls if call[0] == "tune"] == [
            TUNING,
            OTHER_TUNING,
        ]
    finally:
        first.close()
        second.close()
        replacement.close()
        server.shutdown()
        server.close()
        serving.join(timeout=2)


def test_server_ignores_disconnect_while_sending_error(monkeypatch):
    def raise_broken_pipe(*args, **kwargs):
        raise BrokenPipeError

    monkeypatch.setattr(BaseHTTPRequestHandler, "send_error", raise_broken_pipe)
    handler = object.__new__(_StreamRequestHandler)
    handler.send_error(503, "no adapter is currently available")


def test_server_reuses_adapter_after_client_disconnect():
    adapter = FakeAdapter()
    server = LocalTS_HTTPServer([adapter], make_service_map(), port=0)
    serving = Thread(target=server.serve_forever)
    serving.start()
    connection = http.client.HTTPConnection(*server.address, timeout=2)
    try:
        connection.request("GET", "/service/1.2.3")
        assert connection.getresponse().status == 200
    finally:
        connection.close()
    deadline = monotonic() + 2
    while monotonic() < deadline and not server._available_adapters:
        sleep(0.01)
    assert server._available_adapters == [adapter]
    server.shutdown()
    server.close()
    serving.join(timeout=2)