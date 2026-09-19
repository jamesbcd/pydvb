"""DVB transport-stream demuxer interface and AF9015 stream handling."""

from __future__ import annotations

from abc import ABC, abstractmethod
from threading import Lock

from .bridge import AF9015Bridge
from .usb import STREAM_ENDPOINTS


class Demuxer(ABC):
    @abstractmethod
    def start(self) -> None: ...

    @abstractmethod
    def stop(self) -> None: ...

    @abstractmethod
    def read_ts(self, timeout_ms: int = 2000) -> bytes: ...

    @abstractmethod
    def set_pid_filter(self, pid: int, enabled: bool = True) -> None: ...


class TSDemuxer(Demuxer):
    """Read one AF9015 USB transport stream and control its AF9013 filter.

    The AF9015 controls the USB endpoint, while the integrated AF9013
    demodulator owns the 32 hardware PID slots.
    """

    PID_FILTER_COUNT = 32
    TS_PACKET_SIZE = 188
    TS_PACKET_COUNT = 87
    AF9013_PID_FILTER_CONTROL = 0xD503
    AF9013_PID_FILTER_SLOT = 0xD504
    AF9013_PID_FILTER_VALUE = 0xD505

    def __init__(
        self,
        bridge: AF9015Bridge,
        adapter: int = 0,
        dual_mode: bool = False,
        demodulator_address: int = 0x1C,
        full_speed: bool = False,
    ) -> None:
        if adapter not in (0, 1) or (adapter == 1 and not dual_mode):
            raise ValueError("adapter must be 0, or 1 for a dual tuner")
        if not 0 <= demodulator_address <= 0x7F:
            raise ValueError("demodulator address must be a 7-bit I2C address")
        self.bridge = bridge
        self.adapter = adapter
        self._dual_mode = dual_mode
        self.demodulator_address = demodulator_address
        self.endpoint = STREAM_ENDPOINTS[adapter]
        self._buffer_size = 64 if full_speed else 512
        self.packet_count = 5 if full_speed else self.TS_PACKET_COUNT
        self._running = False
        self._configured = False
        self._pid_lock = Lock()
        self._pid_slots: dict[int, int] = {}
        self._pid_filter_enabled = False

    def get_config_dict(self) -> dict[str, object]:
        """Return demuxer configuration and current state for diagnostics."""
        return {
            "adapter": self.adapter,
            "dual_mode": self._dual_mode,
            "demodulator_address": self.demodulator_address,
            "endpoint": self.endpoint,
            "buffer_size": self._buffer_size,
            "packet_count": self.packet_count,
            "running": self._running,
            "configured": self._configured,
            "pid_filter_enabled": self._pid_filter_enabled,
            "pids": frozenset(self._pid_slots),
            "pid_slots": dict(self._pid_slots),
        }

    def start(self) -> None:
        if not self._configured:
            stream_register = 0xDD88 if self.adapter == 0 else 0xDD8A
            buffer_register = 0xDD0C if self.adapter == 0 else 0xDD0D
            buffer_words = self.packet_count * self.TS_PACKET_SIZE // 4
            self.bridge.write_memory(stream_register, buffer_words.to_bytes(2, "little"))
            self.bridge.write_memory(buffer_register, bytes((self._buffer_size // 4,)))
            self.bridge.update_memory_bits(0xD50B, 0x01, 0x01 if self._dual_mode else 0)
            self.bridge.update_memory_bits(0xD520, 0x10, 0x10 if self._dual_mode else 0)
            self._configured = True

        if self.adapter == 0:
            self.bridge.update_memory_bits(0xDD13, 0x20, 0)
            self.bridge.update_memory_bits(0xDD11, 0x20, 0x20)
            self.bridge.update_memory_bits(0xD507, 0x04, 0)
        else:
            self.bridge.update_memory_bits(0xDD13, 0x40, 0)
            self.bridge.update_memory_bits(0xDD11, 0x40, 0x40)
            self.bridge.update_memory_bits(0xD50B, 0x02, 0)
        self._running = True

    def stop(self) -> None:
        if not self._running:
            return
        if self.adapter == 0:
            self.bridge.update_memory_bits(0xD507, 0x04, 0x04)
            self.bridge.update_memory_bits(0xDD11, 0x20, 0)
            self.bridge.update_memory_bits(0xDD13, 0x20, 0x20)
        else:
            self.bridge.update_memory_bits(0xD50B, 0x02, 0x02)
            self.bridge.update_memory_bits(0xDD11, 0x40, 0)
            self.bridge.update_memory_bits(0xDD13, 0x40, 0x40)
        self._running = False

    def read_ts(self, timeout_ms: int = 2000) -> bytes:
        if not self._running:
            raise RuntimeError("demuxer is not started")
        return self.bridge.transport.bulk_read(
            self.endpoint, self.packet_count * self.TS_PACKET_SIZE, timeout_ms
        )

    def set_pid_filter(self, pid: int, enabled: bool = True) -> None:
        if not 0 <= pid <= 0x2000:
            raise ValueError("PID must be between 0 and 0x2000")
        if pid == 0x2000:
            return

        with self._pid_lock:
            if enabled:
                if pid in self._pid_slots:
                    return
                if len(self._pid_slots) >= self.PID_FILTER_COUNT:
                    raise ValueError("AF9015 PID filter has 32 slots")
                slot = next(index for index in range(self.PID_FILTER_COUNT) if index not in self._pid_slots.values())
                self.bridge.write_i2c(
                    self.demodulator_address,
                    self.AF9013_PID_FILTER_VALUE,
                    pid.to_bytes(2, "little"),
                    address_length=3,
                )
                self.bridge.write_i2c(
                    self.demodulator_address,
                    self.AF9013_PID_FILTER_SLOT,
                    bytes((0x20 | slot,)),
                    address_length=3,
                )
                self._pid_slots[pid] = slot
            else:
                slot = self._pid_slots.pop(pid, None)
                if slot is not None:
                    self.bridge.write_i2c(
                        self.demodulator_address,
                        self.AF9013_PID_FILTER_SLOT,
                        bytes((slot,)),
                        address_length=3,
                    )

    def enable_pid_filter(self, enabled: bool = True) -> None:
        """Enable or disable AF9013 hardware PID filtering."""
        if enabled == self._pid_filter_enabled:
            return
        self.bridge.write_i2c(
            self.demodulator_address,
            self.AF9013_PID_FILTER_CONTROL,
            bytes((int(enabled),)),
            address_length=3,
        )
        self._pid_filter_enabled = enabled

    @property
    def pid_filter_enabled(self) -> bool:
        return self._pid_filter_enabled

    @property
    def pids(self) -> frozenset[int]:
        return frozenset(self._pid_slots)
