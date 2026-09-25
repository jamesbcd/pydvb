"""Test command-line tool for the Leadtek AF9015 device."""

import argparse
import pprint
from pathlib import Path
from time import monotonic

from .dvb import DVBScanResult, DVBScanner
from mactv.driver_api.tuner import DVBTuning

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
            read_timeout_ms=max(0, int(read_timeout * 1000)),
        )
    finally:
        adapter.stop()
        if hasattr(adapter, "sleep"):
            adapter.sleep()


def _run_device(device, args) -> int:
    print(f"Device state: {'warm' if device.is_warm else 'cold'}")
    if not device.is_warm:
        if args.safe:
            return 0
        print("Device is cold; initializing and downloading firmware to device...")
        device.init_hardware()
        print(f"Device state: {'warm' if device.is_warm else 'cold'}")
    else:
        print("Device is warm; reinitializing hardware...")
        device.init_hardware()

    tuning_status = None
    if args.sleep:
        print("Putting device to sleep...")
        device.sleep()
        print("Device is sleeping")
    else:
        # Brisbane DVB-T transmitters use 7 MHz channels.
        tuning = DVBTuning(
            frequency_hz=args.frequency,
            bandwidth_hz=args.bandwidth * 1_000_000,
        )
        print(f"Tuning to frequency {args.frequency} Hz with bandwidth {args.bandwidth} MHz")
        tuning_status = device.tune(tuning, adapter_index=args.adapter_index, wait_for_lock=True)

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
        pprint.pprint(result, underscore_numbers=True)

    if args.serve is not None:
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

    if args.status:
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

    if args.sleep or args.record is not None or args.serve is not None or args.scan_stream:
        return 0
    status = device.adapters[args.adapter_index].read_status()
    print(f"Lock result: {'LOCKED' if status.lock else 'NO LOCK'}")
    return 0 if status.lock else 2


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Test an AF9015 device"
    )
    parser.add_argument("--vendor-id", type=lambda value: int(value, 0), default=0x0413)
    parser.add_argument("--product-id", type=lambda value: int(value, 0), default=0x6029)
    parser.add_argument(
        "--safe",
        action="store_true",
        help="Only check device state; do not initialize or upload firmware",
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="Print status information about the device",
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
        "--sleep",
        action="store_true",
        help="Put the initialized device into its low-power state instead of tuning",
    )
    parser.add_argument(
        "--record",
        type=Path,
        help="Capture up to 5 MiB of transport-stream data to this file",
    )
    parser.add_argument(
        "--serve",
        nargs="?",
        type=parse_server_address,
        metavar="HOST:PORT",
        const=DEFAULT_SERVER_ADDRESS,
        help="Serve at http://HOST:PORT/stream.ts (default: http://127.0.0.1:8080/stream.ts)",
    )
    parser.add_argument(
        "--scan-stream",
        action="store_true",
        help="Scan a live transport stream until DVB service tables are complete",
    )
    parser.add_argument(
        "--frequency",
        type=parse_frequency,
        default=177_500_000,
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
    args = parser.parse_args()
    stream_actions = [
        name for name, enabled in (
            ("--record", args.record is not None),
            ("--serve", args.serve is not None),
            ("--scan-stream", args.scan_stream),
        ) if enabled
    ]
    if len(stream_actions) > 1:
        parser.error(f"{', '.join(stream_actions)} cannot be used together")
    if args.sleep and stream_actions:
        parser.error(f"--sleep cannot be used with {stream_actions[0]}")
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