"""DVB transport-stream demuxer interface and AF9015 stream handling."""

from __future__ import annotations

from threading import Lock

from .bridge import AF9015Bridge
from ...driver_api.demux import DVBDemuxer


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
        endpoint: int = 0x84,
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
            endpoint=endpoint,
            packet_count=5 if full_speed else self.TS_PACKET_COUNT,
        )
        self._hw_pid_lock = Lock()
        self._hw_pid_slots: dict[int, int] = {}
        self._hw_pid_filter_enabled = False

    def get_config_dict(self) -> dict[str, object]:
        """Return demuxer configuration and current state for diagnostics."""
        """Return demuxer configuration and current state for diagnostics.

        Threading:
            Safe to call concurrently with lifecycle operations. The returned
            values are a synchronized snapshot and may change immediately
            after this method returns.
        """
        with self._state_condition:
            with self._hw_pid_lock:
                return {
                    "adapter_index": self.adapter_index,
                    "dual_mode": self._dual_mode,
                    "demodulator_address": self.demodulator_address,
                    "endpoint": self.endpoint,
                    "buffer_size": self._buffer_size,
                    "packet_count": self.packet_count,
                    "running": self._running,
                    "configured": self._configured,
                    "pid_filter_enabled": self._hw_pid_filter_enabled,
                    "pids": frozenset(self._hw_pid_slots),
                    "pid_slots": dict(self._hw_pid_slots),
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

    def _read_stream_raw(self, length: int, timeout_ms: int) -> bytes:
        return self.bridge.transport.bulk_read(self.endpoint, length, timeout_ms)

    def configure_one_hw_pid_filter(self, pid: int, enabled: bool = True) -> None:
        """
        Configure a single hardware PID filter for the given PID.

        Args:
            pid (int): The PID to configure the filter for.
            enabled (bool, optional): Whether to enable or disable the filter for this PID. Defaults to True.

        Raises:
            ValueError: If the PID is out of the valid range or if there are no available slots.
        """
        if not 0 <= pid <= 0x1FFF:
            raise ValueError("PID must be between 0 and 0x1FFF")

        with self._hw_pid_lock:
            if enabled:
                if pid in self._hw_pid_slots:
                    return
                if len(self._hw_pid_slots) >= self.PID_FILTER_COUNT:
                    raise ValueError("AF9015 PID filter has 32 slots")
                slot = next(
                    index
                    for index in range(self.PID_FILTER_COUNT)
                    if index not in self._hw_pid_slots.values()
                )
                self._configure_hw_pid_filter_slot(pid, slot, True)
                self._hw_pid_slots[pid] = slot
            else:
                slot = self._hw_pid_slots.pop(pid, None)
                if slot is not None:
                    self._configure_hw_pid_filter_slot(pid, slot, False)

    def clear_hw_pid_filters(self) -> None:
        """Disable and forget all currently configured hardware PID filter slots."""
        with self._hw_pid_lock:
            for pid, slot in sorted(self._hw_pid_slots.items(), key=lambda item: item[1]):
                self._configure_hw_pid_filter_slot(pid, slot, False)
                del self._hw_pid_slots[pid]

    def set_hw_pid_filter_enabled(self, enabled: bool = True) -> None:
        with self._hw_pid_lock:
            if enabled == self._hw_pid_filter_enabled:
                return
            self._set_hw_pid_filter_enabled(enabled)
            self._hw_pid_filter_enabled = enabled
            if enabled:
                pids = frozenset(self._hw_pid_slots)
                print(f"Hardware PID filter active with {len(pids)} PIDs: {sorted(pids)}")
            else:
                print(f"Hardware PID filter disabled")


    @property
    def hw_pid_filter_enabled(self) -> bool:
        with self._hw_pid_lock:
            return self._hw_pid_filter_enabled

    @property
    def hw_filter_pids(self) -> frozenset[int]:
        with self._hw_pid_lock:
            return frozenset(self._hw_pid_slots)

    def _configure_hw_pid_filter_slot(self, pid: int, slot: int, enabled: bool) -> None:
        """
        Actually configure the PID filter slot on the demodulator.

        Args:
            pid (int): The PID to set the filter for.
            slot (int): The slot number to assign to the PID.
            enabled (bool): Whether to enable or disable the filter for this PID.
        """
        if enabled:
            # Write the PID value to the filter value register before enabling the slot
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
            # Disable the PID filter slot by writing the slot number without the enable bit
            self.bridge.write_i2c(
                self.demodulator_address,
                self.AF9013_PID_FILTER_SLOT,
                bytes((slot,)),
                address_length=3,
            )

    def _set_hw_pid_filter_enabled(self, enabled: bool) -> None:
        current = self.bridge.read_i2c_byte(
            self.demodulator_address,
            self.AF9013_PID_FILTER_CONTROL,
            address_length=3,
        )
        self.bridge.write_i2c_byte(
            self.demodulator_address,
            self.AF9013_PID_FILTER_CONTROL,
            (current & ~0x01) | int(enabled),
            address_length=3,
        )
