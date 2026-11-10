import os
import re
from pathlib import Path

from PyInstaller.utils.hooks import get_hook_config
from PyInstaller.utils.hooks.qt import add_qt6_dependencies, pyside6_library_info


_QML_IMPORT = re.compile(r"^\s*import\s+([A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*)")
_QML_URI = re.compile(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*")
_UNUSED_QT_PLUGIN_TYPES = {"platforminputcontexts", "qmltooling"}


def _module_files(module_dir: Path, styles: set[str]):
    """Yield QML module files without descending into other modules or styles."""
    for current, dirs, filenames in os.walk(module_dir):
        current_path = Path(current)
        dirs[:] = [
            name
            for name in dirs
            if not (current_path / name / "qmldir").is_file()
            and (not name.startswith("+") or name[1:] in styles)
        ]
        yield from (
            current_path / name
            for name in filenames
            if name == "qmldir" or name.endswith((".qml", ".js"))
        )


def _module_dependencies(module_dir: Path, styles: set[str]) -> set[str]:
    """Find QML imports and required module dependencies in a QML module."""
    dependencies = set()
    for path in _module_files(module_dir, styles):
        lines = path.read_text(encoding="utf-8").splitlines()
        for line in lines:
            dependency = None
            if path.name == "qmldir":
                fields = line.split()
                if fields and fields[0] == "depends" and len(fields) > 1:
                    dependency = fields[1]
                elif fields and fields[0] == "import" and len(fields) > 1:
                    dependency = fields[1]
            else:
                match = _QML_IMPORT.match(line)
                if match:
                    dependency = match.group(1)

            if dependency and _QML_URI.fullmatch(dependency):
                dependencies.add(dependency)

    return dependencies


def _collect_qml_modules(modules: list[str], styles: set[str]):
    """Collect only requested QML modules and their required QML dependencies."""
    qml_root = Path(pyside6_library_info.location["QmlImportsPath"]).resolve()
    module_dirs = set()
    initial_modules = set(modules)
    pending = list(modules)

    while pending:
        module = pending.pop()
        module_dir = qml_root.joinpath(*module.split("."))
        qmldir = module_dir / "qmldir"
        if not qmldir.is_file():
            if module in initial_modules:
                raise FileNotFoundError(
                    f"Required PySide6 QML module {module!r} is missing: {module_dir}"
                )
            continue

        relative_dir = module_dir.relative_to(qml_root)
        if relative_dir in module_dirs:
            continue
        module_dirs.add(relative_dir)
        pending.extend(_module_dependencies(module_dir, styles) - initial_modules)

    all_binaries, all_datas = pyside6_library_info.collect_qtqml_files()

    def is_allowed(source: str) -> bool:
        source_path = Path(source).resolve()
        try:
            relative_path = source_path.relative_to(qml_root)
        except ValueError:
            return False

        parents = list(relative_path.parents)
        for parent in parents:
            if parent == Path("."):
                continue
            parent_path = qml_root / parent
            if parent_path.name.startswith("+") and parent_path.name[1:] not in styles:
                return False
            if (parent_path / "qmldir").is_file() and parent not in module_dirs:
                return False

        return any(relative_path.is_relative_to(module_dir) for module_dir in module_dirs)

    return (
        [entry for entry in all_binaries if is_allowed(entry[0])],
        [entry for entry in all_datas if is_allowed(entry[0])],
    )


def _is_unused_qt_plugin(entry: tuple[str, str]) -> bool:
    """Identify Qt plugin categories unused by this application."""
    destination_parts = Path(entry[1]).parts
    if "plugins" not in destination_parts:
        return False
    plugin_type_index = destination_parts.index("plugins") + 1
    return (
        plugin_type_index < len(destination_parts)
        and destination_parts[plugin_type_index] in _UNUSED_QT_PLUGIN_TYPES
    )


def hook(hook_api):
    """Preserve PyInstaller Qt dependency handling with a curated QML resource set."""
    hiddenimports, binaries, datas = add_qt6_dependencies(__file__)
    modules = get_hook_config(hook_api, "PySide6.QtQml", "qml_modules") or []
    styles = set(get_hook_config(hook_api, "PySide6.QtQml", "qml_styles") or [])
    qml_binaries, qml_datas = _collect_qml_modules(modules, styles)

    hook_api.add_imports(*hiddenimports)
    binaries = [entry for entry in binaries if not _is_unused_qt_plugin(entry)]
    hook_api.add_binaries(binaries + qml_binaries)
    hook_api.add_datas(datas + qml_datas)
