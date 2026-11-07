"""HTTP serving for live MPEG transport streams."""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import logging
from queue import Empty, Full, Queue
import select
from socketserver import BaseServer
from threading import Condition, Event, Lock, Thread
from time import monotonic
from typing import cast
from urllib.parse import urlsplit

from pydvb.driver_api.adapter import DVBAdapter
from pydvb.driver_api.tuner import DVBTuning
from .service_map import DVBServiceMap
from .stream_scanner import DVBEventScanner, DVBDoubletID, DVBStreamInfo, DVBTripletID


logger = logging.getLogger(__name__)

ADAPTER_LEASE_RETRY_TIMEOUT = 2.0
ADAPTER_LEASE_RETRY_INTERVAL = 0.1
EVENT_SCAN_QUEUE_SIZE = 8


@dataclass(eq=False)
class _StreamSubscriber:
    pids: frozenset[int]
    output: Queue[bytes]
    peer: str = ""
    service_name: str = ""
    service_id: str = ""
    connected_at: float = field(default_factory=monotonic)
    bytes_sent: int = 0


@dataclass(frozen=True)
class HTTPClientStatus:
    """Immutable snapshot of one connected HTTP stream client."""

    peer: str
    service_name: str
    service_id: str
    bytes_sent: int
    connected_seconds: float


@dataclass(eq=False)
class _MultiplexSession:
    adapter: DVBAdapter
    tuning: DVBTuning
    stream_info: DVBStreamInfo
    subscribers: set[_StreamSubscriber]
    pid_routes: dict[int, tuple[_StreamSubscriber, ...]]
    stop_event: Event
    ready_event: Event
    done_event: Event
    startup_error: Exception | None = None
    producer: Thread | None = None


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
        # When evict_oldest_on_exhaustion is True, the server stops the oldest
        # multiplex session, disconnecting all subscribers using that adapter.
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
        self._sessions: dict[DVBDoubletID, _MultiplexSession] = {}
        self._active_sessions: list[_MultiplexSession] = []
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

    @property
    def client_statuses(self) -> tuple[HTTPClientStatus, ...]:
        """Return a thread-safe snapshot of active HTTP stream clients."""
        now = monotonic()
        with self._adapters_condition:
            return tuple(
                HTTPClientStatus(
                    peer=subscriber.peer,
                    service_name=subscriber.service_name,
                    service_id=subscriber.service_id,
                    bytes_sent=subscriber.bytes_sent,
                    connected_seconds=max(0.0, now - subscriber.connected_at),
                )
                for session in self._active_sessions
                for subscriber in session.subscribers
            )

    def start(self) -> None:
        """Mark the server ready to accept service requests."""

    def serve_forever(self) -> None:
        """Serve HTTP requests until ``shutdown()`` is called from another thread.

        Calling ``close()`` alone does not stop this request loop.
        """
        self.httpd.serve_forever()

    def shutdown(self) -> None:
        """Stop the ``serve_forever()`` loop without closing the listening socket.

        Call this from a different thread than ``serve_forever()``. Call
        ``close()`` afterward to stop active stream sessions and release the socket.
        """
        self.httpd.shutdown()

    def close(self) -> None:
        """Stop active streams, close the listening socket, and wait briefly.

        If ``serve_forever()`` is running in another thread, call ``shutdown()``
        first; this method does not stop that request loop. Waits up to two
        seconds total for active multiplex sessions to finish.
        """
        self._stop_event.set()
        with self._adapters_condition:
            sessions = tuple(self._active_sessions)
            for session in sessions:
                session.stop_event.set()
            self._adapters_condition.notify_all()
        self.httpd.server_close()
        deadline = monotonic() + 2.0
        for session in sessions:
            remaining = deadline - monotonic()
            if remaining <= 0:
                break
            session.done_event.wait(remaining)

    def _subscribe(
        self,
        tuning: DVBTuning,
        stream_info: DVBStreamInfo,
        pids: frozenset[int],
        *,
        peer: str = "",
        service_name: str = "",
        service_id: str = "",
    ) -> tuple[_MultiplexSession, _StreamSubscriber] | None:
        doublet_id = stream_info.doublet_id
        if doublet_id is None:
            raise ValueError("stream info must identify its multiplex")
        deadline = monotonic() + ADAPTER_LEASE_RETRY_TIMEOUT
        wait_started = monotonic()
        waiting_logged = False
        with self._adapters_condition:
            while not self._stop_event.is_set():
                session = self._sessions.get(doublet_id)
                if session is not None and not session.stop_event.is_set():
                    subscriber = self._add_subscriber_locked(
                        session, pids, peer, service_name, service_id
                    )
                    return session, subscriber

                if self._available_adapters:
                    if waiting_logged:
                        logger.info(
                            "Adapter became available after %.3f seconds",
                            monotonic() - wait_started,
                        )
                    session = _MultiplexSession(
                        adapter=self._available_adapters.pop(),
                        tuning=tuning,
                        stream_info=stream_info,
                        subscribers=set(),
                        pid_routes={},
                        stop_event=Event(),
                        ready_event=Event(),
                        done_event=Event(),
                    )
                    subscriber = self._add_subscriber_locked(
                        session, pids, peer, service_name, service_id
                    )
                    self._sessions[doublet_id] = session
                    self._active_sessions.append(session)
                    session.producer = Thread(
                        target=self._run_session,
                        args=(session,),
                        name=f"dvb-stream-{tuning.frequency_hz}",
                        daemon=True,
                    )
                    try:
                        session.producer.start()
                    except RuntimeError:
                        self._active_sessions.remove(session)
                        del self._sessions[doublet_id]
                        self._available_adapters.append(session.adapter)
                        self._adapters_condition.notify_all()
                        return None
                    return session, subscriber

                if self.evict_oldest_on_exhaustion:
                    victim = next(
                        (
                            active
                            for active in self._active_sessions
                            if not active.stop_event.is_set()
                        ),
                        None,
                    )
                    if victim is not None:
                        logger.info(
                            "Evicting multiplex at %s Hz for request at %s Hz",
                            victim.tuning.frequency_hz,
                            tuning.frequency_hz,
                        )
                        victim.stop_event.set()

                if not waiting_logged:
                    logger.debug(
                        "Waiting up to %.1f seconds for an adapter to become available",
                        ADAPTER_LEASE_RETRY_TIMEOUT,
                    )
                    waiting_logged = True
                remaining = deadline - monotonic()
                if remaining <= 0:
                    return None
                self._adapters_condition.wait(
                    min(ADAPTER_LEASE_RETRY_INTERVAL, remaining)
                )

        return None

    def _add_subscriber_locked(
        self,
        session: _MultiplexSession,
        pids: frozenset[int],
        peer: str = "",
        service_name: str = "",
        service_id: str = "",
    ) -> _StreamSubscriber:
        subscriber = _StreamSubscriber(
            pids=pids,
            output=Queue(maxsize=self.client_queue_size),
            peer=peer,
            service_name=service_name,
            service_id=service_id,
        )
        session.subscribers.add(subscriber)
        self._refresh_pid_routes_locked(session)
        return subscriber

    @staticmethod
    def _refresh_pid_routes_locked(session: _MultiplexSession) -> None:
        routes: dict[int, list[_StreamSubscriber]] = {}
        for subscriber in session.subscribers:
            for pid in subscriber.pids:
                routes.setdefault(pid, []).append(subscriber)
        session.pid_routes = {
            pid: tuple(subscribers) for pid, subscribers in routes.items()
        }

    def _unsubscribe(
        self, session: _MultiplexSession, subscriber: _StreamSubscriber
    ) -> None:
        with self._adapters_condition:
            if subscriber not in session.subscribers:
                return
            session.subscribers.remove(subscriber)
            self._refresh_pid_routes_locked(session)
            if not session.subscribers:
                session.stop_event.set()
            self._adapters_condition.notify_all()

    def _record_client_bytes(
        self,
        session: _MultiplexSession,
        subscriber: _StreamSubscriber,
        count: int,
    ) -> None:
        """Update delivered-byte telemetry while holding the session lock."""
        with self._adapters_condition:
            if subscriber in session.subscribers:
                subscriber.bytes_sent += count

    def _run_session(self, session: _MultiplexSession) -> None:
        dvb_event_scan_queue: Queue[bytes] | None = None
        dvb_event_scan_stop = Event()
        dvb_event_scanner: Thread | None = None
        try:
            logger.info(
                "Tuning adapter to %s Hz (bandwidth %s Hz)",
                session.tuning.frequency_hz,
                session.tuning.bandwidth_hz,
            )
            status = session.adapter.tune(session.tuning, wait_for_lock=True)
            if status is not None and not status.lock:
                raise RuntimeError("adapter did not lock the requested multiplex")
            if session.stop_event.is_set():
                return
            session.adapter.set_pid_filter_enabled(False)
            session.adapter.start()
            logger.info(
                "Started multiplex stream with ID %s for %d subscriber(s)",
                session.stream_info.doublet_id,
                len(session.subscribers),
            )
            if session.stream_info.services:
                # Start DVB event scanner if there are services in the stream info
                dvb_event_scan_queue = Queue(maxsize=EVENT_SCAN_QUEUE_SIZE)
                dvb_event_scanner = Thread(
                    target=self._run_dvb_event_scanner,
                    args=(session, dvb_event_scan_queue, dvb_event_scan_stop),
                    name=f"dvb-events-{session.stream_info.doublet_id}",
                    daemon=True,
                )
                try:
                    dvb_event_scanner.start()
                except RuntimeError as error:
                    dvb_event_scan_queue = None
                    dvb_event_scanner = None
                    logger.error("Could not start DVB event scanner: %s", error)
            session.ready_event.set()
            while not self._stop_event.is_set() and not session.stop_event.is_set():
                try:
                    chunk = session.adapter.read_stream(1000)
                except TimeoutError:
                    continue
                self._route_stream_chunk(session, chunk)
                if dvb_event_scanner is not None and not dvb_event_scanner.is_alive():
                    # DVB event scanner has stopped unexpectedly, stop feeding it chunks
                    dvb_event_scan_queue = None
                if dvb_event_scan_queue is not None:
                    self._enqueue_dvb_event_scan_chunk(dvb_event_scan_queue, chunk)
        except Exception as error:
            session.startup_error = error
            session.stop_event.set()
            logger.error(
                "Multiplex stream failed at %s Hz: %s: %s",
                session.tuning.frequency_hz,
                type(error).__name__,
                error,
            )
        finally:
            dvb_event_scan_stop.set()
            if dvb_event_scanner is not None:
                dvb_event_scanner.join(timeout=1.0)
            session.ready_event.set()
            try:
                session.adapter.stop()
            except (RuntimeError, OSError):
                pass
            try:
                session.adapter.sleep()
            except (RuntimeError, OSError):
                pass
            with self._adapters_condition:
                doublet_id = session.stream_info.doublet_id
                if doublet_id is not None and self._sessions.get(doublet_id) is session:
                    del self._sessions[doublet_id]
                if session in self._active_sessions:
                    self._active_sessions.remove(session)
                session.subscribers.clear()
                session.pid_routes = {}
                self._available_adapters.append(session.adapter)
                self._adapters_condition.notify_all()
            session.done_event.set()

    def _run_dvb_event_scanner(
        self,
        session: _MultiplexSession,
        scan_queue: Queue[bytes],
        stop_event: Event,
    ) -> None:
        """Consume queued multiplex chunks and update mapped service DVB events (i.e., programs)."""
        try:
            scanner = DVBEventScanner(session.stream_info)
            while not stop_event.is_set():
                try:
                    chunk = scan_queue.get(timeout=0.1)
                except Empty:
                    continue
                scanner.feed(chunk)
        except Exception as error:
            logger.error(
                "Event scan failed for multiplex %s: %s: %s",
                session.stream_info.doublet_id,
                type(error).__name__,
                error,
            )

    @staticmethod
    def _enqueue_dvb_event_scan_chunk(scan_queue: Queue[bytes], chunk: bytes) -> None:
        """Enqueue a chunk without blocking stream delivery, dropping stale scan data."""
        try:
            scan_queue.put_nowait(chunk)
        except Full:
            try:
                scan_queue.get_nowait()
            except Empty:
                pass
            try:
                scan_queue.put_nowait(chunk)
            except Full:
                logger.debug("Dropping transport-stream chunk from event scan queue")

    def _route_stream_chunk(self, session: _MultiplexSession, chunk: bytes) -> None:
        packet_size = 188
        packet_end = len(chunk) - len(chunk) % packet_size
        routes = session.pid_routes
        outputs: dict[_StreamSubscriber, bytearray] = {}
        data = memoryview(chunk)
        for offset in range(0, packet_end, packet_size):
            pid = ((data[offset + 1] & 0x1F) << 8) | data[offset + 2]
            for subscriber in routes.get(pid, ()):
                output = outputs.get(subscriber)
                if output is None:
                    output = bytearray()
                    outputs[subscriber] = output
                output.extend(data[offset : offset + packet_size])

        for subscriber, output in outputs.items():
            self._enqueue_output(subscriber, bytes(output))

    @staticmethod
    def _enqueue_output(subscriber: _StreamSubscriber, output: bytes) -> None:
        try:
            subscriber.output.put_nowait(output)
        except Full:
            try:
                subscriber.output.get_nowait()
            except Empty:
                pass
            try:
                subscriber.output.put_nowait(output)
            except Full:
                pass

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
    def _log(message: str, *, level: int = logging.DEBUG) -> None:
        """Log a handler diagnostic at the requested level."""
        logger.log(level, message)

    def do_GET(self) -> None:
        owner = cast(_HTTPServer, self.server).owner
        path = urlsplit(self.path).path
        logger.info(
            "HTTP GET from %s:%s path=%r",
            self.client_address[0],
            self.client_address[1],
            path,
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
                self._log(
                    f"Rejecting playlist request with invalid Host header: {authority!r}",
                    level=logging.WARNING,
                )
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
            self._log(f"Rejecting unknown HTTP path: {path!r}", level=logging.WARNING)
            self.send_error(404, "stream endpoint is /service/X.Y.Z")
            return

        try:
            triplet = DVBTripletID.from_dotted(path.removeprefix(prefix))
        except (TypeError, ValueError):
            self._log(f"Rejecting malformed service path: {path!r}", level=logging.WARNING)
            self.send_error(400, "service path must be /service/X.Y.Z")
            return
        self._log(f"Resolved service triplet: {triplet}")

        try:
            tuning = owner.service_map.tunings[triplet]
            service = owner.service_map.services[triplet]
            stream_info = owner.service_map.stream_infos[triplet]
        except KeyError:
            self._log(
                f"Service triplet not found in service map: {triplet}",
                level=logging.WARNING,
            )
            self.send_error(404, "service was not found")
            return
        self._log(
            f"Service lookup succeeded: name={service.name!r} "
            f"frequency_hz={tuning.frequency_hz} bandwidth_hz={tuning.bandwidth_hz}"
        )

        client_address = f"{self.client_address[0]}:{self.client_address[1]}"
        subscription = owner._subscribe(
            tuning,
            stream_info,
            service.pids(),
            peer=client_address,
            service_name=service.name or "Unnamed service",
            service_id=triplet.dotted(),
        )
        if subscription is None:
            self._log("No adapter available for service request", level=logging.WARNING)
            self.send_error(503, "no adapter is currently available")
            return
        session, subscriber = subscription

        self._log(f">>> Client connected: {client_address}", level=logging.INFO)
        chunk_count = 0
        byte_count = 0
        timeout_count = 0
        try:
            session.ready_event.wait()
            if session.startup_error is not None:
                self._log(
                    f"Could not start multiplex stream: {session.startup_error}",
                    level=logging.WARNING,
                )
                self.send_error(503, "adapter could not start the requested multiplex")
                return
            if session.stop_event.is_set():
                self.send_error(503, "multiplex stream is stopping")
                return

            self.protocol_version = "HTTP/1.1"
            self.send_response(200)
            self.send_header("Content-Type", "video/mp2t")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            self._log("Sent HTTP 200 video/mp2t response headers")
            while not owner._stop_event.is_set() and not session.stop_event.is_set():
                if self._client_disconnected():
                    self._log("Client disconnected before next stream read")
                    break
                try:
                    chunk = subscriber.output.get(timeout=0.1)
                except Empty:
                    if session.done_event.is_set():
                        break
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
                owner._record_client_bytes(session, subscriber, len(chunk))
        except (BrokenPipeError, ConnectionResetError) as error:
            self._log(f"Client connection closed while streaming: {error!r}")
        except OSError as error:
            self._log(
                f"Streaming request failed: {type(error).__name__}: {error}",
                level=logging.ERROR,
            )
        finally:
            owner._unsubscribe(session, subscriber)
            self._log(
                f"<<< Client disconnected: {client_address}; "
                f"chunks={chunk_count} bytes={byte_count} read_timeouts={timeout_count}",
                level=logging.INFO,
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