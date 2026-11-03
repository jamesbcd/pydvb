"""GUI log history controller."""

from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtCore import QObject, Property, Signal, Slot

if TYPE_CHECKING:
    from ..app_controller import AppController

_MAX_LOG_MESSAGES = 1000


class LogController(QObject):
    """Expose and manage the bounded log history shown in the logs window."""

    logsChanged = Signal()

    def __init__(self, app: AppController) -> None:
        super().__init__(app)
        self._log_messages: list[str] = []

    @Property("QVariantList", notify=logsChanged)
    def logMessages(self) -> list[str]:
        """Return the recent formatted application log messages."""
        return self._log_messages.copy()

    @Slot()
    def clearLogs(self) -> None:
        """Clear the log messages shown in the GUI."""
        if self._log_messages:
            self._log_messages.clear()
            self.logsChanged.emit()

    @Slot(str)
    def _append_log_message(self, message: str) -> None:
        """Append a log message and discard the oldest entries when full."""
        self._log_messages.append(message)
        del self._log_messages[:-_MAX_LOG_MESSAGES]
        self.logsChanged.emit()
