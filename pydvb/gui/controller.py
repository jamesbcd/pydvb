"""Qt controller for discovery, tuning, scanning, and HTTP server management."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from math import isfinite
from os import PathLike
from pathlib import Path
from threading import Thread
from typing import Callable, Sequence

from PySide6.QtCore import (
    QObject,
    Property,
    QRunnable,
    QThreadPool,
    QTimer,
    Qt,
    QUrl,
    Signal,
    Slot,
)

from pydvb.device_manager import DeviceConnectionResult, DeviceRemovalFailure, DVBDeviceManager
from pydvb.driver_api.device import DVBDevice
from pydvb.driver_api.frontend import DVBFrontendStatus
from pydvb.driver_api.tuner import DVBTuning
from pydvb.drivers.af9015.device import AF9015Device
from pydvb.scan_tables import known_table_names, parse_table
from pydvb.server import LocalTS_HTTPServer
from pydvb.service_map import DVBServiceMap, map_from_tunings
from pydvb.usb import LIBUSB_ERROR_NO_DEVICE, LibUSBError

from .profile import GuiProfile, load_profile, save_profile

_MAX_LOG_MESSAGES = 1000
logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class _DiscoveryOutcome:
    manager: DVBDeviceManager
    connection_result: DeviceConnectionResult


@dataclass(frozen=True, slots=True)
class _InitializationOutcome:
    failures: tuple[tuple[DVBDevice, Exception], ...]


@dataclass(frozen=True, slots=True)
class _StatusOutcome:
    readings: tuple[tuple[DVBDevice, int, str], ...]
    removed: tuple[DVBDevice, ...]
    removal_failures: tuple[DeviceRemovalFailure, ...]


class _WorkerSignals(QObject):
    finished = Signal(object)
    failed = Signal(str)


class _Worker(QRunnable):
    def __init__(self, operation: Callable[[], object]) -> None:
        super().__init__()
        self.operation = operation
        self.signals = _WorkerSignals()

    def run(self) -> None:
        try:
            result = self.operation()
        except Exception as error:
            self.signals.failed.emit(f"{type(error).__name__}: {error}")
        else:
            self.signals.finished.emit(result)


class AppController(QObject):
    """Own application state and expose asynchronous operations to QML."""

    devicesChanged = Signal()
    tuningsChanged = Signal()
    channelsChanged = Signal()
    knownTablesChanged = Signal()
    serverChanged = Signal()
    stateChanged = Signal()
    logsChanged = Signal()
    logMessageReceived = Signal(str)
    shutdownFinished = Signal()
    _taskSucceeded = Signal(object, object, object)
    _taskFailed = Signal(object, str)

    def __init__(
        self,
        device_types: Sequence[type[DVBDevice]] | None = None,
        profile: GuiProfile | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._device_types = tuple((AF9015Device,) if device_types is None else device_types)
        self._profile = profile or GuiProfile()
        self._manager: DVBDeviceManager | None = None
        self._devices: list[DVBDevice] = []
        self._selected_device_index: int | None = None
        self._discovery_rows: list[dict[str, object]] = []
        self._show_unsupported_devices = False
        self._auto_discover_and_initialize_devices = True
        self._initialization_errors: dict[int, str] = {}
        self._signal_by_adapter: dict[tuple[int, int], str] = {}
        self._known_tables: list[str] = []
        self._log_messages: list[str] = []
        self._busy = False
        self._status_message = "Ready"
        self._server_state = "Stopped"
        self._server: LocalTS_HTTPServer | None = None
        self._server_thread: Thread | None = None
        self._shutting_down = False
        self._shutdown_task_started = False
        self._workers: set[_Worker] = set()
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(1)
        self._taskSucceeded.connect(
            self._finish_task,
            Qt.ConnectionType.QueuedConnection,
        )
        self._taskFailed.connect(
            self._fail_task,
            Qt.ConnectionType.QueuedConnection,
        )
        self.logMessageReceived.connect(
            self._append_log_message,
            Qt.ConnectionType.QueuedConnection,
        )
        self._status_timer = QTimer(self)
        self._status_timer.setInterval(2000)
        self._status_timer.timeout.connect(self._poll_status)
        self._status_timer.start()
        self._discovery_timer = QTimer(self)
        self._discovery_timer.setInterval(2000)
        self._discovery_timer.timeout.connect(self._auto_discover)
        self._discovery_timer.start()

    @Property(bool, notify=stateChanged)
    def busy(self) -> bool:
        return self._busy

    @Property(str, notify=stateChanged)
    def statusMessage(self) -> str:
        return self._status_message

    @Property("QVariantList", notify=logsChanged)
    def logMessages(self) -> list[str]:
        """Return the recent formatted application log messages."""
        return self._log_messages.copy()

    @Property(bool, notify=stateChanged)
    def shuttingDown(self) -> bool:
        return self._shutting_down

    @Property("QVariantList", notify=devicesChanged)
    def devices(self) -> list[dict[str, object]]:
        rows: list[dict[str, object]] = []
        for index, device in enumerate(self._devices):
            device_id = self._device_id(device)
            rows.append(
                {
                    "deviceIndex": index,
                    "deviceId": device_id,
                    "name": device.model_name or "DVB device",
                    "state": (
                        "Initialization failed"
                        if id(device) in self._initialization_errors
                        else "Initialized"
                        if device.adapters
                        else "Connected"
                    ),
                    "adapterCount": len(device.adapters),
                    "signal": (
                        "; ".join(
                            f"Adapter {adapter_index}: "
                            f"{self._signal_by_adapter.get((index, adapter_index), 'Not polled')}"
                            for adapter_index in range(len(device.adapters))
                        )
                        if device.adapters
                        else "Not initialized"
                    ),
                    "selected": index == self._selected_device_index,
                }
            )
        rows.extend(
            row
            for row in self._discovery_rows
            if self._show_unsupported_devices or not row.get("unsupported", False)
        )
        return rows

    @Property(str, notify=devicesChanged)
    def deviceSummary(self) -> str:
        """Summarize connected devices and their initialized adapters."""
        initialized_devices = sum(bool(device.adapters) for device in self._devices)
        active_adapters = sum(len(device.adapters) for device in self._devices)
        return (
            f"{len(self._devices)} device(s) connected; "
            f"{initialized_devices} initialized; {active_adapters} active adapter(s)"
        )

    @Property(bool, notify=devicesChanged)
    def showUnsupportedDevices(self) -> bool:
        return self._show_unsupported_devices

    @showUnsupportedDevices.setter
    def showUnsupportedDevices(self, value: bool) -> None:
        if self._show_unsupported_devices != value:
            self._show_unsupported_devices = value
            self.devicesChanged.emit()

    @Property(bool, notify=stateChanged)
    def autoDiscoverAndInitializeDevices(self) -> bool:
        return self._auto_discover_and_initialize_devices

    @autoDiscoverAndInitializeDevices.setter
    def autoDiscoverAndInitializeDevices(self, value: bool) -> None:
        if self._auto_discover_and_initialize_devices == value:
            return
        self._auto_discover_and_initialize_devices = value
        if value:
            self._discovery_timer.start()
        else:
            self._discovery_timer.stop()
        self.stateChanged.emit()

    @Property("QVariantList", notify=devicesChanged)
    def adapters(self) -> list[dict[str, object]]:
        device = self._selected_device()
        if device is None:
            return []
        return [
            {
                "adapterIndex": index,
                "name": f"Adapter {adapter.adapter_index}",
                "state": "Initialized",
                "signal": self._signal_by_adapter.get((self._selected_device_index or 0, index), "Not polled"),
                "selected": index == self._profile.selected_adapter,
            }
            for index, adapter in enumerate(device.adapters)
        ]

    @Property("QVariantList", notify=tuningsChanged)
    def tunings(self) -> list[dict[str, object]]:
        return [
            {
                "index": index,
                "frequency": tuning.frequency_hz / 1_000_000,
                "bandwidth": tuning.bandwidth_hz // 1_000_000,
            }
            for index, tuning in enumerate(self._profile.tunings)
        ]

    @Property("QVariantList", notify=channelsChanged)
    def channels(self) -> list[dict[str, object]]:
        service_map = self._profile.service_map
        if service_map is None:
            return []
        rows: list[dict[str, object]] = []
        for service in service_map.services_sorted():
            triplet_id = service.triplet_id
            tuning = service_map.tunings.get(triplet_id) if triplet_id is not None else None
            rows.append(
                {
                    "lcn": service.logical_channel_number,
                    "name": service.name or "Unnamed service",
                    "type": getattr(service.service_type, "name", "Other"),
                    "id": triplet_id.dotted() if triplet_id is not None else "Unknown",
                    "frequency": (
                        f"{tuning.frequency_hz / 1_000_000:.3f} MHz"
                        if tuning is not None
                        else "Unknown"
                    ),
                }
            )
        return rows

    @Property(bool, notify=channelsChanged)
    def hasChannelMap(self) -> bool:
        """Report whether a channel map is loaded, even if it has no services."""
        return self._profile.service_map is not None

    @Property("QVariantList", notify=knownTablesChanged)
    def knownTables(self) -> list[str]:
        return self._known_tables

    @Property(str, notify=serverChanged)
    def serverState(self) -> str:
        return self._server_state

    @Property(str, notify=serverChanged)
    def serverAddress(self) -> str:
        if self._server is None:
            return "Not listening"
        host, port = self._server.address
        return f"{host}:{port}"

    @Property(str, notify=serverChanged)
    def playlistUrl(self) -> str:
        if self._server is None:
            return ""
        host, port = self._server.address
        return f"http://{host}:{port}/playlist.m3u8"

    @Property("QVariantList", notify=serverChanged)
    def serverClients(self) -> list[dict[str, object]]:
        if self._server is None:
            return []
        client_statuses = getattr(self._server, "client_statuses", ())
        return [
            {
                "peer": getattr(client, "peer", "Unknown client"),
                "service": getattr(client, "service_name", "Unknown service"),
                "serviceId": getattr(client, "service_id", ""),
                "bytesSent": getattr(client, "bytes_sent", 0),
            }
            for client in client_statuses
        ]

    @Property(str, notify=serverChanged)
    def serverSignalSummary(self) -> str:
        active = [
            f"Device {device_index + 1} / Adapter {adapter_index}: "
            f"{self._signal_by_adapter.get((device_index, adapter_index), 'Not polled')}"
            for device_index, device in enumerate(self._devices)
            for adapter_index in range(len(device.adapters))
        ]
        return "; ".join(active) if active else "No adapter signal readings"

    @Property(str, notify=serverChanged)
    def serverHost(self) -> str:
        return self._profile.server_host

    @serverHost.setter
    def serverHost(self, value: str) -> None:
        value = value.strip()
        if value:
            self._profile.server_host = value
            self.serverChanged.emit()

    @Property(int, notify=serverChanged)
    def serverPort(self) -> int:
        return self._profile.server_port

    @serverPort.setter
    def serverPort(self, value: int) -> None:
        if 0 <= value <= 65535:
            self._profile.server_port = value
            self.serverChanged.emit()

    @Slot()
    def discover(self) -> None:
        """Discover new receivers, initializing them when automatic mode is enabled."""
        if self._server is not None:
            self._set_status("Stop the server before rediscovering devices")
            return
        self._discover_devices(
            auto_initialize=self._auto_discover_and_initialize_devices,
            automatic=False,
        )

    @Slot()
    def _auto_discover(self) -> None:
        """Poll for newly connected receivers while automatic discovery is enabled."""
        if (
            not self._auto_discover_and_initialize_devices
            or self._server is not None
            or self._busy
            or self._shutting_down
        ):
            return
        self._discover_devices(auto_initialize=True, automatic=True)

    def _discover_devices(self, *, auto_initialize: bool, automatic: bool) -> None:
        """Connect newly discovered devices before optionally initializing them."""
        if not automatic:
            self._set_status("Discovering USB DVB devices...")

        def operation() -> _DiscoveryOutcome:
            manager = self._manager or DVBDeviceManager(self._device_types)
            connection_result = manager.connect_new()
            return _DiscoveryOutcome(manager, connection_result)

        def complete(result: object) -> None:
            if not isinstance(result, _DiscoveryOutcome):
                raise TypeError("discovery worker returned an unexpected result")
            self._manager = result.manager
            self._remove_devices(result.connection_result.removed)
            self._devices.extend(result.connection_result.connected)
            self._discovery_rows = [
                {
                    "deviceIndex": -1,
                    "deviceId": self._usb_id(failure.device.device_id),
                    "name": "Supported DVB device",
                    "state": f"Unavailable: {failure.error}",
                    "adapterCount": 0,
                    "signal": "Not available",
                    "selected": False,
                }
                for failure in result.connection_result.unavailable
            ]
            self._discovery_rows.extend(
                {
                    "deviceIndex": -1,
                    "deviceId": self._usb_id(device.device_id),
                    "name": "Unsupported USB device",
                    "state": "No registered driver",
                    "adapterCount": 0,
                    "signal": "Not available",
                    "selected": False,
                    "unsupported": True,
                }
                for device in result.connection_result.unsupported
            )
            self._signal_by_adapter.clear()
            self._select_first_device()
            self.devicesChanged.emit()

            def set_discovery_status(
                initialization_failures: tuple[tuple[DVBDevice, Exception], ...] = (),
                *,
                initializing: bool = False,
            ) -> None:
                message = (
                    f"Connected to {len(self._devices)} supported device(s); "
                    f"{len(result.connection_result.unavailable)} unavailable; "
                    f"{len(result.connection_result.removed)} removed"
                )
                if initialization_failures:
                    failures = "; ".join(
                        f"{device.model_name or 'DVB device'}: {error}"
                        for device, error in initialization_failures
                    )
                    message += f"; initialization failed for {failures}"
                elif initializing:
                    message += "; initializing newly discovered devices"
                elif auto_initialize and result.connection_result.connected:
                    message += "; newly discovered devices initialized"
                if result.connection_result.removal_failures:
                    failures = "; ".join(
                        f"{failure.device.model_name or 'DVB device'}: {failure.error}"
                        for failure in result.connection_result.removal_failures
                    )
                    message += f"; cleanup failed for removed devices: {failures}"
                self._set_status(message)

            if (
                not automatic
                or result.connection_result.connected
                or result.connection_result.unavailable
                or result.connection_result.removed
                or result.connection_result.removal_failures
            ):
                set_discovery_status(
                    initializing=auto_initialize and bool(result.connection_result.connected)
                )

            if auto_initialize and result.connection_result.connected:
                new_devices = result.connection_result.connected

                def initialize() -> _InitializationOutcome:
                    failures: list[tuple[DVBDevice, Exception]] = []
                    for device in new_devices:
                        try:
                            device.init_hardware()
                        except Exception as error:
                            failures.append((device, error))
                    return _InitializationOutcome(tuple(failures))

                def initialized(initialization_result: object) -> None:
                    if not isinstance(initialization_result, _InitializationOutcome):
                        raise TypeError("initialization worker returned an unexpected result")
                    for device in new_devices:
                        self._initialization_errors.pop(id(device), None)
                    for device, error in initialization_result.failures:
                        self._initialization_errors[id(device)] = str(error)
                    self.devicesChanged.emit()
                    set_discovery_status(initialization_result.failures)

                self._run_task(initialize, initialized, report_status=not automatic)

        self._run_task(operation, complete, report_status=not automatic)

    @Slot(int)
    def selectDevice(self, index: int) -> None:
        """Select a connected device by its displayed index."""
        if 0 <= index < len(self._devices):
            self._selected_device_index = index
            self._profile.selected_device = self._device_id(self._devices[index])
            self.devicesChanged.emit()
            self.stateChanged.emit()

    @Slot(int)
    def selectAdapter(self, index: int) -> None:
        """Select one configured adapter on the currently selected device."""
        device = self._selected_device()
        if device is not None and 0 <= index < len(device.adapters):
            self._profile.selected_adapter = index
            self.devicesChanged.emit()
            self.stateChanged.emit()

    @Slot()
    def initializeSelectedDevice(self) -> None:
        """Initialize the selected device and configure its logical adapters."""
        if self._server is not None:
            self._set_status("Stop the server before initializing a device")
            return
        device = self._selected_device()
        if device is None:
            self._set_status("Discover and select a device first")
            return

        def complete(_result: object) -> None:
            self._initialization_errors.pop(id(device), None)
            self.devicesChanged.emit()
            self._set_status(f"Initialized {device.model_name or 'DVB device'}")

        self._set_status("Initializing selected device...")
        self._run_task(device.init_hardware, complete)

    @Slot()
    def refreshKnownTables(self) -> None:
        """Fetch the available LinuxTV DVB-T table names in the worker pool."""
        self._set_status("Loading known scan tables...")

        def complete(names: object) -> None:
            self._known_tables = list(names)  # type: ignore[arg-type]
            self.knownTablesChanged.emit()
            self._set_status(f"Loaded {len(self._known_tables)} scan tables")

        self._run_task(known_table_names, complete)

    @Slot(str)
    def selectScanTable(self, name: str) -> None:
        """Parse one known or local scan table into the editable tuning list."""
        if not name.strip():
            return

        def complete(tunings: object) -> None:
            parsed = tuple(tunings)  # type: ignore[arg-type]
            if not parsed:
                self._set_status(f"No DVB-T tunings found in {name}")
                return
            self._profile.scan_table = name
            self._profile.tunings = parsed
            self.tuningsChanged.emit()
            self.stateChanged.emit()
            self._set_status(f"Loaded {len(parsed)} tunings from {name}")

        self._set_status(f"Parsing scan table {name}...")
        self._run_task(lambda: parse_table(name), complete)

    @Slot(str)
    def loadLocalScanTable(self, path: str) -> None:
        """Load a local scan-table file into the tuning list."""
        local_path = self._local_path(path)
        if not local_path:
            return
        self.selectScanTable(local_path)

    @Slot(str, str)
    def addTuning(self, frequency_mhz: str, bandwidth_mhz: str) -> None:
        """Append a manually entered DVB-T frequency and bandwidth."""
        tuning = self._parse_tuning(frequency_mhz, bandwidth_mhz)
        if tuning is None:
            return
        self._profile.tunings += (tuning,)
        self.tuningsChanged.emit()
        self.stateChanged.emit()

    @Slot(int, str, str)
    def updateTuning(self, index: int, frequency_mhz: str, bandwidth_mhz: str) -> None:
        """Replace one tuning with manually edited frequency and bandwidth values."""
        if not 0 <= index < len(self._profile.tunings):
            return
        tuning = self._parse_tuning(frequency_mhz, bandwidth_mhz)
        if tuning is None:
            return
        tunings = list(self._profile.tunings)
        tunings[index] = tuning
        self._profile.tunings = tuple(tunings)
        self.tuningsChanged.emit()
        self.stateChanged.emit()

    @Slot(int)
    def removeTuning(self, index: int) -> None:
        """Remove one tuning by its displayed list index."""
        if 0 <= index < len(self._profile.tunings):
            self._profile.tunings = self._profile.tunings[:index] + self._profile.tunings[index + 1 :]
            self.tuningsChanged.emit()
            self.stateChanged.emit()

    @Slot()
    def scan(self) -> None:
        """Scan configured tunings and publish the resulting service map."""
        device = self._selected_device()
        if device is None or not device.adapters:
            self._set_status("Initialize a device before scanning")
            return
        if not self._profile.tunings:
            self._set_status("Load a scan table or add a tuning first")
            return
        adapter = self._selected_adapter(device)
        tunings = self._profile.tunings
        self._set_status(f"Scanning {len(tunings)} tuning(s)...")

        def complete(service_map: object) -> None:
            self._profile.service_map = service_map  # type: ignore[assignment]
            self.channelsChanged.emit()
            self.stateChanged.emit()
            self._set_status(f"Scan complete: {len(self.channels)} service(s) found")

        self._run_task(lambda: map_from_tunings(tunings, adapter), complete)

    @Slot(str)
    def saveProfile(self, path: str) -> None:
        """Save the current hardware-independent application profile."""
        local_path = self._local_path(path)
        if not local_path:
            return
        profile = self._profile

        def complete(_result: object) -> None:
            self._set_status(f"Saved profile to {local_path}")

        self._run_task(lambda: save_profile(profile, local_path), complete)

    @Slot(str)
    def loadProfile(self, path: str) -> None:
        """Load profile values without changing connected hardware state."""
        local_path = self._local_path(path)
        if not local_path:
            return
        if self._server is not None:
            self._set_status("Stop the server before loading a profile")
            return

        def complete(profile: object) -> None:
            loaded = profile  # validated by load_profile
            self._profile = loaded  # type: ignore[assignment]
            self._selected_device_index = None
            self._select_first_device()
            self.tuningsChanged.emit()
            self.channelsChanged.emit()
            self.devicesChanged.emit()
            self.serverChanged.emit()
            self.stateChanged.emit()
            self._set_status(f"Loaded profile from {local_path}")

        self._run_task(lambda: load_profile(local_path), complete)

    @Slot()
    def startServer(self) -> None:
        """Start the local HTTP streaming server on a dedicated thread."""
        if self._server is not None:
            self._set_status("Server is already running")
            return
        if self._profile.service_map is None or not self._profile.service_map.services:
            self._set_status("Scan or load a service map before starting the server")
            return
        adapters = [adapter for device in self._devices for adapter in device.adapters]
        if not adapters:
            self._set_status("Initialize at least one device before starting the server")
            return
        try:
            server = LocalTS_HTTPServer(
                adapters,
                self._profile.service_map,
                host=self._profile.server_host,
                port=self._profile.server_port,
            )
            server.start()
            thread = Thread(target=server.serve_forever, name="pydvb-http-server", daemon=True)
            thread.start()
        except (OSError, RuntimeError, ValueError) as error:
            logger.exception("Could not start HTTP server")
            self._set_status(f"Could not start server: {error}")
            return
        self._server = server
        self._server_thread = thread
        self._server_state = "Running"
        logger.info("HTTP server listening at %s", self.serverAddress)
        self.serverChanged.emit()
        self._set_status(f"Server listening at {self.serverAddress}")

    @Slot()
    def stopServer(self) -> None:
        """Stop HTTP handlers and multiplex sessions before device shutdown."""
        server = self._server
        thread = self._server_thread
        if server is None:
            self._set_status("Server is already stopped")
            return
        self._server_state = "Stopping"
        logger.info("Stopping HTTP server")
        self.serverChanged.emit()

        def operation() -> None:
            server.shutdown()
            server.close()
            if thread is not None:
                thread.join(timeout=3.0)

        def complete(_result: object) -> None:
            self._server = None
            self._server_thread = None
            self._server_state = "Stopped"
            logger.info("HTTP server stopped")
            self.serverChanged.emit()
            self._set_status("Server stopped")

        self._run_task(operation, complete)

    @Slot()
    def shutdown(self) -> None:
        """Stop workers, the HTTP server, and devices in lifecycle order."""
        if not self._shutting_down:
            self._shutting_down = True
            self._status_timer.stop()
            self._discovery_timer.stop()
            self.stateChanged.emit()
        if self._busy:
            QTimer.singleShot(100, self.shutdown)
            return
        if self._shutdown_task_started:
            return
        self._shutdown_task_started = True

        server = self._server
        server_thread = self._server_thread
        manager = self._manager

        def operation() -> list[str]:
            errors: list[str] = []
            if server is not None:
                try:
                    server.shutdown()
                except Exception as error:
                    errors.append(f"server shutdown: {error}")
                try:
                    server.close()
                except Exception as error:
                    errors.append(f"server close: {error}")
                if server_thread is not None:
                    server_thread.join(timeout=3.0)
            if manager is not None:
                try:
                    manager.close()
                except Exception as error:
                    errors.append(f"device close: {error}")
            return errors

        def complete(result: object) -> None:
            self._server = None
            self._server_thread = None
            self._manager = None
            self._devices.clear()
            self._initialization_errors.clear()
            self._server_state = "Stopped"
            self.devicesChanged.emit()
            self.serverChanged.emit()
            errors = result
            if errors:
                self._set_status("Shutdown completed with errors: " + "; ".join(errors))
            self.shutdownFinished.emit()

        self._run_task(operation, complete, allow_shutdown=True)

    @Slot()
    def clearLogs(self) -> None:
        """Clear the log messages shown in the GUI."""
        if self._log_messages:
            self._log_messages.clear()
            self.logsChanged.emit()

    @Slot(str)
    def _append_log_message(self, message: str) -> None:
        """Append a log message and discard the oldest entries when full."""
        self._log_messages.append(message)
        del self._log_messages[:-_MAX_LOG_MESSAGES]
        self.logsChanged.emit()

    def _poll_status(self) -> None:
        if self._busy or self._shutting_down or not any(device.adapters for device in self._devices):
            return
        manager = self._manager
        targets = tuple(
            (device, adapter_index)
            for device in self._devices
            for adapter_index in range(len(device.adapters))
        )

        def operation() -> _StatusOutcome:
            readings: list[tuple[DVBDevice, int, str]] = []
            removed: list[DVBDevice] = []
            removal_failures: list[DeviceRemovalFailure] = []
            for device, adapter_index in targets:
                if any(removed_device is device for removed_device in removed):
                    continue
                try:
                    status = device.read_status(adapter_index)
                    readings.append((device, adapter_index, self._format_status(status)))
                except Exception as error:
                    if (
                        isinstance(error, LibUSBError)
                        and getattr(error, "value", None) == LIBUSB_ERROR_NO_DEVICE
                    ):
                        removed.append(device)
                        if manager is not None:
                            removal_failure = manager.remove_device(device)
                            if removal_failure is not None:
                                removal_failures.append(removal_failure)
                    else:
                        readings.append((device, adapter_index, f"Status unavailable: {error}"))
            return _StatusOutcome(tuple(readings), tuple(removed), tuple(removal_failures))

        def complete(result: object) -> None:
            if not isinstance(result, _StatusOutcome):
                raise TypeError("status worker returned an unexpected result")
            self._remove_devices(result.removed)
            self._signal_by_adapter = {}
            for device, adapter_index, summary in result.readings:
                device_index = next(
                    (
                        index
                        for index, current_device in enumerate(self._devices)
                        if current_device is device
                    ),
                    None,
                )
                if device_index is not None:
                    self._signal_by_adapter[(device_index, adapter_index)] = summary
            self._select_first_device()
            self.devicesChanged.emit()
            self.serverChanged.emit()
            if result.removed:
                message = (
                    f"Removed {len(result.removed)} disconnected device(s); "
                    f"{len(self._devices)} device(s) remain"
                )
                if result.removal_failures:
                    errors = "; ".join(
                        f"{failure.device.model_name or 'DVB device'}: {failure.error}"
                        for failure in result.removal_failures
                    )
                    message += f"; cleanup failed: {errors}"
                self._set_status(message)

        self._run_task(operation, complete, report_status=False)

    def _remove_devices(self, removed: Sequence[DVBDevice]) -> None:
        if not removed:
            return
        removed_ids = {id(device) for device in removed}
        selected_device = self._selected_device()
        self._devices = [device for device in self._devices if id(device) not in removed_ids]
        self._selected_device_index = next(
            (
                index
                for index, device in enumerate(self._devices)
                if device is selected_device
            ),
            None,
        )
        for device in removed:
            self._initialization_errors.pop(id(device), None)
        self._select_first_device()

    def _parse_tuning(self, frequency_mhz: str, bandwidth_mhz: str) -> DVBTuning | None:
        try:
            frequency = float(frequency_mhz)
            bandwidth = int(bandwidth_mhz)
            if not isfinite(frequency) or frequency <= 0 or bandwidth not in (6, 7, 8):
                raise ValueError
        except ValueError:
            self._set_status("Enter a positive frequency and a 6, 7, or 8 MHz bandwidth")
            return None
        return DVBTuning(
            frequency_hz=round(frequency * 1_000_000),
            bandwidth_hz=bandwidth * 1_000_000,
        )

    def _run_task(
        self,
        operation: Callable[[], object],
        complete: Callable[[object], None],
        *,
        report_status: bool = True,
        allow_shutdown: bool = False,
    ) -> None:
        if self._busy or (self._shutting_down and not allow_shutdown):
            if report_status and not self._shutting_down:
                self._set_status("Another device operation is in progress")
            return
        self._busy = True
        self.stateChanged.emit()
        worker = _Worker(operation)
        self._workers.add(worker)
        worker.signals.finished.connect(
            lambda result: self._taskSucceeded.emit(worker, result, complete)
        )
        worker.signals.failed.connect(
            lambda message: self._taskFailed.emit(worker, message)
        )
        self._pool.start(worker)

    @Slot(object, object, object)
    def _finish_task(
        self,
        worker: _Worker,
        result: object,
        complete: Callable[[object], None],
    ) -> None:
        self._workers.discard(worker)
        self._busy = False
        self.stateChanged.emit()
        try:
            complete(result)
        except Exception as error:
            self._set_status(f"Operation failed: {type(error).__name__}: {error}")

    @Slot(object, str)
    def _fail_task(self, worker: _Worker, message: str) -> None:
        self._workers.discard(worker)
        self._busy = False
        self.stateChanged.emit()
        self._set_status(f"Operation failed: {message}")

    def _selected_device(self) -> DVBDevice | None:
        if not self._devices:
            return None
        if (
            self._selected_device_index is not None
            and 0 <= self._selected_device_index < len(self._devices)
        ):
            return self._devices[self._selected_device_index]
        selected_id = self._profile.selected_device
        self._selected_device_index = next(
            (
                index
                for index, device in enumerate(self._devices)
                if self._device_id(device) == selected_id
            ),
            0,
        )
        return self._devices[self._selected_device_index]

    def _selected_adapter(self, device: DVBDevice):
        index = self._profile.selected_adapter
        if not 0 <= index < len(device.adapters):
            index = 0
            self._profile.selected_adapter = index
        return device.adapters[index]

    def _select_first_device(self) -> None:
        selected = self._selected_device()
        if selected is not None:
            self._profile.selected_device = self._device_id(selected)

    @staticmethod
    def _device_id(device: DVBDevice) -> str:
        if device.device_id is None:
            return "Unknown USB ID"
        return AppController._usb_id(device.device_id)

    @staticmethod
    def _usb_id(device_id: object) -> str:
        vendor_id = getattr(device_id, "vendor_id", None)
        product_id = getattr(device_id, "product_id", None)
        if vendor_id is None or product_id is None:
            return str(device_id)
        return f"{vendor_id:04x}:{product_id:04x}"

    @staticmethod
    def _format_status(status: DVBFrontendStatus) -> str:
        if status.lock:
            result = "Locked"
        elif status.signal:
            result = "Signal detected, no lock"
        else:
            result = "No signal"
        if status.signal_strength_dbm is not None:
            result += f" · {status.signal_strength_dbm} dBm"
        if status.snr_db is not None:
            result += f" · SNR {status.snr_db:.1f} dB"
        return result

    def _set_status(self, message: str) -> None:
        self._status_message = message
        self.stateChanged.emit()

    @staticmethod
    def _local_path(path: str | PathLike[str]) -> str:
        value = str(path)
        url = QUrl(value)
        if url.isLocalFile():
            return url.toLocalFile()
        return str(Path(value))