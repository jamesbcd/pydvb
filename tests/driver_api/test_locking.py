from threading import Event, RLock, Thread

from pydvb.driver_api.locking import ControlLockMixin, with_control_lock


class FakeHardware(ControlLockMixin):
    @with_control_lock
    def operation(self):
        return self.control_lock

    @with_control_lock
    def nested_operation(self):
        return self.operation()


def test_control_lock_binding_serializes_and_allows_nested_operations():
    hardware = FakeHardware()
    control_lock = RLock()
    hardware.bind_to_control_lock(control_lock)
    operation_started = Event()
    operation_finished = Event()
    result = []

    def run_operation():
        operation_started.set()
        result.append(hardware.nested_operation())
        operation_finished.set()

    with control_lock:
        worker = Thread(target=run_operation)
        worker.start()
        assert operation_started.wait(timeout=1)
        assert not operation_finished.wait(timeout=0.05)

    worker.join(timeout=1)

    assert not worker.is_alive()
    assert result == [control_lock]
    assert operation_finished.is_set()