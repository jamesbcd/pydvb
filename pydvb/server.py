"""Serve mapped DVB services as live MPEG transport streams over HTTP.

The root and ``/playlist.m3u8`` endpoints return an M3U playlist; each
``/service/X.Y.Z`` endpoint streams the selected service as chunked
``video/mp2t``. Clients for services on the same multiplex share one tuned
adapter session, while each client receives only its service's transport
stream PIDs. Bounded per-client queues prevent a slow client from blocking
other subscribers; when full, the oldest queued block is discarded.

The session producer can also feed a separate bounded queue to a DVB EIT
scanner (i.e. program guide information). Scans run in bounded bursts
with configurable pauses so event discovery does not block stream delivery.

Module constants define adapter lease retry timing and event-scanner queue,
history, burst-duration, and pause defaults.
"""

from __future__ import annotations

from collections.abc import Collection
from collections import deque
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import logging
from math import isfinite
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
from .stream_scanner import (
    DVBEvent,
    DVBEventScanner,
    DVBDoubletID,
    DVBStreamInfo,
    DVBTripletID,
)


logger = logging.getLogger(__name__)

ADAPTER_LEASE_RETRY_TIMEOUT = 2.0
ADAPTER_LEASE_RETRY_INTERVAL = 0.1
EVENT_SCAN_QUEUE_SIZE = 8
EVENT_SCAN_DURATION_HISTORY_SIZE = 10
EVENT_SCAN_MAX_DURATION_SECONDS = 10.0
EVENT_SCAN_PAUSE_SECONDS = 150.0


@dataclass(eq=False)
class _StreamSubscriber:
    """Track one service client and queue its filtered transport-stream data.

    Attributes:
        pids: Transport-stream packet identifiers to forward to this client.
        output: Bounded queue of filtered byte blocks waiting for the client.
        peer: Remote address formatted as ``host:port``, if known.
        service_name: Display name used by client-status reporting.
        service_id: Dotted DVB triplet identifying the selected service.
        connected_at: Monotonic time when this subscriber was created.
        bytes_sent: Number of payload bytes successfully written to the client.
    """

    pids: frozenset[int]
    output: Queue[bytes]
    peer: str = ""
    service_name: str = ""
    service_id: str = ""
    connected_at: float = field(default_factory=monotonic)
    bytes_sent: int = 0


@dataclass(frozen=True)
class HTTPClientStatus:
    """Immutable snapshot of one connected HTTP stream client.

    Attributes:
        peer: Remote address formatted as ``host:port``.
        service_name: Display name of the subscribed service.
        service_id: Dotted DVB triplet identifying the service.
        bytes_sent: Payload bytes written successfully to the client.
        connected_seconds: Elapsed connection time at snapshot creation.
    """

    peer: str
    service_name: str
    service_id: str
    bytes_sent: int
    connected_seconds: float


@dataclass(eq=False)
class _MultiplexSession:
    """Own one adapter tuned to a multiplex and its service subscribers.

    Attributes:
        adapter: Adapter leased for this multiplex until the producer exits.
        tuning: Tuning parameters used when starting the session.
        stream_info: Services and multiplex identity associated with the stream.
        subscribers: Live clients receiving services from this multiplex.
        pid_routes: Snapshot routing each packet PID to interested subscribers.
        stop_event: Signals the producer to stop and release the adapter.
        ready_event: Signals that startup succeeded or failed.
        done_event: Signals that cleanup and adapter release have completed.
        startup_error: Startup/producer exception, if one was raised.
        producer: Thread reading and distributing transport-stream chunks.
    """

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
    """Serve mapped DVB services using a pool of available adapters.

    The listening socket is bound during construction. Call
    :meth:`serve_forever` to process requests, :meth:`shutdown` to stop that
    loop from another thread, and :meth:`close` to stop stream sessions and
    release the socket.

    Attributes:
        service_map: Service, tuning, and multiplex information used by routes.
        client_queue_size: Maximum queued output blocks retained per client.
        buffer_log: Retained compatibility/configuration flag; it does not
            currently change stream processing.
        scan_dvb_events: Whether multiplex sessions scan DVB event information.
        event_scan_pause_seconds: Pause between event-scan bursts.
        event_scan_max_duration_seconds: Maximum duration of one scan burst.
        evict_oldest_on_exhaustion: Whether a new multiplex request stops the
            oldest active multiplex when all adapters are leased.
        httpd: Underlying threaded HTTP server bound to the requested address.
        _available_adapters: Adapters not currently leased to a session.
        _sessions: Active session by multiplex identity, for session reuse.
        _active_sessions: Active sessions in creation order, used for eviction.
        _adapters_condition: Lock/condition protecting adapter and session state.
        _stop_event: Signals the server and its request handlers to stop streams.
    """

    def __init__(
        self,
        adapters: Collection[DVBAdapter],
        service_map: DVBServiceMap,
        host: str = "127.0.0.1",
        port: int = 8080,
        client_queue_size: int = 20,
        buffer_log: bool = False,
        scan_dvb_events: bool = True,
        # When evict_oldest_on_exhaustion is True, the server stops the oldest
        # multiplex session, disconnecting all subscribers using that adapter.
        # When False, the server will wait for an adapter to become available, potentially timing out if
        # none are free.
        evict_oldest_on_exhaustion: bool = False,
        event_scan_pause_seconds: float = EVENT_SCAN_PAUSE_SECONDS,
        event_scan_max_duration_seconds: float = EVENT_SCAN_MAX_DURATION_SECONDS,
    ) -> None:
        """Bind an HTTP listener and configure adapter and event-scan policies.

        Args:
            adapters: Adapters available to serve multiplexes.
            service_map: Mapping from DVB services to tuning and stream data.
            host: Interface address on which to bind the HTTP listener.
            port: TCP port to bind; ``0`` asks the operating system to select it.
            client_queue_size: Positive maximum output blocks buffered per client.
            buffer_log: Compatibility option retained for callers; currently
                has no effect on stream processing.
            scan_dvb_events: Whether to scan event information from stream data.
            evict_oldest_on_exhaustion: Stop the oldest active multiplex when
                all adapters are leased instead of only waiting for an adapter.
            event_scan_pause_seconds: Non-negative pause between scan bursts.
            event_scan_max_duration_seconds: Positive time limit per scan burst.

        Raises:
            ValueError: If a port, queue size, or event-scan option is invalid.
            OSError: If the listening socket cannot be bound.
        """
        if not 0 <= port <= 65535:
            raise ValueError("port must be between 0 and 65535")
        if client_queue_size <= 0:
            raise ValueError("client_queue_size must be positive")
        if type(scan_dvb_events) is not bool:
            raise ValueError("scan_dvb_events must be a bool")
        if (
            isinstance(event_scan_pause_seconds, bool)
            or not isinstance(event_scan_pause_seconds, (int, float))
            or not isfinite(event_scan_pause_seconds)
            or event_scan_pause_seconds < 0
        ):
            raise ValueError("event_scan_pause_seconds must be a finite non-negative number")
        if (
            isinstance(event_scan_max_duration_seconds, bool)
            or not isinstance(event_scan_max_duration_seconds, (int, float))
            or not isfinite(event_scan_max_duration_seconds)
            or event_scan_max_duration_seconds <= 0
        ):
            raise ValueError(
                "event_scan_max_duration_seconds must be a finite positive number"
            )
        self.service_map = service_map
        self._available_adapters = list(adapters)
        self._sessions: dict[DVBDoubletID, _MultiplexSession] = {}
        self._active_sessions: list[_MultiplexSession] = []
        self._adapters_condition = Condition(Lock())
        self.client_queue_size = client_queue_size
        self.buffer_log = buffer_log
        self.scan_dvb_events = scan_dvb_events
        self.event_scan_pause_seconds = event_scan_pause_seconds
        self.event_scan_max_duration_seconds = event_scan_max_duration_seconds
        self.evict_oldest_on_exhaustion = evict_oldest_on_exhaustion
        self._stop_event = Event()
        self.httpd = _HTTPServer((host, port), _StreamRequestHandler, self)

    @property
    def address(self) -> tuple[str, int]:
        """Return the bound host and port, including an OS-selected port."""
        address = self.httpd.server_address
        return cast(str, address[0]), address[1]

    @property
    def client_statuses(self) -> tuple[HTTPClientStatus, ...]:
        """Return immutable status snapshots for current stream subscribers.

        The snapshot is collected while holding the session-state lock; later
        subscriber changes do not mutate the returned tuple or its entries.
        """
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
        """Retain the lifecycle hook expected by callers; no action is needed.

        The socket is bound during construction and the request loop is started
        separately by :meth:`serve_forever`.
        """

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
        """Join or create the session for a multiplex and add a subscriber.

        Adapter acquisition waits for at most
        :data:`ADAPTER_LEASE_RETRY_TIMEOUT`, waking periodically as adapters
        are released. Requests for the same running multiplex share its
        producer; if configured, exhaustion can instead stop the oldest
        active multiplex. Returns ``None`` if acquisition times out, shutdown
        begins, or the producer thread cannot be started.

        The adapter/session condition must protect all changes to shared
        adapter and session bookkeeping.
        """
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
        """Create and register a subscriber while holding the session lock."""
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
        """Rebuild the session's immutable per-PID subscriber routing snapshot.

        Call while holding the adapter/session condition so routing changes
        are published consistently with subscriber membership changes.
        """
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
        """Remove a client and stop its session when it was the last subscriber."""
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
        """Add successfully written payload bytes to a live client's telemetry.

        Updates are ignored if the subscriber has already been removed.
        """
        with self._adapters_condition:
            if subscriber in session.subscribers:
                subscriber.bytes_sent += count

    def _run_session(self, session: _MultiplexSession) -> None:
        """Tune an adapter, distribute chunks, and release all session resources.

        Startup failures are stored on the session for waiting request handlers
        and logged. The ``finally`` block signals readiness, stops optional
        event scanning, stops and sleeps the adapter, removes session state,
        returns the adapter to the pool, and signals completion.
        """
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

            def sync_dvb_event_scanner() -> None:
                """Start, retain, or stop the optional event-scanner worker."""
                nonlocal dvb_event_scan_queue, dvb_event_scan_stop, dvb_event_scanner
                if not session.stream_info.services:
                    return
                if self.scan_dvb_events:
                    if dvb_event_scanner is not None:
                        return
                    dvb_event_scan_queue = Queue(maxsize=EVENT_SCAN_QUEUE_SIZE)
                    dvb_event_scan_stop = Event()
                    dvb_event_scanner = Thread(
                        target=self._run_dvb_event_scanner,
                        args=(session, dvb_event_scan_queue, dvb_event_scan_stop),
                        name=f"dvb-events-{session.stream_info.doublet_id}",
                        daemon=True,
                    )
                    try:
                        dvb_event_scanner.start()
                        logger.info(
                            "Started DVB event scanner for stream ID %s",
                            session.stream_info.doublet_id,
                        )
                    except RuntimeError as error:
                        dvb_event_scan_queue = None
                        dvb_event_scanner = None
                        logger.error("Could not start DVB event scanner: %s", error)
                elif dvb_event_scanner is not None:
                    dvb_event_scan_stop.set()
                    dvb_event_scanner.join(timeout=1.0)
                    dvb_event_scan_queue = None
                    dvb_event_scanner = None

            session.ready_event.set()
            while not self._stop_event.is_set() and not session.stop_event.is_set():
                try:
                    chunk = session.adapter.read_stream(1000)
                except TimeoutError:
                    sync_dvb_event_scanner()
                    continue
                sync_dvb_event_scanner()
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
                "Multiplex stream failed for multiplex %s: %s: %s",
                session.stream_info.doublet_id,
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
        """Scan queued chunks in bounded bursts with pauses between bursts.

        A burst ends when all services have event data, the duration limit is
        reached, or shutdown is requested. Chunks accumulated during a pause
        are discarded before the next burst so scanning resumes on recent data.
        Scanner errors are logged without terminating the stream producer.
        """
        scan_durations: deque[float] = deque(maxlen=EVENT_SCAN_DURATION_HISTORY_SIZE)
        try:
            while not stop_event.is_set():
                scanner = DVBEventScanner(
                    session.stream_info,
                    on_event_ready=self._update_service_events,
                )
                chunks_received = 0
                bytes_received = 0
                started_at = monotonic()
                last_progress_log = started_at

                while not stop_event.is_set() and not scanner.events_complete:
                    remaining = (
                        self.event_scan_max_duration_seconds
                        - (monotonic() - started_at)
                    )
                    if remaining <= 0:
                        break
                    try:
                        chunk = scan_queue.get(timeout=min(0.1, remaining))
                    except Empty:
                        chunk = None
                    if chunk is not None:
                        chunks_received += 1
                        bytes_received += len(chunk)
                        scanner.feed(chunk)
                    now = monotonic()
                    if now - last_progress_log >= 30:
                        self._log_event_scan_progress(
                            "progress",
                            session,
                            scanner,
                            chunks_received,
                            bytes_received,
                        )
                        last_progress_log = now

                if stop_event.is_set():
                    break

                duration = monotonic() - started_at
                completed = (
                    scanner.events_complete
                    and duration <= self.event_scan_max_duration_seconds
                )
                if completed:
                    scan_durations.append(duration)
                    typical_duration = sum(scan_durations) / len(scan_durations)
                    self._log_event_scan_progress(
                        "completed",
                        session,
                        scanner,
                        chunks_received,
                        bytes_received,
                    )
                    logger.info(
                        "DVB EIT scan duration for multiplex %s: %.3f seconds "
                        "(recent average %.3f seconds over %d completed bursts)",
                        session.stream_info.doublet_id,
                        duration,
                        typical_duration,
                        len(scan_durations),
                    )
                else:
                    self._log_event_scan_progress(
                        "incomplete",
                        session,
                        scanner,
                        chunks_received,
                        bytes_received,
                    )
                    pending_services = ", ".join(
                        str(triplet_id)
                        for triplet_id in sorted(scanner.pending_services)
                    )
                    logger.warning(
                        "DVB EIT scan timed out for multiplex %s after %.3f seconds: "
                        "%d/%d services ready; pending services: %s",
                        session.stream_info.doublet_id,
                        duration,
                        len(scanner.events) - len(scanner.pending_services),
                        len(scanner.events),
                        pending_services or "none",
                    )

                if stop_event.wait(self.event_scan_pause_seconds):
                    break
                self._discard_dvb_event_scan_chunks(scan_queue)
        except Exception as error:
            logger.error(
                "Event scan failed for multiplex %s: %s: %s",
                session.stream_info.doublet_id,
                type(error).__name__,
                error,
            )
        finally:
            logger.info(
                "DVB EIT scan stopped for multiplex %s",
                session.stream_info.doublet_id,
            )

    @staticmethod
    def _log_event_scan_progress(
        status: str,
        session: _MultiplexSession,
        scanner: DVBEventScanner,
        chunks_received: int,
        bytes_received: int,
    ) -> None:
        """Log packet, section, service-match, readiness, and update counters."""
        total_services = len(scanner.events)
        ready_services = total_services - len(scanner.pending_services)
        logger.info(
            "DVB EIT scan %s for multiplex %s: chunks=%d bytes=%d "
            "PID 0x12 packets=%d present/following sections=%d "
            "matched services=%d ready services=%d/%d ONID fallbacks=%d "
            "unmapped services=%d event updates=%d",
            status,
            session.stream_info.doublet_id,
            chunks_received,
            bytes_received,
            scanner.eit_packets_seen,
            scanner.eit_sections_seen,
            len(scanner.matched_eit_services),
            ready_services,
            total_services,
            len(scanner.onid_mismatch_eit_services),
            len(scanner.unmatched_eit_services),
            scanner.event_updates,
        )

    def _update_service_events(
        self,
        triplet_id: DVBTripletID,
        current_event: DVBEvent | None,
        next_event: DVBEvent | None,
    ) -> None:
        """Replace the mapped service's current and next event snapshots.

        A scanner result for a service absent from the current service map is
        ignored after a warning.
        """
        service = self.service_map.services.get(triplet_id)
        if service is None:
            logger.warning("Cannot apply DVB events for unmapped service %s", triplet_id)
            return
        service.current_event = current_event
        service.next_event = next_event

    @staticmethod
    def _discard_dvb_event_scan_chunks(scan_queue: Queue[bytes]) -> None:
        """Drain currently queued chunks, preserving any future arrivals."""
        for _ in range(scan_queue.qsize()):
            try:
                scan_queue.get_nowait()
            except Empty:
                return

    @staticmethod
    def _enqueue_dvb_event_scan_chunk(scan_queue: Queue[bytes], chunk: bytes) -> None:
        """Queue a scan chunk without blocking stream delivery.

        On overflow, discard one oldest chunk and retry once with the newest
        chunk; if the retry still fails, drop the new chunk as well.
        """
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
        """Filter complete 188-byte TS packets and queue blocks for each client.

        Trailing bytes that do not form a complete packet are ignored. Each
        subscriber's interested packets are coalesced into one block per input
        chunk before entering its bounded output queue.
        """
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
        """Queue a client's output block, dropping the oldest block on overflow."""
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
        """Build a UTF-8 M3U playlist rooted at ``base_url``.

        Services are emitted in service-map order; entries without a triplet
        are skipped, missing channel numbers use ``---``, and missing names use
        ``Unnamed service``.
        """
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
    """Threaded listener that gives request handlers access to its owner.

    Attributes:
        owner: :class:`LocalTS_HTTPServer` coordinating requests and streams.
    """

    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, server_address, request_handler, owner) -> None:
        """Store the owning service and initialize the threaded listener."""
        self.owner = owner
        super().__init__(server_address, request_handler)


class _StreamRequestHandler(BaseHTTPRequestHandler):
    """Handle playlist and filtered-service GET requests.

    The base handler supplies the request, connection, client address, and
    server references. For streams, this handler sets HTTP/1.1 and writes
    payloads using HTTP chunked transfer encoding.
    """

    server: BaseServer

    def send_error(
        self,
        code: int,
        message: str | None = None,
        explain: str | None = None,
    ) -> None:
        """Send an HTTP error unless the client has already disconnected.

        Broken pipes and connection resets during error delivery are ignored
        because there is no remaining peer to receive the response.
        """
        try:
            super().send_error(code, message, explain)
        except (BrokenPipeError, ConnectionResetError):
            return

    @staticmethod
    def _log(message: str, *, level: int = logging.DEBUG) -> None:
        """Write a handler diagnostic through the module logger."""
        logger.log(level, message)

    def do_GET(self) -> None:
        """Serve a playlist or stream one mapped service.

        Playlist paths validate the ``Host`` authority before using it to
        construct absolute URLs. Service paths parse a dotted DVB triplet,
        resolve its service/tuning/multiplex, and subscribe to the shared
        session. The response is chunked MPEG-TS containing only that
        subscriber's service PIDs. Invalid, missing, or unavailable resources
        receive the corresponding HTTP error response.
        """
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
        """Return whether a non-blocking socket peek observes peer EOF.

        Socket readiness without EOF is treated as still connected; transient
        non-blocking and socket errors are likewise left for normal writes to
        report.
        """
        try:
            readable, _, _ = select.select([self.connection], [], [], 0)
            if not readable:
                return False
            data = self.connection.recv(1, 0x02 | 0x40)
        except (BlockingIOError, ConnectionResetError, OSError):
            return False
        return data == b""

    def log_message(self, format: str, *_args: object) -> None:
        """Suppress the base handler's default stderr access logging."""
        return