"""Generic frontend lifecycle and lock polling for DVB receivers."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from threading import RLock
from typing import Any

from .locking import ControlLockMixin


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


class DVBFrontend(ControlLockMixin, ABC):
    """Common operations shared by DVB frontends.

    A standalone frontend owns a private control lock. A device replaces it
    with one lock shared by all of that device's frontend and tuner objects.
    """

    def __init__(self, control_lock: RLock | None = None) -> None:
        """Create a frontend for standalone use or device composition.

        Subclass constructors may receive a device-owned lock; ordinary
        callers should let the frontend create its private lock.
        """
        super().__init__(control_lock)

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