import sys

from mactv import __main__


class FakeDevice:
    def __init__(self):
        self.initialize_called = False

    @property
    def is_warm(self):
        return False

    def initialize(self):
        self.initialize_called = True


def test_safe_probe_does_not_initialize(monkeypatch, capsys):
    device = FakeDevice()
    monkeypatch.setattr(__main__.Af9015Device, "open", lambda vendor_id, product_id: device)
    monkeypatch.setattr(sys, "argv", ["mactv", "--safe"])

    assert __main__.main() == 0
    assert not device.initialize_called
    assert "Device state: cold" in capsys.readouterr().out