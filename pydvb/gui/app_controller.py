"""Application-level Qt controller and shared operation coordinator."""

from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass, replace
from os import PathLike
from pathlib import Path
from threading import Thread
from typing import Callable, Literal, Sequence, cast

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
from pydvb.server import LocalTS_HTTPServer
from pydvb.usb import LIBUSB_ERROR_NO_DEVICE, LibUSBError

from .settings import GuiSettings, load_settings, save_settings
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


@dataclass(frozen=True, slots=True)
class _PendingTask:
    """Describe work and its completion callback before it enters a worker lane."""

    operation: Callable[[], object]
    complete: Callable[[object], None]
    on_finished: Callable[[], None] | None
    # The lane chooses the serialized pool; intent only affects device priority/UI.
    lane: Literal["device", "general"]
    user_initiated: bool = True


class _WorkerSignals(QObject):
    finished = Signal(object)
    failed = Signal(str)


class _Worker(QRunnable):
    """Execute one operation off the Qt thread and report its outcome by signal."""

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
    _taskSucceeded = Signal(object, object)
    _taskFailed = Signal(object, str)

    def __init__(
        self,
        device_types: Sequence[type[DVBDevice]] | None = None,
        settings: GuiSettings | None = None,
        parent: QObject | None = None,
        settings_path: str | PathLike[str] | None = None,
    ) -> None:
        super().__init__(parent)
        self._device_types = None if device_types is None else tuple(device_types)
        self._settings_path = Path(settings_path) if settings_path is not None else None
        settings_load_error: Exception | None = None
        if settings is not None:
            self._settings = settings
        elif self._settings_path is None:
            self._settings = GuiSettings()
        else:
            try:
                self._settings = load_settings(self._settings_path)
            except FileNotFoundError:
                self._settings = GuiSettings()
            except (OSError, ValueError, TypeError, KeyError) as error:
                self._settings = GuiSettings()
                settings_load_error = error
                logging.getLogger(__name__).warning(
                    "Could not load GUI settings from %s; using defaults: %s",
                    self._settings_path,
                    error,
                )
        self._manager: DVBDeviceManager | None = None
        self._devices: list[DVBDevice] = []
        self._selected_device_index: int | None = None
        self._discovery_rows: list[dict[str, object]] = []
        self._show_unsupported_devices = self._settings.show_unsupported_devices
        self._auto_discover_and_initialize_devices = (
            self._settings.auto_discover_and_initialize_devices
        )
        self._initialization_errors: dict[int, str] = {}
        self._initializing_devices: set[int] = set()
        self._signal_by_adapter: dict[tuple[int, int], str] = {}
        self._known_tables: list[str] = []
        # UI busy state is intentionally distinct from device-lane occupancy:
        # automatic polling uses the lane but should not flash action buttons.
        self._user_device_busy = False
        self._device_lane_busy = False
        self._device_task_user_initiated = False
        self._general_lane_busy = False
        self._status_message = (
            f"Could not load settings from {self._settings_path}; using defaults: "
            f"{settings_load_error}"
            if settings_load_error is not None
            else "Ready"
        )
        self._server_state = "Stopped"
        self._server: LocalTS_HTTPServer | None = None
        self._server_thread: Thread | None = None
        self._shutting_down = False
        self._shutdown_task_started = False
        # General-lane work is serialized independently of device operations.
        self._pending_general_tasks: deque[_PendingTask] = deque()
        # User device work takes priority over deferred automatic device work.
        self._pending_user_device_tasks: deque[_PendingTask] = deque()
        self._pending_automatic_device_tasks: deque[_PendingTask] = deque()
        self._workers: set[_Worker] = set()
        self._worker_tasks: dict[_Worker, _PendingTask] = {}
        # Each pool is single-threaded; device operations share hardware state,
        # while general-lane tasks are independent of device control.
        self._device_pool = QThreadPool(self)
        self._device_pool.setMaxThreadCount(1)
        self._general_pool = QThreadPool(self)
        self._general_pool.setMaxThreadCount(1)
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
        # Polling occupies the device lane as automatic work, not user busy state.
        self._status_timer = QTimer(self)
        self._status_timer.setInterval(2000)
        self._status_timer.timeout.connect(self._poll_status)
        self._status_timer.start()
        self._discovery_timer = QTimer(self)
        self._discovery_timer.setInterval(2000)
        self._discovery_timer.timeout.connect(self._devices_controller._auto_discover)
        if self._auto_discover_and_initialize_devices:
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
        """Report user device work or general-lane work for the global indicator."""
        return self._user_device_busy or self._general_lane_busy

    @Property(bool, notify=stateChanged)
    def userDeviceBusy(self) -> bool:
        """Return whether a user-requested device operation is active or queued."""
        return self._user_device_busy

    @Property(bool, notify=stateChanged)
    def generalBusy(self) -> bool:
        """Return whether general-lane work is active or waiting to run."""
        return self._general_lane_busy or bool(self._pending_general_tasks)

    @Property(str, notify=stateChanged)
    def statusMessage(self) -> str:
        """Return the latest status text displayed by the application."""
        return self._status_message

    @Property(bool, notify=stateChanged)
    def shuttingDown(self) -> bool:
        """Report whether shutdown has begun and new work is being rejected."""
        return self._shutting_down

    def _discover_devices(self, *, auto_initialize: bool, automatic: bool) -> None:
        """Connect new devices; user discovery takes priority over automatic polling."""
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
                    self._maybe_auto_start_server()

                self._run_task(
                    initialize,
                    initialized,
                    lane="device",
                    report_status=not automatic,
                    user_initiated=not automatic,
                    defer_if_device_busy=automatic,
                )

        self._run_task(
            operation,
            complete,
            lane="device",
            report_status=not automatic,
            user_initiated=not automatic,
        )

    @Slot(str)
    def saveSettings(self, path: str) -> None:
        """Save the current hardware-independent application settings."""
        local_path = self._local_path(path)
        if not local_path:
            return
        def complete(_result: object) -> None:
            self._set_status(f"Saved settings to {local_path}")

        settings = replace(self._settings)
        self._run_task(
            lambda: save_settings(settings, local_path),
            complete,
            lane="general",
        )

    @Slot(str)
    def saveSettingsAndReset(self, path: str) -> None:
        """Save the current settings, then reset them after a successful save."""
        local_path = self._local_path(path)
        if not local_path:
            return
        if self._server is not None:
            self._set_status("Stop the server before resetting settings")
            return

        settings = replace(self._settings)

        def complete(_result: object) -> None:
            if self._server is not None:
                self._set_status("Stop the server before resetting settings")
                return
            self._apply_settings(GuiSettings())
            self._set_status(f"Saved settings to {local_path} and reset to defaults")

        self._run_task(
            lambda: save_settings(settings, local_path),
            complete,
            lane="general",
        )

    @Slot()
    def resetSettings(self) -> None:
        """Restore default settings without changing connected hardware state."""
        if self._server is not None:
            self._set_status("Stop the server before resetting settings")
            return

        def complete(settings: object) -> None:
            if self._server is not None:
                self._set_status("Stop the server before resetting settings")
                return
            self._apply_settings(cast(GuiSettings, settings))
            self._set_status("Reset settings to defaults")

        self._run_task(lambda: GuiSettings(), complete, lane="general")

    @Slot(str)
    def loadSettings(self, path: str) -> None:
        """Load settings without changing connected hardware state."""
        local_path = self._local_path(path)
        if not local_path:
            return
        if self._server is not None:
            self._set_status("Stop the server before loading settings")
            return
        if self._shutting_down:
            self._set_status("Cannot load settings while shutting down")
            return

        def complete(settings: object) -> None:
            if self._shutting_down:
                return
            if self._server is not None:
                self._set_status("Stop the server before loading settings")
                return
            loaded = cast(GuiSettings, settings)
            self._apply_settings(loaded)
            self._set_status(f"Loaded settings from {local_path}")

        self._run_task(
            lambda: load_settings(local_path),
            complete,
            lane="general",
        )

    def _apply_settings(self, settings: GuiSettings) -> None:
        """Apply persisted settings and notify every affected controller."""
        self._settings = settings
        self._show_unsupported_devices = settings.show_unsupported_devices
        self._auto_discover_and_initialize_devices = (
            settings.auto_discover_and_initialize_devices
        )
        if self._auto_discover_and_initialize_devices:
            self._discovery_timer.start()
        else:
            self._discovery_timer.stop()
        self._selected_device_index = None
        self._select_first_device()
        self.tuningsChanged.emit()
        self._channel_controller._channels_changed()
        self.devicesChanged.emit()
        self.serverChanged.emit()
        self.stateChanged.emit()
        self._maybe_auto_start_server()

    def _maybe_auto_start_server(self) -> None:
        """Start the server when enabled and both a channel map and adapter are ready."""
        if (
            not self._settings.auto_start_server
            or self._server is not None
            or self._server_state != "Stopped"
            or self._settings.service_map is None
            or not self._settings.service_map.services
            or not any(device.adapters for device in self._devices)
        ):
            return
        self._server_controller.startServer()

    @Slot()
    def shutdown(self) -> None:
        """Stop workers, the HTTP server, and devices in lifecycle order."""
        if not self._shutting_down:
            self._shutting_down = True
            self._status_timer.stop()
            self._discovery_timer.stop()
            self._pending_general_tasks.clear()
            self._channel_controller.cancelScan()
            self.stateChanged.emit()
        self._pending_user_device_tasks.clear()
        self._pending_automatic_device_tasks.clear()
        self._update_user_device_busy()
        if self._device_lane_busy or self._general_lane_busy:
            QTimer.singleShot(100, self.shutdown)
            return
        if self._shutdown_task_started:
            return
        self._shutdown_task_started = True

        server = self._server
        server_thread = self._server_thread
        manager = self._manager
        settings_path = self._settings_path
        settings = replace(self._settings)

        def operation() -> list[str]:
            errors: list[str] = []
            if settings_path is not None:
                try:
                    save_settings(settings, settings_path)
                except Exception as error:
                    logging.getLogger(__name__).exception(
                        "Could not save GUI settings to %s",
                        settings_path,
                    )
                    errors.append(f"settings save: {error}")
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

        self._run_task(operation, complete, allow_shutdown=True, lane="device")

    def _poll_status(self) -> None:
        """Read adapter signal status as automatic work on the device lane."""
        if (
            self._device_lane_busy
            or self._shutting_down
            or not any(device.adapters for device in self._devices)
        ):
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
            for device, adapter_index, summary in result.readings:
                if any(current_device is device for current_device in self._devices):
                    self._signal_by_adapter[(id(device), adapter_index)] = summary
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

        self._run_task(
            operation,
            complete,
            report_status=False,
            lane="device",
            user_initiated=False,
        )

    def _remove_devices(self, removed: Sequence[DVBDevice]) -> None:
        """Remove disconnected devices while keeping selection state consistent."""
        if not removed:
            return
        removed_ids = {id(device) for device in removed}
        selected_device = self._selected_device()
        self._devices = [device for device in self._devices if id(device) not in removed_ids]
        self._signal_by_adapter = {
            key: value
            for key, value in self._signal_by_adapter.items()
            if key[0] not in removed_ids
        }
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
        lane: Literal["device", "general"],
        on_finished: Callable[[], None] | None = None,
        report_status: bool = True,
        allow_shutdown: bool = False,
        user_initiated: bool = True,
        defer_if_device_busy: bool = False,
    ) -> bool:
        """Run work on the device or general lane.

        ``user_initiated`` controls device-lane priority and UI busy state;
        it is independent of which worker lane executes the task.
        """
        if lane not in ("device", "general"):
            raise ValueError(f"unsupported task lane: {lane}")
        if self._shutting_down and not allow_shutdown:
            if report_status:
                self._set_status("Application is shutting down")
            return False
        task = _PendingTask(operation, complete, on_finished, lane, user_initiated)
        if lane == "device":
            if self._device_lane_busy:
                if (
                    user_initiated
                    and not self._device_task_user_initiated
                    and not self._pending_user_device_tasks
                ):
                    self._pending_user_device_tasks.append(task)
                    self._update_user_device_busy()
                    if report_status:
                        self._set_status("Waiting for the current device operation to finish")
                    return True
                if not user_initiated and defer_if_device_busy:
                    self._pending_automatic_device_tasks.append(task)
                    return True
                if report_status and user_initiated:
                    self._set_status("Another device operation is in progress")
                return False
            if self._pending_user_device_tasks:
                if not user_initiated and defer_if_device_busy:
                    self._pending_automatic_device_tasks.append(task)
                    return True
                if report_status and user_initiated:
                    self._set_status("Another device operation is in progress")
                return False
        if lane == "general" and self._general_lane_busy:
            self._pending_general_tasks.append(task)
            self.stateChanged.emit()
            return True
        self._start_task(task)
        return True

    def _start_task(self, task: _PendingTask) -> None:
        """Start a task in its dedicated serialized worker lane."""
        if task.lane == "device":
            self._device_lane_busy = True
            self._device_task_user_initiated = task.user_initiated
            self._update_user_device_busy()
            pool = self._device_pool
        else:
            self._general_lane_busy = True
            pool = self._general_pool
            self.stateChanged.emit()
        worker = _Worker(task.operation)
        self._workers.add(worker)
        self._worker_tasks[worker] = task
        worker.signals.finished.connect(
            lambda result: self._taskSucceeded.emit(worker, result)
        )
        worker.signals.failed.connect(
            lambda message: self._taskFailed.emit(worker, message)
        )
        pool.start(worker)

    @Slot(object, object)
    def _finish_task(
        self,
        worker: _Worker,
        result: object,
    ) -> None:
        """Release a completed worker and run its Qt-thread completion callback."""
        self._workers.discard(worker)
        task = self._worker_tasks.pop(worker)
        if task.lane == "device":
            self._device_lane_busy = False
            self._device_task_user_initiated = False
            try:
                task.complete(result)
            except Exception as error:
                self._set_status(f"Operation failed: {type(error).__name__}: {error}")
            finally:
                if task.on_finished is not None:
                    task.on_finished()
            self._run_next_device_lane_task()
            self._update_user_device_busy()
        else:
            try:
                task.complete(result)
            except Exception as error:
                self._set_status(f"Operation failed: {type(error).__name__}: {error}")
            finally:
                if task.on_finished is not None:
                    task.on_finished()
                self._general_lane_busy = False
                self.stateChanged.emit()
        self._run_next_general_task()

    @Slot(object, str)
    def _fail_task(self, worker: _Worker, message: str) -> None:
        """Release a failed worker, report its error, and continue queued work."""
        self._workers.discard(worker)
        task = self._worker_tasks.pop(worker)
        if task.lane == "device":
            self._device_lane_busy = False
            self._device_task_user_initiated = False
        else:
            self._general_lane_busy = False
        self._update_user_device_busy()
        self._set_status(f"Operation failed: {message}")
        if task.on_finished is not None:
            task.on_finished()
        if task.lane == "device":
            self._run_next_device_lane_task()
        self._run_next_general_task()

    def _run_next_device_lane_task(self) -> None:
        """Run queued user work before any deferred automatic device task."""
        if self._device_lane_busy or self._shutting_down:
            return
        if self._pending_user_device_tasks:
            self._start_task(self._pending_user_device_tasks.popleft())
        elif self._pending_automatic_device_tasks:
            self._start_task(self._pending_automatic_device_tasks.popleft())

    def _update_user_device_busy(self) -> None:
        """Notify the UI when user-requested device work starts, waits, or ends."""
        busy = self._device_task_user_initiated or bool(self._pending_user_device_tasks)
        if self._user_device_busy != busy:
            self._user_device_busy = busy
            self.stateChanged.emit()

    def _run_next_general_task(self) -> None:
        """Start the next task queued for the general lane."""
        if self._general_lane_busy or self._shutting_down:
            return
        if self._pending_general_tasks:
            self._start_task(self._pending_general_tasks.popleft())

    def _selected_device(self) -> DVBDevice | None:
        """Return the selected connected device, resolving the saved ID if needed."""
        if not self._devices:
            return None
        if (
            self._selected_device_index is not None
            and 0 <= self._selected_device_index < len(self._devices)
        ):
            return self._devices[self._selected_device_index]
        selected_id = self._settings.selected_device
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
        """Return the settings-selected adapter, falling back to adapter zero."""
        index = self._settings.selected_adapter
        if not 0 <= index < len(device.adapters):
            index = 0
            self._settings.selected_adapter = index
        return device.adapters[index]

    def _select_first_device(self) -> None:
        """Preserve the current selection or select the first connected device."""
        selected = self._selected_device()
        if selected is not None:
            self._settings.selected_device = self._device_id(selected)

    @staticmethod
    def _device_id(device: DVBDevice) -> str:
        """Format a device's USB identifier for settings persistence."""
        if device.device_id is None:
            return "Unknown USB ID"
        return AppController._usb_id(device.device_id)

    @staticmethod
    def _usb_id(device_id: object) -> str:
        """Format a USB ID when available, retaining a useful fallback string."""
        vendor_id = getattr(device_id, "vendor_id", None)
        product_id = getattr(device_id, "product_id", None)
        if vendor_id is None or product_id is None:
            return str(device_id)
        return f"{vendor_id:04x}:{product_id:04x}"

    @staticmethod
    def _format_status(status: DVBFrontendStatus) -> str:
        """Convert frontend lock, signal, and quality readings to display text."""
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
        """Update the status text and notify QML bindings."""
        self._status_message = message
        self.stateChanged.emit()

    @staticmethod
    def _local_path(path: str | PathLike[str]) -> str:
        """Convert a local-file URL or path into a filesystem path."""
        value = str(path)
        url = QUrl(value)
        if url.isLocalFile():
            return url.toLocalFile()
        return str(Path(value))