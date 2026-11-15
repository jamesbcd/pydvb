"""Service-map display and channel scan controller."""

from __future__ import annotations

from threading import Event
from typing import TYPE_CHECKING

from PySide6.QtCore import QObject, Property, QTimer, Qt, Signal, Slot

from pydvb.driver_api.tuner import DVBTuning
from pydvb.service_map import DVBServiceMap, map_from_tunings
from pydvb.stream_scanner import DVBEvent

if TYPE_CHECKING:
    from ..app_controller import AppController


def _format_event_time(event: DVBEvent | None) -> str:
    """Return an event's start and stop times in the system's local timezone."""
    if event is None or event.start_time is None or event.duration is None:
        return ""
    start_time = event.start_time.astimezone()
    stop_time = (event.start_time + event.duration).astimezone()
    return f"{start_time:%H:%M}–{stop_time:%H:%M}"


class ChannelController(QObject):
    """Expose the current service map to the TV stations view."""

    channelsChanged = Signal()
    programGuideChanged = Signal()
    programGuideEnabledChanged = Signal()
    stationStreamsChanged = Signal()
    scanStateChanged = Signal()
    _scanProgress = Signal(str)

    def __init__(self, app: AppController) -> None:
        super().__init__(app)
        self._app = app
        self._scan_active = False
        self._scan_cancel_event: Event | None = None
        self._scanProgress.connect(
            self._set_scan_progress_status,
            Qt.ConnectionType.QueuedConnection,
        )
        app.channelsChanged.connect(self._channels_changed)
        app.channelsChanged.connect(self.stationStreamsChanged.emit)
        app.serverChanged.connect(self._sync_program_guide_refresh_timer)
        self._program_guide_refresh_timer = QTimer(self)
        self._program_guide_refresh_timer.setInterval(5000)
        self._program_guide_refresh_timer.timeout.connect(self.programGuideChanged.emit)
        self._sync_program_guide_refresh_timer()

    @Property(bool, notify=scanStateChanged)
    def scanActive(self) -> bool:
        """Return whether a tuning scan is active or queued."""
        return self._scan_active

    @Slot(str)
    def _set_scan_progress_status(self, message: str) -> None:
        """Display progress emitted by the scan worker on the Qt thread."""
        self._app._set_status(message)

    @Slot()
    def cancelScan(self) -> None:
        """Request cooperative cancellation of the current tuning scan."""
        if self._scan_cancel_event is None:
            return
        self._scan_cancel_event.set()
        self._app._set_status("Cancelling scan...")

    @Slot()
    def _finish_scan(self) -> None:
        """Clear scan state after the worker and completion callback finish."""
        self._scan_active = False
        self._scan_cancel_event = None
        self.scanStateChanged.emit()

    @Slot()
    def _sync_program_guide_refresh_timer(self) -> None:
        """Refresh guide bindings only while enabled and the server is running."""
        if (
            self.programGuideEnabled
            and self._app._server_state == "Running"
        ):
            if not self._program_guide_refresh_timer.isActive():
                self._program_guide_refresh_timer.start()
        else:
            self._program_guide_refresh_timer.stop()

    def _channels_changed(self) -> None:
        """Notify channel bindings and refresh guide bindings only when enabled."""
        self.channelsChanged.emit()
        self.programGuideEnabledChanged.emit()
        if self.programGuideEnabled:
            self.programGuideChanged.emit()

    @Property(bool, notify=programGuideEnabledChanged)
    def programGuideEnabled(self) -> bool:
        """Return whether program-guide display and event scanning are enabled."""
        return self._app._settings.program_guide_enabled

    @programGuideEnabled.setter
    def programGuideEnabled(self, value: bool) -> None:
        if type(value) is not bool:
            raise ValueError("programGuideEnabled must be a bool")
        app = self._app
        if app._settings.program_guide_enabled == value:
            return
        app._settings.program_guide_enabled = value
        if app._server is not None:
            app._server.scan_dvb_events = value
        self._sync_program_guide_refresh_timer()
        self.programGuideEnabledChanged.emit()
        self.programGuideChanged.emit()

    @Property("QVariantList", notify=channelsChanged)
    def channels(self) -> list[dict[str, object]]:
        service_map = self._app._settings.service_map
        if service_map is None:
            return []
        rows: list[dict[str, object]] = []
        for service in service_map.services_sorted():
            triplet_id = service.triplet_id
            stream_info = (
                service_map.stream_infos.get(triplet_id)
                if triplet_id is not None
                else None
            )
            rows.append(
                {
                    "lcn": service.logical_channel_number,
                    "name": service.name or "Unnamed service",
                    "type": getattr(service.service_type, "name", "Other"),
                    "metatype": service.service_metatype.value,
                    "id": triplet_id.dotted() if triplet_id is not None else "Unknown",
                    "networkName": (
                        stream_info.network_name or "Unknown Network"
                        if stream_info is not None
                        else "Unknown Network"
                    ),
                }
            )
        return rows

    @Property("QVariantList", notify=programGuideChanged)
    def programGuide(self) -> list[dict[str, object]]:
        """Return channel rows with present and following event metadata."""
        if not self.programGuideEnabled:
            return []
        service_map = self._app._settings.service_map
        if service_map is None:
            return []
        rows: list[dict[str, object]] = []
        for service in service_map.services_sorted():
            current_event = service.current_event
            next_event = service.next_event
            rows.append(
                {
                    "lcn": service.logical_channel_number,
                    "name": service.name or "Unnamed service",
                    "id": (
                        service.triplet_id.dotted()
                        if service.triplet_id is not None
                        else "Unknown"
                    ),
                    "now": current_event.name or "" if current_event is not None else "",
                    "nowDescription": (
                        current_event.text or "" if current_event is not None else ""
                    ),
                    "nowTime": _format_event_time(current_event),
                    "next": next_event.name or "" if next_event is not None else "",
                    "nextDescription": (
                        next_event.text or "" if next_event is not None else ""
                    ),
                    "nextTime": _format_event_time(next_event),
                }
            )
        return rows

    @Property(bool, notify=channelsChanged)
    def hasChannelMap(self) -> bool:
        """Report whether a channel map is loaded, even if it has no services."""
        return self._app._settings.service_map is not None

    @Property("QVariantList", notify=stationStreamsChanged)
    def stationStreams(self) -> list[dict[str, object]]:
        service_map = self._app._settings.service_map
        if service_map is None:
            return []
        rows: list[dict[str, object]] = []
        for doublet_id, tuning in service_map.tunings_by_doublet.items():
            stream_info = service_map.stream_infos_by_doublet.get(doublet_id)
            if stream_info is not None:
                service_count = len(stream_info.services)
                rows.append(
                    {
                        "networkName": stream_info.network_name or "Unknown Network",
                        "dvbId": doublet_id.dotted() if doublet_id is not None else "Unknown",
                        "channels": service_count,
                        "frequency": f"{tuning.frequency_hz / 1_000_000:.3f} MHz",
                        "bandwidth": f"{tuning.bandwidth_hz / 1_000_000:.1f} MHz",
                    }
                )
        return rows

    @Slot()
    def scan(self) -> None:
        """Scan through the device lane and publish results if inputs stay current."""
        app = self._app
        if self._scan_active:
            return
        if app._server is not None:
            app._set_status("Stop the server before scanning")
            return
        device = app._selected_device()
        if device is None or not device.adapters:
            app._set_status("Initialize a device before scanning")
            return
        if not app._settings.tunings:
            app._set_status("Load a scan table or add a tuning first")
            return
        settings = app._settings
        adapter = app._selected_adapter(device)
        adapter_index = settings.selected_adapter
        tunings = settings.tunings
        cancel_event = Event()
        self._scan_cancel_event = cancel_event
        self._scan_active = True
        self.scanStateChanged.emit()
        app._set_status(
            f"Preparing to scan {len(tunings)} tuning(s)... "
            "0 discovered, 0 mapped"
        )

        current_tuning = 0
        current_frequency_hz = 0
        service_count = 0
        mapped_count = 0

        def report_progress() -> None:
            if current_tuning == 0:
                return
            self._scanProgress.emit(
                f"Scanning tuning {current_tuning}/{len(tunings)} "
                f"({current_frequency_hz / 1_000_000:.3f} MHz)... "
                f"{service_count} discovered, {mapped_count} mapped"
            )

        def on_tuning_progress(index: int, total: int, tuning: DVBTuning) -> None:
            nonlocal current_tuning, current_frequency_hz
            current_tuning = index
            current_frequency_hz = tuning.frequency_hz
            report_progress()

        def on_service_count(count: int) -> None:
            nonlocal service_count
            service_count = count
            report_progress()

        def on_mapped_count(count: int) -> None:
            nonlocal mapped_count
            mapped_count = count
            report_progress()

        def scan() -> DVBServiceMap | None:
            if (
                device not in app._devices
                or adapter_index >= len(device.adapters)
                or device.adapters[adapter_index] is not adapter
            ):
                return None
            return map_from_tunings(
                tunings,
                adapter,
                on_service_count=on_service_count,
                on_mapped_count=on_mapped_count,
                on_tuning_progress=on_tuning_progress,
                cancel_event=cancel_event,
            )

        def complete(service_map: object) -> None:
            if service_map is None:
                app._set_status("Selected device is no longer available")
                return
            if not isinstance(service_map, DVBServiceMap):
                raise TypeError("Scan worker thread returned an unexpected result")
            if (
                app._settings is not settings
                or app._selected_device() is not device
                or app._settings.selected_adapter != adapter_index
                or app._settings.tunings != tunings
            ):
                app._set_status("Scan results discarded because the settings or device changed")
                return
            app._settings.service_map = service_map
            app.channelsChanged.emit()
            app.stateChanged.emit()
            if cancel_event.is_set():
                app._set_status(
                    f"Scan cancelled: {service_count} discovered, "
                    f"{len(service_map.services)} mapped"
                )
            else:
                app._set_status(
                    f"Scan complete: {service_count} discovered, "
                    f"{len(service_map.services)} mapped"
                )
                app._maybe_auto_start_server()

        if not app._run_task(
            scan,
            complete,
            lane="device",
            on_finished=self._finish_scan,
        ):
            self._finish_scan()
