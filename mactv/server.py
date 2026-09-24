"""HTTP serving for live MPEG transport streams."""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from queue import Empty, Full, Queue
from socketserver import BaseServer
from threading import Event, Lock, Thread
from typing import cast

BUFFER_LOG_ON_CHANGE = True


class LocalTS_HTTPServer:
    """Serve one transport stream to one or more HTTP clients.

    The broadcaster reads directly from the adapter. Each connected HTTP
    client receives its own bounded queue, so a slow client cannot block the
    USB reader or other clients.
    """

    def __init__(
        self,
        adapter,
        host: str = "127.0.0.1",
        port: int = 8080,
        client_queue_size: int = 20,
        buffer_log: bool = False,
    ) -> None:
        if not 0 <= port <= 65535:
            raise ValueError("port must be between 0 and 65535")
        if client_queue_size <= 0:
            raise ValueError("client_queue_size must be positive")
        self.adapter = adapter
        self.client_queue_size = client_queue_size
        self.buffer_log = buffer_log
        self._clients: set[Queue[bytes | None]] = set()
        self._clients_lock = Lock()
        self._stop_event = Event()
        self._broadcaster: Thread | None = None
        self.httpd = _HTTPServer((host, port), _StreamRequestHandler, self)

    @property
    def address(self) -> tuple[str, int]:
        """Return the address selected by the operating system."""
        address = self.httpd.server_address
        return cast(str, address[0]), address[1]

    def start(self) -> None:
        """Start the transport stream and its broadcaster."""
        self.adapter.start()
        self._broadcaster = Thread(
            target=self._broadcast,
            name="mactv-ts-broadcaster",
            daemon=True,
        )
        self._broadcaster.start()

    def serve_forever(self) -> None:
        """Serve HTTP requests until ``shutdown`` or ``close`` is called."""
        self.httpd.serve_forever()

    def shutdown(self) -> None:
        """Stop accepting HTTP requests while allowing active handlers to exit."""
        self.httpd.shutdown()

    def close(self) -> None:
        """Stop HTTP serving, the broadcaster, and the adapter."""
        self._stop_event.set()
        self.httpd.server_close()
        self.adapter.stop()
        self._close_clients()
        if self._broadcaster is not None:
            self._broadcaster.join(timeout=2.0)

    def _register_client(self) -> Queue[bytes | None]:
        client = Queue[bytes | None](maxsize=self.client_queue_size)
        with self._clients_lock:
            self._clients.add(client)
        return client

    def _unregister_client(self, client: Queue[bytes | None]) -> None:
        with self._clients_lock:
            self._clients.discard(client)

    def _close_clients(self) -> None:
        with self._clients_lock:
            clients = tuple(self._clients)
            self._clients.clear()
        for client in clients:
            try:
                client.put_nowait(None)
            except Full:
                pass

    def _broadcast(self) -> None:
        try:
            while not self._stop_event.is_set():
                try:
                    chunk = self.adapter.read_stream(1000)
                except TimeoutError:
                    continue
                with self._clients_lock:
                    clients = tuple(self._clients)
                for client in clients:
                    try:
                        client.put_nowait(chunk)
                    except Full:
                        try:
                            client.get_nowait()
                            client.put_nowait(chunk)
                        except (Empty, Full):
                            pass
        finally:
            self._close_clients()


class _HTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, server_address, request_handler, owner) -> None:
        self.owner = owner
        super().__init__(server_address, request_handler)


class _StreamRequestHandler(BaseHTTPRequestHandler):
    server: BaseServer

    def do_GET(self) -> None:
        if self.path != "/stream.ts":
            self.send_error(404, "stream endpoint is /stream.ts")
            return

        owner = cast(_HTTPServer, self.server).owner
        client = owner._register_client()
        client_address = f"{self.client_address[0]}:{self.client_address[1]}"
        print(f"Client connected: {client_address}", flush=True)
        try:
            self.protocol_version = "HTTP/1.1"
            self.send_response(200)
            self.send_header("Content-Type", "video/mp2t")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            while not owner._stop_event.is_set():
                try:
                    chunk = client.get(timeout=1.0)
                except Empty:
                    continue
                if chunk is None:
                    break
                self.wfile.write(f"{len(chunk):X}\r\n".encode("ascii"))
                self.wfile.write(chunk)
                self.wfile.write(b"\r\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            pass
        finally:
            owner._unregister_client(client)
            print(f"Client disconnected: {client_address}", flush=True)

    def log_message(self, format: str, *_args: object) -> None:
        return