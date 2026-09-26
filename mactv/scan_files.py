"""Download and parse DVB scan files."""

from __future__ import annotations

from html.parser import HTMLParser
from pathlib import Path

import requests

from mactv.driver_api.tuner import (
    CodeRate,
    DVBTuning,
    GuardInterval,
    Hierarchy,
    Modulation,
    TransmissionMode,
)


DVB_T_SCAN_TABLES_URL = "https://git.linuxtv.org/dtv-scan-tables.git/tree/dvb-t"
DVB_T_SCAN_TABLE_URL = "https://git.linuxtv.org/dtv-scan-tables.git/plain/dvb-t/{name}"


class _ScanTableLinkParser(HTMLParser):
    """Collect direct file links from the LinuxTV DVB-T directory page."""

    def __init__(self) -> None:
        super().__init__()
        self.file_names: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "a":
            return
        href = dict(attrs).get("href")
        if href is None:
            return
        prefix = "/dtv-scan-tables.git/tree/dvb-t/"
        file_name = href.removeprefix(prefix)
        if (
            file_name
            and not file_name.endswith("/")
            and "/" not in file_name
            and file_name not in self.file_names
        ):
            self.file_names.append(file_name)


def known_dvb_t_scan_tables() -> list[str]:
    """Download and return known LinuxTV DVB-T scan-table filenames."""
    parser = _ScanTableLinkParser()
    response = requests.get(DVB_T_SCAN_TABLES_URL, timeout=15)
    response.raise_for_status()
    parser.feed(response.content.decode("utf-8"))
    return sorted(parser.file_names)


def parse_scan(scan: str | Path) -> list[DVBTuning]:
    """Read a local or known LinuxTV scan table into DVB tuning values."""
    scan_path = Path(scan)
    if scan_path.is_file():
        contents = scan_path.read_text(encoding="utf-8")
    else:
        response = requests.get(
            DVB_T_SCAN_TABLE_URL.format(name=scan_path.name),
            timeout=15,
        )
        response.raise_for_status()
        contents = response.content.decode("utf-8")

    tunings: list[DVBTuning] = []
    channel: dict[str, str] = {}

    for line_number, line in enumerate(contents.splitlines(), 1):
        stripped = line.strip()
        if stripped == "[CHANNEL]":
            tuning = _parse_channel(channel, line_number)
            if tuning is not None:
                tunings.append(tuning)
            channel.clear()
            continue
        if "=" in stripped:
            key, value = (part.strip() for part in stripped.split("=", 1))
            if key in {
                "DELIVERY_SYSTEM",
                "FREQUENCY",
                "BANDWIDTH_HZ",
                "CODE_RATE_HP",
                "CODE_RATE_LP",
                "MODULATION",
                "TRANSMISSION_MODE",
                "GUARD_INTERVAL",
                "HIERARCHY",
            }:
                channel[key] = value
            continue
        fields = line.split()
        if not fields or fields[0] != "T":
            continue
        if len(fields) != 9:
            raise ValueError(f"invalid scan entry on line {line_number}")
        tunings.append(
            DVBTuning(
                frequency_hz=int(fields[1]),
                bandwidth_hz=_parse_bandwidth(fields[2]),
                code_rate_hp=CodeRate(fields[3].lower()),
                code_rate_lp=CodeRate(fields[4].lower()),
                modulation=Modulation(fields[5].lower()),
                transmission_mode=TransmissionMode(fields[6].lower()),
                guard_interval=GuardInterval(fields[7].lower()),
                hierarchy=Hierarchy(fields[8].lower()),
            )
        )
    tuning = _parse_channel(channel, len(contents.splitlines()) + 1)
    if tuning is not None:
        tunings.append(tuning)
    return tunings


def _parse_channel(channel: dict[str, str], line_number: int) -> DVBTuning | None:
    """Convert one LinuxTV channel block into a DVB tuning value."""
    if not channel or channel.get("DELIVERY_SYSTEM") != "DVBT":
        return None
    required = (
        "FREQUENCY",
        "BANDWIDTH_HZ",
        "CODE_RATE_HP",
        "CODE_RATE_LP",
        "MODULATION",
        "TRANSMISSION_MODE",
        "GUARD_INTERVAL",
        "HIERARCHY",
    )
    if any(field not in channel for field in required):
        raise ValueError(f"invalid scan entry on line {line_number}")
    return DVBTuning(
        frequency_hz=int(channel["FREQUENCY"]),
        bandwidth_hz=int(channel["BANDWIDTH_HZ"]),
        code_rate_hp=CodeRate(channel["CODE_RATE_HP"].lower()),
        code_rate_lp=CodeRate(channel["CODE_RATE_LP"].lower()),
        modulation=Modulation(channel["MODULATION"].lower().replace("/", "")),
        transmission_mode=TransmissionMode(channel["TRANSMISSION_MODE"].lower()),
        guard_interval=GuardInterval(channel["GUARD_INTERVAL"].lower()),
        hierarchy=Hierarchy(channel["HIERARCHY"].lower()),
    )


def _parse_bandwidth(value: str) -> int:
    """Convert a scan-table bandwidth token to Hz."""
    normalized = value.lower()
    if normalized.endswith("mhz"):
        return int(normalized[:-3]) * 1_000_000
    if normalized.endswith("khz"):
        return int(normalized[:-3]) * 1_000
    return int(normalized)
