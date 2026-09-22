"""Generic frontend lifecycle and lock polling for DVB receivers."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class DVBFrontendStatus:
    """The frontend lock state and reception metrics."""

    signal: bool
    carrier: bool
    viterbi: bool
    sync: bool
    lock: bool
    signal_strength_dbm: int | None = None
    snr_db: float | None = None
    bit_errors: int | None = None
    block_errors: int | None = None
    bit_count: int | None = None
    block_count: int | None = None


class DVBFrontend(ABC):
    """Common operations shared by DVB frontends."""

    @abstractmethod
    def init_hardware(self) -> None:
        """Initialize the frontend hardware."""
        ...

    @abstractmethod
    def tune(self, tuning: Any, wait_for_lock: bool = True) -> DVBFrontendStatus:
        """Tune the frontend to the requested channel."""
        ...

    @abstractmethod
    def read_status(self) -> DVBFrontendStatus:
        """Read the current chipset-specific frontend status."""
        ...

    @abstractmethod
    def sleep(self) -> None:
        """Put the frontend hardware into its low-power state."""
        ...