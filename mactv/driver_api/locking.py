"""Reusable locking helpers for hardware driver objects."""

from __future__ import annotations

from functools import wraps
from threading import RLock
from typing import Any, Callable


class ControlLockMixin:
    """Provide a replaceable reentrant lock for hardware control operations.

    An object created on its own owns its private lock. A device may replace
    that lock with the device-owned lock so related hardware objects serialize
    as one control domain.
    """

    def __init__(self, control_lock: RLock | None = None) -> None:
        """Create a private lock or accept one from owning-device wiring.

        Application callers normally omit ``control_lock``. Device and
        adapter composition code may provide the device-owned lock.
        """
        self._control_lock = control_lock if control_lock is not None else RLock()

    def bind_to_control_lock(self, control_lock: RLock) -> None:
        """Bind this object to a lock owned by another object.

        Owning-device wiring calls this method; application code should not
        replace locks after hardware has been configured.
        """
        self._control_lock = control_lock

    @property
    def control_lock(self) -> RLock:
        """Return the lock for driver-internal composition and sharing."""
        return self._control_lock


def with_control_lock(method: Callable[..., Any]) -> Callable[..., Any]:
    """Serialize one operation using the object's shared control lock.

    The lock is reentrant because frontend operations call tuner operations
    and gated register helpers in the same control sequence.

    Driver implementers apply this decorator to public hardware operations;
    application callers invoke the resulting method normally.
    """
    @wraps(method)
    def locked_method(self: ControlLockMixin, *args: Any, **kwargs: Any) -> Any:
        with self._control_lock:
            return method(self, *args, **kwargs)

    return locked_method
