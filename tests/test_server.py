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

    def read_ts(self, timeout_ms=2000):
        try:
            return self.chunks.get(timeout=0.1)
        except Exception:
            return b""

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


def test_server_fans_out_transport_stream_to_multiple_clients():
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


def test_server_logs_stream_queue_size(capsys):
    server = LocalTS_HTTPServer(FakeAdapter(), port=0, buffer_log=True)

    class FakeStream:
        queue_size = 3
        queue_capacity = 20

        def iter_chunks(self, timeout=None):
            yield b"data"

    server.stream = FakeStream()
    server._broadcast()

    assert "Buffer queue: 3/20" in capsys.readouterr().out