"""HTTP streaming server controller."""

from __future__ import annotations

import logging
from threading import Thread
from typing import TYPE_CHECKING

from PySide6.QtCore import QObject, Property, Signal, Slot

from pydvb.server import LocalTS_HTTPServer

if TYPE_CHECKING:
    from ..app_controller import AppController

logger = logging.getLogger("pydvb.gui.app_controller")


class ServerController(QObject):
    """Expose HTTP server settings, lifecycle, and connected-client status."""

    serverChanged = Signal()
    serverDetailsChanged = Signal()

    def __init__(self, app: AppController) -> None:
        super().__init__(app)
        self._app = app
        app.serverChanged.connect(self.serverChanged.emit)
        app.serverChanged.connect(self.serverDetailsChanged.emit)
        app.devicesChanged.connect(self.serverDetailsChanged.emit)

    @Property(str, notify=serverChanged)
    def serverState(self) -> str:
        return self._app._server_state

    @Property(str, notify=serverChanged)
    def serverAddress(self) -> str:
        server = self._app._server
        if server is None:
            return "Not listening"
        host, port = server.address
        return f"{host}:{port}"

    @Property(str, notify=serverChanged)
    def playlistUrl(self) -> str:
        server = self._app._server
        if server is None:
            return ""
        host, port = server.address
        return f"http://{host}:{port}/playlist.m3u8"

    @Property(str, notify=serverChanged)
    def streamBaseUrl(self) -> str:
        """Return a locally reachable base URL for individual service streams."""
        server = self._app._server
        if server is None:
            return ""
        host, port = server.address
        if host in ("", "0.0.0.0"):
            host = "127.0.0.1"
        elif host == "::":
            host = "[::1]"
        elif ":" in host and not host.startswith("["):
            host = f"[{host}]"
        return f"http://{host}:{port}"

    @Property("QVariantList", notify=serverDetailsChanged)
    def serverClients(self) -> list[dict[str, object]]:
        server = self._app._server
        if server is None:
            return []
        client_statuses = getattr(server, "client_statuses", ())
        service_map = self._app._profile.service_map
        services_by_id = (
            {
                triplet_id.dotted(): service
                for triplet_id, service in service_map.services.items()
            }
            if service_map is not None
            else {}
        )
        clients = []
        for client in client_statuses:
            service_id = getattr(client, "service_id", "")
            service = services_by_id.get(service_id)
            clients.append({
                "peer": getattr(client, "peer", "Unknown client"),
                "service": getattr(client, "service_name", "Unknown service"),
                "serviceId": service_id,
                "bytesSent": getattr(client, "bytes_sent", 0),
                "lcn": (
                    service.logical_channel_number
                    if service is not None
                    else None
                ),
            })
        return clients

    @Property(str, notify=serverDetailsChanged)
    def serverSignalSummary(self) -> str:
        app = self._app
        active = [
            f"Device {device_index + 1} / Adapter {adapter_index}: "
            f"{app._signal_by_adapter.get((id(device), adapter_index), 'Not polled')}"
            for device_index, device in enumerate(app._devices)
            for adapter_index in range(len(device.adapters))
        ]
        return "; ".join(active) if active else "No adapter signal readings"

    @Property(str, notify=serverChanged)
    def serverHost(self) -> str:
        return self._app._profile.server_host

    @serverHost.setter
    def serverHost(self, value: str) -> None:
        value = value.strip()
        if value:
            app = self._app
            app._profile.server_host = value
            app.serverChanged.emit()

    @Property(int, notify=serverChanged)
    def serverPort(self) -> int:
        return self._app._profile.server_port

    @serverPort.setter
    def serverPort(self, value: int) -> None:
        if 0 <= value <= 65535:
            app = self._app
            app._profile.server_port = value
            app.serverChanged.emit()

    @Slot()
    def startServer(self) -> None:
        """Start the server after device-lane work finishes, rechecking prerequisites."""
        app = self._app
        if app._server is not None:
            app._set_status("Server is already running")
            return
        if app._server_state != "Stopped":
            app._set_status(f"Server is {app._server_state.lower()}")
            return
        if app._profile.service_map is None or not app._profile.service_map.services:
            app._set_status("Scan or load a service map before starting the server")
            return
        adapters = [adapter for device in app._devices for adapter in device.adapters]
        if not adapters:
            app._set_status("Initialize at least one device before starting the server")
            return
        app._server_state = "Starting"
        app.serverChanged.emit()
        app._set_status("Starting HTTP server...")

        def start_when_ready(_result: object) -> None:
            if app._shutting_down:
                app._server_state = "Stopped"
                app.serverChanged.emit()
                return
            service_map = app._profile.service_map
            adapters = [adapter for device in app._devices for adapter in device.adapters]
            if service_map is None or not service_map.services:
                app._server_state = "Stopped"
                app.serverChanged.emit()
                app._set_status("Scan or load a service map before starting the server")
                return
            if not adapters:
                app._server_state = "Stopped"
                app.serverChanged.emit()
                app._set_status("Initialize at least one device before starting the server")
                return
            try:
                server = LocalTS_HTTPServer(
                    adapters,
                    service_map,
                    host=app._profile.server_host,
                    port=app._profile.server_port,
                )
                server.start()
                thread = Thread(
                    target=server.serve_forever,
                    name="pydvb-http-server",
                    daemon=True,
                )
                thread.start()
            except (OSError, RuntimeError, ValueError) as error:
                logger.exception("Could not start HTTP server")
                app._server_state = "Stopped"
                app.serverChanged.emit()
                app._set_status(f"Could not start server: {error}")
                return
            app._server = server
            app._server_thread = thread
            app._server_state = "Running"
            logger.info("HTTP server listening at %s", self.serverAddress)
            app.serverChanged.emit()
            app._set_status(f"Server listening at {self.serverAddress}")

        if not app._run_task(
            lambda: None,
            start_when_ready,
            lane="device",
        ):
            app._server_state = "Stopped"
            app.serverChanged.emit()

    @Slot()
    def stopServer(self) -> None:
        """Stop HTTP handlers and multiplex sessions before device shutdown."""
        app = self._app
        server = app._server
        thread = app._server_thread
        if server is None:
            app._set_status("Server is already stopped")
            return
        app._server_state = "Stopping"
        logger.info("Stopping HTTP server")
        app.serverChanged.emit()

        def operation() -> None:
            server.shutdown()
            server.close()
            if thread is not None:
                thread.join(timeout=3.0)

        def complete(_result: object) -> None:
            app._server = None
            app._server_thread = None
            app._server_state = "Stopped"
            logger.info("HTTP server stopped")
            app.serverChanged.emit()
            app._set_status("Server stopped")

        if not app._run_task(operation, complete, lane="device"):
            app._server_state = "Running"
            app.serverChanged.emit()
