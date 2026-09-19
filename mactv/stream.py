"""Threaded transport-stream sessions and bounded buffering."""

from __future__ import annotations

from collections.abc import Iterator
from enum import Enum
from queue import Empty, Full, Queue
from threading import Event, Lock, Thread, current_thread
from typing import Protocol


class StreamSource(Protocol):
    """The part of an adapter needed by a transport-stream session."""

    def start(self) -> None: ...

    def stop(self) -> None: ...

    def read_ts(self, timeout_ms: int = 2000) -> bytes: ...


class OverflowPolicy(str, Enum):
    BLOCK = "block"
    DROP_OLDEST = "drop_oldest"


class TSStream:
    """Read one transport stream, using a background producer thread.

    The intended model is:
        Lifecycle thread: creates, starts, and stops the stream.
        Producer thread: created internally by TSStream; reads from the adapter.
        Consumer thread: reads queued chunks and writes them to disk or serves them.
            The simplest model is for the consumer to run in the same lifecycle
            thread that created the TSStream instance.
    
    Only one consumer thread should call read() or iter_chunks() for a given TSStream.
    The queue itself is thread-safe, but multiple consumers would divide the chunks unpredictably.

    Uses a queue of data chunks. The queue is bounded so a slow consumer cannot
    grow memory without limit.
    Overflow policy:
    ``BLOCK`` preserves every chunk and is appropriate for recording.  The
    ``DROP_OLDEST`` policy keeps a live consumer close to the current stream.
    """

    def __init__(
        self,
        source: StreamSource,
        slot_count: int = 20,
        read_timeout_ms: int = 2000,
        overflow_policy: OverflowPolicy | str = OverflowPolicy.BLOCK,
    ) -> None:
        """Create a single-consumer transport-stream session.

        Args:
            source: Adapter or other object that produces transport-stream
                chunks.
            slot_count: Maximum number of chunks retained while the consumer
                is behind.
            read_timeout_ms: Timeout passed to each source read.
            overflow_policy: Whether to block the producer or discard the
                oldest queued chunk when the ring is full.

        Raises:
            ValueError: If a configuration value is invalid.

        Threading:
            Call from the thread that creates and owns the session, before
            sharing the session with its producer or consumer threads.
        """
        if slot_count <= 0:
            raise ValueError("slot_count must be positive")
        if read_timeout_ms < 0:
            raise ValueError("read_timeout_ms must be non-negative")
        try:
            overflow_policy = OverflowPolicy(overflow_policy)
        except ValueError as error:
            raise ValueError("unsupported overflow policy") from error
        self.source = source
        self.read_timeout_ms = read_timeout_ms
        self.overflow_policy = overflow_policy
        self._queue: Queue[bytes | None] = Queue(maxsize=slot_count)
        self._stop_event = Event()  # Event used to signal the producer thread to stop.
        self._state_lock = Lock()
        self._thread: Thread | None = None
        self._started = False
        self._source_stopped = False
        self._error: BaseException | None = None
        self._dropped_chunks = 0

    @property
    def dropped_chunks(self) -> int:
        """Return the number of chunks discarded by ``DROP_OLDEST``.

        Threading:
            Thread-safe. May be read from any thread.
        """
        with self._state_lock:
            return self._dropped_chunks

    @property
    def running(self) -> bool:
        """Return whether the background producer thread is still running.

        Threading:
            Safe to read from any thread. The result is a snapshot and may
            change immediately after it is returned.
        """
        thread = self._thread
        return thread is not None and thread.is_alive()

    def start(self) -> None:
        """Start the source and its background producer thread.

        A session is single-use and cannot be restarted after ``start()`` has
        been called. The source is started before the producer thread begins
        reading from it.

        Threading:
            Call from the main thread or one dedicated stream-lifecycle
            thread. Do not call concurrently with another ``start`` or
            ``stop`` call.

        Raises:
            RuntimeError: If this session has already been started.
        """
        with self._state_lock:
            if self._started:
                raise RuntimeError("stream cannot be restarted")
            self._error = None
            self._stop_event.clear()
            self._dropped_chunks = 0
            self._thread = Thread(target=self._run, name="mactv-ts-stream", daemon=True)
            self._started = True
        self.source.start()
        self._thread.start()

    def stop(self, join_timeout: float | None = None) -> None:
        """Stop the source and wait for the producer thread to exit.

        The source is stopped before joining so a blocking source read has an
        opportunity to be interrupted. Calling this method before ``start``
        is harmless. A finite ``join_timeout`` may leave the producer running.

        Threading:
            Call from the main thread or the dedicated stream-lifecycle
            thread. Do not call concurrently with another ``start`` or
            ``stop`` call. It is safe to call while the consumer is reading.
        """
        if not self._started:
            return
        self._stop_event.set()
        if not self._source_stopped:
            self.source.stop()
            self._source_stopped = True
        thread = self._thread
        if thread is not None and thread is not current_thread():
            thread.join(join_timeout)

    def read(self, timeout: float | None = None) -> bytes:
        """Return the next queued chunk.

        Args:
            timeout: Maximum seconds to wait for a chunk, or ``None`` to wait
                indefinitely.

        Raises:
            TimeoutError: If no chunk arrives before ``timeout``.
            EOFError: If the producer has stopped normally.
            BaseException: The exception raised by the producer if it failed.

        Threading:
            Queue access is thread-safe, but this session supports one
            consumer thread only. Call from that consumer thread; do not
            call concurrently with another ``read`` or ``iter_chunks`` call.
        """
        try:
            chunk = self._queue.get(timeout=timeout)
        except Empty:
            raise TimeoutError("timed out waiting for transport-stream data") from None
        if chunk is not None:
            return chunk
        if self._error is not None:
            raise self._error
        # If we reach this point, we have discovered the sentinel value indicating
        # the end of the stream.
        raise EOFError("transport-stream session has stopped")

    def iter_chunks(self, timeout: float | None = None) -> Iterator[bytes]:
        """Yield queued chunks until the producer stops normally.

        Producer exceptions are propagated to the caller. The timeout is
        applied independently to each call to ``read``.

        Threading:
            Iterate from one consumer thread only. The underlying queue is
            thread-safe, but concurrent consumers would divide the stream and
            could receive the end-of-stream marker unpredictably.

        Example::
            stream.start()
            try:
                for chunk in stream.iter_chunks(timeout=1.0):
                    output.write(chunk)
                    if output.tell() >= max_bytes:
                        break
            finally:
                stream.stop()
        """
        while True:
            try:
                yield self.read(timeout)
            except (EOFError, StopIteration):
                return

    def _run(self) -> None:
        """Read from the source until stopped, failed, or exhausted.
        
        Threading:
            Runs only on the background producer thread created by ``start``.
            Do not call directly.
        """
        try:
            while not self._stop_event.is_set():
                chunk = self.source.read_ts(self.read_timeout_ms)
                self._put_chunk(chunk)
        except BaseException as error:
            if not self._stop_event.is_set():
                self._error = error
        finally:
            # Ensure that a sentinel value is placed in the queue to signal the end of the stream.
            self._put_sentinel()

    def _put_chunk(self, chunk: bytes) -> None:
        """Place a chunk in the bounded queue according to the policy.
        
        Threading:
            Runs only on the background producer thread. Do not call directly
            or concurrently from a consumer thread.
        """
        if self.overflow_policy is OverflowPolicy.BLOCK:
            while not self._stop_event.is_set():
                try:
                    self._queue.put(chunk, timeout=0.1)
                    return
                except Full:
                    continue
            return

        # By this point, overflow policy must be DROP: remove the oldest chunk to make room
        # for the new one.
        try:
            self._queue.put_nowait(chunk)
        except Full:
            try:
                self._queue.get_nowait()
            except Empty:
                # If the queue is somehow empty (despite the Full exception), there's nothing
                # to remove, so just drop the new chunk by returning early.
                return
            with self._state_lock:
                self._dropped_chunks += 1
            try:
                self._queue.put_nowait(chunk)
            except Full:
                # If the queue is still full after removing the oldest chunk, just drop the
                # new chunk as well by returning early.
                return

    def _put_sentinel(self) -> None:
        """Mark the queue as finished without discarding pending chunks.
        
        Threading:
            Runs only on the background producer thread. Do not call directly
            or concurrently from a consumer thread.
        """
        while not self._stop_event.is_set():
            try:
                # Attempt to place the sentinel value in the queue, blocking briefly if necessary.
                # The sentinel object is None, which signals the end of the stream.
                self._queue.put(None, timeout=0.1)
                return
            except Full:
                continue

        # If the stop event is set, attempt to place the sentinel value in the queue
        # without blocking.
        try:
            # The sentinel object is None, which signals the end of the stream.
            self._queue.put_nowait(None)
        except Full:
            pass
