"""Reusable DVB-T tuner types and lifecycle contract."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
from threading import RLock

from pydvb.serialize import serializable
from .locking import ControlLockMixin


@serializable
class TransmissionMode(str, Enum):
    AUTO = "auto"
    K2 = "2k"
    K8 = "8k"


@serializable
class GuardInterval(str, Enum):
    AUTO = "auto"
    GI_1_32 = "1/32"
    GI_1_16 = "1/16"
    GI_1_8 = "1/8"
    GI_1_4 = "1/4"


@serializable
class Hierarchy(str, Enum):
    AUTO = "auto"
    NONE = "none"
    LEVEL_1 = "1"
    LEVEL_2 = "2"
    LEVEL_4 = "4"


@serializable
class Modulation(str, Enum):
    AUTO = "auto"
    QPSK = "qpsk"
    QAM16 = "qam16"
    QAM64 = "qam64"


@serializable
class CodeRate(str, Enum):
    AUTO = "auto"
    RATE_1_2 = "1/2"
    RATE_2_3 = "2/3"
    RATE_3_4 = "3/4"
    RATE_5_6 = "5/6"
    RATE_7_8 = "7/8"
    NONE = "none"


@serializable
@dataclass(frozen=True)
class DVBTuning:
    frequency_hz: int
    bandwidth_hz: int = 8_000_000
    transmission_mode: TransmissionMode = TransmissionMode.AUTO
    guard_interval: GuardInterval = GuardInterval.AUTO
    hierarchy: Hierarchy = Hierarchy.AUTO
    modulation: Modulation = Modulation.AUTO
    code_rate_hp: CodeRate = CodeRate.AUTO
    code_rate_lp: CodeRate = CodeRate.AUTO


class DVBTuner(ControlLockMixin, ABC):
    """Common lifecycle and tuning operations for DVB-T tuners.

    The tuner normally shares its frontend's device-owned control lock so
    tuner I2C operations cannot interleave with demodulator operations.
    """

    def __init__(self, control_lock: RLock | None = None) -> None:
        """Create a tuner for standalone use or device composition."""
        super().__init__(control_lock)

    @abstractmethod
    def init_hardware(self) -> None:
        """Initialize the tuner hardware."""
        ...

    @abstractmethod
    def tune(self, tuning: DVBTuning) -> None:
        """Tune to the requested DVB-T channel."""
        ...

    @abstractmethod
    def get_if_frequency_hz(self) -> int:
        """Return the tuner's intermediate frequency."""
        ...

    @abstractmethod
    def sleep(self) -> None:
        """Put the tuner hardware into its low-power state."""
        ...

    @abstractmethod
    def get_config_dict(self) -> dict[str, object]:
        """Return tuner configuration and state for diagnostics."""
        ...
