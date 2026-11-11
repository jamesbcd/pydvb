"""Base transport-stream buffering, synchronization, and read lifecycle.

Concrete demuxers implement the hardware-specific stream configuration,
start/stop, and raw-read hooks. This base class aligns the raw byte stream to
MPEG transport packets and synchronizes active reads with stream shutdown.
"""

from abc import ABC, abstractmethod
from threading import Condition


class DVBDemuxer(ABC):
    """Reusable DVB transport-stream demuxer functionality.

    Demuxer state and active-read synchronization are intentionally separate
    from the device frontend/control lock so ``stop()`` can interrupt a
    blocking stream read.

    Attributes:
        endpoint: Device-specific endpoint identifier used by the raw reader.
        packet_count: Number of transport-packet-sized units requested per raw
            read.
        _state_condition: Protects stream state and coordinates active readers
            with ``start()`` and ``stop()``.
        _active_reads: Number of reads currently inside the raw-read path
            (limited to one).
        _running: Whether stream delivery has been started.
        _configured: Whether the hardware stream has been configured.
        _ts_buffer: Bytes received but not yet returned as complete,
            synchronized transport packets.
        _ts_synchronized: Whether packet boundaries have been found in the
            current stream.
    """

    # MPEG transport-stream framing and the minimum run used to establish it.
    TS_PACKET_SIZE = 188
    MPEG_TS_SYNC_BYTE = 0x47
    SYNC_PACKET_COUNT = 4

    def __init__(self, endpoint: int, packet_count: int) -> None:
        """Create a demuxer for a device stream endpoint.

        Args:
            endpoint: Device-specific endpoint identifier passed to
                ``_read_stream_raw``.
            packet_count: Number of 188-byte packet lengths requested for each
                raw read.
        """
        self.endpoint = endpoint
        self.packet_count = packet_count
        self._state_condition = Condition()
        self._active_reads = 0
        self._running = False
        self._configured = False
        self._ts_buffer = bytearray()
        self._ts_synchronized = False

    def start(self) -> None:
        """Configure once, clear buffered framing state, and start streaming.

        Waits for any active read to finish before resetting the stream.
        """
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
        """Return whether stream delivery is currently active."""
        with self._state_condition:
            return self._running

    def stop(self) -> None:
        """Stop delivery after any active raw read has returned.

        A stopped demuxer discards data returned late by an in-flight read.
        """
        with self._state_condition:
            if not self._running:
                return
            self._running = False
            while self._active_reads:
                self._state_condition.wait()
            self._stop_stream()

    def read_stream_once(self, timeout_ms: int = 2000) -> bytes:
        """Read one packet-aligned demuxed stream chunk, which may be empty.

        Every non-empty result contains only complete 188-byte transport-stream
        packets. Empty results indicate that the demuxer is still synchronizing
        or buffering an incomplete packet; any incomplete packet tail is kept
        internally for the next call.
        """
        with self._state_condition:
            if not self._running:
                raise RuntimeError("demuxer is not started")
            if self._active_reads:
                raise RuntimeError("demuxer read is already in progress")
            self._active_reads = 1

        try:
            data = self._read_stream_raw(
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
    def _configure_stream(self) -> None:
        """Configure device-specific stream parameters before the first start."""
        ...

    @abstractmethod
    def _start_stream(self) -> None:
        """Enable the device-specific stream endpoint."""
        ...

    @abstractmethod
    def _stop_stream(self) -> None:
        """Disable the device-specific stream endpoint."""
        ...

    @abstractmethod
    def _read_stream_raw(self, length: int, timeout_ms: int) -> bytes:
        """Read up to ``length`` raw bytes, respecting the supplied timeout."""
        ...
