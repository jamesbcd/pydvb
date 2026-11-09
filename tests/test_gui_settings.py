"""Tests for desktop GUI settings persistence."""

import json

import pytest

from pydvb.driver_api.tuner import DVBTuning
from pydvb.gui.settings import GuiSettings, load_settings, save_settings
from pydvb.service_map import DVBServiceMap


def test_gui_settings_round_trip(tmp_path):
    settings = GuiSettings(
        scan_table="au-Brisbane",
        tunings=(DVBTuning(frequency_hz=177_500_000, bandwidth_hz=7_000_000),),
        server_host="127.0.0.1",
        server_port=8090,
        selected_device="0413:6029",
        auto_discover_and_initialize_devices=False,
        show_unsupported_devices=True,
        program_guide_enabled=False,
        auto_start_server=True,
        service_map=DVBServiceMap(),
    )

    path = tmp_path / "settings.pydvb"
    save_settings(settings, path)

    serialized = json.loads(path.read_text())
    assert serialized["__type__"] == "GuiSettings"
    assert serialized["__data__"]["program_guide_enabled"] is False
    loaded = load_settings(path)

    assert loaded.schema_version == settings.schema_version
    assert loaded.scan_table == settings.scan_table
    assert loaded.tunings == settings.tunings
    assert loaded.server_host == settings.server_host
    assert loaded.server_port == settings.server_port
    assert loaded.selected_device == settings.selected_device
    assert loaded.selected_adapter == settings.selected_adapter
    assert loaded.auto_discover_and_initialize_devices is False
    assert loaded.show_unsupported_devices is True
    assert loaded.program_guide_enabled is False
    assert loaded.auto_start_server is True
    assert isinstance(loaded.service_map, DVBServiceMap)
    assert loaded.service_map.services == settings.service_map.services


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("schema_version", 2, "unsupported GUI settings version"),
        ("server_host", "", "server_host must not be empty"),
        ("server_port", 65536, "server_port must be between"),
        ("selected_adapter", -1, "selected_adapter must be non-negative"),
        ("auto_discover_and_initialize_devices", 1, "must be a bool"),
        ("show_unsupported_devices", 1, "must be a bool"),
        ("program_guide_enabled", 1, "program_guide_enabled must be a bool"),
        ("auto_start_server", 1, "auto_start_server must be a bool"),
        ("tunings", [DVBTuning(frequency_hz=177_500_000)], "tunings must be a tuple"),
        (
            "tunings",
            (DVBTuning(frequency_hz=177_500_000, bandwidth_hz=5_000_000),),
            "tunings must have a positive frequency",
        ),
    ],
)
def test_gui_settings_reject_invalid_values(field, value, message):
    with pytest.raises(ValueError, match=message):
        GuiSettings(**{field: value})


def test_load_settings_rejects_another_serializable_type(tmp_path):
    from pydvb.serialize import save

    path = tmp_path / "not-settings.pydvb"
    save(DVBServiceMap(), path)

    with pytest.raises(TypeError, match="does not contain GUI settings"):
        load_settings(path)


def test_older_settings_files_use_defaults_for_new_toggles(tmp_path):
    path = tmp_path / "settings.pydvb"
    save_settings(GuiSettings(), path)
    serialized = json.loads(path.read_text())
    serialized["__data__"].pop("auto_discover_and_initialize_devices")
    serialized["__data__"].pop("show_unsupported_devices")
    serialized["__data__"].pop("program_guide_enabled")
    serialized["__data__"].pop("auto_start_server")
    path.write_text(json.dumps(serialized))

    loaded = load_settings(path)

    assert loaded.auto_discover_and_initialize_devices is True
    assert loaded.show_unsupported_devices is False
    assert loaded.program_guide_enabled is True
    assert loaded.auto_start_server is False