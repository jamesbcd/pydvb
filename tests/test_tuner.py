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


def test_tda18271_init_uses_linux_16_byte_chunks(monkeypatch):
    monkeypatch.setattr(TDA18271Tuner, "_initialize_hdc2_calibration", lambda self: None)
    frontend = FakeFrontend()
    tuner = TDA18271Tuner(frontend)

    tuner.init_hardware()

    assert frontend.gated_reads == 1
    assert [len(data) for _, _, data in frontend.writes[:3]] == [16, 16, 7]
    assert [register for _, register, _ in frontend.writes[:3]] == [0, 16, 32]
    assert any(register == tuner.REG_EP3 and data[0] == 0x1F for _, register, data in frontend.writes)
    assert all(address == 0x60 for address, _, _ in frontend.writes)


def test_tda18271_state_properties_are_read_only():
    tuner = TDA18271Tuner(FakeFrontend())

    assert tuner.chip_id is None
    assert not tuner.initialized
    assert tuner.sleeping

    with pytest.raises(AttributeError):
        tuner.chip_id = tuner.CHIP_ID_HDC1
    with pytest.raises(AttributeError):
        tuner.initialized = True
    with pytest.raises(AttributeError):
        tuner.sleeping = False

    tuner.init_hardware()

    assert tuner.chip_id == tuner.CHIP_ID_HDC2
    assert tuner.initialized
    assert not tuner.sleeping

    tuner.sleep()

    assert tuner.initialized
    assert tuner.sleeping


def test_tda18271_get_config_dict_contains_configuration_and_is_a_snapshot():
    tuner = TDA18271Tuner(FakeFrontend())

    config = tuner.get_config_dict()

    assert config == {
        "i2c_address": 0x60,
        "chip_id": None,
        "initialized": False,
        "sleeping": True,
        "current_tuning": None,
        "temperature": None,
        "rf_calibration_initialized": False,
        "rf_calibration_temperature": None,
    }

    config["i2c_address"] = 1
    assert tuner.get_config_dict()["i2c_address"] == 0x60


def test_tda18271_get_config_dict_includes_temperature_after_initialization():
    tuner = TDA18271Tuner(FakeFrontend())

    tuner.init_hardware()

    assert tuner.get_config_dict()["temperature"] == 60


def test_tda18271_tune_programs_pll_and_dvb_t_registers():
    frontend = FakeFrontend()
    tuner = TDA18271Tuner(frontend)

    tuner.tune(Tuning(650_000_000, 8_000_000))

    assert tuner.current_tuning == Tuning(650_000_000, 8_000_000)
    pll_write = next(data for _, register, data in frontend.writes if register == tuner.REG_EP1 and len(data) >= 13)
    assert pll_write[tuner.REG_MPD - tuner.REG_EP1] != 0
    assert any(register == tuner.REG_EB13 for _, register, _ in frontend.writes)
    assert any(register == tuner.REG_TM and len(data) == 7 for _, register, data in frontend.writes)
    assert all(len(data) <= tuner.I2C_WRITE_CHUNK_SIZE for _, _, data in frontend.writes)


def test_tda18271_reports_linux_standard_map_if_frequency():
    tuner = TDA18271Tuner(FakeFrontend())

    tuner.tune(Tuning(177_500_000, 7_000_000))

    assert tuner.get_if_frequency_hz() == 3_500_000


def test_tda18271_hdc2_calibration_uses_linux_rf_cal_base_at_177_5_mhz():
    tuner = TDA18271Tuner(FakeFrontend())
    tuner._chip_id = tuner.CHIP_ID_HDC2
    tuner._rf_calibration_initialized = True
    tuner._rf_calibration_temperature = None
    tuner._write_cached = lambda *_: None

    tuner._apply_hdc2_calibration(177_500_000)

    assert tuner._registers[tuner.REG_EB14] == 0x1D


def test_tda18271_hdc2_calibration_switches_slopes_at_second_point():
    tuner = TDA18271Tuner(FakeFrontend())
    tuner._chip_id = tuner.CHIP_ID_HDC2
    tuner._rf_calibration_initialized = True
    tuner._rf_calibration_temperature = None
    tuner._write_cached = lambda *_: None
    band = tuner._lookup_rf_band(177_500_000)
    band.measured_hz[:] = [177_000_000, 177_400_000, 178_000_000]
    band.slope_low = 0
    band.offset_low = 0
    band.slope_high = 2
    band.offset_high = 7

    tuner._apply_hdc2_calibration(177_500_000)

    assert tuner._registers[tuner.REG_EB14] == 0xEC


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
    tuner.init_hardware()
    write_count = len(frontend.writes)
    tuner._registers[tuner.REG_EB14] = 0x42

    tuner.init_hardware()

    assert len(frontend.writes) == write_count + 1
    assert tuner._registers[tuner.REG_EB14] == 0x42


def test_tda18271_uses_hdc1_defaults_and_pll_map():
    frontend = FakeFrontend(0x83)
    tuner = TDA18271Tuner(frontend)

    tuner.init_hardware()

    assert frontend.writes[0][2][tuner.REG_ID] == 0x83
    assert frontend.writes[1][2][tuner.REG_EB1 - 16] == 0xFF
    tuner.tune(Tuning(650_000_000, 8_000_000))
    pll_write = next(data for _, register, data in frontend.writes if register == tuner.REG_EP1 and len(data) >= 13)
    assert pll_write[tuner.REG_MPD - tuner.REG_EP1] == 0x1C


def test_tda18271_hdc2_calibration_is_persistent(monkeypatch):
    monkeypatch.setattr("mactv.tuner.sleep", lambda _: None)
    frontend = FakeFrontend(0x84)
    tuner = TDA18271Tuner(frontend)

    tuner.init_hardware()

    assert tuner._rf_calibration_initialized
    assert tuner._rf_calibration_temperature is not None
    writes_after_calibration = len(frontend.writes)
    tuner.sleep()
    tuner.tune(Tuning(650_000_000, 8_000_000))

    assert len(frontend.writes) < writes_after_calibration * 2


def test_tda18271_tune_wakes_after_sleep():
    frontend = FakeFrontend()
    tuner = TDA18271Tuner(frontend)
    tuner.tune(Tuning(650_000_000, 8_000_000))
    tuner.sleep()

    tuner.tune(Tuning(650_000_000, 8_000_000))

    assert tuner._registers[tuner.REG_EP3] & 0x80 == 0


def test_tda18271_power_scan_keeps_successful_downward_frequency(monkeypatch):
    tuner = TDA18271Tuner(FakeFrontend())
    tuner._chip_id = 0x84
    tuner._apply_rf_maps = lambda _: None
    tuner._write_cached = lambda *_: None
    tuner._set_main_pll = lambda _: None
    readings = iter((0, 0, 10))
    tuner._read_extended = lambda: tuner._registers.__setitem__(tuner.REG_EB10, next(readings)) or b""
    tuner._lookup_cid_target = lambda _: (10, 0)
    monkeypatch.setattr("mactv.tuner.sleep", lambda _: None)

    calibrated, frequency_hz = tuner._power_scan(100_000_000)

    assert calibrated
    assert frequency_hz == 99_800_000


def test_tda18271_thermometer_retries_ambiguous_reading(monkeypatch):
    class ThermometerFrontend(FakeFrontend):
        def __init__(self):
            super().__init__()
            self.thermometer_values = iter((0x20, 0x00))

        def gated_read_register(self, address, register, length):
            values = bytearray(super().gated_read_register(address, register, length))
            if length == TDA18271Tuner.REGISTER_COUNT:
                values[TDA18271Tuner.REG_TM] = next(self.thermometer_values)
            return bytes(values)

    tuner = TDA18271Tuner(ThermometerFrontend())
    tuner._chip_id = 0x84
    monkeypatch.setattr("mactv.tuner.sleep", lambda _: None)

    temperature = tuner._read_thermometer()

    assert temperature == 60