#!/usr/bin/env python3
"""Minimal FNIRSI FNB48 logger for Linux/macOS/Windows using bleak.

    pip install bleak
    ./fnb48.py                 # scan for an FNB48 and print CSV readings
    ./fnb48.py -a BA:03:C8:07:70:4C --fast > log.csv
"""
import argparse
import asyncio
import struct
import sys
import time

from bleak import BleakClient, BleakScanner

NOTIFY = "0000ffe4-0000-1000-8000-00805f9b34fb"
WRITE = "0000ffe9-0000-1000-8000-00805f9b34fb"


def crc16_xmodem(data):
    crc = 0
    for b in data:
        crc ^= b << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc


def frame(cmd, payload=b""):
    f = bytes([0xAA, cmd, len(payload)]) + bytes(payload)
    return f + bytes([crc16_xmodem(f) & 0xFF])


class Parser:
    def __init__(self):
        self.buf = bytearray()

    def feed(self, data):
        self.buf += data
        while True:
            i = self.buf.find(0xAA)
            if i < 0:
                self.buf.clear()
                return
            del self.buf[:i]
            if len(self.buf) < 3 or len(self.buf) < self.buf[2] + 4:
                return
            n = self.buf[2] + 4
            f = bytes(self.buf[:n])
            if crc16_xmodem(f[:-1]) & 0xFF != f[-1]:
                del self.buf[:1]
                continue
            del self.buf[:n]
            yield f[1], f[3:-1]


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-a", "--address", help="BLE address (default: first FNB*/FNIRSI* device found)")
    ap.add_argument("--fast", action="store_true", help="log fast 1 mV/1 mA samples (0x07) instead of 0.1 mV/0.1 mA readings (0x04)")
    args = ap.parse_args()

    address = args.address
    if not address:
        dev = await BleakScanner.find_device_by_filter(
            lambda d, adv: (adv.local_name or "").startswith(("FNB", "FNIRSI")), timeout=15)
        if not dev:
            sys.exit("No FNB48 found")
        address = dev.address
    print(f"# connecting to {address}", file=sys.stderr)

    parser = Parser()
    t0 = time.time()
    print("time_s,voltage_V,current_A,power_W,temp_C")
    temp = float("nan")

    def on_notify(_, data):
        nonlocal temp
        for cmd, p in parser.feed(data):
            t = time.time() - t0
            if cmd == 0x03:
                model, fw, sn, runs, groups, group = struct.unpack("<HHIIBB", p[:14])
                print(f"# model={model} fw={fw / 100:.2f} sn={sn:06d} power_ons={runs} group={group}/{groups}", file=sys.stderr)
            elif cmd == 0x05:
                temp = (1 if p[4] > 0 else -1) * struct.unpack("<H", p[5:7])[0] / 10
            elif cmd == 0x04 and not args.fast:
                v, a, w = struct.unpack("<iii", p[:12])
                print(f"{t:.3f},{v / 1e4:.4f},{a / 1e4:.4f},{w / 1e4:.4f},{temp:.1f}", flush=True)
            elif cmd == 0x07 and args.fast:
                v, a = struct.unpack("<Hh", p[:4])
                print(f"{t:.3f},{v / 1e3:.3f},{a / 1e3:.3f},{v * a / 1e6:.4f},{temp:.1f}", flush=True)

    async with BleakClient(address, timeout=20) as client:
        await client.start_notify(NOTIFY, on_notify)
        await client.write_gatt_char(WRITE, frame(0x81), response=True)
        await client.write_gatt_char(WRITE, frame(0x82), response=True)
        try:
            while client.is_connected:
                await asyncio.sleep(1)
        finally:
            if client.is_connected:
                await client.write_gatt_char(WRITE, frame(0x84), response=True)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
