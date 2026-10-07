import http.client
from http.server import BaseHTTPRequestHandler
from queue import Queue
from threading import Event, Thread
from time import monotonic, sleep

from mactv.server import LocalTS_HTTPServer, _StreamRequestHandler
from mactv.service_map import DVBServiceMap
from mactv.stream_scanner import DVBElementaryStream, DVBService, DVBServiceType, DVBStreamInfo, DVBTripletID
from mactv.driver_api.tuner import DVBTuning


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
TUNING = DVBTuning(650_000_000)


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
    service_map.add_stream_info(TUNING, DVBStreamInfo(services=(service,)))
    return service_map


def test_server_streams_requested_service_and_releases_adapter(capsys):
    adapter = FakeAdapter()
    server = LocalTS_HTTPServer([adapter], make_service_map(), port=0)
    serving = Thread(target=server.serve_forever)
    serving.start()
    server.start()
    connection = http.client.HTTPConnection(*server.address, timeout=2)
    try:
        connection.request("GET", "/service/1.2.3")
        response = connection.getresponse()

        payload = b"\x47" * 188
        adapter.chunks.put(payload)

        assert response.status == 200
        assert response.read(188) == payload
    finally:
        connection.close()
        server.shutdown()
        server.close()
        serving.join(timeout=2)

    assert adapter.stopped.is_set()
    assert adapter.calls[:5] == [
        ("tune", TUNING, True),
        ("start",),
        ("configure_pid_filter", frozenset({0, 1, 16, 17, 18, 20, 100, 200})),
        ("set_pid_filter_enabled", True),
        ("stop",),
    ]
    assert adapter.calls[5:] == [("sleep",)]
    output = ""
    deadline = monotonic() + 2
    while monotonic() < deadline and "Client disconnected:" not in output:
        output += capsys.readouterr().out
        sleep(0.01)
    output += capsys.readouterr().out
    assert "Client connected:" in output
    assert "Client disconnected:" in output


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
        "#EXTINF:-1,2 First TV\n"
        f"{base_url}/service/1.2.4\n"
        "#EXTINF:-1,20 Second TV\n"
        f"{base_url}/service/1.2.3\n"
        "#EXTINF:-1,- Unnumbered\n"
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


def test_server_returns_unavailable_when_all_adapters_are_leased():
    adapter = FakeAdapter()
    server = LocalTS_HTTPServer(
        [adapter],
        make_service_map(),
        port=0,
        evict_oldest_on_exhaustion=False,
    )
    serving = Thread(target=server.serve_forever)
    serving.start()
    first = http.client.HTTPConnection(*server.address, timeout=2)
    second = http.client.HTTPConnection(*server.address, timeout=2)
    try:
        first.request("GET", "/service/1.2.3")
        assert first.getresponse().status == 200
        second.request("GET", "/service/1.2.3")
        assert second.getresponse().status == 503
    finally:
        first.close()
        second.close()
        server.shutdown()
        server.close()
        serving.join(timeout=2)


def test_server_evicts_oldest_stream_and_reuses_same_tuning_without_sleeping():
    adapter = FakeAdapter()
    server = LocalTS_HTTPServer([adapter], make_service_map(), port=0)
    serving = Thread(target=server.serve_forever)
    serving.start()
    first = http.client.HTTPConnection(*server.address, timeout=2)
    second = http.client.HTTPConnection(*server.address, timeout=2)
    try:
        first.request("GET", "/service/1.2.3")
        assert first.getresponse().status == 200

        second.request("GET", "/service/1.2.3")
        response = second.getresponse()
        assert response.status == 200
        assert [call[0] for call in adapter.calls] == [
            "tune",
            "start",
            "configure_pid_filter",
            "set_pid_filter_enabled",
            "stop",
            "start",
            "configure_pid_filter",
            "set_pid_filter_enabled",
        ]
    finally:
        first.close()
        second.close()
        server.shutdown()
        server.close()
        serving.join(timeout=2)


def test_server_retunes_evicted_adapter_for_different_tuning():
    adapter = FakeAdapter()
    server = LocalTS_HTTPServer([adapter], make_service_map(), port=0)
    serving = Thread(target=server.serve_forever)
    serving.start()
    first = http.client.HTTPConnection(*server.address, timeout=2)
    second = http.client.HTTPConnection(*server.address, timeout=2)
    try:
        first.request("GET", "/service/1.2.3")
        assert first.getresponse().status == 200
        service = server.service_map.services[TRIPLET]
        server.service_map.add_stream_info(
            DVBTuning(666_000_000),
            DVBStreamInfo(services=(service,)),
        )

        second.request("GET", "/service/1.2.3")
        assert second.getresponse().status == 200
        assert [call[0] for call in adapter.calls].count("tune") == 2
    finally:
        first.close()
        second.close()
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