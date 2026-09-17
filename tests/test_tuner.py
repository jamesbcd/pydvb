import pytest

from mactv.tuner import TDA18271Tuner, Tuning


class FakeFrontend:
    def __init__(self, chip_id=0x84):
        self.writes = []
        self.gated_reads = 0
        self.chip_id = chip_id

    def gated_read_register(self, address, register, length):
        self.gated_reads += 1
        return bytes((self.chip_id,)) + bytes(length - 1)

    def gated_write_register(self, address, register, data):
        self.writes.append((address, register, bytes(data)))


def test_tda18271_init_uses_linux_16_byte_chunks():
    frontend = FakeFrontend()
    tuner = TDA18271Tuner(frontend)

    tuner.init()

    assert frontend.gated_reads == 1
    assert [len(data) for _, _, data in frontend.writes[:3]] == [16, 16, 7]
    assert [register for _, register, _ in frontend.writes[:3]] == [0, 16, 32]
    assert any(register == tuner.REG_EP3 and data[0] == 0x1F for _, register, data in frontend.writes)
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
    frontend = FakeFrontend()
    with pytest.raises(ValueError, match="6, 7, or 8 MHz"):
        TDA18271Tuner(frontend).tune(Tuning(650_000_000, 5_000_000))
    assert frontend.gated_reads == 0
    assert frontend.writes == []


def test_tda18271_supports_low_band_frequencies():
    tuner = TDA18271Tuner(FakeFrontend())

    tuner.tune(Tuning(45_000_000, 8_000_000))

    assert tuner.current_tuning == Tuning(45_000_000, 8_000_000)


def test_tda18271_hdc1_tune_runs_rf_tracking_calibration():
    frontend = FakeFrontend(0x83)
    tuner = TDA18271Tuner(frontend)

    tuner.tune(Tuning(650_000_000, 8_000_000))

    assert any(register == tuner.REG_EB20 and data == b"\xcc" for _, register, data in frontend.writes)
    assert any(register == tuner.REG_EB20 and data == b"\xec" for _, register, data in frontend.writes)


def test_tda18271_reinit_wakes_without_resetting_register_cache():
    frontend = FakeFrontend()
    tuner = TDA18271Tuner(frontend)
    tuner.init()
    write_count = len(frontend.writes)
    tuner._registers[tuner.REG_EB14] = 0x42

    tuner.init()

    assert len(frontend.writes) == write_count + 1
    assert tuner._registers[tuner.REG_EB14] == 0x42


def test_tda18271_uses_hdc1_defaults_and_pll_map():
    frontend = FakeFrontend(0x83)
    tuner = TDA18271Tuner(frontend)

    tuner.init()

    assert frontend.writes[0][2][tuner.REG_ID] == 0x83
    assert frontend.writes[1][2][tuner.REG_EB1 - 16] == 0xFF
    tuner.tune(Tuning(650_000_000, 8_000_000))
    pll_write = next(data for _, register, data in frontend.writes if register == tuner.REG_EP1 and len(data) >= 13)
    assert pll_write[tuner.REG_MPD - tuner.REG_EP1] == 0x1C


def test_tda18271_hdc2_calibration_is_persistent(monkeypatch):
    monkeypatch.setattr("mactv.tuner.sleep", lambda _: None)
    frontend = FakeFrontend(0x84)
    tuner = TDA18271Tuner(frontend)

    tuner.tune(Tuning(650_000_000, 8_000_000))

    assert tuner._rf_calibration_initialized
    assert tuner._rf_calibration_temperature is not None
    writes_after_calibration = len(frontend.writes)
    tuner.sleep()
    tuner.tune(Tuning(650_000_000, 8_000_000))

    assert len(frontend.writes) < writes_after_calibration * 2