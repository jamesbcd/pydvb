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

    def _playlist(self, base_url: str) -> bytes:
        lines = ["#EXTM3U"]
        for service in self.service_map.services_sorted():
            if service.triplet_id is None:
                continue
            lcn = "-" if service.logical_channel_number is None else str(service.logical_channel_number)
            name = service.name or "Unnamed service"
            lines.append(f"#EXTINF:-1,{lcn} {name}")
            lines.append(f"{base_url}/service/{service.triplet_id.dotted()}")
        return ("\n".join(lines) + "\n").encode("utf-8")


class _HTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, server_address, request_handler, owner) -> None:
        self.owner = owner
        super().__init__(server_address, request_handler)


class _StreamRequestHandler(BaseHTTPRequestHandler):
    server: BaseServer

    def send_error(
        self,
        code: int,
        message: str | None = None,
        explain: str | None = None,
    ) -> None:
        """Send an HTTP error unless the client has already disconnected."""
        try:
            super().send_error(code, message, explain)
        except (BrokenPipeError, ConnectionResetError):
            return

    def do_GET(self) -> None:
        owner = cast(_HTTPServer, self.server).owner
        path = urlsplit(self.path).path
        print(
            f"HTTP GET from {self.client_address[0]}:{self.client_address[1]} "
            f"path={self.path!r}",
            flush=True,
        )
        if path in ("/", "/playlist.m3u8"):
            authority = self.headers.get("Host")
            if authority is None:
                host, port = self.connection.getsockname()[:2]
                host = f"[{host}]" if ":" in host else host
                authority = f"{host}:{port}"
            parsed_authority = urlsplit(f"http://{authority}")
            try:
                parsed_authority.port
            except ValueError:
                self.send_error(400, "invalid Host header")
                return
            if (
                not authority
                or parsed_authority.netloc != authority
                or parsed_authority.hostname is None
                or parsed_authority.username is not None
                or parsed_authority.password is not None
                or parsed_authority.path
                or parsed_authority.query
                or parsed_authority.fragment
                or any(character.isspace() for character in authority)
            ):
                print(f"Rejecting playlist request with invalid Host header: {authority!r}", flush=True)
                self.send_error(400, "invalid Host header")
                return
            body = owner._playlist(f"http://{authority}")
            print(f"Serving playlist with {len(body)} bytes", flush=True)
            self.send_response(200)
            self.send_header("Content-Type", "application/vnd.apple.mpegurl; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        prefix = "/service/"
        if not path.startswith(prefix):
            print(f"Rejecting unknown HTTP path: {path!r}", flush=True)
            self.send_error(404, "stream endpoint is /service/X.Y.Z")
            return

        try:
            triplet = DVBTripletID.from_dotted(path.removeprefix(prefix))
        except (TypeError, ValueError):
            print(f"Rejecting malformed service path: {path!r}", flush=True)
            self.send_error(400, "service path must be /service/X.Y.Z")
            return
        print(f"Resolved service triplet: {triplet}", flush=True)

        try:
            tuning = owner.service_map.tunings[triplet]
            service = owner.service_map.services[triplet]
        except KeyError:
            print(f"Service triplet not found in service map: {triplet}", flush=True)
            self.send_error(404, "service was not found")
            return
        print(
            f"Service lookup succeeded: name={service.name!r} "
            f"frequency_hz={tuning.frequency_hz} bandwidth_hz={tuning.bandwidth_hz}",
            flush=True,
        )

        adapter = owner._lease_adapter()
        if adapter is None:
            print("No adapter available for service request", flush=True)
            self.send_error(503, "no adapter is currently available")
            return

        client_address = f"{self.client_address[0]}:{self.client_address[1]}"
        print(f"Client connected: {client_address}", flush=True)
        chunk_count = 0
        byte_count = 0
        timeout_count = 0
        try:
            print("Tuning adapter and waiting for frontend lock", flush=True)
            status = adapter.tune(tuning, wait_for_lock=True)
            print(f"Frontend tune returned status: {status!r}", flush=True)
            if status is not None and not status.lock:
                print("Frontend did not lock requested service", flush=True)
                self.send_error(503, "adapter did not lock the requested service")
                return
            if status and status.lock:
                print("Adapter successfully locked the requested service", flush=True)
            adapter.start()
            print("Started adapter for streaming", flush=True)
            adapter.configure_pid_filter(service.pids())
            adapter.set_pid_filter_enabled(True)
            print("Configured PID filter for streaming", flush=True)
            
            self.protocol_version = "HTTP/1.1"
            self.send_response(200)
            self.send_header("Content-Type", "video/mp2t")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            print("Sent HTTP 200 video/mp2t response headers", flush=True)
            while not owner._stop_event.is_set():
                if self._client_disconnected():
                    print("Client disconnected before next stream read", flush=True)
                    break
                try:
                    chunk = adapter.read_stream(1000)
                except TimeoutError:
                    timeout_count += 1
                    if timeout_count == 1 or timeout_count % 10 == 0:
                        print(
                            f"Transport-stream read timed out ({timeout_count} timeouts)",
                            flush=True,
                        )
                    continue
                chunk_count += 1
                byte_count += len(chunk)
                if chunk_count == 1 or chunk_count % 100 == 0:
                    print(
                        f"Read stream chunk {chunk_count}: {len(chunk)} bytes "
                        f"({byte_count} bytes total)",
                        flush=True,
                    )
                self.wfile.write(f"{len(chunk):X}\r\n".encode("ascii"))
                self.wfile.write(chunk)
                self.wfile.write(b"\r\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError) as error:
            print(f"Client connection closed while streaming: {error!r}", flush=True)
        except (TimeoutError, RuntimeError, OSError) as error:
            print(f"Streaming request failed: {type(error).__name__}: {error}", flush=True)
        finally:
            owner._stop_adapter_once(adapter)
            try:
                adapter.sleep()
            except (RuntimeError, OSError) as error:
                print(f"Adapter sleep failed during cleanup: {error!r}", flush=True)
            owner._release_adapter(adapter)
            print(
                f"Client disconnected: {client_address}; "
                f"chunks={chunk_count} bytes={byte_count} read_timeouts={timeout_count}",
                flush=True,
            )

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