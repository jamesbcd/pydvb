"""Tests for generating application icons."""

import struct
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from PySide6.QtGui import QImage

from scripts import generate_app_icon
from scripts.generate_app_icon import create_macos_app_icon, create_windows_app_icon


def test_create_macos_app_icon_renders_iconset_and_writes_icns(monkeypatch, tmp_path):
    svg_path = Path(__file__).parents[1] / "pydvb" / "gui" / "icons" / "tv-play.svg"
    output_path = tmp_path / "tv-play.icns"

    def run_iconutil(command, *, check):
        assert check is True
        iconset = Path(command[-1])
        expected = {
            f"icon_{size}x{size}{'@2x' if scale == 2 else ''}.png": size * scale
            for size in (16, 32, 128, 256, 512)
            for scale in (1, 2)
        }
        assert {path.name for path in iconset.iterdir()} == set(expected)
        for name, pixels in expected.items():
            image = QImage(str(iconset / name))
            assert not image.isNull()
            assert (image.width(), image.height()) == (pixels, pixels)
        Path(command[-2]).write_bytes(b"generated icon")

    monkeypatch.setattr(subprocess, "run", run_iconutil)
    create_macos_app_icon(svg_path, output_path)

    assert output_path.read_bytes() == b"generated icon"
    assert list(tmp_path.iterdir()) == [output_path]


def test_create_macos_app_icon_preserves_existing_icon_on_failure(monkeypatch, tmp_path):
    svg_path = Path(__file__).parents[1] / "pydvb" / "gui" / "icons" / "tv-play.svg"
    output_path = tmp_path / "tv-play.icns"
    output_path.write_bytes(b"existing icon")

    def fail_iconutil(command, *, check):
        raise subprocess.CalledProcessError(1, command)

    monkeypatch.setattr(subprocess, "run", fail_iconutil)
    with pytest.raises(subprocess.CalledProcessError):
        create_macos_app_icon(svg_path, output_path)

    assert output_path.read_bytes() == b"existing icon"
    assert list(tmp_path.iterdir()) == [output_path]


def test_create_macos_app_icon_rejects_invalid_svg(tmp_path):
    svg_path = tmp_path / "invalid.svg"
    svg_path.write_text("not an SVG")

    with pytest.raises(ValueError, match="Could not load application icon SVG"):
        create_macos_app_icon(svg_path, tmp_path / "tv-play.icns")


def test_create_windows_app_icon_contains_rendered_images(tmp_path):
    svg_path = Path(__file__).parents[1] / "pydvb" / "gui" / "icons" / "tv-play.svg"
    output_path = tmp_path / "tv-play.ico"
    create_windows_app_icon(svg_path, output_path)

    data = output_path.read_bytes()
    assert struct.unpack_from("<HHH", data) == (0, 1, 7)
    for index, size in enumerate((16, 24, 32, 48, 64, 128, 256)):
        width, height, colors, reserved, planes, depth, length, offset = (
            struct.unpack_from("<BBBBHHII", data, 6 + index * 16)
        )
        assert (width or 256, height or 256) == (size, size)
        assert (colors, reserved, planes, depth) == (0, 0, 1, 32)
        image = QImage.fromData(data[offset : offset + length], "PNG")
        assert not image.isNull()
        assert (image.width(), image.height()) == (size, size)
        assert any(
            image.pixelColor(x, y).alpha()
            for x in range(size)
            for y in range(size)
        )
    assert list(tmp_path.iterdir()) == [output_path]


def test_create_windows_app_icon_rejects_invalid_svg(tmp_path):
    svg_path = tmp_path / "invalid.svg"
    svg_path.write_text("not an SVG")

    with pytest.raises(ValueError, match="Could not load application icon SVG"):
        create_windows_app_icon(svg_path, tmp_path / "tv-play.ico")


@pytest.mark.parametrize(
    ("platform", "expected"), [("win32", [".ico"]), ("darwin", [".ico", ".icns"])]
)
def test_main_generates_icons_supported_on_platform(monkeypatch, platform, expected):
    outputs = []
    monkeypatch.setattr(generate_app_icon, "sys", SimpleNamespace(platform=platform))
    monkeypatch.setattr(
        generate_app_icon, "create_windows_app_icon", lambda source, target: outputs.append(target.suffix)
    )
    monkeypatch.setattr(
        generate_app_icon, "create_macos_app_icon", lambda source, target: outputs.append(target.suffix)
    )

    generate_app_icon.main()

    assert outputs == expected
