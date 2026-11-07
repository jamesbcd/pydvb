"""Tests for desktop GUI profile persistence."""

import pytest

from pydvb.driver_api.tuner import DVBTuning
from pydvb.gui.profile import GuiProfile, load_profile, save_profile
from pydvb.service_map import DVBServiceMap


def test_gui_profile_round_trips(tmp_path):
    profile = GuiProfile(
        scan_table="au-Brisbane",
        tunings=(DVBTuning(frequency_hz=177_500_000, bandwidth_hz=7_000_000),),
        server_host="127.0.0.1",
        server_port=8090,
        selected_device="0413:6029",
        show_program_guide_info=False,
        service_map=DVBServiceMap(),
    )

    path = tmp_path / "profile.json"
    save_profile(profile, path)

    loaded = load_profile(path)

    assert loaded.schema_version == profile.schema_version
    assert loaded.scan_table == profile.scan_table
    assert loaded.tunings == profile.tunings
    assert loaded.server_host == profile.server_host
    assert loaded.server_port == profile.server_port
    assert loaded.selected_device == profile.selected_device
    assert loaded.selected_adapter == profile.selected_adapter
    assert loaded.show_program_guide_info is False
    assert isinstance(loaded.service_map, DVBServiceMap)
    assert loaded.service_map.services == profile.service_map.services


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("schema_version", 2, "unsupported GUI profile version"),
        ("server_host", "", "server_host must not be empty"),
        ("server_port", 65536, "server_port must be between"),
        ("selected_adapter", -1, "selected_adapter must be non-negative"),
        ("show_program_guide_info", 1, "show_program_guide_info must be a bool"),
        ("tunings", [DVBTuning(frequency_hz=177_500_000)], "tunings must be a tuple"),
        (
            "tunings",
            (DVBTuning(frequency_hz=177_500_000, bandwidth_hz=5_000_000),),
            "tunings must have a positive frequency",
        ),
    ],
)
def test_gui_profile_rejects_invalid_values(field, value, message):
    with pytest.raises(ValueError, match=message):
        GuiProfile(**{field: value})


def test_load_profile_rejects_another_serializable_type(tmp_path):
    from pydvb.serialize import save

    path = tmp_path / "not-a-profile.json"
    save(DVBServiceMap(), path)

    with pytest.raises(TypeError, match="does not contain a GUI profile"):
        load_profile(path)