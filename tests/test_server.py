import http.client
from queue import Queue
from threading import Event, Thread
from time import monotonic, sleep

from mactv.server import LocalTS_HTTPServer


class FakeAdapter:
    def __init__(self):
        self.chunks = Queue()
        self.stopped = Event()

    def start(self):
        pass

    def read_stream(self, timeout_ms=2000):
        try:
            return self.chunks.get(timeout=0.1)
        except Exception as error:
            raise TimeoutError from error

    def stop(self):
        self.stopped.set()


def wait_for_clients(server, count):
    deadline = monotonic() + 2
    while monotonic() < deadline:
        with server._clients_lock:
            if len(server._clients) >= count:
                return
        sleep(0.01)
    raise AssertionError("HTTP clients did not connect")


def test_server_fans_out_transport_stream_to_multiple_clients(capsys):
    adapter = FakeAdapter()
    server = LocalTS_HTTPServer(adapter, port=0)
    serving = Thread(target=server.serve_forever)
    serving.start()
    server.start()
    first = http.client.HTTPConnection(*server.address, timeout=2)
    second = http.client.HTTPConnection(*server.address, timeout=2)
    try:
        first.request("GET", "/stream.ts")
        first_response = first.getresponse()
        second.request("GET", "/stream.ts")
        second_response = second.getresponse()
        wait_for_clients(server, 2)

        payload = b"\x47" * 188
        adapter.chunks.put(payload)

        assert first_response.status == 200
        assert second_response.status == 200
        assert first_response.read(188) == payload
        assert second_response.read(188) == payload
    finally:
        first.close()
        second.close()
        server.shutdown()
        server.close()
        serving.join(timeout=2)

    assert adapter.stopped.is_set()
    output = ""
    deadline = monotonic() + 2
    while monotonic() < deadline and output.count("Client disconnected:") < 2:
        output += capsys.readouterr().out
        sleep(0.01)
    output += capsys.readouterr().out
    assert output.count("Client connected:") == 2
    assert output.count("Client disconnected:") == 2


def test_server_returns_not_found_for_unknown_endpoint():
    server = LocalTS_HTTPServer(FakeAdapter(), port=0)
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


def test_server_does_not_depend_on_ts_stream():
    server = LocalTS_HTTPServer(FakeAdapter(), port=0)

    assert server.adapter is not None
    assert not hasattr(server, "stream")