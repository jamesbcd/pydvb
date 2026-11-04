"""Launch the PyDVB Qt Quick desktop application."""

from __future__ import annotations

import os
import sys
from importlib.util import find_spec
from pathlib import Path

from PySide6.QtCore import QUrl
from PySide6.QtGui import QGuiApplication
from PySide6.QtQml import QQmlApplicationEngine
from PySide6.QtQuickControls2 import QQuickStyle

from .app_controller import AppController
from .logging_handler import capture_gui_logs


def _configure_media_environment() -> None:
    """Configure Qt's FFmpeg backend for reliable MPEG-TS playback."""
    os.environ["QT_MEDIA_BACKEND"] = "ffmpeg"


def main() -> int:
    """Start the QML application and return its exit status."""
    QQuickStyle.setStyle("Fusion") # if sys.platform == "darwin" else "Fusion")
    _configure_media_environment()
    application = QGuiApplication(sys.argv)
    application.setApplicationName("PyDVB")
    application.setOrganizationName("pydvb")

    engine = QQmlApplicationEngine()
    controller = AppController()
    application.setQuitOnLastWindowClosed(False)
    engine.rootContext().setContextProperty(
        "playerAvailable",
        find_spec("PySide6.QtMultimedia") is not None,
    )
    engine.rootContext().setContextProperty("controller", controller)
    controller.shutdownFinished.connect(application.quit)
    with capture_gui_logs(controller):
        engine.load(QUrl.fromLocalFile(str(Path(__file__).with_name("Main.qml"))))
        if not engine.rootObjects():
            return 1
        return application.exec()


if __name__ == "__main__":
    raise SystemExit(main())