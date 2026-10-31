"""HTTP serving for live MPEG transport streams."""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from queue import Empty
import select
from socketserver import BaseServer
from threading import Condition, Event, Lock, Thread
from time import monotonic
from typing import cast
from urllib.parse import urlsplit

from pydvb.driver_api.adapter import DVBAdapter
from .service_map import DVBServiceMap
from .stream_scanner import DVBTripletID


ADAPTER_LEASE_RETRY_TIMEOUT = 2.0
ADAPTER_LEASE_RETRY_INTERVAL = 0.1


@dataclass
class _AdapterLease:
    adapter: DVBAdapter
    tuning: object | None = None
    handoff_requested: bool = False
    stopped: bool = False


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
        # When evict_oldest_on_exhaustion is True, the server will forcibly evict the oldest active adapter
        # lease when all adapters are in use and a new request arrives. This can be useful for live streaming
        # scenarios where you want to prioritize new requests over existing ones.
        # When False, the server will wait for an adapter to become available, potentially timing out if
        # none are free.
        evict_oldest_on_exhaustion: bool = False,
    ) -> None:
        if not 0 <= port <= 65535:
            raise ValueError("port must be between 0 and 65535")
        if client_queue_size <= 0:
            raise ValueError("client_queue_size must be positive")
        self.service_map = service_map
        self._available_adapters = list(adapters)
        self._leased_adapters: list[_AdapterLease] = []
        self._adapters_condition = Condition(Lock())
        self.client_queue_size = client_queue_size
        self.buffer_log = buffer_log
        self.evict_oldest_on_exhaustion = evict_oldest_on_exhaustion
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
        with self._adapters_condition:
            self._adapters_condition.notify_all()
        self.httpd.server_close()
        with self._adapters_condition:
            active_leases = tuple(self._leased_adapters)
        for lease in active_leases:
            self._stop_lease_once(lease)
        deadline = monotonic() + 2.0
        with self._adapters_condition:
            while self._leased_adapters:
                remaining = deadline - monotonic()
                if remaining <= 0:
                    break
                self._adapters_condition.wait(remaining)

    def _lease_adapter(self) -> _AdapterLease | None:
        with self._adapters_condition:
            if self._available_adapters:
                lease = _AdapterLease(self._available_adapters.pop())
                self._leased_adapters.append(lease)
                return lease
            if self.evict_oldest_on_exhaustion:
                if not self._leased_adapters:
                    deadline = monotonic() + ADAPTER_LEASE_RETRY_TIMEOUT
                    while not self._available_adapters and not self._leased_adapters:
                        if self._stop_event.is_set():
                            return None
                        remaining = deadline - monotonic()
                        if remaining <= 0:
                            return None
                        self._adapters_condition.wait(
                            min(ADAPTER_LEASE_RETRY_INTERVAL, remaining)
                        )
                    if self._available_adapters:
                        lease = _AdapterLease(self._available_adapters.pop())
                        self._leased_adapters.append(lease)
                        return lease
            else:
                deadline = monotonic() + ADAPTER_LEASE_RETRY_TIMEOUT
                while not self._available_adapters:
                    if self._stop_event.is_set():
                        return None
                    remaining = deadline - monotonic()
                    if remaining <= 0:
                        return None
                    self._adapters_condition.wait(
                        min(ADAPTER_LEASE_RETRY_INTERVAL, remaining)
                    )
                lease = _AdapterLease(self._available_adapters.pop())
                self._leased_adapters.append(lease)
                return lease

            if not self._leased_adapters:
                return None

            victim = self._leased_adapters[0]
            victim.handoff_requested = True

        self._stop_lease_once(victim)
        with self._adapters_condition:
            self._leased_adapters.remove(victim)
            replacement = _AdapterLease(victim.adapter, victim.tuning)
            self._leased_adapters.append(replacement)
            self._adapters_condition.notify_all()
            return replacement

    def _set_lease_tuning(self, lease: _AdapterLease, tuning: object) -> None:
        with self._adapters_condition:
            lease.tuning = tuning

    def _stop_lease_once(self, lease: _AdapterLease) -> None:
        with self._adapters_condition:
            if lease.stopped:
                return
            lease.stopped = True
        try:
            lease.adapter.stop()
        except (RuntimeError, OSError):
            pass

    def _finish_lease(self, lease: _AdapterLease) -> None:
        self._stop_lease_once(lease)
        with self._adapters_condition:
            if lease.handoff_requested:
                self._adapters_condition.notify_all()
                return
            self._leased_adapters.remove(lease)
            self._adapters_condition.notify_all()
        try:
            lease.adapter.sleep()
        except (RuntimeError, OSError):
            pass
        with self._adapters_condition:
            self._available_adapters.append(lease.adapter)
            self._adapters_condition.notify_all()

    def _playlist(self, base_url: str) -> bytes:
        lines = ["#EXTM3U"]
        for service in self.service_map.services_sorted():
            if service.triplet_id is None:
                continue
            lcn = "---" if service.logical_channel_number is None else str(service.logical_channel_number)
            name = service.name or "Unnamed service"
            lines.append(f"#EXTINF:-1,{lcn:0>3} — {name}")
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

    @staticmethod
    def _log(message: str, *, depth: int = 1) -> None:
        """Print a handler diagnostic with nested indentation."""
        print(f"{'  ' * depth}{message}", flush=True)

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
                self._log(f"Rejecting playlist request with invalid Host header: {authority!r}")
                self.send_error(400, "invalid Host header")
                return
            body = owner._playlist(f"http://{authority}")
            self._log(f"Serving playlist with {len(body)} bytes")
            self.send_response(200)
            self.send_header("Content-Type", "audio/x-mpegurl")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        prefix = "/service/"
        if not path.startswith(prefix):
            self._log(f"Rejecting unknown HTTP path: {path!r}")
            self.send_error(404, "stream endpoint is /service/X.Y.Z")
            return

        try:
            triplet = DVBTripletID.from_dotted(path.removeprefix(prefix))
        except (TypeError, ValueError):
            self._log(f"Rejecting malformed service path: {path!r}")
            self.send_error(400, "service path must be /service/X.Y.Z")
            return
        self._log(f"Resolved service triplet: {triplet}")

        try:
            tuning = owner.service_map.tunings[triplet]
            service = owner.service_map.services[triplet]
        except KeyError:
            self._log(f"Service triplet not found in service map: {triplet}")
            self.send_error(404, "service was not found")
            return
        self._log(
            f"Service lookup succeeded: name={service.name!r} "
            f"frequency_hz={tuning.frequency_hz} bandwidth_hz={tuning.bandwidth_hz}"
        )

        lease = owner._lease_adapter()
        if lease is None:
            self._log("No adapter available for service request")
            self.send_error(503, "no adapter is currently available")
            return
        adapter = lease.adapter

        client_address = f"{self.client_address[0]}:{self.client_address[1]}"
        self._log(f">>> Client connected: {client_address}")
        chunk_count = 0
        byte_count = 0
        timeout_count = 0
        try:
            if lease.tuning == tuning:
                self._log("Reusing adapter tuning for requested service", depth=2)
            else:
                self._log("Tuning adapter and waiting for frontend lock", depth=2)
                status = adapter.tune(tuning, wait_for_lock=True)
                self._log(f"Frontend tune returned status: {status!r}", depth=2)
                if status is not None and not status.lock:
                    self._log("Frontend did not lock requested service", depth=2)
                    self.send_error(503, "adapter did not lock the requested service")
                    return
                owner._set_lease_tuning(lease, tuning)
                if status and status.lock:
                    self._log("Adapter successfully locked the requested service", depth=2)
            adapter.start()
            self._log("Started adapter for streaming", depth=2)
            adapter.configure_pid_filter(service.pids())
            adapter.set_pid_filter_enabled(True)
            self._log("Configured PID filter for streaming", depth=2)
            
            self.protocol_version = "HTTP/1.1"
            self.send_response(200)
            self.send_header("Content-Type", "video/mp2t")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            self._log("Sent HTTP 200 video/mp2t response headers", depth=2)
            while not owner._stop_event.is_set():
                if self._client_disconnected():
                    self._log("Client disconnected before next stream read", depth=2)
                    break
                try:
                    chunk = adapter.read_stream(1000)
                except TimeoutError:
                    timeout_count += 1
                    if timeout_count == 1 or timeout_count % 10 == 0:
                        self._log(
                            f"Transport-stream read timed out ({timeout_count} timeouts)",
                            depth=2,
                        )
                    continue
                chunk_count += 1
                byte_count += len(chunk)
                # if chunk_count == 1 or chunk_count % 100 == 0:
                #     print(
                #         f"Read stream chunk {chunk_count}: {len(chunk)} bytes "
                #         f"({byte_count} bytes total)",
                #         flush=True,
                #     )
                self.wfile.write(f"{len(chunk):X}\r\n".encode("ascii"))
                self.wfile.write(chunk)
                self.wfile.write(b"\r\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError) as error:
            self._log(f"Client connection closed while streaming: {error!r}", depth=2)
        except RuntimeError as error:
            if not (lease.stopped and str(error) == "demuxer is not started"):
                self._log(
                    f"Streaming request failed: {type(error).__name__}: {error}",
                    depth=2,
                )
        except (TimeoutError, OSError) as error:
            self._log(f"Streaming request failed: {type(error).__name__}: {error}", depth=2)
        finally:
            owner._finish_lease(lease)
            self._log(
                f"<<< Client disconnected: {client_address}; "
                f"chunks={chunk_count} bytes={byte_count} read_timeouts={timeout_count}",
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