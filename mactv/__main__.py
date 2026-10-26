"""Test command-line tool for the Leadtek AF9015 device."""

import argparse
import math
import pprint
from pathlib import Path
from time import monotonic, sleep
from typing import Any, Callable, cast


from mactv import usb, serialize
from mactv.device_manager import DVBDeviceManager
from mactv.driver_api.device import DVBDevice
from mactv.driver_api.tuner import DVBTuning, GuardInterval, Hierarchy, Modulation, TransmissionMode, CodeRate
from mactv.drivers.af9015.device import AF9015Device
from mactv.scan_tables import known_table_names, parse_table
from mactv.server import LocalTS_HTTPServer
from mactv.service_map import DVBServiceMap, map_from_tunings
from mactv.stream_scanner import DVBServiceType


TS_PACKET_SIZE = 188
MAX_CAPTURE_BYTES = ((35 * 1024 * 1024) // TS_PACKET_SIZE) * TS_PACKET_SIZE
READ_TIMEOUT_SECONDS = 5.0
DEFAULT_SERVER_ADDRESS = ("127.0.0.1", 8080)


def parse_frequency(value: str) -> int:
    """Parse a frequency in MHz (for example, ``177.5``)."""
    try:
        frequency_mhz = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError("frequency must be a number in MHz") from None
    if not math.isfinite(frequency_mhz) or frequency_mhz <= 0:
        raise argparse.ArgumentTypeError("frequency must be positive")
    frequency_hz = frequency_mhz * 1_000_000
    if not math.isfinite(frequency_hz):
        raise argparse.ArgumentTypeError("frequency is too large")
    return round(frequency_hz)


def parse_tuning(value: str) -> tuple[int, int]:
    """Parse ``FREQUENCY_MHZ/BANDWIDTH_MHZ`` into Hz-valued tuning parameters."""
    parts = value.split("/")
    if len(parts) != 2:
        raise argparse.ArgumentTypeError(
            "tuning must be FREQUENCY_MHZ/BANDWIDTH_MHZ (for example, 177.5/7)"
        )

    frequency_hz = parse_frequency(parts[0])
    try:
        bandwidth_mhz = int(parts[1])
    except ValueError:
        raise argparse.ArgumentTypeError("bandwidth must be 6, 7, or 8 MHz") from None
    if bandwidth_mhz not in (6, 7, 8):
        raise argparse.ArgumentTypeError("bandwidth must be 6, 7, or 8 MHz")
    return frequency_hz, bandwidth_mhz * 1_000_000


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


def _report_scan_failure_diagnostics(
    device: DVBDevice, adapter_index: int, error: Exception
) -> None:
    """Report frontend state and optional USB transfer statistics after a scan error."""
    print(
        f"Scan failed with {type(error).__name__}: {error}; collecting diagnostics.",
        flush=True,
    )
    adapter = device.adapters[adapter_index]
    try:
        print(f"Frontend status at scan failure: {adapter.read_status()}", flush=True)
    except Exception as diagnostic_error:
        print(f"Frontend status read failed: {diagnostic_error}", flush=True)

    if usb.RECORD_STATS:
        try:
            device.log_transport_stats()
        except Exception as diagnostic_error:
            print(f"USB transfer statistics failed: {diagnostic_error}", flush=True)
    else:
        print("USB transfer statistics are disabled; rerun with --usb-stats to collect them.")


def _run_device(device: DVBDevice, args) -> int:
    # 'device' command stops at device discovery
    if args.command == "discover":
        print(f"Device state: {'warm' if device.is_warm() else 'cold'}")
        return 0

    if not device.is_warm():
        print("Device is cold; initializing device...", end="", flush=True)
        device.init_hardware()
    else:
        print("Device is warm; reinitializing hardware...", end="", flush=True)
        device.init_hardware()
    print("Done.")

    if args.command == "init":
        return 0

    if args.command == "status":
        _print_status(device)
        return 0

    if args.command == "sleep":
        print("Putting device to sleep...", end="", flush=True)
        device.sleep()
        print("Done.")
        return 0

    if args.use_table is not None:
        print(f"Parsing scan table from {args.use_table}...", end="", flush=True)
        tunings = parse_table(args.use_table, prefer_auto=True)
        if not tunings:
            raise ValueError(f"scan table contains no DVB-T channels: {args.use_table}")
        print("Done.")
    else:
        tunings = [DVBTuning(frequency_hz=args.tuning[0], bandwidth_hz=args.tuning[1])]

    tuning_status = None
    if args.command == "tune":
        locks_acquired = []
        for index, tuning in enumerate(tunings):
            print(f"Tuning {index+1} of {len(tunings)}: Frequency {tuning.frequency_hz / 1_000_000} MHz" +
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

    if args.record is not None:
        print(f"Tuning to frequency {tunings[0].frequency_hz / 1_000_000} MHz" +
                f" with bandwidth {tunings[0].bandwidth_hz / 1_000_000} MHz: ", end="", flush=True)
        tuning_status = device.tune(tunings[0], adapter_index=args.adapter_index, wait_for_lock=True)
        print(f"Lock result: {'LOCKED' if tuning_status and tuning_status.lock else 'NO LOCK'}")

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
        device.log_transport_stats()
        print(f"Captured {bytes_written} bytes in {elapsed:.2f} seconds to {args.record}")
        return 0

    if args.load_services is not None:
        channel_map = cast(DVBServiceMap, serialize.load(args.load_services))
    else:
        # Scan and serve build a channel map and display the services.
        channel_map = map_from_tunings(
            tunings,
            device.adapters[args.adapter_index],
            on_scan_error=lambda error: _report_scan_failure_diagnostics(
                device, args.adapter_index, error
            ),
        )

    if args.save_services is not None:
        serialize.save(channel_map, args.save_services)

    if args.command == "scan":
        for service in channel_map.services_sorted():
            network_name = None if service is None or service.triplet_id is None else \
                            channel_map.stream_infos[service.triplet_id].network_name
            service_type = DVBServiceType(service.service_type).name \
                            if service.service_type in DVBServiceType else service.service_type
            print(f"Ch {service.logical_channel_number:3} {service.name:25} {service.service_metatype:5} " +
                    f"ID={service.triplet_id:17} {network_name:25} " + f"Type: {service_type}")
        device.log_transport_stats()
        return 0

    if args.command == "serve" and args.serve is not None:
        host, port = args.serve
        server = LocalTS_HTTPServer(
            device.adapters,
            channel_map,
            host=host,
            port=port,
        )
        bound_host, bound_port = server.address
        print(f"Serving playlist at http://{bound_host}:{bound_port} and " +
              f"http://{bound_host}:{bound_port}/playlist.m3u8")
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
            device.log_transport_stats()

    return 0


def main() -> int:
    needs_device = argparse.ArgumentParser(add_help=False)
    needs_device.add_argument(
        "--adapter",
        "--adapter-index",
        dest="adapter_index",
        type=int,
        default=0,
        help="Adapter index to tune (default: 0)",
    )

    needs_tuning = argparse.ArgumentParser(add_help=False)
    needs_tuning.add_argument(
        "--tuning",
        type=parse_tuning,
        default=None,
        metavar="FREQUENCY_MHZ/BANDWIDTH_MHZ",
        help="DVB-T centre frequency and channel bandwidth in MHz",
    )
    needs_tuning.add_argument(
        "--use-table",
        type=str,
        metavar="scan-table-name-or-path",
        help="Use a known or local DVB-T scan table",
    )
    needs_tuning.add_argument(
        "--save-services",
        type=Path,
        metavar="path-to-save-services-json",
        help="Path to save the scanned DVB-T services",
    )
    needs_tuning.add_argument(
        "--load-services",
        type=Path,
        metavar="path-to-load-services-json",
        help="Path to load previously saved DVB-T services",
    )
 
    global_options = argparse.ArgumentParser(add_help=False)
    global_options.add_argument(
        "--usb-log",
        action="store_true",
        help="Log USB bulk transfers",
    )
    global_options.add_argument(
        "--usb-stats",
        action="store_true",
        help="Log stream USB read timing and returned byte counts",
    )
    global_options.add_argument(
        "--hardware-diagnostics",
        action="store_true",
        help="Print tuner and demodulator register diagnostics after tuning",
    )

    main_parser = argparse.ArgumentParser(
        description="Test an AF9015 device",
   )
    subparsers = main_parser.add_subparsers(
        dest="command",
        title="commands",
        help="Available commands",
    )

    subparsers.add_parser(
        "discover",
        help="Discover if a known DVB-T device is connected, without initializing it.",
        parents=[needs_device, global_options],
    )
    subparsers.add_parser(
        "init",
        help="Initialize the device without tuning",
        parents=[needs_device, global_options],
    )
    subparsers.add_parser(
        "status",
        help="Initialize the device, print status information, and exit",
        parents=[needs_device, global_options],
    )
    subparsers.add_parser(
        "sleep",
        help="Initialize the device, put it into its low-power state, and exit",
        parents=[needs_device, global_options],
    )
    subparsers.add_parser(
        "tune",
        help="Initialize the device, tune it, and exit with the lock result",
        parents=[needs_device, needs_tuning, global_options],
    )
    subparsers.add_parser(
        "known-tables",
        help="Print known LinuxTV DVB-T scan-table names (from online repository)",
        parents=[global_options],
    )
    parse_scan_parser = subparsers.add_parser(
        "parse-table",
        help="Parse a known DVB scan-table name or local scan-table file",
        parents=[global_options],
    )
    subparsers.add_parser(
        "scan",
        help="Tune and scan live MPEG-TS stream for its service metadata (ie. channels)",
        parents=[needs_device, needs_tuning, global_options],
    )
    parse_scan_parser.add_argument("scan", metavar="scan-table-name-or-path")
    record_parser = subparsers.add_parser(
        "record",
        help="Record a live MPEG-TS stream data to a file",
        parents=[needs_device, needs_tuning, global_options],
    )
    record_parser.add_argument("record", type=Path, metavar="OUTPUT_PATH")
    serve_parser = subparsers.add_parser(
        "serve",
        help="Serve live MPEG-TS streams over HTTP",
        parents=[needs_device, needs_tuning, global_options],
    )
    serve_parser.add_argument(
        "serve",
        nargs="?",
        type=parse_server_address,
        metavar="HOST:PORT",
        default=DEFAULT_SERVER_ADDRESS,
    )

    args = main_parser.parse_args()
    args.record = getattr(args, "record", None)
    args.serve = getattr(args, "serve", None)
    args.tuning = getattr(args, "tuning", None)

    if args.command in {"tune", "scan", "record", "serve"} and args.use_table is None and args.tuning is None:
        main_parser.error("--tuning is required unless --use-table is provided")

    if args.command == "known-tables":
        for table_name in known_table_names():
            print(table_name)
        return 0

    if args.command == "parse-table":
        pprint.pprint(parse_table(args.scan), underscore_numbers=True)
        return 0

    usb.LOG = args.usb_log
    usb.RECORD_STATS = args.usb_stats

    with DVBDeviceManager([AF9015Device]) as manager:
        result = manager.connect_all()

        if len(result.connected) < 1:
            print("No DVB devices found.")
            return 1

        if len(result.connected) > 1:
            for device in result.connected:
                print(f"Discovered {device.model_name} ({device.device_id})")

        device = result.connected[0]
        print(f"Using {device.model_name} ({device.device_id})")
        return _run_device(device, args)


if __name__ == "__main__":
    raise SystemExit(main())