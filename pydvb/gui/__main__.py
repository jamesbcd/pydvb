"""Launch the PyDVB Qt Quick desktop application."""

from __future__ import annotations

import os
import sys
from importlib.util import find_spec
from pathlib import Path

from PySide6.QtCore import QStandardPaths, QUrl
from PySide6.QtGui import QGuiApplication, QIcon
from PySide6.QtQml import QQmlApplicationEngine
from PySide6.QtQuickControls2 import QQuickStyle

from pydvb.gui.app_controller import AppController
from pydvb.gui.logging_handler import capture_gui_logs


def _configure_media_environment() -> None:
    """Configure Qt's FFmpeg backend for reliable MPEG-TS playback."""
    os.environ["QT_MEDIA_BACKEND"] = "ffmpeg"


def _default_settings_path() -> Path:
    """Return the application-specific path for automatically persisted settings."""
    config_directory = QStandardPaths.writableLocation(
        QStandardPaths.StandardLocation.AppConfigLocation
    )
    return Path(config_directory) / "gui-settings.pydvb"


def _main_qml_path() -> Path:
    """Return the main QML path from either the source tree or frozen bundle."""
    bundle_root = getattr(sys, "_MEIPASS", None)
    if bundle_root is not None:
        resource_root = Path(bundle_root)
        if sys.platform == "darwin":
            resource_root = resource_root.parent / "Resources"
        return resource_root / "pydvb" / "gui" / "Main.qml"
    return Path(__file__).with_name("Main.qml")


def main() -> int:
    """Start the QML application and return its exit status."""
    QQuickStyle.setStyle("Fusion") # if sys.platform == "darwin" else "Fusion")
    _configure_media_environment()
    application = QGuiApplication(sys.argv)
    application.setApplicationName("PyDVB")
    application.setOrganizationName("pydvb")
    application.setWindowIcon(
        QIcon(str(_main_qml_path().parent / "icons" / "tv-play.svg"))
    )

    engine = QQmlApplicationEngine()
    controller = AppController(settings_path=_default_settings_path())
    application.setQuitOnLastWindowClosed(False)
    engine.setInitialProperties(
        {
            "controller": controller,
            "playerAvailable": find_spec("PySide6.QtMultimedia") is not None,
        }
    )
    controller.shutdownFinished.connect(application.quit)
    with capture_gui_logs(controller):
        engine.load(QUrl.fromLocalFile(str(_main_qml_path())))
        if not engine.rootObjects():
            return 1
        return application.exec()


if __name__ == "__main__":
    raise SystemExit(main())