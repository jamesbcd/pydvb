"""HTTP serving for live MPEG transport streams."""

from __future__ import annotations

from collections.abc import Collection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from queue import Empty
import select
from socketserver import BaseServer
from threading import Condition, Event, Lock, Thread
from time import monotonic
from typing import cast
from urllib.parse import urlsplit

from mactv.driver_api.adapter import DVBAdapter
from .service_map import DVBServiceMap
from .stream_scanner import DVBTripletID


class LocalTS_HTTPServer:
    """Serve mapped DVB services using a pool of available adapters."""

    def __init__(
        self,
        adapters: Collection[DVBAdapter],
        service_map: DVBServiceMap,
        host: str = "127.0.0.1",
        port: int = 8080,
        client_queue_size: int = 20,
        buffer_log: bool = False,
    ) -> None:
        if not 0 <= port <= 65535:
            raise ValueError("port must be between 0 and 65535")
        if client_queue_size <= 0:
            raise ValueError("client_queue_size must be positive")
        self.service_map = service_map
        self._available_adapters = list(adapters)
        self._leased_adapters: list[DVBAdapter] = []
        self._adapters_condition = Condition(Lock())
        self._stopped_adapter_ids: set[int] = set()
        self.client_queue_size = client_queue_size
        self.buffer_log = buffer_log
        self._stop_event = Event()
        self.httpd = _HTTPServer((host, port), _StreamRequestHandler, self)

    @property
    def address(self) -> tuple[str, int]:
        """Return the address selected by the operating system."""
        address = self.httpd.server_address
        return cast(str, address[0]), address[1]

    def start(self) -> None:
        """Mark the server ready to accept service requests."""

    def serve_forever(self) -> None:
        """Serve HTTP requests until ``shutdown`` or ``close`` is called."""
        self.httpd.serve_forever()

    def shutdown(self) -> None:
        """Stop accepting HTTP requests while allowing active handlers to exit."""
        self.httpd.shutdown()

    def close(self) -> None:
        """Stop HTTP serving and interrupt active adapter reads."""
        self._stop_event.set()
        self.httpd.server_close()
        with self._adapters_condition:
            active_adapters = tuple(self._leased_adapters)
        for adapter in active_adapters:
            self._stop_adapter_once(adapter)
        deadline = monotonic() + 2.0
        with self._adapters_condition:
            while self._leased_adapters:
                remaining = deadline - monotonic()
                if remaining <= 0:
                    break
                self._adapters_condition.wait(remaining)

    def _lease_adapter(self) -> DVBAdapter | None:
        with self._adapters_condition:
            if not self._available_adapters:
                return None
            adapter = self._available_adapters.pop()
            self._leased_adapters.append(adapter)
            return adapter

    def _release_adapter(self, adapter: DVBAdapter) -> None:
        with self._adapters_condition:
            self._leased_adapters.remove(adapter)
            self._available_adapters.append(adapter)
            self._adapters_condition.notify_all()

    def _stop_adapter_once(self, adapter: DVBAdapter) -> None:
        with self._adapters_condition:
            if id(adapter) in self._stopped_adapter_ids:
                return
            self._stopped_adapter_ids.add(id(adapter))
        try:
            adapter.stop()
        except (RuntimeError, OSError):
            pass


class _HTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, server_address, request_handler, owner) -> None:
        self.owner = owner
        super().__init__(server_address, request_handler)


class _StreamRequestHandler(BaseHTTPRequestHandler):
    server: BaseServer

    def do_GET(self) -> None:
        owner = cast(_HTTPServer, self.server).owner
        path = urlsplit(self.path).path
        prefix = "/service/"
        if not path.startswith(prefix):
            self.send_error(404, "stream endpoint is /service/X.Y.Z")
            return

        try:
            triplet = DVBTripletID.from_dotted(path.removeprefix(prefix))
        except (TypeError, ValueError):
            self.send_error(400, "service path must be /service/X.Y.Z")
            return

        try:
            tuning = owner.service_map.tunings[triplet]
            service = owner.service_map.services[triplet]
        except KeyError:
            self.send_error(404, "service was not found")
            return

        adapter = owner._lease_adapter()
        if adapter is None:
            self.send_error(503, "no adapter is currently available")
            return

        client_address = f"{self.client_address[0]}:{self.client_address[1]}"
        print(f"Client connected: {client_address}", flush=True)
        try:
            status = adapter.tune(tuning, wait_for_lock=True)
            if status is not None and not status.lock:
                self.send_error(503, "adapter did not lock the requested service")
                return
            adapter.start()
            adapter.configure_pid_filter(service.pids())
            adapter.set_pid_filter_enabled(True)

            self.protocol_version = "HTTP/1.1"
            self.send_response(200)
            self.send_header("Content-Type", "video/mp2t")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            while not owner._stop_event.is_set():
                if self._client_disconnected():
                    break
                try:
                    chunk = adapter.read_stream(1000)
                except TimeoutError:
                    continue
                self.wfile.write(f"{len(chunk):X}\r\n".encode("ascii"))
                self.wfile.write(chunk)
                self.wfile.write(b"\r\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, TimeoutError, RuntimeError, OSError):
            pass
        finally:
            owner._stop_adapter_once(adapter)
            try:
                adapter.sleep()
            except (RuntimeError, OSError):
                pass
            owner._release_adapter(adapter)
            print(f"Client disconnected: {client_address}", flush=True)

    def _client_disconnected(self) -> bool:
        try:
            readable, _, _ = select.select([self.connection], [], [], 0)
            if not readable:
                return False
            data = self.connection.recv(1, 0x02 | 0x40)
        except (BlockingIOError, ConnectionResetError, OSError):
            return False
        return data == b""

    def log_message(self, format: str, *_args: object) -> None:
        return