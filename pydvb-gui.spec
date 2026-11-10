# PyInstaller spec for the pydvb-gui windowed application.

import subprocess
from importlib.util import find_spec
from pathlib import Path

from PyInstaller.compat import is_darwin
from PyInstaller.config import CONF
from PyInstaller.utils.hooks import collect_submodules


project_root = Path(SPECPATH)
gui_dir = project_root / "pydvb" / "gui"
firmware_dir = project_root / "pydvb" / "drivers" / "af9015" / "firmware"
qt_hiddenimports = ["PySide6.QtSvg"]
if find_spec("PySide6.QtMultimedia") is not None:
    qt_hiddenimports.append("PySide6.QtMultimedia")


def create_macos_app_icon(svg_path: Path) -> Path:
    """Render the GUI SVG as an ICNS file for the macOS app bundle."""
    from PySide6.QtCore import QRectF, Qt
    from PySide6.QtGui import QImage, QPainter
    from PySide6.QtSvg import QSvgRenderer

    work_path = Path(CONF["workpath"])
    iconset_path = work_path / "pydvb-gui.iconset"
    iconset_path.mkdir(parents=True, exist_ok=True)
    renderer = QSvgRenderer(str(svg_path))
    if not renderer.isValid():
        raise ValueError(f"Could not load application icon SVG: {svg_path}")

    for size in (16, 32, 128, 256, 512):
        for scale in (1, 2):
            pixel_size = size * scale
            image = QImage(
                pixel_size,
                pixel_size,
                QImage.Format.Format_ARGB32_Premultiplied,
            )
            image.fill(Qt.GlobalColor.transparent)
            painter = QPainter(image)
            renderer.render(painter, QRectF(0, 0, pixel_size, pixel_size))
            painter.end()
            scale_suffix = "@2x" if scale == 2 else ""
            png_path = iconset_path / f"icon_{size}x{size}{scale_suffix}.png"
            if not image.save(str(png_path), "PNG"):
                raise OSError(f"Could not write application icon image: {png_path}")

    icon_path = work_path / "pydvb-gui.icns"
    subprocess.run(
        [
            "/usr/bin/iconutil",
            "--convert",
            "icns",
            "--output",
            str(icon_path),
            str(iconset_path),
        ],
        check=True,
    )
    return icon_path


a = Analysis(
    [str(gui_dir / "__main__.py")],
    pathex=[str(project_root)],
    datas=[
        (str(gui_dir / "Main.qml"), "pydvb/gui"),
        (str(gui_dir / "Player.qml"), "pydvb/gui"),
        (str(gui_dir / "icons"), "pydvb/gui/icons"),
        (
            str(firmware_dir / "dvb-usb-af9015.fw"),
            "pydvb/drivers/af9015/firmware",
        ),
    ],
    hiddenimports=[
        *qt_hiddenimports,
        *collect_submodules("pydvb.drivers"),
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="pydvb-gui",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="PyDVB",
)
if is_darwin:
    app_icon = create_macos_app_icon(gui_dir / "icons" / "tv.svg")
    app = BUNDLE(
        coll,
        name="PyDVB.app",
        bundle_identifier="org.pydvb.gui",
        icon=str(app_icon),
    )
