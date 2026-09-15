"""AF9015 USB bridge protocol."""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum
from threading import Lock

from .usb import ACK_LENGTH, BUFFER_LENGTH, CONTROL_IN, CONTROL_OUT, HEADER_LENGTH, UsbTransport


class Command(IntEnum):
    GET_CONFIG = 0x10
    DOWNLOAD_FIRMWARE = 0x11
    BOOT = 0x13
    READ_MEMORY = 0x20
    WRITE_MEMORY = 0x21
    READ_WRITE_I2C = 0x22
    COPY_FIRMWARE = 0x23
    RECONNECT_USB = 0x5A
    WRITE_VIRTUAL_MEMORY = 0x26
    GET_IR_CODE = 0x27
    READ_I2C = 0x28         # Pseudo-command, replaced by READ_WRITE_I2C on the wire
    WRITE_I2C = 0x29        # Pseudo-command, replaced by READ_WRITE_I2C on the wire


@dataclass(frozen=True)
class ControlRequest:
    command: Command
    i2c_address: int = 0
    address: int = 0        # Often the register address for I2C operations
    mailbox: int = 0
    address_length: int = 0
    data: bytes = b""
    # Linux uses the data field as a shared buffer for reads and writes. In Python, we create a separate buffer
    # for reads. We therefore need an explicit read_length field here to indicate how many bytes to read.
    read_length: int | None = None


class AF9015Bridge:
    """AF9015 command layer, equivalent to Linux ``af9015_ctrl_msg``."""

    def __init__(self, transport: UsbTransport, timeout_ms: int = 2000) -> None:
        self.transport = transport
        self.timeout_ms = timeout_ms
        self._sequence = 0
        self._lock = Lock()

    def transact(self, request: ControlRequest) -> bytes:
        if not 0 <= request.i2c_address <= 0x7F:
            raise ValueError("I2C address must be a 7-bit value")
        if not 0 <= request.address <= 0xFFFF:
            raise ValueError("address must fit in 16 bits")
        if not 0 <= request.mailbox <= 0xFF or not 0 <= request.address_length <= 3:
            raise ValueError("invalid mailbox or address length")
        is_read = request.command in (
            Command.GET_CONFIG,
            Command.READ_MEMORY,
            Command.READ_I2C,
            Command.RECONNECT_USB,
        )
        data_length = request.read_length if is_read and request.read_length is not None else len(request.data)
        if request.command == Command.RECONNECT_USB:
            data_length = 0
        max_data_length = BUFFER_LENGTH - ACK_LENGTH if is_read else BUFFER_LENGTH - HEADER_LENGTH
        if not 0 <= data_length <= max_data_length:
            raise ValueError(f"AF9015 control payload is limited to {max_data_length} bytes")

        with self._lock:
            # Note that READ_I2C and WRITE_I2c are pseudo-commands, and are both replaced with READ_WRITE_I2C
            # on the wire, but with bit 0 of the I2C address set to 0 for writes and 1 for reads. The rest of
            # the I2C address is shifted left by 1 to make room for this read/write indicator bit.
            command = request.command
            packet_command = command
            packet_i2c_address = request.i2c_address << 1
            if command == Command.READ_I2C:
                packet_command = Command.READ_WRITE_I2C
                packet_i2c_address |= 1
            elif command == Command.WRITE_I2C:
                packet_command = Command.READ_WRITE_I2C
            elif command == Command.WRITE_MEMORY and request.address & 0xFF00 in (0xFF00, 0xAE00):
                packet_command = Command.WRITE_VIRTUAL_MEMORY

            header = bytes(
                (
                    packet_command,
                    self._sequence,
                    packet_i2c_address,
                    request.address >> 8,
                    request.address & 0xFF,
                    request.mailbox,
                    request.address_length,
                    data_length,
                )
            )
            self._sequence = (self._sequence + 1) & 0xFF
            self.transport.bulk_write(CONTROL_OUT, header + (request.data if not is_read else b""), self.timeout_ms)
            if command in (Command.DOWNLOAD_FIRMWARE, Command.RECONNECT_USB):
                return b"" # No reply is expected for these commands, so return early.

            reply_length = ACK_LENGTH + (data_length if is_read else 0)
            reply = self.transport.bulk_read(CONTROL_IN, reply_length, self.timeout_ms)
            if len(reply) < ACK_LENGTH:
                raise IOError("short AF9015 acknowledgement")
            if reply[1] != 0:
                raise IOError(f"AF9015 command failed with status 0x{reply[1]:02x}")
            return reply[ACK_LENGTH:] if is_read else b""

    def read_memory(self, address: int, length: int) -> bytes:
        return self.transact(ControlRequest(Command.READ_MEMORY, address=address, read_length=length))

    def get_state(self) -> int:
        return self.transact(ControlRequest(Command.GET_CONFIG, read_length=1))[0]

    def write_memory(self, address: int, data: bytes) -> None:
        self.transact(ControlRequest(Command.WRITE_MEMORY, address=address, data=bytes(data)))

    def update_memory_bits(self, address: int, mask: int, value: int) -> None:
        current = self.read_memory(address, 1)[0]
        self.write_memory(address, bytes(((current & ~mask) | (value & mask),)))

    # In the Linux driver, af9015_write_reg_i2c() and af9015_read_reg_i2c() are narrow, one-byte register helpers.
    # Here, AF9015Bridge.read_i2c() and write_i2c() are more general Python wrappers around the same
    # AF9015 USB command protocol. The Python code is broadly wire-compatible, but it does not reproduce all of
    # the Linux helper defaults automatically.
    
    def read_i2c(
        self,
        address: int,
        register: int,
        length: int,
        address_length: int = 1,
        mailbox: int = 0,
    ) -> bytes:
        return self.transact(
            ControlRequest(
                Command.READ_I2C,
                address,
                register,
                mailbox=mailbox,
                address_length=address_length,
                read_length=length,
            )
        )

    def write_i2c(
        self,
        address: int,
        register: int,
        data: bytes,
        address_length: int = 1,
        mailbox: int = 0,
    ) -> None:
        self.transact(
            ControlRequest(
                Command.WRITE_I2C,
                address,
                register,
                mailbox=mailbox,
                address_length=address_length,
                data=bytes(data),
            )
        )

    def read_i2c_byte(self, address: int, register: int, address_length: int = 1) -> int:
        return self.read_i2c(address, register, 1, address_length)[0]

    def write_i2c_byte(self, address: int, register: int, value: int, address_length: int = 1) -> None:
        self.write_i2c(address, register, bytes((value & 0xFF,)), address_length)

    def download_firmware(self, firmware: bytes) -> None:
        max_payload_length = BUFFER_LENGTH - HEADER_LENGTH
        for offset in range(0, len(firmware), max_payload_length):
            chunk = firmware[offset : offset + max_payload_length]
            self.transact(ControlRequest(Command.DOWNLOAD_FIRMWARE, address=0x5100 + offset, data=chunk))
        self.transact(ControlRequest(Command.BOOT))

    def copy_firmware(self, firmware_size: int, firmware_checksum: int) -> None:
        payload = firmware_size.to_bytes(2, "big") + firmware_checksum.to_bytes(2, "big")
        self.transact(ControlRequest(Command.COPY_FIRMWARE, address=0x5100, data=payload))
