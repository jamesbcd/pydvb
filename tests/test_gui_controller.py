"""Tests for the hardware-independent desktop application controller."""

from __future__ import annotations

import logging
from threading import Event, Thread

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QCoreApplication, QThread, QTimer

from pydvb.driver_api.frontend import DVBFrontendStatus
from pydvb.driver_api.tuner import DVBTuning
from pydvb.device_manager import DeviceConnectionResult
from pydvb.gui.app_controller import AppController
from pydvb.gui.logging_handler import QtLogHandler, capture_gui_logs
from pydvb.gui.profile import GuiProfile, save_profile
from pydvb.service_map import DVBServiceMap
from pydvb.usb import USBDeviceID, usb1


@pytest.fixture(scope="module")
def qt_application():
    return QCoreApplication.instance() or QCoreApplication([])


def test_log_handler_forwards_records_from_worker_threads(qt_application):
    controller = AppController()
    handler = QtLogHandler(controller)
    record = logging.LogRecord(
        "pydvb.test",
        logging.WARNING,
        __file__,
        1,
        "Example %s",
        ("warning",),
        None,
    )
    worker = Thread(target=handler.emit, args=(record,))

    worker.start()
    worker.join(timeout=1)
    qt_application.processEvents()

    assert not worker.is_alive()
    assert len(controller.logController.logMessages) == 1
    assert controller.logController.logMessages[0].endswith("WARNING pydvb.test: Example warning")
    handler.close()


def test_gui_logging_captures_server_debug_without_usb_trace(qt_application):
    controller = AppController()
    root_logger = logging.getLogger()
    server_logger = logging.getLogger("pydvb.server")
    usb_logger = logging.getLogger("pydvb.usb")
    original_root_level = root_logger.level
    original_server_level = server_logger.level
    original_usb_level = usb_logger.level
    try:
        with capture_gui_logs(controller):
            usb_logger.setLevel(logging.DEBUG)
            server_logger.debug("Server request diagnostic")
            usb_logger.debug("USB transfer trace")
            usb_logger.info("USB transfer summary")
            qt_application.processEvents()

            assert any(
                message.endswith("DEBUG pydvb.server: Server request diagnostic")
                for message in controller.logController.logMessages
            )
            assert any(
                message.endswith("INFO pydvb.usb: USB transfer summary")
                for message in controller.logController.logMessages
            )
            assert not any(
                "USB transfer trace" in message
                for message in controller.logController.logMessages
            )
    finally:
        root_logger.setLevel(original_root_level)
        server_logger.setLevel(original_server_level)
        usb_logger.setLevel(original_usb_level)


def test_log_messages_are_bounded_and_can_be_cleared(qt_application):
    controller = AppController()

    for index in range(1001):
        controller.logController._append_log_message(f"entry {index}")

    assert len(controller.logController.logMessages) == 1000
    assert controller.logController.logMessages[0] == "entry 1"
    assert controller.logController.logMessages[-1] == "entry 1000"

    controller.logController.clearLogs()

    assert controller.logController.logMessages == []


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
        lane="device",
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

    controller.devicesController.discover()
    controller.devicesController._auto_discover()

    assert not controller.busy
    assert controller.statusMessage == "Stop the server before rediscovering devices"
    assert manager.discovery_count == 0


def test_auto_discovery_defaults_enabled_and_can_be_disabled(qt_application):
    controller = AppController()

    assert controller.devicesController.autoDiscoverAndInitializeDevices is True
    assert controller._discovery_timer.interval() == 2000
    assert controller._discovery_timer.isActive()

    controller.devicesController.autoDiscoverAndInitializeDevices = False

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
        controller.devicesController._auto_discover()
        QTimer.singleShot(2000, qt_application.quit)
        qt_application.exec()

    run_discovery()
    run_discovery()

    assert controller.devicesController.devices[0]["state"] == "Connected"
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
                controller.devicesController.devices[0]["state"] == "Connected"
                and controller.devicesController.devices[0]["adapterCount"] == 0
                and controller.devicesController.adapters == []
            )
            allow_initialization_to_finish.set()
        if controller.devicesController.adapters:
            qt_application.quit()
            return
        QTimer.singleShot(10, check_initialization_state)

    controller.devicesController._auto_discover()
    QTimer.singleShot(10, check_initialization_state)
    QTimer.singleShot(
        3000,
        lambda: (allow_initialization_to_finish.set(), qt_application.quit()),
    )
    qt_application.exec()

    assert visible_before_initialization
    assert controller.devicesController.devices[0]["state"] == "Initialized"
    assert controller.devicesController.devices[0]["adapterCount"] == 1
    assert len(controller.devicesController.adapters) == 1


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
    controller.devicesController._auto_discover()
    QTimer.singleShot(2000, qt_application.quit)
    qt_application.exec()

    assert controller.devicesController.devices == []
    assert controller.devicesController.adapters == []
    assert controller.devicesController.deviceSummary == "0 device(s) connected; 0 initialized; 0 active adapter(s)"


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
    assert controller.devicesController.devices == []
    assert controller.devicesController.adapters == []
    assert "Removed 1 disconnected device" in controller.statusMessage


def test_status_poll_does_not_notify_player_stream_url(qt_application):
    class FakeDevice:
        device_id = USBDeviceID(0x0413, 0x6029)
        model_name = "Receiver"
        adapters = [type("Adapter", (), {"adapter_index": 0})()]

        def read_status(self, _adapter_index):
            return DVBFrontendStatus(True, True, True, True, True)

    controller = AppController()
    controller._discovery_timer.stop()
    controller._status_timer.stop()
    controller._devices = [FakeDevice()]
    controller._select_first_device()
    server_changes = []
    detail_changes = []
    controller.serverController.serverChanged.connect(
        lambda: server_changes.append(True)
    )
    controller.serverController.serverDetailsChanged.connect(
        lambda: (detail_changes.append(True), qt_application.quit())
    )
    QTimer.singleShot(2000, qt_application.quit)

    controller._poll_status()
    qt_application.exec()

    assert server_changes == []
    assert detail_changes == [True]


@pytest.mark.parametrize("lane", ["device", "background"])
def test_worker_result_callback_runs_on_the_qt_thread(qt_application, lane):
    controller = AppController()
    controller.devicesController.autoDiscoverAndInitializeDevices = False
    outcomes = []
    controller._run_task(
        lambda: QThread.currentThread() == qt_application.thread(),
        lambda result: (
            outcomes.append((QThread.currentThread() == qt_application.thread(), result)),
            qt_application.quit(),
        ),
        lane=lane,
    )
    QTimer.singleShot(2000, qt_application.quit)

    qt_application.exec()

    assert outcomes == [(True, False)]


def test_profile_load_runs_during_device_operation(qt_application, tmp_path):
    device_operation_started = Event()
    finish_device_operation = Event()
    profile = GuiProfile(scan_table="loaded-profile")
    profile_path = tmp_path / "profile.json"
    save_profile(profile, profile_path)
    controller = AppController(profile=GuiProfile(scan_table="current-profile"))
    controller.devicesController.autoDiscoverAndInitializeDevices = False
    controller._run_task(
        lambda: (
            device_operation_started.set(),
            finish_device_operation.wait(timeout=2),
        ),
        lambda _result: None,
        lane="device",
    )
    assert device_operation_started.wait(timeout=1)

    controller.loadProfile(str(profile_path))

    loaded_during_device_operation = []

    def finish_when_loaded():
        if controller._profile.scan_table == "loaded-profile":
            loaded_during_device_operation.append(controller.deviceBusy)
            finish_device_operation.set()
            qt_application.quit()
            return
        QTimer.singleShot(10, finish_when_loaded)

    QTimer.singleShot(10, finish_when_loaded)
    QTimer.singleShot(3000, qt_application.quit)
    qt_application.exec()

    assert loaded_during_device_operation == [True]
    assert controller._profile.scan_table == "loaded-profile"


def test_profile_load_during_scan_discards_stale_scan_results(
    qt_application,
    monkeypatch,
    tmp_path,
):
    from pydvb.gui.controllers import tuning as tuning_module

    scan_started = Event()
    finish_scan = Event()
    scan_map = DVBServiceMap()
    loaded_map = DVBServiceMap()
    profile = GuiProfile(
        scan_table="loaded-profile",
        tunings=(DVBTuning(frequency_hz=177_500_000),),
        service_map=loaded_map,
    )
    profile_path = tmp_path / "profile.json"
    save_profile(profile, profile_path)

    def scan(_tunings, _adapter):
        scan_started.set()
        finish_scan.wait(timeout=2)
        return scan_map

    monkeypatch.setattr(tuning_module, "map_from_tunings", scan)
    controller = AppController()
    controller.devicesController.autoDiscoverAndInitializeDevices = False
    adapter = type("Adapter", (), {"adapter_index": 0})()
    device = type(
        "Device",
        (),
        {
            "device_id": USBDeviceID(0x0413, 0x6029),
            "model_name": "Receiver",
            "adapters": [adapter],
        },
    )()
    controller._devices = [device]
    controller._select_first_device()
    controller.tuningController.addTuning("177.5", "7")
    controller.tuningController.scan()
    assert scan_started.wait(timeout=1)

    controller.loadProfile(str(profile_path))

    loaded_profile_map = []

    def finish_when_loaded():
        if controller._profile.scan_table == "loaded-profile" and not loaded_profile_map:
            loaded_profile_map.append(controller._profile.service_map)
            finish_scan.set()
        if not controller.deviceBusy and loaded_profile_map:
            qt_application.quit()
            return
        QTimer.singleShot(10, finish_when_loaded)

    QTimer.singleShot(10, finish_when_loaded)
    QTimer.singleShot(3000, qt_application.quit)
    qt_application.exec()

    assert loaded_profile_map == [controller._profile.service_map]
    assert controller._profile.service_map is not scan_map
    assert "discarded" in controller.statusMessage.lower()


def test_identical_devices_can_be_selected_independently(qt_application):
    controller = AppController()
    device_id = USBDeviceID(0x0413, 0x6029)
    first = type("Device", (), {"device_id": device_id, "model_name": "Receiver", "adapters": []})()
    second = type("Device", (), {"device_id": device_id, "model_name": "Receiver", "adapters": []})()
    controller._devices = [first, second]
    controller._select_first_device()

    controller.devicesController.selectDevice(1)

    assert controller._selected_device() is second
    assert controller.devicesController.devices[0]["selected"] is False
    assert controller.devicesController.devices[1]["selected"] is True


def test_unsupported_devices_are_hidden_until_enabled(qt_application):
    controller = AppController()
    controller._discovery_rows = [
        {"deviceIndex": -1, "name": "Supported DVB device"},
        {"deviceIndex": -1, "name": "Unsupported USB device", "unsupported": True},
    ]

    assert controller.devicesController.showUnsupportedDevices is False
    assert [row["name"] for row in controller.devicesController.devices] == ["Supported DVB device"]

    controller.devicesController.showUnsupportedDevices = True

    assert [row["name"] for row in controller.devicesController.devices] == [
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

    controller.devicesController.selectAdapter(1)

    assert controller.devicesController.adapters == [
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

    assert controller.devicesController.deviceSummary == "0 device(s) connected; 0 initialized; 0 active adapter(s)"

    controller._devices = [uninitialized, initialized]

    assert controller.devicesController.deviceSummary == "2 device(s) connected; 1 initialized; 2 active adapter(s)"


def test_manual_tunings_can_be_added_edited_and_removed(qt_application):
    profile = GuiProfile(tunings=(DVBTuning(frequency_hz=650_000_000),))
    controller = AppController(profile=profile)

    controller.tuningController.updateTuning(0, "666.5", "7")
    assert controller.tuningController.tunings[0] == {
        "index": 0,
        "frequency": 666.5,
        "bandwidth": 7,
    }

    controller.tuningController.addTuning("177.5", "8")
    assert len(controller.tuningController.tunings) == 2
    controller.tuningController.removeTuning(0)
    assert controller.tuningController.tunings[0]["frequency"] == 177.5


def test_channel_map_status_is_independent_of_channel_count(qt_application):
    controller = AppController()

    assert controller.channelController.hasChannelMap is False
    assert controller.channelController.channels == []

    controller._profile.service_map = DVBServiceMap()

    assert controller.channelController.hasChannelMap is True
    assert controller.channelController.channels == []


def test_server_settings_are_exposed_by_server_controller(qt_application):
    controller = AppController()

    controller.serverController.serverHost = " 127.0.0.1 "
    controller.serverController.serverPort = 8080

    assert controller.serverController.serverHost == "127.0.0.1"
    assert controller.serverController.serverPort == 8080
    assert controller.serverController.serverState == "Stopped"
    assert controller.serverController.serverAddress == "Not listening"
    assert controller.serverController.playlistUrl == ""

    controller.serverController.serverHost = " "
    controller.serverController.serverPort = 65536

    assert controller.serverController.serverHost == "127.0.0.1"
    assert controller.serverController.serverPort == 8080


def test_player_stream_base_url_is_locally_reachable(qt_application):
    controller = AppController()

    class FakeServer:
        address = ("0.0.0.0", 8080)

    controller._server = FakeServer()
    assert controller.serverController.streamBaseUrl == "http://127.0.0.1:8080"

    controller._server.address = ("192.168.1.10", 8080)
    assert controller.serverController.streamBaseUrl == "http://192.168.1.10:8080"


def test_device_status_updates_do_not_notify_player_stream_url(qt_application):
    controller = AppController()
    server_changes = []
    detail_changes = []
    controller.serverController.serverChanged.connect(
        lambda: server_changes.append(True)
    )
    controller.serverController.serverDetailsChanged.connect(
        lambda: detail_changes.append(True)
    )

    controller.devicesChanged.emit()

    assert server_changes == []
    assert detail_changes == [True]

    controller.serverChanged.emit()

    assert server_changes == [True]
    assert detail_changes == [True, True]