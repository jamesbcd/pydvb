"""Per-adapter configuration and component composition."""

from dataclasses import dataclass

from .demux import TSDemuxer
from .frontend import AF9013Frontend
from .tuner import TDA18271Tuner


@dataclass(frozen=True)
class DTVAdapterConfig:
    """EEPROM-derived configuration for one demodulator/tuner chain."""

    demod_address: int
    clock_hz: int
    intermed_freq_hz: int
    mt2060_if: int
    tuner_id: int
    spectrum_inverted: bool


@dataclass
class DTVAdapter:
    """One independently tunable AF9015 adapter chain."""

    config: DTVAdapterConfig
    demuxer: TSDemuxer
    tuner: TDA18271Tuner | None
    frontend: AF9013Frontend | None
