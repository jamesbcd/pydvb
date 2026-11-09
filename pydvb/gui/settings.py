"""Persistent, hardware-independent settings for the desktop application."""

from __future__ import annotations

from dataclasses import dataclass
from os import PathLike

from pydvb.driver_api.tuner import DVBTuning
from pydvb.serialize import load, save, serializable
from pydvb.service_map import DVBServiceMap


SETTINGS_SCHEMA_VERSION = 1


@serializable
@dataclass
class GuiSettings:
    """Store GUI preferences, tuning inputs, and an optional scanned service map."""

    schema_version: int = SETTINGS_SCHEMA_VERSION
    scan_table: str | None = None
    tunings: tuple[DVBTuning, ...] = ()
    server_host: str = "127.0.0.1"
    server_port: int = 8080
    selected_device: str | None = None
    selected_adapter: int = 0
    auto_discover_and_initialize_devices: bool = True
    show_unsupported_devices: bool = False
    program_guide_enabled: bool = True
    service_map: DVBServiceMap | None = None

    def __post_init__(self) -> None:
        """Reject incompatible settings versions and invalid server/device settings."""
        if type(self.schema_version) is not int or self.schema_version != SETTINGS_SCHEMA_VERSION:
            raise ValueError(f"unsupported GUI settings version: {self.schema_version}")
        if self.scan_table is not None and not isinstance(self.scan_table, str):
            raise ValueError("scan_table must be a string or None")
        if self.selected_device is not None and not isinstance(self.selected_device, str):
            raise ValueError("selected_device must be a string or None")
        if not isinstance(self.server_host, str) or not self.server_host.strip():
            raise ValueError("server_host must not be empty")
        if type(self.server_port) is not int or not 0 <= self.server_port <= 65535:
            raise ValueError("server_port must be between 0 and 65535")
        if type(self.selected_adapter) is not int or self.selected_adapter < 0:
            raise ValueError("selected_adapter must be non-negative")
        if type(self.auto_discover_and_initialize_devices) is not bool:
            raise ValueError("auto_discover_and_initialize_devices must be a bool")
        if type(self.show_unsupported_devices) is not bool:
            raise ValueError("show_unsupported_devices must be a bool")
        if type(self.program_guide_enabled) is not bool:
            raise ValueError("program_guide_enabled must be a bool")
        if not isinstance(self.tunings, tuple) or any(
            not isinstance(tuning, DVBTuning) for tuning in self.tunings
        ):
            raise ValueError("tunings must be a tuple of DVBTuning values")
        if any(
            type(tuning.frequency_hz) is not int
            or tuning.frequency_hz <= 0
            or type(tuning.bandwidth_hz) is not int
            or tuning.bandwidth_hz not in (6_000_000, 7_000_000, 8_000_000)
            for tuning in self.tunings
        ):
            raise ValueError("tunings must have a positive frequency and 6, 7, or 8 MHz bandwidth")
        if self.service_map is not None and not isinstance(self.service_map, DVBServiceMap):
            raise ValueError("service_map must be a DVBServiceMap")


def save_settings(settings: GuiSettings, path: str | PathLike[str]) -> None:
    """Serialize GUI settings to a JSON file."""
    if not isinstance(settings, GuiSettings):
        raise TypeError("settings must be a GuiSettings")
    save(settings, path)


def load_settings(path: str | PathLike[str]) -> GuiSettings:
    """Load and validate GUI settings from a JSON file."""
    settings = load(path)
    if not isinstance(settings, GuiSettings):
        raise TypeError("file does not contain GUI settings")
    return settings