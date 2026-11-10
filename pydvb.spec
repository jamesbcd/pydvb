# PyInstaller spec for the pydvb command-line application.

from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules


project_root = Path(SPECPATH)
firmware_dir = project_root / "pydvb" / "drivers" / "af9015" / "firmware"

a = Analysis(
    [str(project_root / "pydvb" / "__main__.py")],
    pathex=[str(project_root)],
    binaries=[],
    datas=[
        (str(firmware_dir / "dvb-usb-af9015.fw"), "pydvb/drivers/af9015/firmware"),
    ],
    hiddenimports=collect_submodules("pydvb.drivers"),
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
    name="pydvb",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,
    disable_windowed_traceback=False,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="pydvb",
)
