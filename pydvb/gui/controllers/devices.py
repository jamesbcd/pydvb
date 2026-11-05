"""Receiver discovery, selection, and initialization controller."""

from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtCore import QObject, Property, Signal, Slot

if TYPE_CHECKING:
    from ..app_controller import AppController


class DeviceController(QObject):
    """Expose receiver discovery, selection, and initialization to the device view."""

    devicesChanged = Signal()
    stateChanged = Signal()

    def __init__(self, app: AppController) -> None:
        super().__init__(app)
        self._app = app
        app.devicesChanged.connect(self.devicesChanged.emit)
        app.stateChanged.connect(self.stateChanged.emit)

    @Property("QVariantList", notify=devicesChanged)
    def devices(self) -> list[dict[str, object]]:
        """Return connected devices together with visible discovery results."""
        app = self._app
        rows: list[dict[str, object]] = []
        for index, device in enumerate(app._devices):
            device_id = app._device_id(device)
            rows.append(
                {
                    "deviceIndex": index,
                    "deviceId": device_id,
                    "name": device.model_name or "DVB device",
                    "state": (
                        "Initialization failed"
                        if id(device) in app._initialization_errors
                        else "Initialized"
                        if device.adapters
                        else "Connected"
                    ),
                    "adapterCount": len(device.adapters),
                    "signal": (
                        "; ".join(
                            f"Adapter {adapter_index}: "
                            f"{app._signal_by_adapter.get((index, adapter_index), 'Not polled')}"
                            for adapter_index in range(len(device.adapters))
                        )
                        if device.adapters
                        else "Not initialized"
                    ),
                    "selected": index == app._selected_device_index,
                }
            )
        rows.extend(
            row
            for row in app._discovery_rows
            if app._show_unsupported_devices or not row.get("unsupported", False)
        )
        return rows

    @Property(str, notify=devicesChanged)
    def deviceSummary(self) -> str:
        """Summarize connected devices and their initialized adapters."""
        devices = self._app._devices
        initialized_devices = sum(bool(device.adapters) for device in devices)
        active_adapters = sum(len(device.adapters) for device in devices)
        return (
            f"{len(devices)} device(s) connected; "
            f"{initialized_devices} initialized; {active_adapters} active adapter(s)"
        )

    @Property(bool, notify=devicesChanged)
    def showUnsupportedDevices(self) -> bool:
        return self._app._show_unsupported_devices

    @showUnsupportedDevices.setter
    def showUnsupportedDevices(self, value: bool) -> None:
        app = self._app
        if app._show_unsupported_devices != value:
            app._show_unsupported_devices = value
            app.devicesChanged.emit()

    @Property(bool, notify=stateChanged)
    def autoDiscoverAndInitializeDevices(self) -> bool:
        return self._app._auto_discover_and_initialize_devices

    @autoDiscoverAndInitializeDevices.setter
    def autoDiscoverAndInitializeDevices(self, value: bool) -> None:
        app = self._app
        if app._auto_discover_and_initialize_devices == value:
            return
        app._auto_discover_and_initialize_devices = value
        if value:
            app._discovery_timer.start()
        else:
            app._discovery_timer.stop()
        app.stateChanged.emit()

    @Property("QVariantList", notify=devicesChanged)
    def adapters(self) -> list[dict[str, object]]:
        device = self._app._selected_device()
        if device is None:
            return []
        app = self._app
        return [
            {
                "adapterIndex": index,
                "name": f"Adapter {adapter.adapter_index}",
                "state": "Initialized",
                "signal": app._signal_by_adapter.get(
                    (app._selected_device_index or 0, index),
                    "Not polled",
                ),
                "selected": index == app._profile.selected_adapter,
            }
            for index, adapter in enumerate(device.adapters)
        ]

    @Slot()
    def discover(self) -> None:
        """Discover receivers, initializing them when automatic mode is enabled."""
        app = self._app
        if app._server is not None:
            app._set_status("Stop the server before rediscovering devices")
            return
        app._discover_devices(
            auto_initialize=app._auto_discover_and_initialize_devices,
            automatic=False,
        )

    @Slot()
    def _auto_discover(self) -> None:
        """Poll for newly connected receivers while automatic discovery is enabled."""
        app = self._app
        if (
            not app._auto_discover_and_initialize_devices
            or app._server is not None
            or app._busy
            or app._shutting_down
        ):
            return
        app._discover_devices(auto_initialize=True, automatic=True)

    @Slot(int)
    def selectDevice(self, index: int) -> None:
        """Select a connected device by its displayed index."""
        app = self._app
        if 0 <= index < len(app._devices):
            app._selected_device_index = index
            app._profile.selected_device = app._device_id(app._devices[index])
            app.devicesChanged.emit()
            app.stateChanged.emit()

    @Slot(int)
    def selectAdapter(self, index: int) -> None:
        """Select one configured adapter on the currently selected device."""
        app = self._app
        device = app._selected_device()
        if device is not None and 0 <= index < len(device.adapters):
            app._profile.selected_adapter = index
            app.devicesChanged.emit()
            app.stateChanged.emit()

    @Slot()
    def initializeSelectedDevice(self) -> None:
        """Initialize the selected device and configure its logical adapters."""
        app = self._app
        if app._server is not None:
            app._set_status("Stop the server before initializing a device")
            return
        device = app._selected_device()
        if device is None:
            app._set_status("Discover and select a device first")
            return

        def complete(_result: object) -> None:
            app._initialization_errors.pop(id(device), None)
            app.devicesChanged.emit()
            app._set_status(f"Initialized {device.model_name or 'DVB device'}")

        app._set_status("Initializing selected device...")
        app._run_task(device.init_hardware, complete, lane="device")
