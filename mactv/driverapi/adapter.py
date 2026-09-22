"""Generic adapter protocol for DVB receivers."""

from __future__ import annotations

from typing import Any, Protocol


class DVBAdapter(Protocol):
	tuner: object | None

	def sleep(self) -> None:
		...

	def tune(self, tuning: Any, wait_for_lock: bool = True) -> Any:
		...

	def read_status(self) -> Any:
		...

	def start(self) -> None:
		...

	def stop(self) -> None:
		...

	def read_ts(self, timeout_ms: int = 2000) -> bytes:
		...

	def set_pid_filter(self, pid: int, enabled: bool = True) -> None:
		...

	def enable_pid_filter(self, enabled: bool = True) -> None:
		...
