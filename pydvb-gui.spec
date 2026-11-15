# PyInstaller spec for the pydvb-gui windowed application.

from importlib.util import find_spec
from pathlib import Path

from PyInstaller.compat import is_darwin, is_win
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
    name="PyDVB",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    icon=str(gui_dir / "icons" / "app.ico") if is_win else None,
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
    app = BUNDLE(
        coll,
        name="PyDVB.app",
        bundle_identifier="org.pydvb.gui",
        icon=str(gui_dir / "icons" / "app.icns"),
    )
