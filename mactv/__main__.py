"""Test command-line tool for the Leadtek AF9015 device."""

import argparse
import pprint

import usb.core

from mactv.tuner import Tuning

from .device import AF9015Device
from . import usb as usb_logging
from .usb import USBSpeed


def parse_frequency(value: str) -> int:
    """Parse a frequency in MHz (for example, ``177.5``)."""
    frequency_mhz = float(value)
    if frequency_mhz <= 0:
        raise argparse.ArgumentTypeError("frequency must be positive")
    return round(frequency_mhz * 1_000_000)


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
        "--sleep",
        action="store_true",
        help="Put the initialized device into its low-power state instead of tuning",
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
    usb_logging.LOG = args.usb_log

    try:
        device = AF9015Device.open(args.vendor_id, args.product_id)
    except (ValueError, usb.core.USBError) as error:
        print(f"Error: {error}")
        return 1

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

    if args.sleep:
        print("Putting device to sleep...")
        device.sleep()
        print("Device is sleeping")
    else:
        # Brisbane DVB-T transmitters use 7 MHz channels.
        tuning = Tuning(
            frequency_hz=args.frequency,
            bandwidth_hz=args.bandwidth * 1_000_000,
        )
        print(f"Tuning to frequency {args.frequency} Hz with bandwidth {args.bandwidth} MHz")
        device.tune(tuning, adapter_index=args.adapter_index, wait_for_lock=True)

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

    if args.sleep:
        return 0
    status = device.adapters[args.adapter_index].read_status()
    print(f"Lock result: {'LOCKED' if status.lock else 'NO LOCK'}")
    return 0 if status.lock else 2


if __name__ == "__main__":
    raise SystemExit(main())