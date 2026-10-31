from hashlib import sha256
from pathlib import Path

import pytest
import requests

from pydvb.drivers.af9015 import device as device_module
from pydvb.drivers.af9015.device import AF9015Device
from pydvb.driver_api import device as driver_device_module
from pydvb.usb import USBDeviceID, USBDeviceInfo


class FakeTransport:
    def __init__(self):
        self.writes = []

    def bulk_write(self, endpoint, data, timeout):
        self.writes.append(bytes(data))

    def bulk_read(self, endpoint, length, timeout):
        return b"\x00\x00\x02" if length >= 3 else b"\x00\x00"

    def close(self):
        pass

    def reconnect(self, timeout=5.0, poll_interval=0.1):
        return self


def test_firmware_cache_is_inside_concrete_driver_package():
    assert AF9015Device.FIRMWARE_CACHE_DIR == Path(device_module.__file__).resolve().parent / "firmware"


def test_open_usb_device_uses_manager_transport_opener(monkeypatch):
    transport = FakeTransport()
    calls = []

    device_info = USBDeviceInfo(0x0413, 0x6029, 1, 2, object(), object())

    def open_transport(device, **kwargs):
        calls.append((device, kwargs))
        return transport

    monkeypatch.setattr(device_module, "open_usb_transport", open_transport)

    device = AF9015Device.open_usb_device(device_info)

    assert calls == [(device_info, {"async_endpoints": AF9015Device.STREAM_ENDPOINTS})]
    assert device.bridge.transport is transport


def test_device_loads_firmware_from_path(tmp_path):
    firmware_path = tmp_path / "test.fw"
    firmware_path.write_bytes(b"firmware")
    transport = FakeTransport()
    device = AF9015Device(transport)

    device.download_firmware(firmware_path=firmware_path)

    assert transport.writes[0][8:] == b"firmware"


def test_firmware_bytes_and_path_are_mutually_exclusive(tmp_path):
    with pytest.raises(ValueError):
        AF9015Device(FakeTransport()).download_firmware(b"firmware", tmp_path / "test.fw")


class ReconnectTransport:
    def __init__(self, reply, candidates=()):
        self.reply = reply
        self.candidates = iter(candidates)
        self.closed = False

    def bulk_write(self, endpoint, data, timeout):
        pass

    def bulk_read(self, endpoint, length, timeout):
        return self.reply

    def close(self):
        self.closed = True

    def reconnect(self, timeout=5.0, poll_interval=0.1):
        candidate = next(self.candidates)
        if isinstance(candidate, Exception):
            raise candidate
        self.close()
        return candidate


def test_reconnect_replaces_handle_after_get_config_succeeds():
    candidate = ReconnectTransport(b"\x00\x00\x02")
    original = ReconnectTransport(b"\x00\x00\x00", (candidate,))
    device = AF9015Device(original, USBDeviceID(0x0413, 0x6029))

    assert device._reconnect(timeout=1, poll_interval=0.001) == 0x02
    assert original.closed
    assert device.bridge.transport is candidate
    assert not device._hardware_initialized


class FirmwareResponse:
    def __init__(self, content):
        self.content = content

    def __enter__(self):
        return self

    def __exit__(self, _exception_type, _exception, _traceback):
        pass

    def raise_for_status(self):
        pass


def configure_test_firmware(monkeypatch, tmp_path, urls, firmware):
    monkeypatch.setattr(AF9015Device, "FIRMWARE_URLS", urls)
    monkeypatch.setattr(AF9015Device, "FIRMWARE_FILENAME", "test-firmware.fw")
    monkeypatch.setattr(AF9015Device, "FIRMWARE_SHA256", sha256(firmware).hexdigest())
    monkeypatch.setattr(AF9015Device, "FIRMWARE_CACHE_DIR", tmp_path)


def test_download_firmware_caches_and_uses_remote_image(monkeypatch, tmp_path):
    firmware = b"verified firmware"
    configure_test_firmware(monkeypatch, tmp_path, ("https://example.test/firmware",), firmware)
    monkeypatch.setattr(
        driver_device_module.requests,
        "get",
        lambda url, timeout: FirmwareResponse(firmware),
    )
    transport = FakeTransport()
    device = AF9015Device(transport)

    device.download_firmware()

    assert transport.writes[0][8:] == firmware
    assert (tmp_path / "test-firmware.fw").read_bytes() == firmware