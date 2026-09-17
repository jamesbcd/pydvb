"""Test command-line tool for the Leadtek AF9015 device."""

import argparse
import pprint

import usb.core

from .device import AF9015Device


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
    args = parser.parse_args()

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
        device.initialize()
        print(f"Device state: {'warm' if device.is_warm else 'cold'}")
    else:
        device.initialize()

    print(f"Adapter count: {device.adapter_count}")
    for index, adapter in enumerate(device.adapters):
        print(f"Adapter index {index}:")
        pprint.pprint({
            "demod_address": adapter.demodulator_address,
            "clock_hz": adapter.clock_hz,
            "intermediate_frequency_hz": adapter.intermediate_frequency_hz,
            "mt2060_if": adapter.mt2060_if,
            "tuner_id": adapter.tuner_id,
            "spectrum_inverted": adapter.spectrum_inverted,
        })
        print()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())