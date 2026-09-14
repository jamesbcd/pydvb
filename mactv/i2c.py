"""Linux-like I2C client operations over the AF9015 bridge."""

from .bridge import AF9015Bridge


class Af9013TunerBus:
    """Expose the tuner bus behind an AF9013 digital I2C gate."""

    GATE_REGISTER = 0xD417
    GATE_MAILBOX = 1
    GATE_MASK = 0x08

    def __init__(self, bus: AF9015Bridge, demodulator_address: int) -> None:
        self.bus = bus
        self.demodulator_address = demodulator_address

    def _gate(self, enabled: bool) -> None:
        current = self.bus.read_i2c(
            self.demodulator_address,
            self.GATE_REGISTER,
            1,
            address_length=3,
            mailbox=self.GATE_MAILBOX,
        )[0]
        value = (current & ~self.GATE_MASK) | (self.GATE_MASK if enabled else 0)
        self.bus.write_i2c(
            self.demodulator_address,
            self.GATE_REGISTER,
            bytes((value,)),
            address_length=3,
            mailbox=self.GATE_MAILBOX,
        )

    def read(self, address: int, register: int, length: int) -> bytes:
        self._gate(True)
        try:
            return self.bus.read_i2c(address, register, length)
        finally:
            self._gate(False)

    def write(self, address: int, register: int, data: bytes) -> None:
        self._gate(True)
        try:
            self.bus.write_i2c(address, register, data)
        finally:
            self._gate(False)
