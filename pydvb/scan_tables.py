"""Download and parse DVB scan files."""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path

import requests
from requests import Response

from pydvb.driver_api.tuner import (
    CodeRate,
    DVBTuning,
    GuardInterval,
    Hierarchy,
    Modulation,
    SpectralInversion,
    TransmissionMode,
)


logger = logging.getLogger(__name__)


DVB_T_SCAN_TABLES_URL = [
    "https://git.linuxtv.org/dtv-scan-tables.git/tree/dvb-t",
    "https://api.github.com/repos/tvheadend/dtv-scan-tables/contents/dvb-t"
    "?ref=tvheadend",
]
DVB_T_SCAN_TABLE_URL = [
    "https://git.linuxtv.org/dtv-scan-tables.git/plain/dvb-t/{name}",
    "https://raw.githubusercontent.com/tvheadend/dtv-scan-tables/"
    "tvheadend/dvb-t/{name}",
]


@dataclass(frozen=True, slots=True)
class ScanTableResult:
    """Parsed DVB tunings and counts of skipped or failed scan-table blocks."""

    tunings: list[DVBTuning]
    skipped_blocks: int = 0
    failed_blocks: int = 0


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


def known_table_names() -> list[str]:
    """Download LinuxTV DVB-T filenames, falling back to the Tvheadend mirror."""
    last_error: requests.RequestException | None = None
    for url in DVB_T_SCAN_TABLES_URL:
        try:
            response = _get_from_urls([url])
        except requests.RequestException as error:
            last_error = error
            continue

        try:
            names = _parse_table_names(response)
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
            continue
        if names:
            return names

    if last_error is not None:
        raise last_error
    return []


def _parse_table_names(response: Response) -> list[str]:
    """Extract table filenames from a LinuxTV HTML page or mirror JSON response."""
    content = response.content.lstrip()
    if content.startswith(b"["):
        entries = json.loads(content)
        if not isinstance(entries, list):
            raise ValueError("invalid DVB-T scan-table listing")
        return sorted(
            entry["name"]
            for entry in entries
            if isinstance(entry, dict)
            and entry.get("type") == "file"
            and isinstance(entry.get("name"), str)
        )
    parser = _ScanTableLinkParser()
    parser.feed(response.content.decode("utf-8"))
    return sorted(parser.file_names)


def parse_table(scan: str | Path, prefer_auto: bool = True) -> ScanTableResult:
    """Read a local or known LinuxTV/mirror scan table into DVB tuning values.

    Args:
        scan: Local path or name of a known LinuxTV DVB-T scan table.
        prefer_auto: Whether to prefer 'auto' values when parsing.

    Returns:
        Parsed tunings and the number of skipped and failed blocks.
    """
    scan_path = Path(scan)
    if scan_path.is_file():
        return _parse_table_contents(scan_path.read_text(encoding="utf-8"), prefer_auto)

    last_error: requests.RequestException | None = None
    successful_response = False
    last_result = ScanTableResult([])
    for url in DVB_T_SCAN_TABLE_URL:
        try:
            response = _get_from_urls([url.format(name=scan_path.name)])
        except requests.RequestException as error:
            last_error = error
            continue

        successful_response = True
        try:
            contents = response.content.decode("utf-8")
        except UnicodeDecodeError:
            continue
        last_result = _parse_table_contents(contents, prefer_auto)
        if last_result.tunings:
            return last_result

    if last_error is not None and not successful_response:
        raise last_error
    return last_result


def _parse_table_contents(contents: str, prefer_auto: bool) -> ScanTableResult:
    """Parse scan-table text into DVB tuning values."""
    tunings: list[DVBTuning] = []
    channel: dict[str, str] = {}
    channel_line_number = 0
    skipped_blocks = 0
    failed_blocks = 0

    def parse_channel_block() -> None:
        nonlocal skipped_blocks, failed_blocks
        if not channel:
            return
        try:
            tuning = _parse_channel(channel, channel_line_number, prefer_auto)
        except ValueError as error:
            logger.warning(
                "Could not parse scan entry on line %d: %s",
                channel_line_number,
                error,
            )
            failed_blocks += 1
            return
        if tuning is None:
            skipped_blocks += 1
        else:
            tunings.append(tuning)

    for line_number, line in enumerate(contents.splitlines(), 1):
        stripped = line.strip()
        if stripped == "[CHANNEL]":
            parse_channel_block()
            channel.clear()
            channel_line_number = line_number
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
                "INVERSION",
            }:
                channel[key] = value
            continue
        fields = line.split()
        if not fields or fields[0] != "T":
            continue
        try:
            if len(fields) != 9:
                raise ValueError(f"invalid scan entry on line {line_number}")
            tuning = DVBTuning(
                frequency_hz=int(fields[1]),
                bandwidth_hz=_parse_bandwidth(fields[2]),
                code_rate_hp=CodeRate(fields[3].lower()),
                code_rate_lp=CodeRate(fields[4].lower()),
                modulation=Modulation(fields[5].lower()),
                transmission_mode=TransmissionMode(fields[6].lower()),
                guard_interval=GuardInterval(fields[7].lower()),
                hierarchy=Hierarchy(fields[8].lower()),
            )
        except ValueError as error:
            logger.warning(
                "Could not parse scan entry on line %d: %s", line_number, error
            )
            failed_blocks += 1
            continue
        tunings.append(_prefer_auto_values(tuning) if prefer_auto else tuning)
    parse_channel_block()
    return ScanTableResult(tunings, skipped_blocks, failed_blocks)


def _get_from_urls(urls: Iterable[str]) -> Response:
    """Return the first successful HTTP response from an ordered URL sequence."""
    last_error: requests.RequestException | None = None
    for url in urls:
        try:
            response = requests.get(url, timeout=15)
            response.raise_for_status()
            return response
        except requests.RequestException as error:
            last_error = error
    if last_error is not None:
        raise last_error
    raise ValueError("no DVB-T scan-table URLs configured")


def _parse_channel(
    channel: dict[str, str], line_number: int, prefer_auto: bool
) -> DVBTuning | None:
    """Convert one LinuxTV channel block into a DVB tuning value."""
    if not channel:
        return None
    delivery_system = channel.get("DELIVERY_SYSTEM")
    if delivery_system != "DVBT":
        if delivery_system is not None:
            logger.warning(
                "Skipping unsupported delivery system %s in scan table at line %d",
                delivery_system,
                line_number,
            )
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
    tuning = DVBTuning(
        frequency_hz=int(channel["FREQUENCY"]),
        bandwidth_hz=int(channel["BANDWIDTH_HZ"]),
        code_rate_hp=CodeRate(channel["CODE_RATE_HP"].lower()),
        code_rate_lp=CodeRate(channel["CODE_RATE_LP"].lower()),
        modulation=Modulation(channel["MODULATION"].lower().replace("/", "")),
        transmission_mode=TransmissionMode(channel["TRANSMISSION_MODE"].lower()),
        guard_interval=GuardInterval(channel["GUARD_INTERVAL"].lower()),
        hierarchy=Hierarchy(channel["HIERARCHY"].lower()),
        inversion=SpectralInversion(channel.get("INVERSION", "auto").lower()),
    )
    return _prefer_auto_values(tuning) if prefer_auto else tuning


def _prefer_auto_values(tuning: DVBTuning) -> DVBTuning:
    """Replace discoverable DVB-T parameters with automatic values."""
    return DVBTuning(
        frequency_hz=tuning.frequency_hz,
        bandwidth_hz=tuning.bandwidth_hz,
    )


def _parse_bandwidth(value: str) -> int:
    """Convert a scan-table bandwidth token to Hz."""
    normalized = value.lower()
    if normalized.endswith("mhz"):
        return int(normalized[:-3]) * 1_000_000
    if normalized.endswith("khz"):
        return int(normalized[:-3]) * 1_000
    return int(normalized)
