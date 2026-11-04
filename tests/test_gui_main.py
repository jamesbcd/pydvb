import os
import sys

from pydvb.gui.__main__ import _configure_media_environment


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
