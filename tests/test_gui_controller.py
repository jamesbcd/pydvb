"""Tests for the hardware-independent desktop application controller."""

from __future__ import annotations

from threading import Event

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QCoreApplication, QThread, QTimer

from pydvb.driver_api.tuner import DVBTuning
from pydvb.gui.controller import AppController
from pydvb.gui.profile import GuiProfile
from pydvb.usb import USBDeviceID


@pytest.fixture(scope="module")
def qt_application():
    return QCoreApplication.instance() or QCoreApplication([])


def test_shutdown_waits_for_current_work_and_closes_server_before_devices(qt_application):
    events: list[str] = []
    operation_started = Event()

    class FakeServer:
        def shutdown(self):
            events.append("server shutdown")

        def close(self):
            events.append("server close")

    class FakeManager:
        def close(self):
            events.append("manager close")

    controller = AppController()
    controller._server = FakeServer()
    controller._manager = FakeManager()
    controller._run_task(
        lambda: (operation_started.set(), Event().wait(0.2)),
        lambda _result: None,
    )
    assert operation_started.wait(timeout=1)

    controller.shutdown()
    assert events == []

    controller.shutdownFinished.connect(
        lambda: (events.append("shutdown finished"), qt_application.quit())
    )
    QTimer.singleShot(3000, qt_application.quit)
    qt_application.exec()

    assert events == [
        "server shutdown",
        "server close",
        "manager close",
        "shutdown finished",
    ]


def test_shutdown_continues_device_cleanup_after_server_error(qt_application):
    events: list[str] = []

    class FakeServer:
        def shutdown(self):
            events.append("server shutdown")
            raise RuntimeError("shutdown failed")

        def close(self):
            events.append("server close")

    class FakeManager:
        def close(self):
            events.append("manager close")

    controller = AppController()
    controller._server = FakeServer()
    controller._manager = FakeManager()
    controller.shutdownFinished.connect(
        lambda: (events.append("shutdown finished"), qt_application.quit())
    )
    QTimer.singleShot(3000, qt_application.quit)

    controller.shutdown()
    qt_application.exec()

    assert events == [
        "server shutdown",
        "server close",
        "manager close",
        "shutdown finished",
    ]
    assert "shutdown failed" in controller.statusMessage


def test_discovery_is_blocked_while_server_owns_adapters(qt_application):
    controller = AppController()
    controller._server = object()

    controller.discover()

    assert not controller.busy
    assert controller.statusMessage == "Stop the server before rediscovering devices"


def test_worker_result_callback_runs_on_the_qt_thread(qt_application):
    controller = AppController()
    outcomes = []
    controller._run_task(
        lambda: QThread.currentThread() == qt_application.thread(),
        lambda result: (
            outcomes.append((QThread.currentThread() == qt_application.thread(), result)),
            qt_application.quit(),
        ),
    )
    QTimer.singleShot(2000, qt_application.quit)

    qt_application.exec()

    assert outcomes == [(True, False)]


def test_identical_devices_can_be_selected_independently(qt_application):
    controller = AppController()
    device_id = USBDeviceID(0x0413, 0x6029)
    first = type("Device", (), {"device_id": device_id, "model_name": "Receiver", "adapters": []})()
    second = type("Device", (), {"device_id": device_id, "model_name": "Receiver", "adapters": []})()
    controller._devices = [first, second]
    controller._select_first_device()

    controller.selectDevice(1)

    assert controller._selected_device() is second
    assert controller.devices[0]["selected"] is False
    assert controller.devices[1]["selected"] is True


def test_adapters_are_listed_and_selectable(qt_application):
    controller = AppController()
    device_id = USBDeviceID(0x0413, 0x6029)
    adapter_zero = type("Adapter", (), {"adapter_index": 0})()
    adapter_one = type("Adapter", (), {"adapter_index": 1})()
    device = type(
        "Device",
        (),
        {"device_id": device_id, "model_name": "Receiver", "adapters": [adapter_zero, adapter_one]},
    )()
    controller._devices = [device]
    controller._select_first_device()

    controller.selectAdapter(1)

    assert controller.adapters == [
        {
            "adapterIndex": 0,
            "name": "Adapter 0",
            "state": "Initialized",
            "signal": "Not polled",
            "selected": False,
        },
        {
            "adapterIndex": 1,
            "name": "Adapter 1",
            "state": "Initialized",
            "signal": "Not polled",
            "selected": True,
        },
    ]
    assert controller._selected_adapter(device) is adapter_one


def test_manual_tunings_can_be_added_edited_and_removed(qt_application):
    profile = GuiProfile(tunings=(DVBTuning(frequency_hz=650_000_000),))
    controller = AppController(profile=profile)

    controller.updateTuning(0, "666.5", "7")
    assert controller.tunings[0] == {
        "index": 0,
        "frequency": 666.5,
        "bandwidth": 7,
    }

    controller.addTuning("177.5", "8")
    assert len(controller.tunings) == 2
    controller.removeTuning(0)
    assert controller.tunings[0]["frequency"] == 177.5