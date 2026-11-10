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
qt_qml_modules = [
    "QtQuick",
    "QtQuick.Controls",
    "QtQuick.Controls.Fusion",
    "QtQuick.Dialogs",
    "QtQuick.Layouts",
]
if find_spec("PySide6.QtMultimedia") is not None:
    qt_qml_modules.append("QtMultimedia")


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
    hookspath=[str(project_root / "pyinstaller_hooks")],
    hooksconfig={
        "PySide6.QtQml": {
            "qml_modules": qt_qml_modules,
            "qml_styles": ["Fusion"],
        },
    },
    runtime_hooks=[],
    excludes=[
        "PySide6.Qt3DAnimation",
        "PySide6.Qt3DCore",
        "PySide6.Qt3DExtras",
        "PySide6.Qt3DInput",
        "PySide6.Qt3DLogic",
        "PySide6.Qt3DRender",
        "PySide6.Qt3DScene2D",
        "PySide6.Qt3DScene3D",
        "PySide6.QtBluetooth",
        "PySide6.QtCharts",
        "PySide6.QtDataVisualization",
        "PySide6.QtGraphs",
        "PySide6.QtHelp",
        "PySide6.QtHttpServer",
        "PySide6.QtLocation",
        "PySide6.QtMultimediaWidgets",
        "PySide6.QtNetworkAuth",
        "PySide6.QtNfc",
        "PySide6.QtOpenGLWidgets",
        "PySide6.QtPdf",
        "PySide6.QtPdfWidgets",
        "PySide6.QtPositioning",
        "PySide6.QtPrintSupport",
        "PySide6.QtQuick3D",
        "PySide6.QtQuick3DAssetUtils",
        "PySide6.QtQuick3DHelpers",
        "PySide6.QtQuick3DParticles",
        "PySide6.QtQuick3DRuntimeRender",
        "PySide6.QtQuick3DUtils",
        "PySide6.QtQuickWidgets",
        "PySide6.QtRemoteObjects",
        "PySide6.QtScxml",
        "PySide6.QtSensors",
        "PySide6.QtSerialBus",
        "PySide6.QtSerialPort",
        "PySide6.QtSpatialAudio",
        "PySide6.QtSql",
        "PySide6.QtStateMachine",
        "PySide6.QtTest",
        "PySide6.QtTextToSpeech",
        "PySide6.QtVirtualKeyboard",
        "PySide6.QtWebChannel",
        "PySide6.QtWebEngineCore",
        "PySide6.QtWebEngineQuick",
        "PySide6.QtWebEngineWidgets",
        "PySide6.QtWebSockets",
        "PySide6.QtWidgets",
        "PySide6.QtXml",
    ],
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
