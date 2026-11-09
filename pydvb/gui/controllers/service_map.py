"""Service-map display and channel scan controller."""

from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtCore import QObject, Property, QTimer, Signal, Slot

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

    def __init__(self, app: AppController) -> None:
        super().__init__(app)
        self._app = app
        app.channelsChanged.connect(self._channels_changed)
        app.channelsChanged.connect(self.stationStreamsChanged.emit)
        app.serverChanged.connect(self._sync_program_guide_refresh_timer)
        self._program_guide_refresh_timer = QTimer(self)
        self._program_guide_refresh_timer.setInterval(5000)
        self._program_guide_refresh_timer.timeout.connect(self.programGuideChanged.emit)
        self._sync_program_guide_refresh_timer()

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
        app._set_status(f"Scanning {len(tunings)} tuning(s)...")

        def on_service_count(count: int) -> None:
            app._set_status(f"Scanning {len(tunings)} tuning(s)... {count} channel(s) found")

        def scan() -> DVBServiceMap | None:
            if (
                device not in app._devices
                or adapter_index >= len(device.adapters)
                or device.adapters[adapter_index] is not adapter
            ):
                return None
            return map_from_tunings(tunings, adapter, on_service_count=on_service_count)

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
            app._set_status(f"Scan complete: {len(service_map.services)} channel(s) found")

        app._run_task(
            scan,
            complete,
            lane="device",
        )
