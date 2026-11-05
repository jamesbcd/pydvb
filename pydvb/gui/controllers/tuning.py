"""Scan-table, tuning, and service-scan controller."""

from __future__ import annotations

from math import isfinite
from typing import TYPE_CHECKING

from PySide6.QtCore import QObject, Property, Signal, Slot

from pydvb.driver_api.tuner import DVBTuning
from pydvb.scan_tables import known_table_names, parse_table
from pydvb.service_map import DVBServiceMap, map_from_tunings

if TYPE_CHECKING:
    from ..app_controller import AppController


class TuningController(QObject):
    """Expose scan-table selection, editable tunings, and service scanning."""

    tuningsChanged = Signal()
    knownTablesChanged = Signal()

    def __init__(self, app: AppController) -> None:
        super().__init__(app)
        self._app = app
        app.tuningsChanged.connect(self.tuningsChanged.emit)
        app.knownTablesChanged.connect(self.knownTablesChanged.emit)

    @Property("QVariantList", notify=tuningsChanged)
    def tunings(self) -> list[dict[str, object]]:
        return [
            {
                "index": index,
                "frequency": tuning.frequency_hz / 1_000_000,
                "bandwidth": tuning.bandwidth_hz // 1_000_000,
            }
            for index, tuning in enumerate(self._app._profile.tunings)
        ]

    @Property("QVariantList", notify=knownTablesChanged)
    def knownTables(self) -> list[str]:
        return self._app._known_tables

    @Slot()
    def refreshKnownTables(self) -> None:
        """Fetch the available LinuxTV DVB-T table names in the worker pool."""
        app = self._app
        app._set_status("Loading known scan tables...")

        def complete(names: object) -> None:
            app._known_tables = list(names)  # type: ignore[arg-type]
            app.knownTablesChanged.emit()
            app._set_status(f"Loaded {len(app._known_tables)} scan tables")

        app._run_task(known_table_names, complete, lane="general")

    @Slot(str)
    def selectScanTable(self, name: str) -> None:
        """Parse one known or local scan table into the editable tuning list."""
        if not name.strip():
            return
        app = self._app

        def complete(tunings: object) -> None:
            parsed = tuple(tunings)  # type: ignore[arg-type]
            if not parsed:
                app._set_status(f"No DVB-T tunings found in {name}")
                return
            app._profile.scan_table = name
            app._profile.tunings = parsed
            app.tuningsChanged.emit()
            app.stateChanged.emit()
            app._set_status(f"Loaded {len(parsed)} tunings from {name}")

        app._set_status(f"Parsing scan table {name}...")
        app._run_task(lambda: parse_table(name), complete, lane="general")

    @Slot(str)
    def loadLocalScanTable(self, path: str) -> None:
        """Load a local scan-table file into the tuning list."""
        local_path = self._app._local_path(path)
        if local_path:
            self.selectScanTable(local_path)

    @Slot(str, str)
    def addTuning(self, frequency_mhz: str, bandwidth_mhz: str) -> None:
        """Append a manually entered DVB-T frequency and bandwidth."""
        tuning = self._parse_tuning(frequency_mhz, bandwidth_mhz)
        if tuning is None:
            return
        self._app._profile.tunings += (tuning,)
        self._app.tuningsChanged.emit()
        self._app.stateChanged.emit()

    @Slot(int, str, str)
    def updateTuning(self, index: int, frequency_mhz: str, bandwidth_mhz: str) -> None:
        """Replace one tuning with manually edited frequency and bandwidth values."""
        app = self._app
        if not 0 <= index < len(app._profile.tunings):
            return
        tuning = self._parse_tuning(frequency_mhz, bandwidth_mhz)
        if tuning is None:
            return
        tunings = list(app._profile.tunings)
        tunings[index] = tuning
        app._profile.tunings = tuple(tunings)
        app.tuningsChanged.emit()
        app.stateChanged.emit()

    @Slot(int)
    def removeTuning(self, index: int) -> None:
        """Remove one tuning by its displayed list index."""
        app = self._app
        if 0 <= index < len(app._profile.tunings):
            app._profile.tunings = (
                app._profile.tunings[:index] + app._profile.tunings[index + 1 :]
            )
            app.tuningsChanged.emit()
            app.stateChanged.emit()

    @Slot()
    def scan(self) -> None:
        """Scan configured tunings and publish the resulting service map."""
        app = self._app
        if app._server is not None:
            app._set_status("Stop the server before scanning")
            return
        device = app._selected_device()
        if device is None or not device.adapters:
            app._set_status("Initialize a device before scanning")
            return
        if not app._profile.tunings:
            app._set_status("Load a scan table or add a tuning first")
            return
        profile = app._profile
        adapter = app._selected_adapter(device)
        adapter_index = profile.selected_adapter
        tunings = profile.tunings
        app._set_status(f"Scanning {len(tunings)} tuning(s)...")

        def scan() -> DVBServiceMap | None:
            if (
                device not in app._devices
                or adapter_index >= len(device.adapters)
                or device.adapters[adapter_index] is not adapter
            ):
                return None
            return map_from_tunings(tunings, adapter)

        def complete(service_map: object) -> None:
            if service_map is None:
                app._set_status("Selected device is no longer available")
                return
            if not isinstance(service_map, DVBServiceMap):
                raise TypeError("scan worker returned an unexpected result")
            if (
                app._profile is not profile
                or app._selected_device() is not device
                or app._profile.selected_adapter != adapter_index
                or app._profile.tunings != tunings
            ):
                app._set_status("Scan results discarded because the profile or device changed")
                return
            app._profile.service_map = service_map
            app.channelsChanged.emit()
            app.stateChanged.emit()
            app._set_status(f"Scan complete: {len(service_map.services)} service(s) found")

        app._run_task(
            scan,
            complete,
            lane="device",
        )

    def _parse_tuning(self, frequency_mhz: str, bandwidth_mhz: str) -> DVBTuning | None:
        try:
            frequency = float(frequency_mhz)
            bandwidth = int(bandwidth_mhz)
            if not isfinite(frequency) or frequency <= 0 or bandwidth not in (6, 7, 8):
                raise ValueError
        except ValueError:
            self._app._set_status(
                "Enter a positive frequency and a 6, 7, or 8 MHz bandwidth"
            )
            return None
        return DVBTuning(
            frequency_hz=round(frequency * 1_000_000),
            bandwidth_hz=bandwidth * 1_000_000,
        )
