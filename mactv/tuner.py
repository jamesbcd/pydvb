"""Tuner interfaces and the AF9015-supported TDA18271 implementation."""

from dataclasses import dataclass, field
from enum import Enum
from time import sleep
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from .frontend import AF9013Frontend


class TransmissionMode(str, Enum):
    AUTO = "auto"
    K2 = "2k"
    K8 = "8k"


class GuardInterval(str, Enum):
    AUTO = "auto"
    GI_1_32 = "1/32"
    GI_1_16 = "1/16"
    GI_1_8 = "1/8"
    GI_1_4 = "1/4"


class Hierarchy(str, Enum):
    AUTO = "auto"
    NONE = "none"
    LEVEL_1 = "1"
    LEVEL_2 = "2"
    LEVEL_4 = "4"


class Modulation(str, Enum):
    AUTO = "auto"
    QPSK = "qpsk"
    QAM16 = "qam16"
    QAM64 = "qam64"


class CodeRate(str, Enum):
    AUTO = "auto"
    RATE_1_2 = "1/2"
    RATE_2_3 = "2/3"
    RATE_3_4 = "3/4"
    RATE_5_6 = "5/6"
    RATE_7_8 = "7/8"
    NONE = "none"


@dataclass(frozen=True)
class Tuning:
    frequency_hz: int
    bandwidth_hz: int = 8_000_000
    transmission_mode: TransmissionMode = TransmissionMode.AUTO
    guard_interval: GuardInterval = GuardInterval.AUTO
    hierarchy: Hierarchy = Hierarchy.AUTO
    modulation: Modulation = Modulation.AUTO
    code_rate_hp: CodeRate = CodeRate.AUTO
    code_rate_lp: CodeRate = CodeRate.AUTO


@dataclass
class RFCalibrationBand:
    """Calibration data for one RF band.

    Example: ``RFCalibrationBand(47_900_000, 0, (46_000_000, 0, 0))``.
    """

    rf_max_hz: int
    rf_band: int
    defaults_hz: tuple[int, int, int]
    measured_hz: list[int] = field(default_factory=lambda: [0, 0, 0])
    slope_low: int = 0
    offset_low: int = 0
    slope_high: int = 0
    offset_high: int = 0


class Tuner(Protocol):
    def init_hardware(self) -> None: ...

    def tune(self, tuning: Tuning) -> None: ...

    def get_if_frequency_hz(self) -> int: ...

    def sleep(self) -> None: ...

    def get_config_dict(self) -> dict[str, object]: ...


class TDA18271Tuner(Tuner):
    """Control an NXP TDA18271 HDC1 or HDC2 tuner attached to an AF9013 frontend.

    The implementation supports 6, 7, and 8 MHz channels and follows the
    Linux ``tda18271`` driver's register-cache, PLL, image-rejection, RF
    tracking-filter, power-scan, and temperature-calibration behavior. It is
    intentionally limited to the AF9015 digital DVB-T path; analog TV/FM,
    ATSC, QAM, DVB-T2, slave-tuner, configurable-gate, output-option, and
    external-AGC features are not exposed.

    The tuner is addressed at 0x60 on AF9015 boards and accepts a one-byte
    register followed by register data. The AF9015 I2C bridge limits writes
    to 21 bytes including that register byte, so Linux's 16-byte init mode is
    used here.
    """

    CHIP_ID_HDC1 = 0x83
    CHIP_ID_HDC2 = 0x84
    I2C_ADDRESS = 0x60
    REGISTER_COUNT = 39
    INIT_CHUNK_SIZE = 16
    I2C_WRITE_CHUNK_SIZE = 20
    MIN_FREQUENCY_HZ = 45_000_000
    MAX_FREQUENCY_HZ = 865_000_000

    REG_ID = 0
    REG_TM = 1
    REG_PL = 2
    REG_EP1 = 3
    REG_EP2 = 4
    REG_EP3 = 5
    REG_EP4 = 6
    REG_EP5 = 7
    REG_CPD = 8
    REG_CD1 = 9
    REG_CD2 = 10
    REG_CD3 = 11
    REG_MPD = 12
    REG_MD1 = 13
    REG_MD2 = 14
    REG_MD3 = 15
    REG_EB1 = 16
    REG_EB2 = 17
    REG_EB3 = 18
    REG_EB4 = 19
    REG_EB5 = 20
    REG_EB6 = 21
    REG_EB7 = 22
    REG_EB8 = 23
    REG_EB9 = 24
    REG_EB10 = 25
    REG_EB11 = 26
    REG_EB12 = 27
    REG_EB13 = 28
    REG_EB14 = 29
    REG_EB15 = 30
    REG_EB16 = 31
    REG_EB17 = 32
    REG_EB18 = 33
    REG_EB19 = 34
    REG_EB20 = 35
    REG_EB21 = 36
    REG_EB22 = 37
    REG_EB23 = 38

    DEFAULT_REGISTERS = {
        CHIP_ID_HDC1: bytes.fromhex(
            "830880c6df166080 8000000000000000 "
            "ff01844101844007 0000960fc1008f00 "
            "000000203348b0"
        ),
        CHIP_ID_HDC2: bytes.fromhex(
            "840880c6df166080 8000000000000000 "
            "fc01844101844007 00009633c1008f00 "
            "008c0020b348b0"
        ),
    }

    STANDARD_MAP = {
        CHIP_ID_HDC1: {
            6_000_000: (3_300_000, 3, 4, 1, 0x37),
            7_000_000: (3_800_000, 3, 5, 1, 0x37),
            8_000_000: (4_300_000, 3, 6, 1, 0x37),
        },
        CHIP_ID_HDC2: {
            6_000_000: (3_300_000, 3, 4, 1, 0x37),
            7_000_000: (3_500_000, 3, 4, 1, 0x37),
            8_000_000: (4_000_000, 3, 5, 1, 0x37),
        },
    }

    MAIN_PLL_MAP = {
        CHIP_ID_HDC1: ((32_000, 0x5F, 0xF0), (35_000, 0x5E, 0xE0), (37_000, 0x5D, 0xD0),
            (41_000, 0x5C, 0xC0), (44_000, 0x5B, 0xB0), (49_000, 0x5A, 0xA0),
            (54_000, 0x59, 0x90), (61_000, 0x58, 0x80), (65_000, 0x4F, 0x78),
            (70_000, 0x4E, 0x70), (75_000, 0x4D, 0x68), (82_000, 0x4C, 0x60),
            (89_000, 0x4B, 0x58), (98_000, 0x4A, 0x50), (109_000, 0x49, 0x48),
            (123_000, 0x48, 0x40), (131_000, 0x3F, 0x3C), (141_000, 0x3E, 0x38),
            (151_000, 0x3D, 0x34), (164_000, 0x3C, 0x30), (179_000, 0x3B, 0x2C),
            (197_000, 0x3A, 0x28), (219_000, 0x39, 0x24), (246_000, 0x38, 0x20),
            (263_000, 0x2F, 0x1E), (282_000, 0x2E, 0x1C), (303_000, 0x2D, 0x1A),
            (329_000, 0x2C, 0x18), (359_000, 0x2B, 0x16), (395_000, 0x2A, 0x14),
            (438_000, 0x29, 0x12), (493_000, 0x28, 0x10), (526_000, 0x1F, 0x0F),
            (564_000, 0x1E, 0x0E), (607_000, 0x1D, 0x0D), (658_000, 0x1C, 0x0C),
            (718_000, 0x1B, 0x0B), (790_000, 0x1A, 0x0A), (877_000, 0x19, 0x09),
            (987_000, 0x18, 0x08)),
        CHIP_ID_HDC2: ((33_125, 0x57, 0xF0), (35_500, 0x56, 0xE0), (38_188, 0x55, 0xD0),
            (41_375, 0x54, 0xC0), (45_125, 0x53, 0xB0), (49_688, 0x52, 0xA0),
            (55_188, 0x51, 0x90), (62_125, 0x50, 0x80), (66_250, 0x47, 0x78),
            (71_000, 0x46, 0x70), (76_375, 0x45, 0x68), (82_750, 0x44, 0x60),
            (90_250, 0x43, 0x58), (99_375, 0x42, 0x50), (110_375, 0x41, 0x48),
            (124_250, 0x40, 0x40), (132_500, 0x37, 0x3C), (142_000, 0x36, 0x38),
            (152_750, 0x35, 0x34), (165_500, 0x34, 0x30), (180_500, 0x33, 0x2C),
            (198_750, 0x32, 0x28), (220_750, 0x31, 0x24), (248_500, 0x30, 0x20),
            (265_000, 0x27, 0x1E), (284_000, 0x26, 0x1C), (305_500, 0x25, 0x1A),
            (331_000, 0x24, 0x18), (361_000, 0x23, 0x16), (397_500, 0x22, 0x14),
            (441_500, 0x21, 0x12), (497_000, 0x20, 0x10), (530_000, 0x17, 0x0F),
            (568_000, 0x16, 0x0E), (611_000, 0x15, 0x0D), (662_000, 0x14, 0x0C),
            (722_000, 0x13, 0x0B), (795_000, 0x12, 0x0A), (883_000, 0x11, 0x09),
            (994_000, 0x10, 0x08)),
    }

    BP_FILTER_MAP = ((62_000_000, 0), (84_000_000, 1), (100_000_000, 2), (140_000_000, 3),
        (170_000_000, 4), (180_000_000, 5), (865_000_000, 6))
    RF_BAND_MAP = ((47_900_000, 0), (61_100_000, 1), (152_600_000, 2), (164_700_000, 3),
        (203_500_000, 4), (457_800_000, 5), (865_000_000, 6))
    KM_MAP = {
        CHIP_ID_HDC1: ((61_100_000, 0x74), (350_000_000, 0x40), (720_000_000, 0x30), (865_000_000, 0x40)),
        CHIP_ID_HDC2: ((47_900_000, 0x38), (61_100_000, 0x44), (350_000_000, 0x30), (720_000_000, 0x24),
            (865_000_000, 0x3C))}
    GAIN_TAPER_MAP = (
        (45_400_000, 0x1F), (45_800_000, 0x1E), (46_200_000, 0x1D),
        (46_700_000, 0x1C), (47_100_000, 0x1B), (47_500_000, 0x1A),
        (47_900_000, 0x19), (49_600_000, 0x17), (51_200_000, 0x16),
        (52_900_000, 0x15), (54_500_000, 0x14), (56_200_000, 0x13),
        (57_800_000, 0x12), (59_500_000, 0x11), (61_100_000, 0x10),
        (67_600_000, 0x0D), (74_200_000, 0x0C), (80_700_000, 0x0B),
        (87_200_000, 0x0A), (93_800_000, 0x09), (100_300_000, 0x08),
        (106_900_000, 0x07), (113_400_000, 0x06), (119_900_000, 0x05),
        (126_500_000, 0x04), (133_000_000, 0x03), (139_500_000, 0x02),
        (146_100_000, 0x01), (152_600_000, 0x00), (154_300_000, 0x1F),
        (156_100_000, 0x1E), (157_800_000, 0x1D), (159_500_000, 0x1C),
        (161_200_000, 0x1B), (163_000_000, 0x1A), (164_700_000, 0x19),
        (170_200_000, 0x17), (175_800_000, 0x16), (181_300_000, 0x15),
        (186_900_000, 0x14), (192_400_000, 0x13), (198_000_000, 0x12),
        (203_500_000, 0x11), (216_200_000, 0x14), (228_900_000, 0x13),
        (241_600_000, 0x12), (254_400_000, 0x11), (267_100_000, 0x10),
        (279_800_000, 0x0F), (292_500_000, 0x0E), (305_200_000, 0x0D),
        (317_900_000, 0x0C), (330_700_000, 0x0B), (343_400_000, 0x0A),
        (356_100_000, 0x09), (368_800_000, 0x08), (381_500_000, 0x07),
        (394_200_000, 0x06), (406_900_000, 0x05), (419_700_000, 0x04),
        (432_400_000, 0x03), (445_100_000, 0x02), (457_800_000, 0x01),
        (476_300_000, 0x19), (494_800_000, 0x18), (513_300_000, 0x17),
        (531_800_000, 0x16), (550_300_000, 0x15), (568_900_000, 0x14),
        (587_400_000, 0x13), (605_900_000, 0x12), (624_400_000, 0x11),
        (642_900_000, 0x10), (661_400_000, 0x0F), (679_900_000, 0x0E),
        (698_400_000, 0x0D), (716_900_000, 0x0C), (735_400_000, 0x0B),
        (753_900_000, 0x0A), (772_500_000, 0x09), (791_000_000, 0x08),
        (809_500_000, 0x07), (828_000_000, 0x06), (846_500_000, 0x05),
        (865_000_000, 0x04),
    )
    IR_MEASURE_MAP = ((30_000_000, 4), (200_000_000, 5), (600_000_000, 6), (865_000_000, 7))
    RF_CAL_MAP = {
        CHIP_ID_HDC1: ((41_000_000, 0x1E), (43_000_000, 0x30), (45_000_000, 0x43), (46_000_000, 0x4D),
            (47_000_000, 0x54), (47_900_000, 0x64), (49_100_000, 0x20), (50_000_000, 0x22),
            (51_000_000, 0x2A), (53_000_000, 0x32), (55_000_000, 0x35), (56_000_000, 0x3C),
            (57_000_000, 0x3F), (58_000_000, 0x48), (59_000_000, 0x4D), (60_000_000, 0x58),
            (61_100_000, 0x5F)),
        CHIP_ID_HDC2: ((41_000_000, 0x0F), (43_000_000, 0x1C), (45_000_000, 0x2F), (46_000_000, 0x39),
            (47_000_000, 0x40), (47_900_000, 0x50), (49_100_000, 0x16), (50_000_000, 0x18),
            (51_000_000, 0x20), (53_000_000, 0x28), (55_000_000, 0x2B), (56_000_000, 0x32),
            (57_000_000, 0x35), (58_000_000, 0x3E), (59_000_000, 0x43), (60_000_000, 0x4E),
            (61_100_000, 0x55), (63_000_000, 0x0F), (64_000_000, 0x11), (65_000_000, 0x12),
            (66_000_000, 0x15), (67_000_000, 0x16), (68_000_000, 0x17), (70_000_000, 0x19),
            (71_000_000, 0x1C), (72_000_000, 0x1D), (73_000_000, 0x1F), (74_000_000, 0x20),
            (75_000_000, 0x21), (76_000_000, 0x24), (77_000_000, 0x25), (78_000_000, 0x27),
            (80_000_000, 0x28), (81_000_000, 0x29), (82_000_000, 0x2D), (83_000_000, 0x2E),
            (84_000_000, 0x2F), (85_000_000, 0x31), (86_000_000, 0x33), (87_000_000, 0x34),
            (88_000_000, 0x35), (89_000_000, 0x37), (90_000_000, 0x38), (91_000_000, 0x39),
            (93_000_000, 0x3C), (94_000_000, 0x3E), (95_000_000, 0x3F), (96_000_000, 0x40),
            (97_000_000, 0x42), (99_000_000, 0x45), (100_000_000, 0x46), (102_000_000, 0x48),
            (103_000_000, 0x4A), (105_000_000, 0x4D), (106_000_000, 0x4E), (107_000_000, 0x50),
            (108_000_000, 0x51), (110_000_000, 0x54), (111_000_000, 0x56), (112_000_000, 0x57),
            (113_000_000, 0x58), (114_000_000, 0x59), (115_000_000, 0x5C), (116_000_000, 0x5D),
            (117_000_000, 0x5F), (119_000_000, 0x60), (120_000_000, 0x64), (121_000_000, 0x65),
            (122_000_000, 0x66), (123_000_000, 0x68), (124_000_000, 0x69), (125_000_000, 0x6C),
            (126_000_000, 0x6D), (127_000_000, 0x6E), (128_000_000, 0x70), (129_000_000, 0x71),
            (130_000_000, 0x75), (131_000_000, 0x77), (132_000_000, 0x78), (133_000_000, 0x7B),
            (134_000_000, 0x7E), (135_000_000, 0x81), (136_000_000, 0x82), (137_000_000, 0x87),
            (138_000_000, 0x88), (139_000_000, 0x8D), (140_000_000, 0x8E), (141_000_000, 0x91),
            (142_000_000, 0x95), (143_000_000, 0x9A), (144_000_000, 0x9D), (145_000_000, 0xA1),
            (146_000_000, 0xA2), (147_000_000, 0xA4), (148_000_000, 0xA9), (149_000_000, 0xAE),
            (150_000_000, 0xB0), (151_000_000, 0xB1), (152_000_000, 0xB7), (152_600_000, 0xBD),
            (154_000_000, 0x20), (155_000_000, 0x22), (156_000_000, 0x24), (157_000_000, 0x25),
            (158_000_000, 0x27), (159_000_000, 0x29), (160_000_000, 0x2C), (161_000_000, 0x2D),
            (163_000_000, 0x2E), (164_000_000, 0x2F), (164_700_000, 0x30), (166_000_000, 0x11),
            (167_000_000, 0x12), (168_000_000, 0x13), (169_000_000, 0x14), (170_000_000, 0x15),
            (172_000_000, 0x16), (173_000_000, 0x17), (174_000_000, 0x18), (175_000_000, 0x1A),
            (176_000_000, 0x1B), (178_000_000, 0x1D), (179_000_000, 0x1E), (180_000_000, 0x1F),
            (181_000_000, 0x20), (182_000_000, 0x21), (183_000_000, 0x22), (184_000_000, 0x24),
            (185_000_000, 0x25), (186_000_000, 0x26), (187_000_000, 0x27), (188_000_000, 0x29),
            (189_000_000, 0x2A), (190_000_000, 0x2C), (191_000_000, 0x2D), (192_000_000, 0x2E),
            (193_000_000, 0x2F), (194_000_000, 0x30), (195_000_000, 0x33), (196_000_000, 0x35),
            (198_000_000, 0x36), (200_000_000, 0x38), (201_000_000, 0x3C), (202_000_000, 0x3D),
            (203_500_000, 0x3E), (206_000_000, 0x0E), (208_000_000, 0x0F), (212_000_000, 0x10),
            (216_000_000, 0x11), (217_000_000, 0x12), (218_000_000, 0x13), (220_000_000, 0x14),
            (222_000_000, 0x15), (225_000_000, 0x16), (228_000_000, 0x17), (231_000_000, 0x18),
            (234_000_000, 0x19), (235_000_000, 0x1A), (236_000_000, 0x1B), (237_000_000, 0x1C),
            (240_000_000, 0x1D), (242_000_000, 0x1E), (244_000_000, 0x1F), (247_000_000, 0x20),
            (249_000_000, 0x21), (252_000_000, 0x22), (253_000_000, 0x23), (254_000_000, 0x24),
            (256_000_000, 0x25), (259_000_000, 0x26), (262_000_000, 0x27), (264_000_000, 0x28),
            (267_000_000, 0x29), (269_000_000, 0x2A), (271_000_000, 0x2B), (273_000_000, 0x2C),
            (275_000_000, 0x2D), (277_000_000, 0x2E), (279_000_000, 0x2F), (282_000_000, 0x30),
            (284_000_000, 0x31), (286_000_000, 0x32), (287_000_000, 0x33), (290_000_000, 0x34),
            (293_000_000, 0x35), (295_000_000, 0x36), (297_000_000, 0x37), (300_000_000, 0x38),
            (303_000_000, 0x39), (305_000_000, 0x3A), (306_000_000, 0x3B), (307_000_000, 0x3C),
            (310_000_000, 0x3D), (312_000_000, 0x3E), (315_000_000, 0x3F), (318_000_000, 0x40),
            (320_000_000, 0x41), (323_000_000, 0x42), (324_000_000, 0x43), (325_000_000, 0x44),
            (327_000_000, 0x45), (331_000_000, 0x46), (334_000_000, 0x47), (337_000_000, 0x48),
            (339_000_000, 0x49), (340_000_000, 0x4A), (341_000_000, 0x4B), (343_000_000, 0x4C),
            (345_000_000, 0x4D), (349_000_000, 0x4E), (352_000_000, 0x4F), (353_000_000, 0x50),
            (355_000_000, 0x51), (357_000_000, 0x52), (359_000_000, 0x53), (361_000_000, 0x54),
            (362_000_000, 0x55), (364_000_000, 0x56), (368_000_000, 0x57), (370_000_000, 0x58),
            (372_000_000, 0x59), (375_000_000, 0x5A), (376_000_000, 0x5B), (377_000_000, 0x5C),
            (379_000_000, 0x5D), (382_000_000, 0x5E), (384_000_000, 0x5F), (385_000_000, 0x60),
            (386_000_000, 0x61), (388_000_000, 0x62), (390_000_000, 0x63), (393_000_000, 0x64),
            (394_000_000, 0x65), (396_000_000, 0x66), (397_000_000, 0x67), (398_000_000, 0x68),
            (400_000_000, 0x69), (402_000_000, 0x6A), (403_000_000, 0x6B), (407_000_000, 0x6C),
            (408_000_000, 0x6D), (409_000_000, 0x6E), (410_000_000, 0x6F), (411_000_000, 0x70),
            (412_000_000, 0x71), (413_000_000, 0x72), (414_000_000, 0x73), (417_000_000, 0x74),
            (418_000_000, 0x75), (420_000_000, 0x76), (422_000_000, 0x77), (423_000_000, 0x78),
            (424_000_000, 0x79), (427_000_000, 0x7A), (428_000_000, 0x7B), (429_000_000, 0x7D),
            (432_000_000, 0x7F), (434_000_000, 0x80), (435_000_000, 0x81), (436_000_000, 0x83),
            (437_000_000, 0x84), (438_000_000, 0x85), (439_000_000, 0x86), (440_000_000, 0x87),
            (441_000_000, 0x88), (442_000_000, 0x89), (445_000_000, 0x8A), (446_000_000, 0x8B),
            (447_000_000, 0x8C), (448_000_000, 0x8E), (449_000_000, 0x8F), (450_000_000, 0x90),
            (452_000_000, 0x91), (453_000_000, 0x93), (454_000_000, 0x94), (456_000_000, 0x96),
            (457_800_000, 0x98), (461_000_000, 0x11), (468_000_000, 0x12), (472_000_000, 0x13),
            (473_000_000, 0x14), (474_000_000, 0x15), (481_000_000, 0x16), (486_000_000, 0x17),
            (491_000_000, 0x18), (498_000_000, 0x19), (499_000_000, 0x1A), (501_000_000, 0x1B),
            (506_000_000, 0x1C), (511_000_000, 0x1D), (516_000_000, 0x1E), (520_000_000, 0x1F),
            (521_000_000, 0x20), (525_000_000, 0x21), (529_000_000, 0x22), (533_000_000, 0x23),
            (539_000_000, 0x24), (541_000_000, 0x25), (547_000_000, 0x26), (549_000_000, 0x27),
            (551_000_000, 0x28), (556_000_000, 0x29), (561_000_000, 0x2A), (563_000_000, 0x2B),
            (565_000_000, 0x2C), (569_000_000, 0x2D), (571_000_000, 0x2E), (577_000_000, 0x2F),
            (580_000_000, 0x30), (582_000_000, 0x31), (584_000_000, 0x32), (588_000_000, 0x33),
            (591_000_000, 0x34), (596_000_000, 0x35), (598_000_000, 0x36), (603_000_000, 0x37),
            (604_000_000, 0x38), (606_000_000, 0x39), (612_000_000, 0x3A), (615_000_000, 0x3B),
            (617_000_000, 0x3C), (621_000_000, 0x3D), (622_000_000, 0x3E), (625_000_000, 0x3F),
            (632_000_000, 0x40), (633_000_000, 0x41), (634_000_000, 0x42), (642_000_000, 0x43),
            (643_000_000, 0x44), (647_000_000, 0x45), (650_000_000, 0x46), (652_000_000, 0x47),
            (657_000_000, 0x48), (661_000_000, 0x49), (662_000_000, 0x4A), (665_000_000, 0x4B),
            (667_000_000, 0x4C), (670_000_000, 0x4D), (673_000_000, 0x4E), (676_000_000, 0x4F),
            (677_000_000, 0x50), (681_000_000, 0x51), (683_000_000, 0x52), (686_000_000, 0x53),
            (688_000_000, 0x54), (689_000_000, 0x55), (691_000_000, 0x56), (695_000_000, 0x57),
            (698_000_000, 0x58), (703_000_000, 0x59), (704_000_000, 0x5A), (705_000_000, 0x5B),
            (707_000_000, 0x5C), (710_000_000, 0x5D), (712_000_000, 0x5E), (717_000_000, 0x5F),
            (718_000_000, 0x60), (721_000_000, 0x61), (722_000_000, 0x62), (723_000_000, 0x63),
            (725_000_000, 0x64), (727_000_000, 0x65), (730_000_000, 0x66), (732_000_000, 0x67),
            (735_000_000, 0x68), (740_000_000, 0x69), (741_000_000, 0x6A), (742_000_000, 0x6B),
            (743_000_000, 0x6C), (745_000_000, 0x6D), (747_000_000, 0x6E), (748_000_000, 0x6F),
            (750_000_000, 0x70), (752_000_000, 0x71), (754_000_000, 0x72), (757_000_000, 0x73),
            (758_000_000, 0x74), (760_000_000, 0x75), (763_000_000, 0x76), (764_000_000, 0x77),
            (766_000_000, 0x78), (767_000_000, 0x79), (768_000_000, 0x7A), (773_000_000, 0x7B),
            (774_000_000, 0x7C), (776_000_000, 0x7D), (777_000_000, 0x7E), (778_000_000, 0x7F),
            (779_000_000, 0x80), (781_000_000, 0x81), (783_000_000, 0x82), (784_000_000, 0x83),
            (785_000_000, 0x84), (786_000_000, 0x85), (793_000_000, 0x86), (794_000_000, 0x87),
            (795_000_000, 0x88), (797_000_000, 0x89), (799_000_000, 0x8A), (801_000_000, 0x8B),
            (802_000_000, 0x8C), (803_000_000, 0x8D), (804_000_000, 0x8E), (810_000_000, 0x90),
            (811_000_000, 0x91), (812_000_000, 0x92), (814_000_000, 0x93), (816_000_000, 0x94),
            (817_000_000, 0x96), (818_000_000, 0x97), (820_000_000, 0x98), (821_000_000, 0x99),
            (822_000_000, 0x9A), (828_000_000, 0x9B), (829_000_000, 0x9D), (830_000_000, 0x9F),
            (831_000_000, 0xA0), (833_000_000, 0xA1), (835_000_000, 0xA2), (836_000_000, 0xA3),
            (837_000_000, 0xA4), (838_000_000, 0xA6), (840_000_000, 0xA8), (842_000_000, 0xA9),
            (845_000_000, 0xAA), (846_000_000, 0xAB), (847_000_000, 0xAD), (848_000_000, 0xAE),
            (852_000_000, 0xAF), (853_000_000, 0xB0), (858_000_000, 0xB1), (860_000_000, 0xB2),
            (861_000_000, 0xB3), (862_000_000, 0xB4), (863_000_000, 0xB6), (864_000_000, 0xB8),
            (865_000_000, 0xB9))
    }
    RF_CAL_BANDS = (
        (47_900_000, 0, (46_000_000, 0, 0)),
        (61_100_000, 1, (52_200_000, 0, 0)),
        (152_600_000, 2, (70_100_000, 136_800_000, 0)),
        (164_700_000, 3, (156_700_000, 0, 0)),
        (203_500_000, 4, (186_250_000, 0, 0)),
        (457_800_000, 5, (230_000_000, 345_000_000, 426_000_000)),
        (865_000_000, 6, (489_500_000, 697_500_000, 842_000_000)),
    )
    CID_TARGET_MAP = (
        (46_000_000, 0x04, 1_800), (52_200_000, 0x0A, 1_500),
        (70_100_000, 0x01, 4_000), (136_800_000, 0x18, 4_000),
        (156_700_000, 0x18, 4_000), (186_250_000, 0x0A, 4_000),
        (230_000_000, 0x0A, 4_000), (345_000_000, 0x18, 4_000),
        (426_000_000, 0x0E, 4_000), (489_500_000, 0x1E, 4_000),
        (697_500_000, 0x32, 4_000), (842_000_000, 0x3A, 4_000),
    )
    RF_CAL_DC_OVER_DT_MAP = (
        (47_900_000, 0x00), (55_000_000, 0x00), (61_100_000, 0x0A),
        (64_000_000, 0x0A), (82_000_000, 0x14), (84_000_000, 0x19),
        (119_000_000, 0x1C), (124_000_000, 0x20), (129_000_000, 0x2A),
        (134_000_000, 0x32), (139_000_000, 0x39), (144_000_000, 0x3E),
        (149_000_000, 0x3F), (152_600_000, 0x40), (154_000_000, 0x40),
        (164_700_000, 0x41), (203_500_000, 0x32), (353_000_000, 0x19),
        (356_000_000, 0x1A), (359_000_000, 0x1B), (363_000_000, 0x1C),
        (366_000_000, 0x1D), (369_000_000, 0x1E), (373_000_000, 0x1F),
        (376_000_000, 0x20), (379_000_000, 0x21), (383_000_000, 0x22),
        (386_000_000, 0x23), (389_000_000, 0x24), (393_000_000, 0x25),
        (396_000_000, 0x26), (399_000_000, 0x27), (402_000_000, 0x28),
        (404_000_000, 0x29), (407_000_000, 0x2A), (409_000_000, 0x2B),
        (412_000_000, 0x2C), (414_000_000, 0x2D), (417_000_000, 0x2E),
        (419_000_000, 0x2F), (422_000_000, 0x30), (424_000_000, 0x31),
        (427_000_000, 0x32), (429_000_000, 0x33), (432_000_000, 0x34),
        (434_000_000, 0x35), (437_000_000, 0x36), (439_000_000, 0x37),
        (442_000_000, 0x38), (444_000_000, 0x39), (447_000_000, 0x3A),
        (449_000_000, 0x3B), (457_800_000, 0x3C), (465_000_000, 0x0F),
        (477_000_000, 0x12), (483_000_000, 0x14), (502_000_000, 0x19),
        (508_000_000, 0x1B), (519_000_000, 0x1C), (522_000_000, 0x1D),
        (524_000_000, 0x1E), (534_000_000, 0x1F), (549_000_000, 0x20),
        (554_000_000, 0x22), (584_000_000, 0x24), (589_000_000, 0x26),
        (658_000_000, 0x27), (664_000_000, 0x2C), (669_000_000, 0x2D),
        (699_000_000, 0x2E), (704_000_000, 0x30), (709_000_000, 0x31),
        (714_000_000, 0x32), (724_000_000, 0x33), (729_000_000, 0x36),
        (739_000_000, 0x38), (744_000_000, 0x39), (749_000_000, 0x3B),
        (754_000_000, 0x3C), (759_000_000, 0x3D), (764_000_000, 0x3E),
        (769_000_000, 0x3F), (774_000_000, 0x40), (779_000_000, 0x41),
        (784_000_000, 0x43), (789_000_000, 0x46), (794_000_000, 0x48),
        (799_000_000, 0x4B), (804_000_000, 0x4F), (809_000_000, 0x54),
        (814_000_000, 0x59), (819_000_000, 0x5D), (824_000_000, 0x61),
        (829_000_000, 0x68), (834_000_000, 0x6E), (839_000_000, 0x75),
        (844_000_000, 0x7E), (849_000_000, 0x82), (854_000_000, 0x84),
        (859_000_000, 0x8F), (865_000_000, 0x9A),
    )

    def __init__(self, frontend: "AF9013Frontend", i2c_address: int = I2C_ADDRESS) -> None:
        if i2c_address != self.I2C_ADDRESS:
            raise ValueError("TDA18271 must use I2C address 0x60")
        self.frontend = frontend
        self.i2c_address = i2c_address
        # For now, we initialize the register cache with the default values for chip ID CHIP_ID_HDC2.
        # init() will update this as needed.
        self._registers = bytearray(self.DEFAULT_REGISTERS[self.CHIP_ID_HDC2])
        self._chip_id: int | None = None
        self._initialized = False
        self._sleeping = True
        self.current_tuning: Tuning | None = None
        self._rf_calibration_bands = [RFCalibrationBand(*band) for band in self.RF_CAL_BANDS]
        self._rf_calibration_initialized = False
        self._rf_calibration_temperature: int | None = None

    @property
    def chip_id(self) -> int | None:
        return self._chip_id

    @property
    def initialized(self) -> bool:
        return self._initialized

    @property
    def sleeping(self) -> bool:
        return self._sleeping

    def get_config_dict(self) -> dict[str, object]:
        """Return tuner configuration and current state for diagnostics."""
        return {
            "i2c_address": self.i2c_address,
            "chip_id": self._chip_id,
            "initialized": self._initialized,
            "sleeping": self._sleeping,
            "current_tuning": self.current_tuning,
            "temperature": self._read_thermometer() if self._initialized else None,
            "rf_calibration_initialized": self._rf_calibration_initialized,
            "rf_calibration_temperature": self._rf_calibration_temperature,
        }

    def get_if_frequency_hz(self) -> int:
        """Return the IF selected by the active digital standard map."""
        if self._chip_id is None or self.current_tuning is None:
            raise RuntimeError("TDA18271 has not been tuned")
        if_frequency, _, _, _, _ = self.STANDARD_MAP[self._chip_id][self.current_tuning.bandwidth_hz]
        return if_frequency

    def _read_register(self, register: int, length: int) -> bytes:
        return self.frontend.gated_read_register(self.i2c_address, register, length)

    def _write_register(self, register: int, data: bytes) -> None:
        if not 0 <= register < self.REGISTER_COUNT:
            raise ValueError("TDA18271 register is out of range")
        if register + len(data) > self.REGISTER_COUNT:
            raise ValueError("TDA18271 register write exceeds register map")
        self.frontend.gated_write_register(self.i2c_address, register, data)
        self._registers[register : register + len(data)] = data

    def _write_cached(self, register: int, length: int) -> None:
        for offset in range(0, length, self.I2C_WRITE_CHUNK_SIZE):
            chunk_length = min(self.I2C_WRITE_CHUNK_SIZE, length - offset)
            self._write_register(
                register + offset,
                bytes(self._registers[register + offset : register + offset + chunk_length]),
            )

    def _read_extended(self) -> bytes:
        values = self._read_register(self.REG_ID, self.REGISTER_COUNT)
        if len(values) != self.REGISTER_COUNT:
            raise IOError("short TDA18271 extended register read")
        for register, value in enumerate(values):
            if register not in (self.REG_EB9, self.REG_EB16, self.REG_EB17, self.REG_EB19, self.REG_EB20):
                self._registers[register] = value
        return values

    def init_hardware(self) -> None:
        chip_id = self._read_register(self.REG_ID, 1)[0]
        if chip_id not in (self.CHIP_ID_HDC1, self.CHIP_ID_HDC2):
            raise IOError(f"unexpected TDA18271 ID 0x{chip_id:02x}")
        self._chip_id = chip_id
        if self._initialized:
            self._registers[self.REG_EP3] &= 0x1F
            self._write_cached(self.REG_EP3, 1)
            self._sleeping = False
            return
        self._registers[:] = self.DEFAULT_REGISTERS[chip_id]
        for register in range(0, self.REGISTER_COUNT, self.INIT_CHUNK_SIZE):
            length = min(self.INIT_CHUNK_SIZE, self.REGISTER_COUNT - register)
            self._write_cached(register, length)
        self._image_rejection_calibration()
        self._initialized = True
        if self._chip_id == self.CHIP_ID_HDC2:
            self._initialize_hdc2_calibration()
        self._sleeping = False

    def _lookup_rf_band(self, frequency_hz: int) -> RFCalibrationBand:
        for limit_hz, _, defaults_hz in self.RF_CAL_BANDS:
            if frequency_hz <= limit_hz:
                return next(band for band in self._rf_calibration_bands if band.rf_max_hz == limit_hz)
        raise ValueError("frequency is outside the HDC2 RF calibration bands")

    def _lookup_cid_target(self, frequency_hz: int) -> tuple[int, int]:
        for limit_hz, target, count_limit in self.CID_TARGET_MAP:
            if frequency_hz <= limit_hz:
                return target, count_limit
        raise ValueError("frequency is outside the HDC2 power-scan bands")

    def _read_thermometer(self) -> int:
        self._registers[self.REG_TM] |= 0x10
        self._write_cached(self.REG_TM, 1)
        self._read_extended()
        if ((self._registers[self.REG_TM] & 0x0F) == 0x00 and self._registers[self.REG_TM] & 0x20) or (
            (self._registers[self.REG_TM] & 0x0F) == 0x08 and not self._registers[self.REG_TM] & 0x20
        ):
            self._registers[self.REG_TM] ^= 0x20
            self._write_cached(self.REG_TM, 1)
            sleep(0.010)
            self._read_extended()
        code = self._registers[self.REG_TM] & 0x0F
        # Temperature values are in Gray-code ordering, and likely represent actual temperatures in Celsius.
        low_values = (60, 62, 66, 64, 74, 72, 68, 70, 90, 88, 84, 86, 76, 78, 82, 80) 
        high_values = (92, 94, 98, 96, 106, 104, 100, 102, 122, 120, 116, 118, 108, 110, 114, 112)
        temperature = (high_values if self._registers[self.REG_TM] & 0x20 else low_values)[code]
        self._registers[self.REG_TM] &= ~0x10
        self._write_cached(self.REG_TM, 1)
        self._registers[self.REG_EP4] &= ~0x03
        self._write_cached(self.REG_EP4, 1)
        return temperature

    def _power_scan(self, frequency_hz: int) -> tuple[bool, int]:
        self._apply_rf_maps(frequency_hz)
        self._write_cached(self.REG_EP2, 1)
        self._write_cached(self.REG_EB14, 1)
        scan_frequency_hz = frequency_hz + 1_000_000
        self._set_main_pll(scan_frequency_hz)
        self._write_cached(self.REG_MPD, 4)
        sleep(0.005)
        self._registers[self.REG_EP4] = (self._registers[self.REG_EP4] & ~0x03) | 0x01
        self._write_cached(self.REG_EP4, 1)
        self._write_cached(self.REG_EP2, 1)
        self._read_extended()
        target, count_limit = self._lookup_cid_target(frequency_hz)
        direction = 1
        count = 0
        wait = False
        while self._registers[self.REG_EB10] & 0x3F < target:
            scan_frequency_hz = frequency_hz + direction * count + 1_000_000
            self._set_main_pll(scan_frequency_hz)
            self._write_cached(self.REG_MPD, 4)
            if wait:
                sleep(0.005)
                wait = False
            else:
                sleep(0.0001)
            self._write_cached(self.REG_EP2, 1)
            self._read_extended()
            count += 200_000
            if count <= count_limit * 1000:
                continue
            if direction < 0:
                break
            direction = -1
            count = 200_000
            wait = True
        calibrated = (self._registers[self.REG_EB10] & 0x3F) >= target
        return calibrated, scan_frequency_hz - 1_000_000 if calibrated else frequency_hz

    def _calibrate_rf_point(self, frequency_hz: int) -> int:
        self._registers[self.REG_EP4] &= ~0x03
        self._write_cached(self.REG_EP4, 1)
        self._registers[self.REG_EP3] |= 0x40
        self._registers[self.REG_EB18] |= 0x03
        self._write_cached(self.REG_EB18, 1)
        self._apply_rf_maps(frequency_hz)
        self._write_cached(self.REG_EP1, 3)
        self._write_cached(self.REG_EB13, 1)
        self._charge_pump(self.REG_EB4, True)
        self._charge_pump(self.REG_EB7, True)
        self._registers[self.REG_EB14] = 0
        self._write_cached(self.REG_EB14, 1)
        self._registers[self.REG_EB20] &= ~0x20
        self._write_cached(self.REG_EB20, 1)
        self._registers[self.REG_EP4] |= 0x03
        self._write_cached(self.REG_EP4, 2)
        self._set_cal_pll(frequency_hz)
        self._write_cached(self.REG_CPD, 4)
        self._set_main_pll(frequency_hz + 1_000_000)
        self._write_cached(self.REG_MPD, 4)
        sleep(0.005)
        self._write_cached(self.REG_EP2, 1)
        self._write_cached(self.REG_EP1, 1)
        self._write_cached(self.REG_EP2, 1)
        self._write_cached(self.REG_EP1, 1)
        self._charge_pump(self.REG_EB4, False)
        self._charge_pump(self.REG_EB7, False)
        sleep(0.010)
        self._registers[self.REG_EB20] |= 0x20
        self._write_cached(self.REG_EB20, 1)
        sleep(0.060)
        self._registers[self.REG_EP4] &= ~0x03
        self._registers[self.REG_EP3] &= ~0x40
        self._registers[self.REG_EB18] &= ~0x03
        self._write_cached(self.REG_EB18, 1)
        self._write_cached(self.REG_EP3, 2)
        self._write_cached(self.REG_EP1, 1)
        self._read_extended()
        return self._registers[self.REG_EB14]

    def _initialize_hdc2_calibration(self) -> None:
        self._registers[self.REG_EP3] = (self._registers[self.REG_EP3] & 0xE0) | 0x12
        self._registers[self.REG_EP4] &= ~0x1F
        self._write_cached(self.REG_EP3, 2)
        self._registers[self.REG_EB18] &= ~0x03
        self._registers[self.REG_EB21] &= ~0x03
        self._registers[self.REG_EB23] |= 0x06
        self._write_cached(self.REG_EB18, 1)
        self._write_cached(self.REG_EB21, 3)
        sleep(0.200)
        for band in self._rf_calibration_bands:
            base_values: list[int] = []
            measured_values: list[int] = []
            for default_frequency_hz in band.defaults_hz:
                if not default_frequency_hz:
                    continue
                calibrated, measured_frequency_hz = self._power_scan(default_frequency_hz)
                base_value = self._registers[self.REG_EB14]
                measured_value = self._calibrate_rf_point(measured_frequency_hz) if calibrated else base_value
                base_values.append(base_value)
                measured_values.append(measured_value)
                band.measured_hz[len(measured_values) - 1] = measured_frequency_hz
            if measured_values:
                if len(measured_values) == 1:
                    band.offset_low = measured_values[0] - base_values[0]
                else:
                    first_delta = measured_values[0] - base_values[0]
                    second_delta = measured_values[1] - base_values[1]
                    span_khz = (band.measured_hz[1] - band.measured_hz[0]) // 1000
                    band.slope_low = (second_delta - first_delta) // max(span_khz, 1)
                    band.offset_low = first_delta
                    if len(measured_values) > 2:
                        band.slope_high = (measured_values[2] - base_values[2] - second_delta) // max(
                            (band.measured_hz[2] - band.measured_hz[1]) // 1000, 1
                        )
                        band.offset_high = second_delta
        self._rf_calibration_temperature = self._read_thermometer()
        self._apply_hdc2_calibration_por()
        self._rf_calibration_initialized = True

    def _apply_hdc2_calibration_por(self) -> None:
        """Restore the Linux driver's tuner power-on-reset state after RF calibration."""
        self._registers[self.REG_EB12] &= ~0x20
        self._write_cached(self.REG_EB12, 1)
        self._registers[self.REG_EB18] &= ~0x83
        self._registers[self.REG_EB21] |= 0x03
        self._registers[self.REG_EP3] = (self._registers[self.REG_EP3] & 0x1F) | 0x80
        self._write_cached(self.REG_EB18, 1)
        self._write_cached(self.REG_EP3, 1)
        self._registers[self.REG_EB23] &= ~0x06
        self._write_cached(self.REG_EB21, 3)

    def _apply_hdc2_calibration(self, frequency_hz: int) -> None:
        if not self._rf_calibration_initialized:
            self._initialize_hdc2_calibration()
        band = self._lookup_rf_band(frequency_hz)
        base = self._map_value(frequency_hz, self.RF_CAL_MAP[self.CHIP_ID_HDC2])
        frequency_khz = frequency_hz // 1000
        if band.measured_hz[2] and frequency_hz >= band.measured_hz[1]:
            correction = band.slope_high * (frequency_khz - band.measured_hz[1] // 1000) + band.offset_high
        elif band.measured_hz[0]:
            correction = band.slope_low * (frequency_khz - band.measured_hz[0] // 1000) + band.offset_low
        else:
            correction = 0
        if self._rf_calibration_temperature is not None:
            dc_over_dt = self._map_value(frequency_hz, self.RF_CAL_DC_OVER_DT_MAP)
            correction += dc_over_dt * (self._read_thermometer() - self._rf_calibration_temperature) // 1000
        self._registers[self.REG_EB14] = max(0, min(255, base + correction))
        self._write_cached(self.REG_EB14, 1)

    def _charge_pump(self, register: int, enabled: bool) -> None:
        self._registers[register] = (self._registers[register] & ~0x20) | (0x20 if enabled else 0)
        self._write_cached(register, 1)

    def _image_rejection_calibration(self) -> None:
        self._registers[self.REG_EB17] = 0x00
        self._write_cached(self.REG_EB17, 1)
        for value in (0x03, 0x43, 0x4C):
            self._registers[self.REG_EB17] = value
            self._write_cached(self.REG_EB17, 1)

        if self._chip_id == self.CHIP_ID_HDC1:
            for value in (0xA0, 0xA7, 0xE7, 0xEC):
                self._registers[self.REG_EB20] = value
                self._write_cached(self.REG_EB20, 1)

        self._registers[self.REG_EP3:self.REG_MD3 + 1] = bytes.fromhex("1f6681cc6c0000cd770800")
        self._write_cached(self.REG_EP3, 11)
        if self._chip_id == self.CHIP_ID_HDC2:
            self._charge_pump(self.REG_EB4, True)
            sleep(0.001)
            self._charge_pump(self.REG_EB4, False)
        sleep(0.005)
        self._write_cached(self.REG_EP1, 1)
        sleep(0.005)

        self._registers[self.REG_EP5] = 0x85
        self._registers[self.REG_CPD:self.REG_CD3 + 1] = bytes((0xCB, 0x66, 0x70, 0x00))
        self._write_cached(self.REG_EP3, 7)
        sleep(0.005)
        self._write_cached(self.REG_EP2, 1)
        sleep(0.030)

        self._registers[self.REG_EP5] = 0x82
        self._registers[self.REG_CPD] = 0xA8
        self._registers[self.REG_CD2] = 0x00
        self._registers[self.REG_MPD:self.REG_MD3 + 1] = bytes((0xA9, 0x73, 0x1A, 0x00))
        self._write_cached(self.REG_EP3, 11)
        sleep(0.005)
        self._write_cached(self.REG_EP1, 1)
        sleep(0.005)

        self._registers[self.REG_EP5] = 0x86
        self._registers[self.REG_CPD] = 0xA8
        self._registers[self.REG_CD1:self.REG_CD3 + 1] = bytes((0x66, 0xA0, 0x00))
        self._write_cached(self.REG_EP3, 7)
        sleep(0.005)
        self._write_cached(self.REG_EP2, 1)
        sleep(0.030)

        self._registers[self.REG_EP5] = 0x83
        self._registers[self.REG_CPD] = 0x98
        self._registers[self.REG_CD1:self.REG_CD3 + 1] = bytes((0x65, 0x00, 0x00))
        self._registers[self.REG_MPD:self.REG_MD3 + 1] = bytes((0x99, 0x71, 0xCD, 0x00))
        self._write_cached(self.REG_EP3, 11)
        sleep(0.005)
        self._write_cached(self.REG_EP1, 1)
        sleep(0.005)

        self._registers[self.REG_EP5] = 0x87
        self._registers[self.REG_CD1:self.REG_CD3 + 1] = bytes((0x65, 0x50, 0x00))
        self._write_cached(self.REG_EP3, 7)
        sleep(0.005)
        self._write_cached(self.REG_EP2, 1)
        sleep(0.030)

        self._registers[self.REG_EP4] = 0x64
        self._write_cached(self.REG_EP4, 1)
        self._write_cached(self.REG_EP1, 1)

    def _set_main_pll(self, frequency_hz: int) -> None:
        if self._chip_id is None:
            raise RuntimeError("TDA18271 is not initialized")
        frequency_khz = frequency_hz // 1000
        for limit_khz, post_divider, multiplier in self.MAIN_PLL_MAP[self._chip_id]:
            if frequency_khz <= limit_khz:
                divider = ((multiplier * frequency_khz) << 7) // 125
                break
        else:
            raise ValueError("frequency is outside the TDA18271 PLL range")
        self._registers[self.REG_MPD] = post_divider
        self._registers[self.REG_MD1] = (divider >> 16) & 0x7F
        self._registers[self.REG_MD2] = (divider >> 8) & 0xFF
        self._registers[self.REG_MD3] = divider & 0xFF

    @staticmethod
    def _map_value(frequency_hz: int, table: tuple[tuple[int, int], ...]) -> int:
        for limit, value in table:
            if frequency_hz <= limit:
                return value
        raise ValueError("frequency is outside the TDA18271 lookup maps")

    def _apply_rf_maps(self, frequency_hz: int) -> None:
        if self._chip_id is None:
            raise RuntimeError("TDA18271 is not initialized")
        self._registers[self.REG_EP1] = (self._registers[self.REG_EP1] & 0xF8) | self._map_value(frequency_hz, self.BP_FILTER_MAP)
        self._registers[self.REG_EP2] = (self._registers[self.REG_EP2] & 0x1F) | (self._map_value(frequency_hz, self.RF_BAND_MAP) << 5)
        self._registers[self.REG_EP2] = (self._registers[self.REG_EP2] & 0xE0) | self._map_value(frequency_hz, self.GAIN_TAPER_MAP)
        self._registers[self.REG_EB13] = (self._registers[self.REG_EB13] & 0x83) | self._map_value(frequency_hz, self.KM_MAP[self._chip_id])
        self._registers[self.REG_EP5] = (self._registers[self.REG_EP5] & 0xF8) | self._map_value(frequency_hz, self.IR_MEASURE_MAP)
        if frequency_hz <= 61_100_000:
            self._registers[self.REG_EB14] = self._map_value(frequency_hz, self.RF_CAL_MAP[self._chip_id])

    def _calibrate_rf_tracking_filter(self, frequency_hz: int, bandwidth_hz: int) -> None:
        if self._chip_id != self.CHIP_ID_HDC1:
            return
        self._registers[self.REG_EP4] &= ~0x03
        self._write_cached(self.REG_EP4, 1)
        self._registers[self.REG_EP3] |= 0x40
        self._registers[self.REG_EB18] |= 0x03
        self._write_cached(self.REG_EB18, 1)
        self._registers[self.REG_EP1] = (self._registers[self.REG_EP1] & 0xF8) | self._map_value(
            frequency_hz, self.BP_FILTER_MAP
        )
        self._registers[self.REG_EP2] = (self._registers[self.REG_EP2] & 0x1F) | (
            self._map_value(frequency_hz, self.RF_BAND_MAP) << 5
        )
        self._registers[self.REG_EP2] = (self._registers[self.REG_EP2] & 0xE0) | self._map_value(
            frequency_hz, self.GAIN_TAPER_MAP
        )
        self._registers[self.REG_EB13] = (self._registers[self.REG_EB13] & 0x83) | self._map_value(
            frequency_hz, self.KM_MAP[self._chip_id]
        )
        self._registers[self.REG_EB4] = (self._registers[self.REG_EB4] & 0x07) | 0x60
        self._registers[self.REG_EB7] = 0x60
        self._registers[self.REG_EB14] = 0x00
        self._registers[self.REG_EB20] = 0xCC
        self._write_cached(self.REG_EP1, 3)
        self._write_cached(self.REG_EB13, 1)
        self._write_cached(self.REG_EB4, 1)
        self._write_cached(self.REG_EB7, 1)
        self._write_cached(self.REG_EB14, 1)
        self._write_cached(self.REG_EB20, 1)
        self._registers[self.REG_EP4] |= 0x03
        self._write_cached(self.REG_EP4, 2)
        self._set_cal_pll(frequency_hz + bandwidth_hz // 2)
        self._set_main_pll(frequency_hz + bandwidth_hz // 2 + 1_000_000)
        self._write_cached(self.REG_EP3, 11)
        sleep(0.005)
        self._write_cached(self.REG_EP2, 1)
        self._write_cached(self.REG_EP1, 1)
        self._write_cached(self.REG_EP2, 1)
        self._write_cached(self.REG_EP1, 1)
        self._registers[self.REG_EB4] = (self._registers[self.REG_EB4] & 0x07) | 0x40
        self._write_cached(self.REG_EB4, 1)
        self._registers[self.REG_EB7] = 0x40
        self._write_cached(self.REG_EB7, 1)
        sleep(0.010)
        self._registers[self.REG_EB20] = 0xEC
        self._write_cached(self.REG_EB20, 1)
        sleep(0.060)
        self._registers[self.REG_EP4] &= ~0x03
        self._write_cached(self.REG_EP4, 1)
        self._write_cached(self.REG_EP1, 1)
        if frequency_hz <= 61_100_000:
            self._registers[self.REG_EB14] = self._map_value(frequency_hz, self.RF_CAL_MAP[self._chip_id])
            self._write_cached(self.REG_EB14, 1)

    def _set_cal_pll(self, frequency_hz: int) -> None:
        if self._chip_id is None:
            raise RuntimeError("TDA18271 is not initialized")
        frequency_khz = frequency_hz // 1000
        cal_map = {
            self.CHIP_ID_HDC1: ((33_000, 0xDD, 0xD0), (36_000, 0xDC, 0xC0), (40_000, 0xDB, 0xB0), (44_000, 0xDA, 0xA0), (49_000, 0xD9, 0x90), (55_000, 0xD8, 0x80), (63_000, 0xD3, 0x70), (67_000, 0xCD, 0x68), (73_000, 0xCC, 0x60), (80_000, 0xCB, 0x58), (88_000, 0xCA, 0x50), (98_000, 0xC9, 0x48), (110_000, 0xC8, 0x40), (126_000, 0xC3, 0x38), (135_000, 0xBD, 0x34), (147_000, 0xBC, 0x30), (160_000, 0xBB, 0x2C), (176_000, 0xBA, 0x28), (196_000, 0xB9, 0x24), (220_000, 0xB8, 0x20), (252_000, 0xB3, 0x1C), (271_000, 0xAD, 0x1A), (294_000, 0xAC, 0x18), (321_000, 0xAB, 0x16), (353_000, 0xAA, 0x14), (392_000, 0xA9, 0x12), (441_000, 0xA8, 0x10), (505_000, 0xA3, 0x0E), (543_000, 0x9D, 0x0D), (589_000, 0x9C, 0x0C), (642_000, 0x9B, 0x0B), (707_000, 0x9A, 0x0A), (785_000, 0x99, 0x09), (883_000, 0x98, 0x08), (1_010_000, 0x93, 0x07)),
            self.CHIP_ID_HDC2: ((33_813, 0xDD, 0xD0), (36_625, 0xDC, 0xC0), (39_938, 0xDB, 0xB0), (43_938, 0xDA, 0xA0), (48_813, 0xD9, 0x90), (54_938, 0xD8, 0x80), (62_813, 0xD3, 0x70), (67_625, 0xCD, 0x68), (73_250, 0xCC, 0x60), (79_875, 0xCB, 0x58), (87_875, 0xCA, 0x50), (97_625, 0xC9, 0x48), (109_875, 0xC8, 0x40), (125_625, 0xC3, 0x38), (135_250, 0xBD, 0x34), (146_500, 0xBC, 0x30), (159_750, 0xBB, 0x2C), (175_750, 0xBA, 0x28), (195_250, 0xB9, 0x24), (219_750, 0xB8, 0x20), (251_250, 0xB3, 0x1C), (270_500, 0xAD, 0x1A), (293_000, 0xAC, 0x18), (319_500, 0xAB, 0x16), (351_500, 0xAA, 0x14), (390_500, 0xA9, 0x12), (439_500, 0xA8, 0x10), (502_500, 0xA3, 0x0E), (541_000, 0x9D, 0x0D), (586_000, 0x9C, 0x0C), (639_000, 0x9B, 0x0B), (703_000, 0x9A, 0x0A), (781_000, 0x99, 0x09), (879_000, 0x98, 0x08)),
        }[self._chip_id]
        for limit_khz, post_divider, multiplier in cal_map:
            if frequency_khz <= limit_khz:
                divider = ((multiplier * frequency_khz) << 7) // 125
                self._registers[self.REG_CPD] = post_divider
                self._registers[self.REG_CD1] = (divider >> 16) & 0x7F
                self._registers[self.REG_CD2] = (divider >> 8) & 0xFF
                self._registers[self.REG_CD3] = divider & 0xFF
                return
        raise ValueError("frequency is outside the TDA18271 calibration PLL range")

    def tune(self, tuning: Tuning) -> None:
        if not self.MIN_FREQUENCY_HZ <= tuning.frequency_hz <= self.MAX_FREQUENCY_HZ:
            raise ValueError("frequency is outside the TDA18271 range")
        if tuning.bandwidth_hz not in (6_000_000, 7_000_000, 8_000_000):
            raise ValueError("TDA18271 DVB-T bandwidth must be 6, 7, or 8 MHz")
        self.init_hardware()
        if self._chip_id is None:
            raise RuntimeError("TDA18271 is not initialized")

        if_frequency, agc_mode, standard, if_level, rfagc_top = self.STANDARD_MAP[self._chip_id][tuning.bandwidth_hz]
        self._calibrate_rf_tracking_filter(tuning.frequency_hz, tuning.bandwidth_hz)
        self._apply_rf_maps(tuning.frequency_hz)
        if self._chip_id == self.CHIP_ID_HDC2:
            self._apply_hdc2_calibration(tuning.frequency_hz)
        self._registers[self.REG_EP3] = (self._registers[self.REG_EP3] & 0xE0) | (agc_mode << 3) | standard
        if self._chip_id == self.CHIP_ID_HDC2:
            self._registers[self.REG_EP3] &= ~0x04
        self._registers[self.REG_EP1] |= 0x40
        self._registers[self.REG_TM] &= ~0x10
        self._registers[self.REG_EB1] |= 0x04
        self._registers[self.REG_EB1] &= ~0x03
        self._registers[self.REG_EP4] = (self._registers[self.REG_EP4] & 0xE3) | (if_level << 2)
        self._registers[self.REG_EP4] &= ~0x83
        self._registers[self.REG_EB22] = (self._registers[self.REG_EB22] & 0x80) | rfagc_top
        self._registers[self.REG_EP3] &= 0x1F
        self._set_main_pll(tuning.frequency_hz + if_frequency)

        self._write_cached(self.REG_EB22, 1)
        self._write_cached(self.REG_EB1, 1)
        self._write_cached(self.REG_EP1, self.REG_MD3 - self.REG_EP1 + 1)
        self._write_cached(self.REG_EB13, 1)
        self._write_cached(self.REG_TM, self.REG_EP5 - self.REG_TM + 1)
        self._registers[self.REG_EB4] |= 0x20
        self._write_cached(self.REG_EB4, 1)
        sleep(0.001)
        self._registers[self.REG_EB4] &= ~0x20
        self._write_cached(self.REG_EB4, 1)
        sleep(0.020)
        if self._chip_id == self.CHIP_ID_HDC2:
            self._registers[self.REG_EP3] |= 0x04
            self._write_cached(self.REG_EP3, 1)
        self.current_tuning = tuning
        self._sleeping = False

    def sleep(self) -> None:
        if not self._initialized or self._sleeping:
            return
        self._registers[self.REG_EP3] = (self._registers[self.REG_EP3] & 0x1F) | 0x80
        self._write_cached(self.REG_EP3, 1)
        self._sleeping = True
