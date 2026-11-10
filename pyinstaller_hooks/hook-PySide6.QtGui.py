from pathlib import Path

from PyInstaller.utils.hooks.qt import add_qt6_dependencies


def hook(hook_api):
    """Keep PyInstaller's QtGui dependencies without its unused keyboard plugin."""
    hiddenimports, binaries, datas = add_qt6_dependencies(__file__)
    binaries = [
        entry
        for entry in binaries
        if not (
            Path(entry[1]).parts[-2:] == ("plugins", "platforminputcontexts")
        )
    ]

    hook_api.add_imports(*hiddenimports)
    hook_api.add_binaries(binaries)
    hook_api.add_datas(datas)
