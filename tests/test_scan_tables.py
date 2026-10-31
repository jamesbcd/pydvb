import pydvb.scan_tables as scan_tables_module
from pydvb.driver_api.tuner import (
    CodeRate,
    GuardInterval,
    Hierarchy,
    Modulation,
    TransmissionMode,
)
from pydvb.scan_tables import parse_table


def test_scanner_lists_known_dvb_t_scan_tables(monkeypatch):
    class Response:
        content = (
            b'<a href="/dtv-scan-tables.git/tree/dvb-t/au-Brisbane">Brisbane</a>'
            b'<a href="/dtv-scan-tables.git/tree/dvb-t/au-Adelaide">Adelaide</a>'
            b'<a href="/dtv-scan-tables.git/tree/dvb-t/subdir/">Directory</a>'
            b'<a href="/other/file">Other</a>'
        )

        def raise_for_status(self):
            pass

    monkeypatch.setattr(scan_tables_module.requests, "get", lambda url, timeout: Response())

    assert scan_tables_module.known_table_names() == ["au-Adelaide", "au-Brisbane"]


def test_scanner_lists_known_tables_from_mirror_when_linux_tv_fails(monkeypatch):
    class Response:
        content = (
            b'[{"name":"au-Brisbane","type":"file"},'
            b'{"name":"subdir","type":"dir"},'
            b'{"name":"au-Adelaide","type":"file"}]'
        )

        def raise_for_status(self):
            pass

    def get(url, timeout):
        assert timeout == 15
        if url == scan_tables_module.DVB_T_SCAN_TABLES_URL[0]:
            raise scan_tables_module.requests.ConnectionError("LinuxTV unavailable")
        assert url == scan_tables_module.DVB_T_SCAN_TABLES_URL[1]
        return Response()

    monkeypatch.setattr(scan_tables_module.requests, "get", get)

    assert scan_tables_module.known_table_names() == ["au-Adelaide", "au-Brisbane"]


def test_scanner_falls_back_when_linux_tv_returns_no_table_links(monkeypatch):
    class Response:
        def __init__(self, content):
            self.content = content

        def raise_for_status(self):
            pass

    calls = []

    def get(url, timeout):
        calls.append(url)
        if url == scan_tables_module.DVB_T_SCAN_TABLES_URL[0]:
            return Response(b"<!doctype html><title>Making sure you're not a bot!</title>")
        return Response(b'[{"name":"au-Brisbane","type":"file"}]')

    monkeypatch.setattr(scan_tables_module.requests, "get", get)

    assert scan_tables_module.known_table_names() == ["au-Brisbane"]
    assert calls == scan_tables_module.DVB_T_SCAN_TABLES_URL


def test_scanner_parses_local_scan_table(tmp_path):
    scan_path = tmp_path / "au-Brisbane"
    scan_path.write_text(
        "# Brisbane\n"
        "T 177500000 7MHz 3/4 1/2 QAM64 8K 1/32 NONE\n"
        "T 226500000 7000kHz AUTO AUTO QPSK 2K AUTO AUTO\n",
        encoding="utf-8",
    )

    assert parse_table(scan_path, prefer_auto=False) == [
        scan_tables_module.DVBTuning(
            177_500_000,
            7_000_000,
            TransmissionMode.K8,
            GuardInterval.GI_1_32,
            Hierarchy.NONE,
            Modulation.QAM64,
            CodeRate.RATE_3_4,
            CodeRate.RATE_1_2,
        ),
        scan_tables_module.DVBTuning(
            226_500_000,
            7_000_000,
            TransmissionMode.K2,
            modulation=Modulation.QPSK,
        ),
    ]
    assert parse_table(scan_path) == [
        scan_tables_module.DVBTuning(177_500_000, 7_000_000),
        scan_tables_module.DVBTuning(226_500_000, 7_000_000),
    ]


def test_scanner_parses_known_scan_table(monkeypatch):
    class Response:
        content = b"T 177500000 7MHz 3/4 1/2 QAM64 8K 1/32 NONE\n"

        def raise_for_status(self):
            pass

    requests_get = lambda url, timeout: Response()
    monkeypatch.setattr(scan_tables_module.requests, "get", requests_get)

    assert parse_table("au-Brisbane", prefer_auto=False) == [
        scan_tables_module.DVBTuning(
            177_500_000,
            7_000_000,
            TransmissionMode.K8,
            GuardInterval.GI_1_32,
            Hierarchy.NONE,
            Modulation.QAM64,
            CodeRate.RATE_3_4,
            CodeRate.RATE_1_2,
        )
    ]


def test_scanner_parses_known_table_from_mirror_when_linux_tv_fails(monkeypatch):
    class Response:
        content = b"T 177500000 7MHz 3/4 1/2 QAM64 8K 1/32 NONE\n"

        def raise_for_status(self):
            pass

    def get(url, timeout):
        assert timeout == 15
        if url == scan_tables_module.DVB_T_SCAN_TABLE_URL[0].format(name="au-Brisbane"):
            raise scan_tables_module.requests.ConnectionError("LinuxTV unavailable")
        assert url == scan_tables_module.DVB_T_SCAN_TABLE_URL[1].format(name="au-Brisbane")
        return Response()

    monkeypatch.setattr(scan_tables_module.requests, "get", get)

    assert parse_table("au-Brisbane") == [
        scan_tables_module.DVBTuning(177_500_000, 7_000_000)
    ]


def test_scanner_parses_known_table_from_mirror_when_linux_tv_returns_html(monkeypatch):
    class Response:
        def __init__(self, content):
            self.content = content

        def raise_for_status(self):
            pass

    calls = []

    def get(url, timeout):
        calls.append(url)
        if url == scan_tables_module.DVB_T_SCAN_TABLE_URL[0].format(name="au-Brisbane"):
            return Response(b"<!doctype html><title>Making sure you're not a bot!</title>")
        assert url == scan_tables_module.DVB_T_SCAN_TABLE_URL[1].format(name="au-Brisbane")
        return Response(b"T 177500000 7MHz 3/4 1/2 QAM64 8K 1/32 NONE\n")

    monkeypatch.setattr(scan_tables_module.requests, "get", get)

    assert parse_table("au-Brisbane") == [
        scan_tables_module.DVBTuning(177_500_000, 7_000_000)
    ]
    assert calls == [
        url.format(name="au-Brisbane")
        for url in scan_tables_module.DVB_T_SCAN_TABLE_URL
    ]


def test_scanner_parses_channel_scan_table_format(tmp_path):
    scan_path = tmp_path / "au-Brisbane"
    scan_path.write_text(
        "[CHANNEL]\n"
        "DELIVERY_SYSTEM = DVBT\n"
        "FREQUENCY = 226500000\n"
        "BANDWIDTH_HZ = 7000000\n"
        "CODE_RATE_HP = 3/4\n"
        "CODE_RATE_LP = NONE\n"
        "MODULATION = QAM/64\n"
        "TRANSMISSION_MODE = 8K\n"
        "GUARD_INTERVAL = 1/16\n"
        "HIERARCHY = NONE\n",
        encoding="utf-8",
    )

    assert parse_table(scan_path, prefer_auto=False) == [
        scan_tables_module.DVBTuning(
            226_500_000,
            7_000_000,
            TransmissionMode.K8,
            GuardInterval.GI_1_16,
            Hierarchy.NONE,
            Modulation.QAM64,
            CodeRate.RATE_3_4,
            CodeRate.NONE,
        )
    ]
    assert parse_table(scan_path) == [
        scan_tables_module.DVBTuning(226_500_000, 7_000_000),
    ]