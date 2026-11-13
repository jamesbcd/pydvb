"""Generate application icons from the GUI's SVG icon."""

import struct
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

from PySide6.QtCore import QBuffer, QIODevice, QRectF, Qt
from PySide6.QtGui import QImage, QPainter
from PySide6.QtSvg import QSvgRenderer


def _load_svg(svg_path: Path) -> QSvgRenderer:
    """Load and validate an SVG icon."""
    renderer = QSvgRenderer(str(svg_path))
    if not renderer.isValid():
        raise ValueError(f"Could not load application icon SVG: {svg_path}")
    return renderer


def _render_image(renderer: QSvgRenderer, size: int) -> QImage:
    """Render the icon at one square pixel size."""
    image = QImage(size, size, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    padding = size / 8
    renderer.render(
        painter, QRectF(padding, padding, size - 2 * padding, size - 2 * padding)
    )
    painter.end()
    return image


def create_macos_app_icon(svg_path: Path, icon_path: Path) -> None:
    """Render an SVG into the ICNS file used by the macOS app bundle."""
    renderer = _load_svg(svg_path)

    with TemporaryDirectory(prefix="pydvb-icon-", dir=icon_path.parent) as temp_dir:
        temp_path = Path(temp_dir)
        iconset_path = temp_path / "pydvb-gui.iconset"
        iconset_path.mkdir()

        for size in (16, 32, 128, 256, 512):
            for scale in (1, 2):
                pixel_size = size * scale
                image = _render_image(renderer, pixel_size)
                scale_suffix = "@2x" if scale == 2 else ""
                png_path = iconset_path / f"icon_{size}x{size}{scale_suffix}.png"
                if not image.save(str(png_path), "PNG"):
                    raise OSError(f"Could not write application icon image: {png_path}")

        generated_icon = temp_path / icon_path.name
        subprocess.run(
            [
                "/usr/bin/iconutil",
                "--convert",
                "icns",
                "--output",
                str(generated_icon),
                str(iconset_path),
            ],
            check=True,
        )
        generated_icon.replace(icon_path)


def create_windows_app_icon(svg_path: Path, icon_path: Path) -> None:
    """Render an SVG into a multi-resolution Windows ICO file."""
    renderer = _load_svg(svg_path)
    sizes = (16, 24, 32, 48, 64, 128, 256)
    images: list[bytes] = []
    for size in sizes:
        image = _render_image(renderer, size)
        buffer = QBuffer()
        if not buffer.open(QIODevice.OpenModeFlag.WriteOnly) or not image.save(
            buffer, "PNG"
        ):
            raise OSError(f"Could not encode application icon image: {size}x{size}")
        images.append(bytes(buffer.data()))

    # ICO directory entries point to PNG images; zero dimensions encode 256 pixels.
    directory = bytearray(struct.pack("<HHH", 0, 1, len(images)))
    offset = len(directory) + 16 * len(images)
    for size, image_data in zip(sizes, images):
        dimension = size if size < 256 else 0
        directory.extend(
            struct.pack(
                "<BBBBHHII", dimension, dimension, 0, 0, 1, 32, len(image_data), offset
            )
        )
        offset += len(image_data)

    with TemporaryDirectory(prefix="pydvb-icon-", dir=icon_path.parent) as temp_dir:
        generated_icon = Path(temp_dir) / icon_path.name
        generated_icon.write_bytes(bytes(directory) + b"".join(images))
        generated_icon.replace(icon_path)


def main() -> None:
    """Generate the Windows icon and, on macOS, the macOS icon."""
    icons_dir = Path(__file__).resolve().parents[1] / "pydvb" / "gui" / "icons"
    svg_path = icons_dir / "tv-play.svg"
    create_windows_app_icon(svg_path, icons_dir / "tv-play.ico")
    if sys.platform == "darwin":
        create_macos_app_icon(svg_path, icons_dir / "tv-play.icns")


if __name__ == "__main__":
    main()
