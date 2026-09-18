import sys

import pytest

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
    monkeypatch.setattr(__main__.AF9015Device, "open", lambda vendor_id, product_id: device)
    monkeypatch.setattr(sys, "argv", ["mactv", "--safe"])

    assert __main__.main() == 0
    assert not device.initialize_called
    assert "Device state: cold" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("value", "expected"),
    [("177.5", 177_500_000), ("650", 650_000_000)],
)
def test_parse_frequency_accepts_mhz(value, expected):
    assert __main__.parse_frequency(value) == expected


def test_parse_frequency_rejects_non_positive_values():
    with pytest.raises(__main__.argparse.ArgumentTypeError):
        __main__.parse_frequency("0")