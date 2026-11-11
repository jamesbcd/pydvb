"""Frontend contracts and normalized reception-status values.

Chipset-specific frontends implement initialization, tuning, status, and sleep.
The base class supplies a shareable control lock so frontend and tuner hardware
operations can participate in their owning device's serialized control path.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from threading import RLock
from typing import Any

from .locking import ControlLockMixin


@dataclass(frozen=True)
class DVBFrontendStatus:
    """The frontend lock state and optional reception metrics.

    Attributes:
        signal: Signal-detection state.
        carrier: Carrier-detection state.
        viterbi: Forward-error-correction/Viterbi lock state.
        sync: Transport synchronization state.
        lock: Overall frontend lock state.
        signal_strength_dbm: Optional signal strength in dBm.
        snr_db: Optional signal-to-noise ratio in dB.
        bit_errors: Optional observed bit-error count.
        block_errors: Optional observed block-error count.
        bit_count: Optional number of bits represented by the error counter.
        block_count: Optional number of blocks represented by the error
            counter.
    """

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

    Attributes:
        adapter_index: Logical adapter index exposed by the concrete frontend.
        tuner: Associated tuner, or ``None`` if no tuner is available.
    """

    adapter_index: int
    tuner: object | None

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