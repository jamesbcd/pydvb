from mactv.drivers.af9015.device import AF9015Device
from mactv.usb import USBSpeed


class FakeTransport:
    def __init__(self, warm=True):
        self.warm = warm
        self.writes = []

    def get_speed(self):
        return USBSpeed.UNKNOWN

    def bulk_write(self, endpoint, data, timeout):
        self.writes.append(bytes(data))

    def bulk_read(self, endpoint, length, timeout):
        if self.writes[-1][0] == 0x10:
            return b"\x00\x00\x02" if self.warm else b"\x00\x00\x00"
        return b"\x00\x00" + bytes(max(0, length - 2))

    def close(self):
        pass

    def reconnect(self, timeout=5.0, poll_interval=0.1):
        return self


def test_initialize_skips_firmware_for_warm_device():
    device = AF9015Device(FakeTransport())

    device.init_hardware()

    assert len(device.adapters) == 1
    assert device._hardware_initialized
    assert device.firmware_size == 0


def test_initialize_does_not_upload_firmware_by_default():
    device = AF9015Device(FakeTransport(warm=False))

    try:
        device.init_hardware(download_firmware=False)
    except RuntimeError as error:
        assert "download_firmware=True" in str(error)
    else:
        raise AssertionError("cold initialization should require explicit firmware upload")

    assert not device._hardware_initialized