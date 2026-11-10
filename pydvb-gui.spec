# PyInstaller spec for the pydvb-gui windowed application.

from pathlib import Path

from PyInstaller.compat import is_darwin
from PyInstaller.utils.hooks import collect_all, collect_submodules


project_root = Path(SPECPATH)
gui_dir = project_root / "pydvb" / "gui"
firmware_dir = project_root / "pydvb" / "drivers" / "af9015" / "firmware"
qt_datas, qt_binaries, qt_hiddenimports = collect_all("PySide6")

a = Analysis(
    [str(gui_dir / "__main__.py")],
    pathex=[str(project_root)],
    binaries=qt_binaries,
    datas=[
        *qt_datas,
        (str(gui_dir / "Main.qml"), "."),
        (str(gui_dir / "Player.qml"), "."),
        (str(gui_dir / "icons"), "icons"),
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
    name="pydvb-gui",
)
if is_darwin:
    app = BUNDLE(
        coll,
        name="pydvb-gui.app",
        bundle_identifier="org.pydvb.gui",
    )
