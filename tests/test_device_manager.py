from types import SimpleNamespace

import pytest

from mactv.device_manager import DVBDeviceManager
from mactv import device_manager as manager_module
from mactv.driver_api.device import DVBDevice
from mactv.usb import USBDeviceID


class FakeTransport:
    def close(self):
        pass


class FakeDevice(DVBDevice):
    SUPPORTED_USB_IDS = frozenset({USBDeviceID(0x1111, 0x2222)})

    @classmethod
    def open_usb_device(cls, device):
        if device.device.fail:
            raise PermissionError("device is busy")
        return cls(FakeTransport(), device.vendor_id, device.product_id)

    def init_hardware(self):
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

        assert len(result.connected) == 1
        assert len(result.unavailable) == 1
        assert result.unavailable[0].device.device_address == 2
        assert len(result.unsupported) == 1
        assert result.unsupported[0].device_id == USBDeviceID(0x3333, 0x4444)
        assert manager.devices == result.connected

    assert context.closed


def test_manager_rejects_duplicate_supported_ids():
    class OtherDevice(FakeDevice):
        pass

    with pytest.raises(ValueError, match="claimed by both"):
        DVBDeviceManager([FakeDevice, OtherDevice])
