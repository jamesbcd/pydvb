"""Application-level Qt controller and shared operation coordinator."""

from __future__ import annotations

from dataclasses import dataclass
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
from pydvb.drivers.af9015.device import AF9015Device
from pydvb.server import LocalTS_HTTPServer
from pydvb.usb import LIBUSB_ERROR_NO_DEVICE, LibUSBError

from .profile import GuiProfile, load_profile, save_profile
from .controllers import (
    ChannelController,
    DeviceController,
    LogController,
    ServerController,
    TuningController,
)


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
    """Coordinate shared application state and asynchronous section operations."""

    devicesChanged = Signal()
    tuningsChanged = Signal()
    channelsChanged = Signal()
    knownTablesChanged = Signal()
    serverChanged = Signal()
    stateChanged = Signal()
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
        self._devices_controller = DeviceController(self)
        self._tuning_controller = TuningController(self)
        self._channel_controller = ChannelController(self)
        self._server_controller = ServerController(self)
        self._log_controller = LogController(self)
        self.logMessageReceived.connect(
            self._log_controller._append_log_message,
            Qt.ConnectionType.QueuedConnection,
        )
        self._status_timer = QTimer(self)
        self._status_timer.setInterval(2000)
        self._status_timer.timeout.connect(self._poll_status)
        self._status_timer.start()
        self._discovery_timer = QTimer(self)
        self._discovery_timer.setInterval(2000)
        self._discovery_timer.timeout.connect(self._devices_controller._auto_discover)
        self._discovery_timer.start()

    @Property(QObject, constant=True)
    def devicesController(self) -> DeviceController:
        """Return the controller for receiver discovery and selection."""
        return self._devices_controller

    @Property(QObject, constant=True)
    def tuningController(self) -> TuningController:
        """Return the controller for tunings and scanning."""
        return self._tuning_controller

    @Property(QObject, constant=True)
    def channelController(self) -> ChannelController:
        """Return the controller for the channel-map view."""
        return self._channel_controller

    @Property(QObject, constant=True)
    def serverController(self) -> ServerController:
        """Return the controller for HTTP streaming."""
        return self._server_controller

    @Property(QObject, constant=True)
    def logController(self) -> LogController:
        """Return the controller for GUI log messages."""
        return self._log_controller

    @Property(bool, notify=stateChanged)
    def busy(self) -> bool:
        return self._busy

    @Property(str, notify=stateChanged)
    def statusMessage(self) -> str:
        return self._status_message

    @Property(bool, notify=stateChanged)
    def shuttingDown(self) -> bool:
        return self._shutting_down

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