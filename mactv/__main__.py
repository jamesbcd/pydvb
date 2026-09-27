"""Test command-line tool for the Leadtek AF9015 device."""

import argparse
import pprint
from pathlib import Path
from time import monotonic, sleep
from typing import Any, Callable, cast


from .scan_tables import known_dvb_t_scan_tables, parse_scan
from .stream_scanner import DVBScanResult, DVBScanner, ScanPolicy
from mactv.driver_api.tuner import DVBTuning, GuardInterval, Hierarchy, Modulation, TransmissionMode, CodeRate

from .drivers.af9015.device import AF9015Device
from . import usb
from .server import LocalTS_HTTPServer


TS_PACKET_SIZE = 188
MAX_CAPTURE_BYTES = ((35 * 1024 * 1024) // TS_PACKET_SIZE) * TS_PACKET_SIZE
READ_TIMEOUT_SECONDS = 5.0
DEFAULT_SERVER_ADDRESS = ("127.0.0.1", 8080)


def parse_frequency(value: str) -> int:
    """Parse a frequency in MHz (for example, ``177.5``)."""
    frequency_mhz = float(value)
    if frequency_mhz <= 0:
        raise argparse.ArgumentTypeError("frequency must be positive")
    return round(frequency_mhz * 1_000_000)


def parse_server_address(value: str) -> tuple[str, int]:
    """Parse a server address in ``HOST:PORT`` form."""
    try:
        host, port_text = value.rsplit(":", 1)
        port = int(port_text)
    except (ValueError, TypeError):
        raise argparse.ArgumentTypeError("server address must be HOST:PORT") from None
    if not host or not 0 <= port <= 65535:
        raise argparse.ArgumentTypeError("server address must contain a valid host and port")
    return host, port


def capture_stream(
    adapter,
    output_path: Path,
    max_bytes: int = MAX_CAPTURE_BYTES,
    read_timeout: float = READ_TIMEOUT_SECONDS,
) -> tuple[int, float]:
    """Capture transport-stream data to disk and return bytes and elapsed seconds."""
    if max_bytes <= 0:
        raise ValueError("max_bytes must be positive")
    if read_timeout < 0:
        raise ValueError("read_timeout must be non-negative")

    bytes_written = 0
    start_time = monotonic()
    try:
        print(f"Starting capture to {output_path} with max_bytes={max_bytes}")
        adapter.start()
        with output_path.open("wb") as output:
            while bytes_written < max_bytes:
                try:
                    chunk = adapter.read_stream(max(0, int(read_timeout * 1000)))
                except TimeoutError as error:
                    raise IOError("transport stream read returned no data") from error
                remaining = max_bytes - bytes_written
                output.write(chunk[:remaining])
                bytes_written += min(len(chunk), remaining)
    finally:
        adapter.stop()
        if hasattr(adapter, "sleep"):
            adapter.sleep()
    return bytes_written, monotonic() - start_time


def scan_stream(
    adapter,
    read_timeout: float = READ_TIMEOUT_SECONDS,
) -> DVBScanResult:
    """Feed a live transport stream to the scanner until its tables are complete."""
    if read_timeout < 0:
        raise ValueError("read_timeout must be non-negative")

    scanner = DVBScanner()
    try:
        adapter.start()
        return scanner.scan(
            adapter,
            policy=ScanPolicy.ALL_SERVICE_METADATA,
            read_timeout_ms=max(0, int(read_timeout * 1000)),
        )
    finally:
        adapter.stop()
        if hasattr(adapter, "sleep"):
            adapter.sleep()


def wait_for_lock_with_diagnostics(device, adapter_index: int, timeout: float = 20.0):
    """Poll and print frontend status while waiting for a DVB lock."""
    deadline = monotonic() + timeout
    while True:
        status = device.read_status(adapter_index=adapter_index)
        print(f"Frontend status: {status}")
        if status.lock or monotonic() >= deadline:
            return status
        sleep(1.0)


def print_tuning_hardware_diagnostics(device, adapter_index: int) -> None:
    """Print physical tuner and demodulator registers after a nonblocking tune."""
    try:
        adapters = getattr(device, "adapters", None)
        if adapters is None:
            return
        adapter = cast(Any, adapters[adapter_index])
        frontend = getattr(adapter, "frontend", None)
        tuner = getattr(frontend, "tuner", None)
        if frontend is None or tuner is None:
            return
        tuner = cast(Any, tuner)
        get_register_cache = getattr(tuner, "get_register_cache", None)
        read_hardware_register = getattr(tuner, "read_hardware_register", None)
        read_tuner_registers = getattr(tuner, "read_hardware_registers", None) if tuner else None
        read_frontend_registers = getattr(frontend, "read_hardware_diagnostics", None) if frontend else None
        if (
            not callable(get_register_cache)
            or not callable(read_hardware_register)
            or not callable(read_tuner_registers)
            or not callable(read_frontend_registers)
        ):
            return
        cached_registers = cast(bytes, cast(Callable[[], bytes], get_register_cache)())
        direct_mpd = cast(int, cast(Callable[[int], int], read_hardware_register)(tuner.REG_MPD))
        tuner_registers = cast(bytes, cast(Callable[[], bytes], read_tuner_registers)())
        frontend_registers = cast(
            dict[int, int], cast(Callable[[], dict[int, int]], read_frontend_registers)()
        )
    except (OSError, RuntimeError, ValueError) as error:
        print(f"Hardware register readback failed: {error}")
        return

    register_dump = " ".join(
        f"{register:02x}:{value:02x}" for register, value in enumerate(tuner_registers)
    )
    print("TDA18271 cached registers 08-0f: " + cached_registers[8:16].hex(" "))
    frequency_registers = (3, 4, 5, 6, 7, 28, 29, 37, 12, 13, 14, 15)
    print("TDA18271 cached frequency registers: " + " ".join(
        f"{register:02x}:{cached_registers[register]:02x}" for register in frequency_registers
    ))
    mpd = tuner_registers[tuner.REG_MPD]
    print(f"TDA18271 physical registers: {register_dump}")
    print(f"TDA18271 physical MPD (0x{tuner.REG_MPD:02x}): 0x{mpd:02x}; "
          f"digital IF notch={'on' if mpd & 0x80 else 'off'}")
    print(f"TDA18271 direct MPD read (0x{tuner.REG_MPD:02x}): 0x{direct_mpd:02x}")
    print("AF9013 physical registers: " + " ".join(
        f"0x{register:04x}={value.hex(' ') if isinstance(value, bytes) else f'{value:02x}'}"
        for register, value in frontend_registers.items()
    ))


def _print_status(device) -> None:
    """Print USB, adapter, frontend, demodulator, and tuner status."""
    print(f"USB speed: {device.bridge.transport.get_speed().name}")
    print(f"Adapter count: {device.adapter_count}")
    for adapter_index, adapter in enumerate(device.adapters):
        print(f"Adapter {adapter_index}:")
        print(f"Frontend index {adapter_index} configuration:")
        pprint.pprint(adapter.frontend.get_config_dict(), underscore_numbers=True)
        print()
        print(f"Demodulator index {adapter_index} configuration:")
        pprint.pprint(adapter.demuxer.get_config_dict(),
                      underscore_numbers=True)
        print()
        print(f"Tuner index {adapter_index} configuration:")
        pprint.pprint(adapter.tuner.get_config_dict() if adapter.tuner else None,
                      underscore_numbers=True)
        print()
        print(f"Frontend index {adapter_index} status:")
        pprint.pprint(adapter.read_status(), underscore_numbers=True)
        print()


def _run_device(device: AF9015Device, args) -> int:
    print(f"Device state: {'warm' if device.is_warm else 'cold'}")
    # 'device' command stops at device discovery
    if args.command == "device":
        return 0

    if not device.is_warm:
        print("Device is cold; initializing and downloading firmware to device...")
        device.init_hardware()
        print(f"Device state: {'warm' if device.is_warm else 'cold'}")
    else:
        print("Device is warm; reinitializing hardware...")
        device.init_hardware()

    if args.command == "init":
        return 0

    if args.command == "status":
        _print_status(device)
        return 0

    if args.command == "sleep":
        print("Putting device to sleep...")
        device.sleep()
        print("Device is sleeping")
        return 0

    if args.use_scan is not None:
        tunings = parse_scan(args.use_scan, prefer_auto=True)
        if not tunings:
            raise ValueError(f"scan table contains no DVB-T channels: {args.use_scan}")
    else:
        tunings = [DVBTuning(frequency_hz=args.frequency,
                             bandwidth_hz=args.bandwidth * 1_000_000,
                             modulation=Modulation.QAM64,
                             transmission_mode=TransmissionMode.K8,
                             guard_interval=GuardInterval.GI_1_16,
                             code_rate_hp=CodeRate.RATE_3_4,
                             code_rate_lp=CodeRate.RATE_1_2,
                             hierarchy=Hierarchy.NONE)]

    tuning_status = None
    if args.command == "tune":
        locks_acquired = []
        for tuning in tunings:
            print(f"Tuning to frequency {tuning.frequency_hz / 1_000_000} MHz" +
                  f" with bandwidth {tuning.bandwidth_hz / 1_000_000} MHz: ", end="", flush=True)
            status = device.tune(tuning, adapter_index=args.adapter_index,
                                 wait_for_lock=not args.hardware_diagnostics)
            if args.hardware_diagnostics:
                print_tuning_hardware_diagnostics(device, args.adapter_index)
                if hasattr(device, "read_status"):
                    status = wait_for_lock_with_diagnostics(device, args.adapter_index)
            print(f"Lock result: {'LOCKED' if status and status.lock else 'NO LOCK'}")
            locks_acquired.append(status and status.lock)
        return 0 if all(locks_acquired) else 2
    else:
        print(f"Tuning to frequency {tunings[0].frequency_hz / 1_000_000} MHz" +
                f" with bandwidth {tunings[0].bandwidth_hz / 1_000_000} MHz: ", end="", flush=True)
        tuning_status = device.tune(tunings[0], adapter_index=args.adapter_index, wait_for_lock=True)
        print(f"Lock result: {'LOCKED' if tuning_status and tuning_status.lock else 'NO LOCK'}")

    if args.record is not None:
        if tuning_status is None or not tuning_status.lock:
            print("Capture failed: frontend did not lock; no transport stream is available")
            return 2
        print(f"Frontend lock acquired. Starting capture to {args.record}")
        adapter = device.adapters[args.adapter_index]
        try:
            bytes_written, elapsed = capture_stream(adapter, args.record)
        except (OSError, RuntimeError, ValueError) as error:
            print(f"Capture failed: {error}")
            return 1
        device.bridge.transport.log_stats()
        print(f"Captured {bytes_written} bytes in {elapsed:.2f} seconds to {args.record}")

    if args.scan_stream:
        if tuning_status is None or not tuning_status.lock:
            print("Scan failed: frontend did not lock; no transport stream is available")
            return 2
        adapter = device.adapters[args.adapter_index]
        try:
            result = scan_stream(adapter)
        except (OSError, RuntimeError, TimeoutError, ValueError) as error:
            print(f"Scan failed: {error}")
            return 1
        device.bridge.transport.log_stats()
        del result.warnings
        for s in result.summarise():
            print(f"{s.logical_channel_number:3} {s.name:30} {s.network_name:15} {s.service_type:3} {s.service_metatype}")
        # pprint.pprint(result.summarise(), underscore_numbers=True)

    if args.command == "serve" and args.serve is not None:
        if tuning_status is None or not tuning_status.lock:
            print("Server failed: frontend did not lock; no transport stream is available")
            return 2
        host, port = args.serve
        adapter = device.adapters[args.adapter_index]
        server = LocalTS_HTTPServer(
            adapter, host=host, port=port
        )
        bound_host, bound_port = server.address
        print(f"Frontend lock acquired. Serving http://{bound_host}:{bound_port}/stream.ts")
        try:
            server.start()
            server.serve_forever()
        except (OSError, RuntimeError, ValueError) as error:
            print(f"Server failed: {error}")
            return 1
        except KeyboardInterrupt:
            print("Stopping transport-stream server")
        finally:
            server.close()
            device.bridge.transport.log_stats()
            if hasattr(adapter, "sleep"):
                adapter.sleep()

    return 0


def main() -> int:
    device_id = argparse.ArgumentParser(add_help=False)
    device_id.add_argument(
        "--vendor-id",
        type=lambda value: int(value, 0),
        default=argparse.SUPPRESS,
    )
    device_id.add_argument(
        "--product-id",
        type=lambda value: int(value, 0),
        default=argparse.SUPPRESS,
    )
    tunable = argparse.ArgumentParser(add_help=False)
    tunable.add_argument(
        "--use-scan",
        type=str,
        metavar="scan-table-name-or-path",
        help="Use a known or local DVB-T scan table",
    )
    parser = argparse.ArgumentParser(
        description="Test an AF9015 device",
        epilog=(
            "stream commands:\n"
            "  record OUTPUT_PATH  Capture transport-stream data to this file\n"
            "  serve [HOST:PORT]   Serve the transport stream over HTTP\n"
            "  scan-stream         Scan a live transport stream until complete"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        parents=[device_id],
    )
    parser.add_argument(
        "--usb-log",
        action="store_true",
        help="Log USB bulk transfers",
    )
    parser.add_argument(
        "--usb-stats",
        action="store_true",
        help="Log stream USB read timing and returned byte counts",
    )
    parser.add_argument(
        "--hardware-diagnostics",
        action="store_true",
        help="Print tuner and demodulator register diagnostics after tuning",
    )
    parser.add_argument(
        "--frequency",
        type=parse_frequency,
        default=226_500_000,    # ABC
        #default=177_500_000,    # Seven Network
        help="DVB-T centre frequency in MHz (default: 177.5)",
    )
    parser.add_argument(
        "--bandwidth",
        type=int,
        choices=(6, 7, 8),
        default=7,
        metavar="MHZ",
        help="DVB-T channel bandwidth in MHz (default: 7)",
    )
    parser.add_argument(
        "--adapter",
        "--adapter-index",
        dest="adapter_index",
        type=int,
        default=0,
        help="Adapter index to tune (default: 0)",
    )
    subparsers = parser.add_subparsers(
        dest="command",
        title="commands",
    )

    subparsers.add_parser(
        "device",
        help="Only check device state; do not initialize or upload firmware",
        parents=[device_id],
    )
    subparsers.add_parser(
        "init",
        help="Initialize the device without tuning",
        parents=[device_id],
    )
    subparsers.add_parser(
        "status",
        help="Initialize the device, print status information, and exit",
        parents=[device_id],
    )
    subparsers.add_parser(
        "sleep",
        help="Initialize the device, put it into its low-power state, and exit",
        parents=[device_id],
    )
    subparsers.add_parser(
        "tune",
        help="Initialize the device, tune it, and exit with the lock result",
        parents=[device_id, tunable],
    )
    subparsers.add_parser(
        "known-scans",
        help="Print known LinuxTV DVB-T scan-table filenames",
    )
    parse_scan_parser = subparsers.add_parser(
        "parse-scan",
        help="Parse a known DVB scan name or local scan file",
    )
    parse_scan_parser.add_argument("scan", metavar="scan-table-name-or-path")
    record_parser = subparsers.add_parser(
        "record",
        help="Capture transport-stream data to a file",
        parents=[tunable],
    )
    record_parser.add_argument("record", type=Path, metavar="OUTPUT_PATH")
    serve_parser = subparsers.add_parser(
        "serve",
        help="Serve the transport stream over HTTP",
        parents=[tunable],
    )
    serve_parser.add_argument(
        "serve",
        nargs="?",
        type=parse_server_address,
        metavar="HOST:PORT",
        default=DEFAULT_SERVER_ADDRESS,
    )
    subparsers.add_parser(
        "scan-stream",
        help="Scan a live transport stream until complete",
        parents=[tunable],
    )

    args = parser.parse_args()
    args.vendor_id = getattr(args, "vendor_id", 0x0413)
    args.product_id = getattr(args, "product_id", 0x6029)
    args.record = getattr(args, "record", None)
    args.serve = getattr(args, "serve", None)
    args.scan_stream = args.command == "scan-stream"

    if args.command == "known-scans":
        for table_name in known_dvb_t_scan_tables():
            print(table_name)
        return 0

    if args.command == "parse-scan":
        pprint.pprint(parse_scan(args.scan), underscore_numbers=True)
        return 0

    stream_actions = [
        name for name, enabled in (
            ("--record", args.record is not None),
            ("--serve", args.serve is not None),
            ("--scan-stream", args.scan_stream),
        ) if enabled
    ]
    if len(stream_actions) > 1:
        parser.error(f"{', '.join(stream_actions)} cannot be used together")
    usb.LOG = args.usb_log
    usb.RECORD_STATS = args.usb_stats

    try:
        with AF9015Device.open(args.vendor_id, args.product_id) as device:
            return _run_device(device, args)
    except (ValueError) as error:
        print(f"Error: {error}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())