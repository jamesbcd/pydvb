import http.client
from queue import Queue
from threading import Event, Thread
from time import monotonic, sleep

from mactv.server import LocalTS_HTTPServer
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
        self.calls.append(("start",))

    def tune(self, tuning, wait_for_lock=True):
        self.calls.append(("tune", tuning, wait_for_lock))
        return type("Status", (), {"lock": True})()

    def configure_pid_filter(self, pids):
        self.calls.append(("configure_pid_filter", frozenset(pids)))

    def set_pid_filter_enabled(self, enabled=True):
        self.calls.append(("set_pid_filter_enabled", enabled))

    def read_stream(self, timeout_ms=2000):
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


def test_server_returns_not_found_for_unknown_endpoint():
    server = LocalTS_HTTPServer([], make_service_map(), port=0)
    serving = Thread(target=server.serve_forever)
    serving.start()
    connection = http.client.HTTPConnection(*server.address, timeout=2)
    try:
        connection.request("GET", "/")
        response = connection.getresponse()
        assert response.status == 404
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
    server = LocalTS_HTTPServer([adapter], make_service_map(), port=0)
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