"""Reusable register access and masked-update helpers."""

from __future__ import annotations

from threading import RLock
from typing import Literal, Protocol


class RegisterIO(Protocol):
    """Low-level register access implemented by a device-specific backend."""

    def read(self, address: int, length: int) -> bytes:
        """Read consecutive register bytes starting at ``address``."""
        ...

    def write(self, address: int, data: bytes) -> None:
        """Write consecutive register bytes starting at ``address``."""
        ...


class RegisterMap:
    """Provide common byte access and serialized masked updates over register I/O."""

    def __init__(
        self,
        io: RegisterIO,
        value_bits: int = 8,
        byteorder: Literal["big", "little"] = "big",
    ) -> None:
        """Create a register map over a device-specific I/O implementation."""
        if value_bits <= 0 or value_bits % 8:
            raise ValueError("value_bits must be a positive multiple of 8")
        if byteorder not in ("big", "little"):
            raise ValueError("byteorder must be 'big' or 'little'")
        self.io = io
        self.value_bits = value_bits
        self.byteorder: Literal["big", "little"] = byteorder
        self._lock = RLock()

    @property
    def value_bytes(self) -> int:
        """Return the number of bytes in one register value."""
        return self.value_bits // 8

    def read(self, address: int, length: int = 1) -> bytes:
        """Read ``length`` consecutive bytes and reject short backend results."""
        if length <= 0:
            raise ValueError("length must be positive")
        with self._lock:
            data = self.io.read(address, length)
            if len(data) != length:
                raise IOError(f"short register read: expected {length} bytes, got {len(data)}")
            return data

    def write(self, address: int, data: bytes) -> None:
        """Write consecutive bytes through the device-specific backend."""
        with self._lock:
            self.io.write(address, bytes(data))

    def update_bits(self, address: int, mask: int, value: int) -> None:
        """Serialize a read/modify/write through this register map."""
        maximum = (1 << self.value_bits) - 1
        if not 0 <= mask <= maximum or not 0 <= value <= maximum:
            raise ValueError(f"mask and value must fit in {self.value_bits} bits")
        with self._lock:
            current = int.from_bytes(self.read(address, self.value_bytes), self.byteorder)
            updated = (current & ~mask) | (value & mask)
            self.write(address, updated.to_bytes(self.value_bytes, self.byteorder))