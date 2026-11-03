from types import SimpleNamespace

import pytest

from pydvb.device_manager import DVBDeviceManager
from pydvb import device_manager as manager_module
from pydvb.driver_api.device import DVBDevice
from pydvb.usb import USBDeviceID


class FakeTransport:
    def close(self):
        self.closed = True


class FakeDevice(DVBDevice):
    SUPPORTED_USB_MODELS = {
        USBDeviceID(0x1111, 0x2222): "First receiver",
        USBDeviceID(0x5555, 0x6666): "Second receiver",
    }

    @classmethod
    def open_usb_device(cls, device):
        if device.device.fail:
            raise PermissionError("device is busy")
        return cls(FakeTransport(), device.device_id)

    def init_hardware(self):
        pass

    def is_warm(self):
        return False

    def log_transport_stats(self):
        pass


class FakeUSBDevice:
    def __init__(self, vendor_id, product_id, fail=False, address=1):
        self.vendor_id = vendor_id
        self.product_id = product_id
        self.fail = fail
        self.address = address

    def getVendorID(self):
        return self.vendor_id

    def getProductID(self):
        return self.product_id

    def getBusNumber(self):
        return 1

    def getDeviceAddress(self):
        return self.address


class FakeContext:
    def __init__(self, devices):
        self.devices = devices
        self.closed = False

    def getDeviceList(self):
        return self.devices

    def close(self):
        self.closed = True


def test_manager_connects_supported_devices_and_reports_failures(monkeypatch):
    context = FakeContext(
        [
            FakeUSBDevice(0x1111, 0x2222, address=1),
            FakeUSBDevice(0x1111, 0x2222, fail=True, address=2),
            FakeUSBDevice(0x3333, 0x4444, address=3),
        ]
    )
    monkeypatch.setattr(manager_module, "usb1", SimpleNamespace(USBContext=lambda: context))

    with DVBDeviceManager([FakeDevice]) as manager:
        result = manager.connect_all()

        assert manager.supported_ids() == frozenset(FakeDevice.SUPPORTED_USB_MODELS)
        assert len(result.connected) == 1
        assert len(result.unavailable) == 1
        assert result.unavailable[0].device.device_address == 2
        assert len(result.unsupported) == 1
        assert result.unsupported[0].device_id == USBDeviceID(0x3333, 0x4444)
        assert manager.devices == result.connected

    assert context.closed


def test_manager_connect_new_opens_only_devices_not_already_connected(monkeypatch):
    first = FakeUSBDevice(0x1111, 0x2222, address=1)
    context = FakeContext([first])
    monkeypatch.setattr(manager_module, "usb1", SimpleNamespace(USBContext=lambda: context))

    with DVBDeviceManager([FakeDevice]) as manager:
        initial = manager.connect_new()
        context.devices.append(FakeUSBDevice(0x1111, 0x2222, address=2))

        newly_discovered = manager.connect_new()
        no_new_devices = manager.connect_new()

        assert len(initial.connected) == 1
        assert len(newly_discovered.connected) == 1
        assert no_new_devices.connected == ()
        assert len(manager.devices) == 2


def test_manager_connect_new_detects_and_closes_removed_devices(monkeypatch):
    context = FakeContext([FakeUSBDevice(0x1111, 0x2222, address=1)])
    monkeypatch.setattr(manager_module, "usb1", SimpleNamespace(USBContext=lambda: context))

    with DVBDeviceManager([FakeDevice]) as manager:
        initial = manager.connect_new()
        removed_device = initial.connected[0]
        context.devices.clear()

        result = manager.connect_new()

        assert result.removed == (removed_device,)
        assert manager.devices == ()
        assert removed_device.transport.closed


def test_manager_rejects_duplicate_supported_ids():
    class OtherDevice(FakeDevice):
        pass

    with pytest.raises(ValueError, match="claimed by both"):
        DVBDeviceManager([FakeDevice, OtherDevice])


def test_device_model_name_is_resolved_from_its_usb_id():
    first = FakeDevice(FakeTransport(), USBDeviceID(0x1111, 0x2222))
    second = FakeDevice(FakeTransport(), USBDeviceID(0x5555, 0x6666))
    unknown = FakeDevice(FakeTransport(), USBDeviceID(0x3333, 0x4444))
    unidentified = FakeDevice(FakeTransport())

    assert first.model_name == "First receiver"
    assert second.model_name == "Second receiver"
    assert unknown.model_name is None
    assert unidentified.model_name is None
