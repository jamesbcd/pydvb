"""Tests for the hardware-independent desktop application controller."""

from __future__ import annotations

from threading import Event

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QCoreApplication, QThread, QTimer

from pydvb.driver_api.tuner import DVBTuning
from pydvb.device_manager import DeviceConnectionResult
from pydvb.gui.controller import AppController
from pydvb.gui.profile import GuiProfile
from pydvb.service_map import DVBServiceMap
from pydvb.usb import USBDeviceID, usb1


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
    class FakeManager:
        def __init__(self):
            self.discovery_count = 0

        def connect_new(self):
            self.discovery_count += 1
            return DeviceConnectionResult((), (), ())

    controller = AppController()
    controller._server = object()
    manager = FakeManager()
    controller._manager = manager

    controller.discover()
    controller._auto_discover()

    assert not controller.busy
    assert controller.statusMessage == "Stop the server before rediscovering devices"
    assert manager.discovery_count == 0


def test_auto_discovery_defaults_enabled_and_can_be_disabled(qt_application):
    controller = AppController()

    assert controller.autoDiscoverAndInitializeDevices is True
    assert controller._discovery_timer.interval() == 2000
    assert controller._discovery_timer.isActive()

    controller.autoDiscoverAndInitializeDevices = False

    assert not controller._discovery_timer.isActive()


def test_auto_discovery_initializes_only_new_devices(qt_application):
    class FakeDevice:
        device_id = USBDeviceID(0x0413, 0x6029)
        model_name = "Receiver"
        adapters = []

        def __init__(self):
            self.initialization_count = 0
            self.initialization_finished = False

        def init_hardware(self):
            self.initialization_count += 1
            self.initialization_finished = True

    device = FakeDevice()

    class FakeManager:
        def __init__(self):
            self.discovery_count = 0

        def connect_new(self):
            self.discovery_count += 1
            connected = (device,) if self.discovery_count == 1 else ()
            return DeviceConnectionResult(connected, (), ())

    controller = AppController()
    controller._discovery_timer.stop()
    manager = FakeManager()
    controller._manager = manager

    def run_discovery():
        controller.devicesChanged.connect(
            lambda: qt_application.quit() if device.initialization_finished else None
        )
        controller._auto_discover()
        QTimer.singleShot(2000, qt_application.quit)
        qt_application.exec()

    run_discovery()
    run_discovery()

    assert controller.devices[0]["state"] == "Connected"
    assert device.initialization_count == 1
    assert manager.discovery_count == 2


def test_auto_discovery_shows_device_before_initialization_finishes(qt_application):
    initialization_started = Event()
    allow_initialization_to_finish = Event()

    class FakeDevice:
        device_id = USBDeviceID(0x0413, 0x6029)
        model_name = "Receiver"

        def __init__(self):
            self.adapters = []

        def init_hardware(self):
            initialization_started.set()
            allow_initialization_to_finish.wait(timeout=2)
            self.adapters = [type("Adapter", (), {"adapter_index": 0})()]

    device = FakeDevice()

    class FakeManager:
        def connect_new(self):
            return DeviceConnectionResult((device,), (), ())

    controller = AppController()
    controller._discovery_timer.stop()
    controller._manager = FakeManager()
    visible_before_initialization = False

    def check_initialization_state():
        nonlocal visible_before_initialization
        if initialization_started.is_set() and not allow_initialization_to_finish.is_set():
            visible_before_initialization = (
                controller.devices[0]["state"] == "Connected"
                and controller.devices[0]["adapterCount"] == 0
                and controller.adapters == []
            )
            allow_initialization_to_finish.set()
        if controller.adapters:
            qt_application.quit()
            return
        QTimer.singleShot(10, check_initialization_state)

    controller._auto_discover()
    QTimer.singleShot(10, check_initialization_state)
    QTimer.singleShot(
        3000,
        lambda: (allow_initialization_to_finish.set(), qt_application.quit()),
    )
    qt_application.exec()

    assert visible_before_initialization
    assert controller.devices[0]["state"] == "Initialized"
    assert controller.devices[0]["adapterCount"] == 1
    assert len(controller.adapters) == 1


def test_auto_discovery_removes_detached_device_and_adapters(qt_application):
    class FakeDevice:
        device_id = USBDeviceID(0x0413, 0x6029)
        model_name = "Receiver"
        adapters = [type("Adapter", (), {"adapter_index": 0})()]

    device = FakeDevice()

    class FakeManager:
        def connect_new(self):
            return DeviceConnectionResult((), (), (), (device,))

    controller = AppController()
    controller._discovery_timer.stop()
    controller._manager = FakeManager()
    controller._devices = [device]
    controller._select_first_device()

    controller.devicesChanged.connect(qt_application.quit)
    controller._auto_discover()
    QTimer.singleShot(2000, qt_application.quit)
    qt_application.exec()

    assert controller.devices == []
    assert controller.adapters == []
    assert controller.deviceSummary == "0 device(s) connected; 0 initialized; 0 active adapter(s)"


def test_status_poll_removes_device_after_libusb_no_device(qt_application):
    class FakeDevice:
        device_id = USBDeviceID(0x0413, 0x6029)
        model_name = "Receiver"
        adapters = [type("Adapter", (), {"adapter_index": 0})()]

        def read_status(self, _adapter_index):
            raise usb1.USBError(-4)

    device = FakeDevice()

    class FakeManager:
        def __init__(self):
            self.removed_devices = []

        def remove_device(self, removed_device):
            self.removed_devices.append(removed_device)
            return None

    controller = AppController()
    controller._discovery_timer.stop()
    controller._status_timer.stop()
    manager = FakeManager()
    controller._manager = manager
    controller._devices = [device]
    controller._select_first_device()

    controller.devicesChanged.connect(qt_application.quit)
    controller._poll_status()
    QTimer.singleShot(2000, qt_application.quit)
    qt_application.exec()

    assert manager.removed_devices == [device]
    assert controller.devices == []
    assert controller.adapters == []
    assert "Removed 1 disconnected device" in controller.statusMessage


def test_worker_result_callback_runs_on_the_qt_thread(qt_application):
    controller = AppController()
    controller.autoDiscoverAndInitializeDevices = False
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


def test_unsupported_devices_are_hidden_until_enabled(qt_application):
    controller = AppController()
    controller._discovery_rows = [
        {"deviceIndex": -1, "name": "Supported DVB device"},
        {"deviceIndex": -1, "name": "Unsupported USB device", "unsupported": True},
    ]

    assert controller.showUnsupportedDevices is False
    assert [row["name"] for row in controller.devices] == ["Supported DVB device"]

    controller.showUnsupportedDevices = True

    assert [row["name"] for row in controller.devices] == [
        "Supported DVB device",
        "Unsupported USB device",
    ]


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


def test_device_summary_counts_connected_devices_and_active_adapters(qt_application):
    controller = AppController()
    device_id = USBDeviceID(0x0413, 0x6029)
    uninitialized = type(
        "Device",
        (),
        {"device_id": device_id, "model_name": "Receiver", "adapters": []},
    )()
    adapters = [
        type("Adapter", (), {"adapter_index": index})()
        for index in range(2)
    ]
    initialized = type(
        "Device",
        (),
        {
            "device_id": device_id,
            "model_name": "Receiver",
            "adapters": adapters,
        },
    )()

    assert controller.deviceSummary == "0 device(s) connected; 0 initialized; 0 active adapter(s)"

    controller._devices = [uninitialized, initialized]

    assert controller.deviceSummary == "2 device(s) connected; 1 initialized; 2 active adapter(s)"


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


def test_channel_map_status_is_independent_of_channel_count(qt_application):
    controller = AppController()

    assert controller.hasChannelMap is False
    assert controller.channels == []

    controller._profile.service_map = DVBServiceMap()

    assert controller.hasChannelMap is True
    assert controller.channels == []