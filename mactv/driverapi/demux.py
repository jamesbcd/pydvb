"""Reusable DVB transport-stream demuxer functionality."""

from abc import ABC, abstractmethod
from threading import Condition


class DVBDemuxer(ABC):
    """Reusable DVB transport-stream demuxer functionality."""

    TS_PACKET_SIZE = 188
    MPEG_TS_SYNC_BYTE = 0x47
    SYNC_PACKET_COUNT = 4

    def __init__(self, endpoint: int, packet_count: int) -> None:
        self.endpoint = endpoint
        self.packet_count = packet_count
        self._state_condition = Condition()
        self._active_reads = 0
        self._running = False
        self._configured = False
        self._ts_buffer = bytearray()
        self._ts_synchronized = False

    def start(self) -> None:
        with self._state_condition:
            while self._active_reads:
                self._state_condition.wait()
            if not self._configured:
                self._configure_stream()
                self._configured = True
            self._ts_buffer.clear()
            self._ts_synchronized = False
            self._start_stream()
            self._running = True

    @property
    def running(self) -> bool:
        with self._state_condition:
            return self._running

    def stop(self) -> None:
        with self._state_condition:
            if not self._running:
                return
            self._running = False
            while self._active_reads:
                self._state_condition.wait()
            self._stop_stream()

    def read_ts(self, timeout_ms: int = 2000) -> bytes:
        with self._state_condition:
            if not self._running:
                raise RuntimeError("demuxer is not started")
            if self._active_reads:
                raise RuntimeError("demuxer read is already in progress")
            self._active_reads = 1

        try:
            data = self._read_stream(
                self.packet_count * self.TS_PACKET_SIZE, timeout_ms
            )
            with self._state_condition:
                if not self._running:
                    raise RuntimeError("demuxer is not started")
                self._ts_buffer.extend(data)
                if not self._ts_synchronized:
                    sync_run = self.TS_PACKET_SIZE * (self.SYNC_PACKET_COUNT - 1)
                    sync_start = next(
                        (
                            index
                            for index, value in enumerate(self._ts_buffer)
                            if value == self.MPEG_TS_SYNC_BYTE
                            and index + sync_run < len(self._ts_buffer)
                            and all(
                                self._ts_buffer[index + offset] == self.MPEG_TS_SYNC_BYTE
                                for offset in range(0, sync_run + 1, self.TS_PACKET_SIZE)
                            )
                        ),
                        None,
                    )
                    if sync_start is None:
                        if len(self._ts_buffer) > sync_run:
                            del self._ts_buffer[:-sync_run]
                        return b""
                    del self._ts_buffer[:sync_start]
                    self._ts_synchronized = True

                packet_bytes = len(self._ts_buffer) - len(self._ts_buffer) % self.TS_PACKET_SIZE
                result = bytes(self._ts_buffer[:packet_bytes])
                del self._ts_buffer[:packet_bytes]
                return result
        finally:
            with self._state_condition:
                self._active_reads = 0
                self._state_condition.notify_all()

    @abstractmethod
    def _configure_stream(self) -> None: ...

    @abstractmethod
    def _start_stream(self) -> None: ...

    @abstractmethod
    def _stop_stream(self) -> None: ...

    @abstractmethod
    def _read_stream(self, length: int, timeout_ms: int) -> bytes: ...

    def set_pid_filter(self, pid: int, enabled: bool = True) -> None:
        """Optionally configure a hardware PID filter."""

    def enable_pid_filter(self, enabled: bool = True) -> None:
        """Optionally enable or disable hardware PID filtering."""
