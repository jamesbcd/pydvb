"""Launch the PyDVB Qt Quick desktop application."""

from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtCore import QUrl
from PySide6.QtGui import QGuiApplication
from PySide6.QtQml import QQmlApplicationEngine
from PySide6.QtQuickControls2 import QQuickStyle

from .controller import AppController


def main() -> int:
    """Start the QML application and return its exit status."""
    QQuickStyle.setStyle("macOS" if sys.platform == "darwin" else "Fusion")
    application = QGuiApplication(sys.argv)
    application.setApplicationName("PyDVB")
    application.setOrganizationName("pydvb")

    engine = QQmlApplicationEngine()
    controller = AppController()
    application.setQuitOnLastWindowClosed(False)
    engine.rootContext().setContextProperty("controller", controller)
    controller.shutdownFinished.connect(application.quit)
    engine.load(QUrl.fromLocalFile(str(Path(__file__).with_name("Main.qml"))))
    if not engine.rootObjects():
        return 1
    return application.exec()


if __name__ == "__main__":
    raise SystemExit(main())