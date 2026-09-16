import pytest

from mactv.tuner import TDA18271Tuner, Tuning


class FakeFrontend:
    def __init__(self):
        self.writes = []
        self.gated_reads = 0

    def gated_read_register(self, address, register, length):
        self.gated_reads += 1
        return bytes((0x84,)) + bytes(length - 1)

    def gated_write_register(self, address, register, data):
        self.writes.append((address, register, bytes(data)))


def test_tda18271_init_uses_linux_16_byte_chunks():
    frontend = FakeFrontend()
    tuner = TDA18271Tuner(frontend)

    tuner.init()

    assert frontend.gated_reads == 1
    assert [len(data) for _, _, data in frontend.writes] == [16, 16, 7]
    assert [register for _, register, _ in frontend.writes] == [0, 16, 32]
    assert all(address == 0x60 for address, _, _ in frontend.writes)


def test_tda18271_tune_programs_pll_and_dvb_t_registers():
    frontend = FakeFrontend()
    tuner = TDA18271Tuner(frontend)

    tuner.tune(Tuning(650_000_000, 8_000_000))

    assert tuner.current_tuning == Tuning(650_000_000, 8_000_000)
    pll_write = next(data for _, register, data in frontend.writes if register == tuner.REG_EP1 and len(data) >= 13)
    assert pll_write[tuner.REG_MPD - tuner.REG_EP1] != 0
    assert any(register == tuner.REG_EB13 for _, register, _ in frontend.writes)


def test_tda18271_rejects_unsupported_bandwidth():
    with pytest.raises(ValueError, match="6, 7, or 8 MHz"):
        TDA18271Tuner(FakeFrontend()).tune(Tuning(650_000_000, 5_000_000))