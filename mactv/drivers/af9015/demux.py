"""DVB transport-stream demuxer interface and AF9015 stream handling."""

from __future__ import annotations

from threading import Lock

from .bridge import AF9015Bridge
from ...driverapi.demux import DVBDemuxer
from ...usb import STREAM_ENDPOINTS


class AF9015Demuxer(DVBDemuxer):
    """Read the transport stream for one logical AF9015 adapter.

    The demuxer controls the adapter's USB endpoint and the integrated AF9013
    demodulator's 32 hardware PID slots.
    """

    TS_PACKET_COUNT = 87        # Number of TS packets in the buffer for full-speed mode
    PID_FILTER_COUNT = 32
    AF9013_PID_FILTER_CONTROL = 0xD503  # Control register for the AF9013 PID filter
    AF9013_PID_FILTER_SLOT = 0xD504     # Slot register for the AF9013 PID filter
    AF9013_PID_FILTER_VALUE = 0xD505    # Value register for the AF9013 PID filter

    def __init__(
        self,
        bridge: AF9015Bridge,
        adapter_index: int = 0,
        dual_mode: bool = False,
        demodulator_address: int = 0x1C,
        full_speed: bool = False,
    ) -> None:
        if adapter_index not in (0, 1) or (adapter_index == 1 and not dual_mode):
            raise ValueError("adapter_index must be 0, or 1 for a dual tuner")
        if not 0 <= demodulator_address <= 0x7F:
            raise ValueError("demodulator address must be a 7-bit I2C address")
        self.bridge = bridge
        self.adapter_index = adapter_index
        self._dual_mode = dual_mode
        self.demodulator_address = demodulator_address
        self._buffer_size = 64 if full_speed else 512
        super().__init__(
            endpoint=STREAM_ENDPOINTS[adapter_index],
            packet_count=5 if full_speed else self.TS_PACKET_COUNT,
        )
        self._pid_lock = Lock()
        self._pid_slots: dict[int, int] = {}
        self._pid_filter_enabled = False

    def get_config_dict(self) -> dict[str, object]:
        """Return demuxer configuration and current state for diagnostics."""
        """Return demuxer configuration and current state for diagnostics.

        Threading:
            Safe to call concurrently with lifecycle operations. The returned
            values are a synchronized snapshot and may change immediately
            after this method returns.
        """
        with self._state_condition:
            with self._pid_lock:
                return {
                    "adapter_index": self.adapter_index,
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

    def _configure_stream(self) -> None:
        stream_register = 0xDD88 if self.adapter_index == 0 else 0xDD8A
        buffer_register = 0xDD0C if self.adapter_index == 0 else 0xDD0D
        buffer_words = self.packet_count * self.TS_PACKET_SIZE // 4
        self.bridge.write_memory(stream_register, buffer_words.to_bytes(2, "little"))
        self.bridge.write_memory(buffer_register, bytes((self._buffer_size // 4,)))
        self.bridge.update_memory_bits(0xD50B, 0x01, 0x01 if self._dual_mode else 0)
        self.bridge.update_memory_bits(0xD520, 0x10, 0x10 if self._dual_mode else 0)

    def _start_stream(self) -> None:
        if self.adapter_index == 0:
            self.bridge.update_memory_bits(0xDD13, 0x20, 0)
            self.bridge.update_memory_bits(0xDD11, 0x20, 0x20)
            self.bridge.update_memory_bits(0xD507, 0x04, 0)
        else:
            self.bridge.update_memory_bits(0xDD13, 0x40, 0)
            self.bridge.update_memory_bits(0xDD11, 0x40, 0x40)
            self.bridge.update_memory_bits(0xD50B, 0x02, 0)

    def _stop_stream(self) -> None:
        if self.adapter_index == 0:
            self.bridge.update_memory_bits(0xD507, 0x04, 0x04)
            self.bridge.update_memory_bits(0xDD11, 0x20, 0)
            self.bridge.update_memory_bits(0xDD13, 0x20, 0x20)
        else:
            self.bridge.update_memory_bits(0xD50B, 0x02, 0x02)
            self.bridge.update_memory_bits(0xDD11, 0x40, 0)
            self.bridge.update_memory_bits(0xDD13, 0x40, 0x40)

    def _read_stream(self, length: int, timeout_ms: int) -> bytes:
        return self.bridge.transport.bulk_read(self.endpoint, length, timeout_ms)

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
                slot = next(
                    index
                    for index in range(self.PID_FILTER_COUNT)
                    if index not in self._pid_slots.values()
                )
                self._set_pid_filter_slot(pid, slot, True)
                self._pid_slots[pid] = slot
            else:
                slot = self._pid_slots.pop(pid, None)
                if slot is not None:
                    self._set_pid_filter_slot(pid, slot, False)

    def enable_pid_filter(self, enabled: bool = True) -> None:
        if enabled == self._pid_filter_enabled:
            return
        self._set_pid_filter_enabled(enabled)
        self._pid_filter_enabled = enabled

    @property
    def pid_filter_enabled(self) -> bool:
        with self._pid_lock:
            return self._pid_filter_enabled

    @property
    def pids(self) -> frozenset[int]:
        with self._pid_lock:
            return frozenset(self._pid_slots)

    def _set_pid_filter_slot(self, pid: int, slot: int, enabled: bool) -> None:
        if enabled:
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
        else:
            self.bridge.write_i2c(
                self.demodulator_address,
                self.AF9013_PID_FILTER_SLOT,
                bytes((slot,)),
                address_length=3,
            )

    def _set_pid_filter_enabled(self, enabled: bool) -> None:
        self.bridge.write_i2c(
            self.demodulator_address,
            self.AF9013_PID_FILTER_CONTROL,
            bytes((int(enabled),)),
            address_length=3,
        )
