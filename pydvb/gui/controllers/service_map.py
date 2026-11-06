"""Service-map display and channel scan controller."""

from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtCore import QObject, Property, Signal, Slot

from pydvb.service_map import DVBServiceMap, map_from_tunings

if TYPE_CHECKING:
    from ..app_controller import AppController


class ChannelController(QObject):
    """Expose the current service map to the TV stations view."""

    channelsChanged = Signal()
    stationStreamsChanged = Signal()

    def __init__(self, app: AppController) -> None:
        super().__init__(app)
        self._app = app
        app.channelsChanged.connect(self.channelsChanged.emit)
        app.channelsChanged.connect(self.stationStreamsChanged.emit)

    @Property("QVariantList", notify=channelsChanged)
    def channels(self) -> list[dict[str, object]]:
        service_map = self._app._profile.service_map
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
                    "id": triplet_id.dotted() if triplet_id is not None else "Unknown",
                    "networkName": (
                        stream_info.network_name or "Unknown Network"
                        if stream_info is not None
                        else "Unknown Network"
                    ),
                }
            )
        return rows

    @Property(bool, notify=channelsChanged)
    def hasChannelMap(self) -> bool:
        """Report whether a channel map is loaded, even if it has no services."""
        return self._app._profile.service_map is not None

    @Property("QVariantList", notify=stationStreamsChanged)
    def stationStreams(self) -> list[dict[str, object]]:
        service_map = self._app._profile.service_map
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
        if not app._profile.tunings:
            app._set_status("Load a scan table or add a tuning first")
            return
        profile = app._profile
        adapter = app._selected_adapter(device)
        adapter_index = profile.selected_adapter
        tunings = profile.tunings
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
            app._set_status(f"Scan complete: {len(service_map.services)} channel(s) found")

        app._run_task(
            scan,
            complete,
            lane="device",
        )

