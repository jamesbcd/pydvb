"""Test command-line tool for the Leadtek AF9015 device."""

import argparse
import logging
import math
import os
import pprint
from pathlib import Path
import signal
from time import monotonic, sleep
from typing import Any, Callable, cast


from pydvb import usb, serialize
from pydvb.device_manager import DVBDeviceManager
from pydvb.driver_api.device import DVBDevice
from pydvb.driver_api.tuner import DVBTuning, GuardInterval, Hierarchy, Modulation, TransmissionMode, CodeRate
from pydvb.scan_tables import known_table_names, parse_table
from pydvb.server import LocalTS_HTTPServer
from pydvb.service_map import DVBServiceMap, map_from_tunings
from pydvb.stream_scanner import DVBServiceType, DVBDoubletID, DVBTripletID


logger = logging.getLogger(__name__)

DEFAULT_CAPTURE_DURATION_SECONDS = 15.0
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


def parse_duration(value: str) -> float:
    """Parse a positive recording duration in seconds."""
    try:
        duration = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError("duration must be a number of seconds") from None
    if not math.isfinite(duration) or duration <= 0:
        raise argparse.ArgumentTypeError("duration must be a positive finite number of seconds")
    return duration


def parse_lcn(value: str) -> int:
    """Parse a DVB logical channel number."""
    try:
        lcn = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("LCN must be an integer from 0 to 1023") from None
    if not 0 <= lcn <= 1023:
        raise argparse.ArgumentTypeError("LCN must be an integer from 0 to 1023")
    return lcn


def parse_triplet_id(value: str) -> DVBTripletID:
    """Parse a DVB service triplet ID in ONID.TSID.SID form."""
    try:
        triplet_id = DVBTripletID.from_dotted(value)
    except ValueError:
        raise argparse.ArgumentTypeError(
            "triplet ID must be ONID.TSID.SID with each value from 0 to 65535"
        ) from None
    if any(not 0 <= identifier <= 65535 for identifier in triplet_id):
        raise argparse.ArgumentTypeError(
            "triplet ID must be ONID.TSID.SID with each value from 0 to 65535"
        )
    return triplet_id


def parse_doublet_id(value: str) -> DVBDoubletID:
    """Parse a DVB multiplex doublet ID in ONID.TSID form."""
    try:
        doublet_id = DVBDoubletID.from_dotted(value)
    except ValueError:
        raise argparse.ArgumentTypeError(
            "doublet ID must be ONID.TSID with each value from 0 to 65535"
        ) from None
    if any(not 0 <= identifier <= 65535 for identifier in doublet_id):
        raise argparse.ArgumentTypeError(
            "doublet ID must be ONID.TSID with each value from 0 to 65535"
        )
    return doublet_id


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


def parse_bool(value: str) -> bool:
    """Parse a case-insensitive boolean command-line value."""
    normalized = value.lower()
    if normalized == "true":
        return True
    if normalized == "false":
        return False
    raise argparse.ArgumentTypeError("value must be true or false")


def capture_stream(
    adapter,
    output_path: Path,
    duration: float,
    read_timeout: float = READ_TIMEOUT_SECONDS,
) -> tuple[int, float]:
    """Capture transport-stream data to disk and return bytes and elapsed seconds."""
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError("duration must be a positive finite number of seconds")
    if not math.isfinite(read_timeout) or read_timeout < 0:
        raise ValueError("read_timeout must be non-negative")

    bytes_written = 0
    start_time = monotonic()
    try:
        logger.info("Starting capture to %s for %g seconds", output_path, duration)
        adapter.start()
        start_time = monotonic()
        deadline = start_time + duration
        try:
            with output_path.open("wb") as output:
                while True:
                    remaining = deadline - monotonic()
                    if remaining <= 0:
                        break
                    timeout_ms = math.ceil(min(read_timeout, remaining) * 1000)
                    try:
                        chunk = adapter.read_stream(timeout_ms)
                    except TimeoutError as error:
                        if monotonic() >= deadline:
                            break
                        raise IOError("transport stream read returned no data") from error
                    output.write(chunk)
                    bytes_written += len(chunk)
        except KeyboardInterrupt:
            logger.warning("Capture interrupted by user; keeping partial recording")
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
        logger.info("Frontend status: %s", status)
        if status.lock or monotonic() >= deadline:
            return status
        sleep(1.0)


def log_tuning_hardware_diagnostics(device, adapter_index: int) -> None:
    """Log physical tuner and demodulator registers after a nonblocking tune."""
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
        logger.warning("Hardware register readback failed: %s", error)
        return

    register_dump = " ".join(
        f"{register:02x}:{value:02x}" for register, value in enumerate(tuner_registers)
    )
    logger.info("TDA18271 cached registers 08-0f: %s", cached_registers[8:16].hex(" "))
    frequency_registers = (3, 4, 5, 6, 7, 28, 29, 37, 12, 13, 14, 15)
    logger.info("TDA18271 cached frequency registers: %s", " ".join(
        f"{register:02x}:{cached_registers[register]:02x}" for register in frequency_registers
    ))
    mpd = tuner_registers[tuner.REG_MPD]
    logger.info("TDA18271 physical registers: %s", register_dump)
    logger.info("TDA18271 physical MPD (0x%02x): 0x%02x; digital IF notch=%s",
                tuner.REG_MPD, mpd, "on" if mpd & 0x80 else "off")
    logger.info("TDA18271 direct MPD read (0x%02x): 0x%02x", tuner.REG_MPD, direct_mpd)
    logger.info("AF9013 physical registers: %s", " ".join(
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
    logger.error("Scan failed with %s: %s; collecting diagnostics.", type(error).__name__, error)
    adapter = device.adapters[adapter_index]
    try:
        logger.info("Frontend status at scan failure: %s", adapter.read_status())
    except Exception as diagnostic_error:
        logger.warning("Frontend status read failed: %s", diagnostic_error)

    if usb.RECORD_STATS:
        try:
            device.log_transport_stats()
        except Exception as diagnostic_error:
            logger.warning("USB transfer statistics failed: %s", diagnostic_error)
    else:
        logger.info("USB transfer statistics are disabled; rerun with --usb-stats to collect them.")


def _run_device(device: DVBDevice, args) -> int:
    # 'device' command stops at device discovery
    if args.command == "discover":
        print(f"Device state: {'warm' if device.is_warm() else 'cold'}")
        return 0

    if not device.is_warm():
        logger.info("Device is cold; initializing device")
        device.init_hardware()
    else:
        logger.info("Device is warm; reinitializing hardware")
        device.init_hardware()
    logger.info("Hardware initialization complete")

    if args.command == "init":
        return 0

    if args.command == "status":
        _print_status(device)
        return 0

    if args.command == "sleep":
        logger.info("Putting device to sleep")
        device.sleep()
        logger.info("Device is asleep")
        return 0

    tunings: list[DVBTuning] = []
    if args.command == "tune" or args.load_services is None:
        if args.use_table is not None:
            logger.info("Parsing scan table from %s", args.use_table)
            scan_result = parse_table(args.use_table, prefer_auto=True)
            tunings = scan_result.tunings
            logger.info(
                "Parsed %d tunings (%d skipped, %d failed)",
                len(tunings),
                scan_result.skipped_blocks,
                scan_result.failed_blocks,
            )
            if not tunings:
                raise ValueError(f"scan table contains no DVB-T channels: {args.use_table}")
        else:
            tunings = [DVBTuning(frequency_hz=args.tuning[0], bandwidth_hz=args.tuning[1])]

    tuning_status = None
    if args.command == "tune":
        locks_acquired = []
        for index, tuning in enumerate(tunings):
            logger.info(
                "Tuning %d of %d: Frequency %s MHz with bandwidth %s MHz",
                index + 1,
                len(tunings),
                tuning.frequency_hz / 1_000_000,
                tuning.bandwidth_hz / 1_000_000,
            )
            status = device.tune(tuning, adapter_index=args.adapter_index,
                                 wait_for_lock=not args.hardware_diagnostics)
            if args.hardware_diagnostics:
                log_tuning_hardware_diagnostics(device, args.adapter_index)
                if hasattr(device, "read_status"):
                    status = wait_for_lock_with_diagnostics(device, args.adapter_index)
            print(f"Lock result: {'LOCKED' if status and status.lock else 'NO LOCK'}")
            locks_acquired.append(status and status.lock)
        return 0 if all(locks_acquired) else 2

    if args.load_services is not None:
        channel_map = cast(DVBServiceMap, serialize.load(args.load_services))
    else:
        # Perform a scan to create a channel map
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
        # Print the results of the channel scan
        for service in channel_map.services_sorted():
            network_name = None if service is None or service.triplet_id is None else \
                            channel_map.stream_infos[service.triplet_id].network_name
            service_type = DVBServiceType(service.service_type).name \
                            if service.service_type in DVBServiceType else service.service_type
            print(f"Ch {service.logical_channel_number:3} {service.name:25} {service.service_metatype:5} " +
                    f"ID={service.triplet_id:17} {network_name:25} " + f"Type: {service_type}")
        device.log_transport_stats()
        return 0

    if args.record is not None:
        service = None
        if args.lcn is not None:
            triplet_id = channel_map.triplet_ids.get(args.lcn)
            if triplet_id is None:
                available_lcns = [lcn for lcn in channel_map.lcns if lcn is not None]
                print(f"Capture failed: LCN {args.lcn} was not found. "
                      f"Available LCNs: {available_lcns}")
                return 2
            service = channel_map.services[triplet_id]
            tuning = channel_map.tunings[triplet_id]
        elif args.triplet_id is not None:
            triplet_id = args.triplet_id
            service = channel_map.services.get(triplet_id)
            if service is None:
                available_triplet_ids = sorted(channel_map.services)
                print(f"Capture failed: triplet ID {triplet_id} was not found. "
                      f"Available triplet IDs: {available_triplet_ids}")
                return 2
            tuning = channel_map.tunings[triplet_id]
        elif args.doublet_id is not None:
            tuning = channel_map.tunings_by_doublet.get(args.doublet_id)
            if tuning is None:
                available_doublet_ids = sorted(channel_map.tunings_by_doublet)
                print(f"Capture failed: doublet ID {args.doublet_id} was not found. "
                      f"Available doublet IDs: {available_doublet_ids}")
                return 2
        else:
            # Should never get to here, but as a fallback, use the first available tuning.
            tuning = list(channel_map.tunings.values())[0]

        if service is not None:
                logger.info("Recording service %s (%s, LCN %s)", service.name,
                    service.triplet_id, service.logical_channel_number)
        elif args.doublet_id is not None:
            stream_info = channel_map.stream_infos_by_doublet.get(args.doublet_id)
            if stream_info is not None:
                logger.info("Recording multiplex %s (%s)", stream_info.network_name, args.doublet_id)
        
            logger.info("Tuning to frequency %s MHz with bandwidth %s MHz",
                    tuning.frequency_hz / 1_000_000, tuning.bandwidth_hz / 1_000_000)
        tuning_status = device.tune(tuning, adapter_index=args.adapter_index, wait_for_lock=True)
        print(f"Lock result: {'LOCKED' if tuning_status and tuning_status.lock else 'NO LOCK'}")

        if tuning_status is None or not tuning_status.lock:
            print("Capture failed: frontend did not lock; no transport stream is available")
            return 2
        logger.info("Frontend lock acquired. Starting capture to %s", args.record)
        adapter = device.adapters[args.adapter_index]
        if service is not None:
            try:
                adapter.configure_pid_filter(service.pids())
            except (OSError, RuntimeError, ValueError) as error:
                try:
                    adapter.set_pid_filter_enabled(False)
                except (OSError, RuntimeError, ValueError) as cleanup_error:
                    print(f"Capture cleanup failed: {cleanup_error}")
                print(f"Capture failed: could not configure service PID filter: {error}")
                return 1
        try:
            bytes_written, elapsed = capture_stream(adapter, args.record, args.duration)
        except (OSError, RuntimeError, ValueError) as error:
            print(f"Capture failed: {error}")
            return 1
        device.log_transport_stats()
        print(f"Captured {bytes_written} bytes in {elapsed:.2f} seconds to {args.record}")
        return 0

    if args.command == "serve" and args.serve is not None:
        host, port = args.serve
        server = LocalTS_HTTPServer(
            device.adapters,
            channel_map,
            host=host,
            port=port,
            scan_dvb_events=args.event_decode,
        )
        bound_host, bound_port = server.address
        logger.info(
            "Serving playlist at http://%s:%s and http://%s:%s/playlist.m3u8",
            bound_host,
            bound_port,
            bound_host,
            bound_port,
        )
        try:
            server.start()
            server.serve_forever()
        except (OSError, RuntimeError, ValueError) as error:
            print(f"Server failed: {error}")
            return 1
        except KeyboardInterrupt:
            logger.info("Stopping transport-stream server")
        finally:
            server.close()
            device.log_transport_stats()
    return 0


def main() -> int:
    adapter_options = argparse.ArgumentParser(add_help=False)
    adapter_options.add_argument(
        "--adapter-index",
        dest="adapter_index",
        type=int,
        default=0,
        help="Adapter index to tune (default: 0)",
    )

    needs_tuning = argparse.ArgumentParser(add_help=False)
    tuning_group = needs_tuning.add_argument_group(
        title="Tuning options",
        description="Specify either a frequency/bandwidth or a known scan table",
    )
    tuning_source = tuning_group.add_mutually_exclusive_group()
    tuning_source.add_argument(
        "--tuning",
        type=parse_tuning,
        default=None,
        metavar="FREQUENCY_MHZ/BANDWIDTH_MHZ",
        help="DVB-T centre frequency and channel bandwidth in MHz",
    )
    tuning_source.add_argument(
        "--use-table",
        type=str,
        metavar="scan-table-name-or-path",
        help="Use a known or local DVB-T scan table",
    )

    can_save_services = argparse.ArgumentParser(add_help=False)
    can_save_services.add_argument(
        "--save-services",
        type=Path,
        metavar="path-to-save-services-json",
        help="Path to save the scanned DVB-T services",
    )

    can_load_services = argparse.ArgumentParser(add_help=False)
    can_load_services.add_argument(
        "--load-services",
        type=Path,
        metavar="path-to-load-services-json",
        help="Path to load previously saved DVB-T services",
    )
 
    usb_options = argparse.ArgumentParser(add_help=False)
    usb_options.add_argument(
        "--usb-log",
        action="store_true",
        help="Log USB bulk transfers",
    )
    usb_options.add_argument(
        "--usb-stats",
        action="store_true",
        help="Log stream USB read timing and returned byte counts",
    )

    main_parser = argparse.ArgumentParser(
        description="Control a DVB-T receiver",
   )
    subparsers = main_parser.add_subparsers(
        dest="command",
        title="commands",
        help="Available commands",
        required=True,
    )

    subparsers.add_parser(
        "discover",
        help="Discover if a known DVB-T device is connected, without initializing it.",
        parents=[usb_options],
    )
    subparsers.add_parser(
        "init",
        help="Initialize the device without tuning",
        parents=[adapter_options, usb_options],
    )
    subparsers.add_parser(
        "status",
        help="Initialize the device, print status information, and exit",
        parents=[adapter_options, usb_options],
    )
    subparsers.add_parser(
        "sleep",
        help="Initialize the device, put it into its low-power state, and exit",
        parents=[adapter_options, usb_options],
    )
    subparsers.add_parser(
        "known-tables",
        help="Print known LinuxTV DVB-T scan-table names (from online repository)",
        parents=[],
    )
    parse_scan_parser = subparsers.add_parser(
        "parse-table",
        help="Parse a known DVB scan-table name or local scan-table file",
        parents=[],
    )
    parse_scan_parser.add_argument("scan", metavar="scan-table-name-or-path")
    tune_parser = subparsers.add_parser(
        "tune",
        help="Initialize the device, tune it, and exit with the lock result",
        parents=[needs_tuning, adapter_options, usb_options],
    )
    tune_parser.add_argument(
        "--hardware-diagnostics",
        action="store_true",
        help="Print tuner and demodulator register diagnostics after tuning",
    )
    subparsers.add_parser(
        "scan",
        help="Tune and scan live MPEG-TS stream for its service metadata (ie. channels)",
        parents=[needs_tuning, can_load_services, can_save_services, adapter_options, usb_options],
    )
    record_parser = subparsers.add_parser(
        "record",
        help="Record a live MPEG-TS stream to a file",
        parents=[needs_tuning, can_load_services, can_save_services, adapter_options, usb_options],
    )
    record_parser.add_argument("record", type=Path, metavar="OUTPUT_PATH")
    record_parser.add_argument(
        "--duration",
        type=parse_duration,
        default=DEFAULT_CAPTURE_DURATION_SECONDS,
        metavar="SECONDS",
        help="Recording duration in seconds (default: 15)",
    )
    service_selector = record_parser.add_mutually_exclusive_group(required=True)
    service_selector.add_argument(
        "--lcn",
        type=parse_lcn,
        metavar="LCN",
        help="Record only the DVB service with this logical channel number",
    )
    service_selector.add_argument(
        "--triplet-id",
        type=parse_triplet_id,
        metavar="ONID.TSID.SID",
        help="Record only the DVB service with this triplet ID",
    )
    service_selector.add_argument(
        "--doublet-id",
        type=parse_doublet_id,
        metavar="ONID.TSID",
        help="Record the full DVB multiplex with this doublet ID",
    )
    serve_parser = subparsers.add_parser(
        "serve",
        help="Serve live MPEG-TS streams over HTTP",
        parents=[needs_tuning, can_load_services, can_save_services, adapter_options, usb_options],
    )
    serve_parser.add_argument(
        "serve",
        nargs="?",
        type=parse_server_address,
        metavar="HOST:PORT",
        default=DEFAULT_SERVER_ADDRESS,
    )
    serve_parser.add_argument(
        "--event-decode",
        type=parse_bool,
        default=True,
        metavar="true|false",
        help="Decode current and next DVB events from the stream (default: true)",
    )

    args = main_parser.parse_args()
    args.record = getattr(args, "record", None)
    args.serve = getattr(args, "serve", None)
    args.tuning = getattr(args, "tuning", None)
    args.load_services = getattr(args, "load_services", None)

    tuning_commands = {"tune", "scan", "record", "serve"}
    if args.command in tuning_commands:
        if args.load_services is not None and (
            args.use_table is not None or args.tuning is not None
        ):
            main_parser.error("--load-services cannot be combined with --tuning or --use-table")
        if (
            args.use_table is None
            and args.tuning is None
            and args.load_services is None
        ):
            main_parser.error(
                "--tuning or --use-table is required unless --load-services is provided"
            )

    if args.command == "known-tables":
        for table_name in known_table_names():
            print(table_name)
        return 0

    if args.command == "parse-table":
        pprint.pprint(parse_table(args.scan), underscore_numbers=True)
        return 0

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    logging.getLogger("pydvb.usb").setLevel(
        logging.DEBUG if args.usb_log else logging.NOTSET
    )

    usb.RECORD_STATS = args.usb_stats

    with DVBDeviceManager() as manager:
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