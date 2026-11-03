"""Forward Python log records to the GUI controller."""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
import logging

from .controller import AppController


class QtLogHandler(logging.Handler):
    """Format Python log records and queue them for display in the GUI."""

    def __init__(self, controller: AppController) -> None:
        super().__init__()
        self._controller = controller
        self.setFormatter(
            logging.Formatter(
                "%(asctime)s %(levelname)s %(name)s: %(message)s",
                datefmt="%H:%M:%S",
            )
        )

    def filter(self, record: logging.LogRecord) -> bool:
        """Exclude verbose USB transfer traces from the GUI log."""
        return super().filter(record) and not (
            record.name == "pydvb.usb" and record.levelno < logging.INFO
        )

    def emit(self, record: logging.LogRecord) -> None:
        """Forward a formatted record through the controller's Qt signal."""
        try:
            self._controller.logMessageReceived.emit(self.format(record))
        except Exception:
            self.handleError(record)


@contextmanager
def capture_gui_logs(controller: AppController) -> Generator[None, None, None]:
    """Capture application logs, including server diagnostics, for one GUI run."""
    handler = QtLogHandler(controller)
    root_logger = logging.getLogger()
    server_logger = logging.getLogger("pydvb.server")
    original_root_level = root_logger.level
    original_server_level = server_logger.level
    root_logger.addHandler(handler)
    root_logger.setLevel(logging.INFO)
    server_logger.setLevel(logging.DEBUG)
    try:
        yield
    finally:
        root_logger.removeHandler(handler)
        root_logger.setLevel(original_root_level)
        server_logger.setLevel(original_server_level)
        handler.close()
