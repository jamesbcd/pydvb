import os
import sys
from pathlib import Path

from pydvb.gui.__main__ import (
    _application_icon_path,
    _configure_media_environment,
    _main_qml_path,
)


def test_application_icon_path_uses_icns_on_macos(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")

    assert _application_icon_path() == (
        Path(__file__).parents[1] / "pydvb" / "gui" / "icons" / "app.icns"
    )


def test_application_icon_path_uses_svg_on_other_platforms(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")

    assert _application_icon_path() == (
        Path(__file__).parents[1] / "pydvb" / "gui" / "icons" / "app.svg"
    )


def test_main_qml_path_uses_source_file_when_not_frozen(monkeypatch):
    monkeypatch.delattr(sys, "_MEIPASS", raising=False)

    assert _main_qml_path() == Path(__file__).parents[1] / "pydvb" / "gui" / "Main.qml"


def test_save_settings_dialog_defaults_to_settings_filename():
    qml = _main_qml_path().read_text()
    save_dialog = qml.split("function showSaveSettingsDialog", maxsplit=1)[1].split(
        "\n    }", maxsplit=1
    )[0]

    assert 'saveSettingsDialog.currentFolder + "/Settings.pydvb"' in save_dialog
    assert "saveSettingsDialog.open()" in save_dialog


def test_main_qml_path_uses_pyinstaller_bundle_root_on_macos(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)

    assert _main_qml_path() == (
        tmp_path.parent / "Resources" / "pydvb" / "gui" / "Main.qml"
    )


def test_main_qml_path_uses_pyinstaller_bundle_root_on_other_platforms(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)

    assert _main_qml_path() == tmp_path / "pydvb" / "gui" / "Main.qml"


def test_configure_media_environment_leaves_macos_decoder_selection_to_qt(
    monkeypatch,
):
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.delenv("QT_FFMPEG_DECODING_HW_DEVICE_TYPES", raising=False)

    _configure_media_environment()

    assert os.environ["QT_MEDIA_BACKEND"] == "ffmpeg"
    assert "QT_FFMPEG_DECODING_HW_DEVICE_TYPES" not in os.environ


def test_configure_media_environment_preserves_explicit_decoder_selection(
    monkeypatch,
):
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setenv("QT_FFMPEG_DECODING_HW_DEVICE_TYPES", "videotoolbox")

    _configure_media_environment()

    assert os.environ["QT_FFMPEG_DECODING_HW_DEVICE_TYPES"] == "videotoolbox"

def test_configure_media_environment_does_not_change_other_platforms(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.delenv("QT_FFMPEG_DECODING_HW_DEVICE_TYPES", raising=False)

    _configure_media_environment()

    assert "QT_FFMPEG_DECODING_HW_DEVICE_TYPES" not in os.environ
