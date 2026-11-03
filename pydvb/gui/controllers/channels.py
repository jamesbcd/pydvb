"""Channel-map display controller."""

from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtCore import QObject, Property, Signal

if TYPE_CHECKING:
    from ..app_controller import AppController


class ChannelController(QObject):
    """Expose the current service map to the channels view."""

    channelsChanged = Signal()

    def __init__(self, app: AppController) -> None:
        super().__init__(app)
        self._app = app
        app.channelsChanged.connect(self.channelsChanged.emit)

    @Property("QVariantList", notify=channelsChanged)
    def channels(self) -> list[dict[str, object]]:
        service_map = self._app._profile.service_map
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
        return self._app._profile.service_map is not None
