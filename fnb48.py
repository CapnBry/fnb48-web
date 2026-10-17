#!/usr/bin/env python3
# /// script
# requires-python = ">=3.9"
# dependencies = ["bleak"]
# ///
"""FNIRSI FNB48 USB meter CLI for Linux/macOS/Windows using bleak.

Every subcommand is non-interactive and prints one JSON document on stdout, so it
can be driven by scripts and LLM agents. See docs/LLM.md for the full reference.

    pip install bleak                 # or: uv run fnb48.py ...
    ./fnb48.py scan                   # list meters in range
    ./fnb48.py info                   # device info, live reading, energy groups
    ./fnb48.py measure -d 30 -o log.csv
    ./fnb48.py summarize log.csv --from 10 --to 20
    ./fnb48.py > log.csv              # legacy: stream CSV until Ctrl-C
"""
import argparse
import asyncio
import csv
import json
import math
import os
import signal
import statistics
import struct
import sys
import time

NOTIFY = "0000ffe4-0000-1000-8000-00805f9b34fb"
WRITE = "0000ffe9-0000-1000-8000-00805f9b34fb"

EXIT_OK, EXIT_ERROR, EXIT_USAGE, EXIT_NOT_FOUND, EXIT_CONNECTION, EXIT_START_TIMEOUT, EXIT_NAK = 0, 1, 2, 3, 4, 5, 6

MODELS = {48: "FNB48", 38: "FNB38", 9: "C1"}
DETECTED_PROTOCOLS = [
    "Unknown", "DCP 1.5A", "QC2.0 5V", "QC2.0 9V", "QC2.0 12V", "QC2.0 20V", "QC3.0", "Apple 2.1A",
    "Apple 2.4A", "Samsung 2.0A", "USB2.0 Full", "USB2.0 High", "FCP/AFC 9V", "FCP/AFC 12V", "Huawei SCP", "PD MTK",
]
TRIGGER_STATES = ["None", "QC2", "QC3", "FCP", "SCP", "AFC", "PD", "PD1", "VOOC", "SVOOC", "SVOOC"]
TRIGGER_MODES = {  # name: (mode byte, allowed volts)
    "qc2": (0, [5, 9, 12, 20]),
    "qc3": (1, [round(3 + 0.2 * i, 1) for i in range(86)]),
    "fcp": (2, [5, 9, 12]),
    "afc": (3, [5, 9, 12]),
}

QUIET = False


class Fail(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def log(msg):
    if not QUIET:
        print(f"# {msg}", file=sys.stderr, flush=True)


def clean(o):
    """Make an object strict-JSON safe (NaN/inf -> null) and round floats."""
    if isinstance(o, float):
        return None if math.isnan(o) or math.isinf(o) else round(o, 7)
    if isinstance(o, dict):
        return {k: clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [clean(v) for v in o]
    return o


def emit(obj, stream=None):
    print(json.dumps(clean(obj), indent=2), file=stream or sys.stdout, flush=True)


# ---------------------------------------------------------------------------
# Protocol

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


def decode(cmd, p):
    """Decode a meter -> host payload into a dict, or None if unknown/short."""
    try:
        if cmd in (0x01, 0x02):
            return {"ack": cmd == 0x01, "cmd": p[0]}
        if cmd == 0x03:
            model, fw, sn, run, groups, group = struct.unpack_from("<HHIIBB", p)
            return {"model": MODELS.get(model, str(model)), "model_id": model, "firmware": f"{fw / 100:.2f}",
                    "serial": f"{sn:06d}", "run": run, "group_count": groups, "current_group": group}
        if cmd == 0x04:
            v, a, w = struct.unpack_from("<iii", p)
            return {"V": v / 1e4, "A": a / 1e4, "W": w / 1e4}
        if cmd == 0x05:
            r, sign, t = struct.unpack_from("<IBH", p)
            return {"resistance_ohm": None if r == 99999999 else r / 1e4, "temp_C": (1 if sign > 0 else -1) * t / 10}
        if cmd == 0x06:
            dp, dm, p1, p2 = struct.unpack_from("<HHBB", p)
            name = lambda i: DETECTED_PROTOCOLS[i] if i < len(DETECTED_PROTOCOLS) else f"#{i}"
            return {"dplus_V": dp / 1e3, "dminus_V": dm / 1e3, "protocols": [name(p1), name(p2)]}
        if cmd == 0x07:
            v, a = struct.unpack_from("<Hh", p)
            return {"V": v / 1e3, "A": a / 1e3, "W": v * a / 1e6}
        if cmd == 0x08:
            g, wh, ah, rec, up = struct.unpack_from("<BIIII", p)
            return {"group": g, "Wh": wh / 1e5, "Ah": ah / 1e5, "record_s": rec, "uptime_s": up}
        if cmd == 0x09:
            s = p[0]
            return {"state_id": s, "state": TRIGGER_STATES[s] if s < len(TRIGGER_STATES) else f"#{s}"}
        if cmd == 0x0A:
            g, wh, ah = struct.unpack_from("<BII", p)
            return {"group": g, "Wh": wh / 1e5, "Ah": ah / 1e5}
    except (struct.error, IndexError):
        pass
    return None


# ---------------------------------------------------------------------------
# BLE session

def _bleak():
    try:
        import bleak
    except ImportError:
        raise Fail(EXIT_ERROR, "bleak is not installed: run 'pip install bleak' or use 'uv run fnb48.py'")
    return bleak


def _is_meter(name):
    return (name or "").startswith(("FNB", "FNIRSI"))


async def find_meter(args):
    """Return a BLEDevice or address string for the meter to use."""
    addr = getattr(args, "address", None) or os.environ.get("FNB48_ADDRESS")
    if addr:
        return addr
    bleak = _bleak()
    timeout = getattr(args, "scan_timeout", 15)
    log(f"scanning up to {timeout:g} s for an FNB48")
    dev = await bleak.BleakScanner.find_device_by_filter(lambda d, adv: _is_meter(adv.local_name or d.name),
                                                         timeout=timeout)
    if not dev:
        raise Fail(EXIT_NOT_FOUND, "no FNB48 found: off, out of range, or already connected "
                                   "(another fnb48.py or the web app still running?)")
    return dev


class Meter:
    def __init__(self, target, on_frame=None):
        self.target = target  # BLEDevice for the first connect, address afterwards
        self.address = getattr(target, "address", target)
        self.client = None
        self.info = None
        self.last_rx = 0.0
        self.listeners = [on_frame] if on_frame else []
        self.disconnected = asyncio.Event()

    async def connect(self, timeout=20):
        bleak = _bleak()
        dev = self.target
        if isinstance(dev, str):
            log(f"looking for {dev}")
            dev = await bleak.BleakScanner.find_device_by_address(dev, timeout=timeout)
            if dev is None:
                raise Fail(EXIT_NOT_FOUND, f"meter {self.address} not found: out of range, off, or already "
                                           "connected (another fnb48.py or the web app still running?)")
        self.target = self.address
        log(f"connecting to {self.address}")
        parser = Parser()

        def on_notify(_, data):
            now = self.last_rx = time.time()
            for cmd, p in parser.feed(data):
                m = decode(cmd, p)
                if m is not None:
                    for fn in list(self.listeners):
                        fn(now, cmd, m)

        self.disconnected.clear()
        self.client = bleak.BleakClient(dev, timeout=timeout, disconnected_callback=lambda _: self.disconnected.set())
        try:
            await self.client.connect()
            await self.client.start_notify(NOTIFY, on_notify)
        except Fail:
            raise
        except Exception as e:
            raise Fail(EXIT_CONNECTION, f"connect to {self.address} failed: {e or type(e).__name__}")

    async def send(self, cmd, payload=b""):
        await self.client.write_gatt_char(WRITE, frame(cmd, payload), response=True)

    async def request(self, cmd, payload=b"", want=(), count=1, timeout=3.0):
        """Send a command and collect up to `count` replies whose command byte is in `want`."""
        got, done = [], asyncio.Event()

        def fn(_t, c, m):
            if c in want:
                got.append(m)
                if len(got) >= count:
                    done.set()

        self.listeners.append(fn)
        try:
            await self.send(cmd, payload)
            if want:
                try:
                    await asyncio.wait_for(done.wait(), timeout)
                except asyncio.TimeoutError:
                    pass
        finally:
            self.listeners.remove(fn)
        return got

    async def open(self, timeout=20):
        await self.connect(timeout)
        for _ in range(3):  # the meter occasionally ignores the first command right after a reconnect
            got = await self.request(0x81, want=(0x03,), timeout=2)
            if got:
                break
        if got:
            self.info = got[0]
            i = self.info
            log(f"{i['model']} fw={i['firmware']} sn={i['serial']} group={i['current_group']}/{i['group_count']}")
        return self.info

    async def groups(self):
        n = (self.info or {}).get("group_count") or 10
        got = await self.request(0x8A, want=(0x0A,), count=n, timeout=3)
        return sorted(got, key=lambda g: g["group"])

    async def snapshot(self, seconds=1.5):
        """Stream briefly and return the latest message of each live type."""
        latest = {}
        fn = lambda _t, c, m: latest.__setitem__(c, m)
        self.listeners.append(fn)
        try:
            await self.send(0x82)
            await asyncio.sleep(seconds)
            await self.send(0x84)
        finally:
            self.listeners.remove(fn)
        r = {}
        if 0x04 in latest:
            r.update({"voltage_V": latest[0x04]["V"], "current_A": latest[0x04]["A"], "power_W": latest[0x04]["W"]})
        if 0x05 in latest:
            r.update(latest[0x05])
        if 0x06 in latest:
            r.update(latest[0x06])
        if 0x08 in latest:
            r["active_group"] = latest[0x08]
        return r

    async def close(self):
        if self.client and self.client.is_connected:
            try:
                await self.send(0x84)
            except Exception:
                pass
            try:
                await self.client.disconnect()
            except Exception:
                pass


async def with_meter(args, fn):
    m = Meter(await find_meter(args))
    try:
        await m.open(args.connect_timeout)
        return await fn(m)
    finally:
        await m.close()


# ---------------------------------------------------------------------------
# Analysis

def stats(xs):
    if not xs:
        return None
    s = sorted(xs)
    n = len(s)
    pct = lambda q: s[min(n - 1, int(round(q * (n - 1))))]
    return {"min": s[0], "max": s[-1], "mean": math.fsum(s) / n, "median": pct(0.5),
            "p5": pct(0.05), "p95": pct(0.95), "stdev": statistics.pstdev(s) if n > 1 else 0.0}


def analyze(rows, t0=None, burst_threshold=None, max_bursts=100):
    """rows: list of (unix_time, V, A, W, temp_C). Times in the result are seconds relative to t0."""
    n = len(rows)
    if not n:
        return {"samples": 0}
    t0 = rows[0][0] if t0 is None else t0
    ts = [r[0] for r in rows]
    dts = [b - a for a, b in zip(ts, ts[1:])]
    pos = sorted(d for d in dts if d > 0)
    gap_threshold = max(2.0, 10 * pos[len(pos) // 2]) if pos else 2.0
    energy = charge = covered = 0.0
    gaps = []
    for i, d in enumerate(dts):
        if d > gap_threshold:
            gaps.append({"from_s": ts[i] - t0, "to_s": ts[i + 1] - t0, "length_s": d})
            continue
        energy += (rows[i][3] + rows[i + 1][3]) / 2 * d
        charge += (rows[i][2] + rows[i + 1][2]) / 2 * d
        covered += d
    temps = [r[4] for r in rows if r[4] is not None and not math.isnan(r[4])]
    out = {
        "samples": n,
        "start_unix": ts[0], "end_unix": ts[-1],
        "from_s": ts[0] - t0, "to_s": ts[-1] - t0,
        "duration_s": ts[-1] - ts[0],
        "rate_hz": (n - 1) / covered if covered else None,
        "voltage_V": stats([r[1] for r in rows]),
        "current_A": stats([r[2] for r in rows]),
        "power_W": stats([r[3] for r in rows]),
        "energy_Wh": energy / 3600,
        "charge_mAh": charge / 3.6,
        "avg_power_W": energy / covered if covered else None,
        "avg_current_A": charge / covered if covered else None,
        "temp_C": {"start": temps[0], "end": temps[-1], "max": max(temps)} if temps else None,
        "gaps": gaps,
    }
    if burst_threshold is not None:
        out["bursts"] = bursts(rows, dts, gap_threshold, t0, burst_threshold, max_bursts)
    return out


def bursts(rows, dts, gap_threshold, t0, threshold, max_bursts):
    """Contiguous runs of samples with current >= threshold."""
    runs, cur = [], None
    above_t = below_t = 0.0
    above_a, below_a = [], []
    for i, r in enumerate(rows):
        d = dts[i] if i < len(dts) and dts[i] <= gap_threshold else 0.0
        if r[2] >= threshold:
            above_t += d
            above_a.append(r[2])
            if cur is None:
                cur = {"start_s": r[0] - t0, "duration_s": 0.0, "peak_A": r[2], "_sum": 0.0, "samples": 0}
            cur["duration_s"] += d
            cur["peak_A"] = max(cur["peak_A"], r[2])
            cur["_sum"] += r[2]
            cur["samples"] += 1
        else:
            below_t += d
            below_a.append(r[2])
            if cur:
                runs.append(cur)
                cur = None
    if cur:
        runs.append(cur)
    for b in runs:
        b["mean_A"] = b.pop("_sum") / b["samples"]
    total = above_t + below_t
    starts = [b["start_s"] for b in runs]
    periods = [b - a for a, b in zip(starts, starts[1:])]
    return {
        "threshold_A": threshold,
        "count": len(runs),
        "time_above_s": above_t,
        "fraction_above": above_t / total if total else None,
        "mean_current_above_A": statistics.fmean(above_a) if above_a else None,
        "mean_current_below_A": statistics.fmean(below_a) if below_a else None,
        "longest_s": max((b["duration_s"] for b in runs), default=None),
        "mean_period_s": statistics.fmean(periods) if periods else None,
        "list": runs[:max_bursts],
        "list_truncated": len(runs) > max_bursts,
    }


def load_log(path):
    """Read a CSV or JSONL log written by `measure`. Returns (rows, t0)."""
    rows, t0 = [], None
    with open(path, newline="") as f:
        first = ""
        for first in f:
            if first.strip() and not first.startswith("#"):
                break
        f.seek(0)
        lines = (l for l in f if l.strip() and not l.startswith("#"))
        if first.lstrip().startswith("{"):
            recs = (json.loads(l) for l in lines)
            get = lambda r: (r.get("unix_time", r.get("time_s")), r.get("time_s"), r["voltage_V"], r["current_A"],
                             r["power_W"], r.get("temp_C"))
        else:
            recs = csv.DictReader(lines)
            num = lambda s: float(s) if s not in (None, "", "nan") else float("nan")
            get = lambda r: (num(r.get("unix_time") or r["time_s"]), num(r.get("time_s")), num(r["voltage_V"]),
                             num(r["current_A"]), num(r["power_W"]), num(r.get("temp_C")))
        for rec in recs:
            t, rel, v, a, w, temp = get(rec)
            if t0 is None:
                t0 = t - rel if rel is not None and not math.isnan(rel) else t
            rows.append((t, v, a, w, float("nan") if temp is None else temp))
    return rows, t0


# ---------------------------------------------------------------------------
# Commands

async def cmd_scan(args):
    bleak = _bleak()
    log(f"scanning for {args.scan_timeout:g} s")
    found = await bleak.BleakScanner.discover(timeout=args.scan_timeout, return_adv=True)
    devs = []
    for dev, adv in found.values():
        name = adv.local_name or dev.name
        if args.all or _is_meter(name):
            devs.append({"address": dev.address, "name": name, "rssi": adv.rssi})
    devs.sort(key=lambda d: -(d["rssi"] or -999))
    emit({"ok": True, "devices": devs})
    return EXIT_OK if devs else EXIT_NOT_FOUND


async def cmd_info(args):
    async def run(m):
        r = {"ok": True, "address": m.address, "device": m.info}
        got = await m.request(0x85, want=(0x09,))
        r["trigger"] = got[0] if got else None
        r["groups"] = await m.groups()
        r["reading"] = await m.snapshot()
        return r
    emit(await with_meter(args, run))
    return EXIT_OK


async def cmd_groups(args):
    async def run(m):
        return {"ok": True, "address": m.address, "current_group": (m.info or {}).get("current_group"),
                "groups": await m.groups()}
    emit(await with_meter(args, run))
    return EXIT_OK


async def cmd_group(args):
    if args.action == "clear" and not args.confirm:
        raise Fail(EXIT_USAGE, "clearing a group erases its stored Wh/Ah; pass --confirm to do it")

    async def run(m):
        count = (m.info or {}).get("group_count") or 10
        if not 1 <= args.group <= count:
            raise Fail(EXIT_USAGE, f"group must be 1..{count}")
        if args.action == "select":
            await m.send(0x88, bytes([args.group]))
            await asyncio.sleep(0.3)
            got = await m.request(0x81, want=(0x03,))
            current = got[0]["current_group"] if got else None
            return {"ok": current == args.group, "action": "select", "group": args.group, "current_group": current}
        await m.send(0x89, bytes([args.group]))
        await asyncio.sleep(0.3)
        g = next((g for g in await m.groups() if g["group"] == args.group), None)
        return {"ok": bool(g) and g["Wh"] == 0 and g["Ah"] == 0, "action": "clear", "group": args.group, "after": g}
    r = await with_meter(args, run)
    emit(r)
    return EXIT_OK if r["ok"] else EXIT_ERROR


async def cmd_trigger(args):
    if args.action == "set":
        if args.mode is None or args.volts is None:
            raise Fail(EXIT_USAGE, "usage: trigger set {qc2,qc3,fcp,afc} VOLTS --confirm")
        mode, allowed = TRIGGER_MODES[args.mode]
        if round(args.volts, 1) not in allowed:
            raise Fail(EXIT_USAGE, f"{args.mode} supports {allowed[0]}..{allowed[-1]} V: {allowed}")
        if not args.confirm:
            raise Fail(EXIT_USAGE, "triggering raises VBUS above 5 V and can damage a connected device; "
                                   "pass --confirm to do it")

    async def run(m):
        r = {"ok": True, "action": args.action}
        if args.action in ("set", "off"):
            if args.action == "set":
                mv = int(round(args.volts * 1000))
                got = await m.request(0x86, bytes([TRIGGER_MODES[args.mode][0], mv & 0xFF, mv >> 8]), want=(0x01, 0x02))
                r.update({"mode": args.mode, "volts": args.volts})
            else:
                got = await m.request(0x87, want=(0x01, 0x02))
            r["ack"] = got[0]["ack"] if got else None
            r["ok"] = r["ack"] is not False
            await asyncio.sleep(0.5)
        got = await m.request(0x85, want=(0x09,))
        r["trigger"] = got[0] if got else None
        r["reading"] = await m.snapshot(1.0)
        return r
    r = await with_meter(args, run)
    emit(r)
    return EXIT_OK if r["ok"] else EXIT_NAK


class Condition:
    """Current above/below a threshold, sustained for `hold` seconds."""

    def __init__(self, above, below, hold):
        self.above, self.below, self.hold, self.since = above, below, hold, None

    @classmethod
    def make(cls, above, below, hold):
        return cls(above, below, hold) if above is not None or below is not None else None

    def describe(self):
        op = f"current >= {self.above} A" if self.above is not None else f"current <= {self.below} A"
        return op + (f" for {self.hold:g} s" if self.hold else "")

    def update(self, t, a):
        met = (self.above is None or a >= self.above) and (self.below is None or a <= self.below)
        if not met:
            self.since = None
            return False
        if self.since is None:
            self.since = t
        return t - self.since >= self.hold


def open_out(args):
    if not args.out:
        return None, None
    fmt = args.format or ("jsonl" if args.out.endswith((".jsonl", ".ndjson")) else "csv")
    f = sys.stdout if args.out == "-" else open(args.out, "w", newline="")
    if fmt == "csv":
        f.write("unix_time,time_s,voltage_V,current_A,power_W,temp_C\n")
        f.flush()
    return f, fmt


async def cmd_measure(args):
    sample_cmd = 0x07 if args.fast else 0x04
    start = Condition.make(args.start_above, args.start_below, args.start_hold)
    stop_cond = Condition.make(args.stop_above, args.stop_below, args.stop_hold)
    out, fmt = open_out(args)
    vfmt = "{:.3f}" if args.fast else "{:.4f}"

    rows = []
    ctx = {"temp": float("nan"), "port": None, "protocols": [], "meter_first": None, "meter_last": None}
    state = {"recording": start is None, "t_start": None, "reason": None}
    stop = asyncio.Event()

    def finish(reason):
        if not stop.is_set():
            state["reason"], state["t_end"] = reason, time.time()
            stop.set()

    def on_frame(t, cmd, m):
        if cmd == 0x05:
            ctx["temp"] = m["temp_C"]
        elif cmd == 0x06:
            ctx["port"] = m
            if m["protocols"] not in ctx["protocols"]:
                ctx["protocols"].append(m["protocols"])
        elif cmd == 0x08 and state["recording"]:
            ctx["meter_first"] = ctx["meter_first"] or m
            ctx["meter_last"] = m
        elif cmd == sample_cmd and not stop.is_set():
            if not state["recording"]:
                if not start.update(t, m["A"]):
                    return
                state["recording"] = True
                log(f"start condition met ({start.describe()}) at {m['A']:.4f} A")
            if state["t_start"] is None:
                state["t_start"] = t
            rel = t - state["t_start"]
            rows.append((t, m["V"], m["A"], m["W"], ctx["temp"]))
            if out:
                if fmt == "csv":
                    out.write(f"{t:.3f},{rel:.3f},{vfmt.format(m['V'])},{vfmt.format(m['A'])},{m['W']:.4f},"
                              f"{ctx['temp']:.1f}\n")
                else:
                    out.write(json.dumps(clean({"unix_time": round(t, 3), "time_s": round(rel, 3), "voltage_V": m["V"],
                                                "current_A": m["A"], "power_W": m["W"], "temp_C": ctx["temp"]})) + "\n")
                out.flush()
            if stop_cond and stop_cond.update(t, m["A"]):
                log(f"stop condition met ({stop_cond.describe()})")
                finish("stop-condition")
            elif args.duration and rel >= args.duration:
                finish("duration")

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, finish, "signal")
        except (NotImplementedError, AttributeError, ValueError):
            pass

    async def until_stopped(coro):
        """Run coro, cancelling it if stop is set first. Returns True if it completed."""
        task = asyncio.ensure_future(coro)
        waiter = asyncio.ensure_future(stop.wait())
        await asyncio.wait({task, waiter}, return_when=asyncio.FIRST_COMPLETED)
        waiter.cancel()
        if not task.done():
            task.cancel()
            return False
        task.result()
        return True

    meter = Meter(await find_meter(args), on_frame)
    disconnects = 0
    try:
        await meter.open(args.connect_timeout)
        await meter.send(0x82)
        t_armed = restarted = meter.last_rx = time.time()
        log(f"waiting for {start.describe()}" if start else
            f"recording {'%g s' % args.duration if args.duration else 'until stopped'}"
            f"{' to ' + args.out if args.out and args.out != '-' else ''}")
        while not stop.is_set():
            now = time.time()
            if state["t_start"] and args.duration and now - state["t_start"] >= args.duration:
                finish("duration")
            elif not state["recording"] and args.start_timeout and now - t_armed >= args.start_timeout:
                finish("start-timeout")
            elif meter.disconnected.is_set():
                disconnects += 1
                if args.no_reconnect:
                    finish("disconnected")
                    break
                log("connection lost, reconnecting")
                lost = time.time()
                while not stop.is_set():
                    if args.reconnect_timeout and time.time() - lost > args.reconnect_timeout:
                        finish("disconnected")
                        break
                    try:
                        if await until_stopped(meter.open(args.connect_timeout)):
                            await meter.send(0x82)
                            restarted = meter.last_rx = time.time()
                            log("reconnected")
                            break
                    except Fail as e:
                        log(str(e))
                        await asyncio.sleep(1)
            elif now - meter.last_rx > 15:
                log("no data for 15 s, reconnecting")
                await meter.close()
                meter.disconnected.set()
            elif now - meter.last_rx > 5 and now - restarted > 5:
                log("no data for 5 s, restarting stream")
                restarted = now
                await meter.send(0x82)
            else:
                try:
                    await asyncio.wait_for(stop.wait(), 0.1)
                except asyncio.TimeoutError:
                    pass
    finally:
        await meter.close()
        if out and out is not sys.stdout:
            out.close()

    t0 = state["t_start"]
    r = {"ok": state["reason"] not in ("start-timeout", "disconnected"), "command": "measure",
         "stop_reason": state["reason"], "address": meter.address, "device": meter.info,
         "mode": "fast" if args.fast else "precise",
         "resolution": {"V": 0.001, "A": 0.001} if args.fast else {"V": 0.0001, "A": 0.0001},
         "out": None if args.out == "-" else args.out, "disconnects": disconnects}
    if start:
        r["start_condition"] = {"condition": start.describe(), "met": t0 is not None,
                                "waited_s": (t0 or state.get("t_end") or time.time()) - t_armed}
    r.update(analyze(rows, t0, args.burst_threshold, args.max_bursts))
    if ctx["meter_first"] and ctx["meter_last"] and ctx["meter_first"]["group"] == ctx["meter_last"]["group"]:
        a, b = ctx["meter_first"], ctx["meter_last"]
        r["meter_counter"] = {"group": b["group"], "delta_Wh": b["Wh"] - a["Wh"], "delta_mAh": (b["Ah"] - a["Ah"]) * 1000,
                              "over_s": b["uptime_s"] - a["uptime_s"], "group_total_Wh": b["Wh"],
                              "group_total_Ah": b["Ah"]}
    if ctx["port"]:
        r["port"] = {"dplus_V": ctx["port"]["dplus_V"], "dminus_V": ctx["port"]["dminus_V"],
                     "protocols_seen": ctx["protocols"]}
    emit(r, sys.stderr if args.out == "-" else sys.stdout)
    if state["reason"] == "start-timeout":
        return EXIT_START_TIMEOUT
    return EXIT_OK if r["ok"] else EXIT_CONNECTION


def parse_time(s, t0):
    """Seconds relative to the log start, or an absolute unix time (> 1e9)."""
    if s is None:
        return None
    v = float(s)
    return v if v > 1e9 else t0 + v


async def cmd_summarize(args):
    try:
        rows, t0 = load_log(args.file)
    except (OSError, ValueError, KeyError) as e:
        raise Fail(EXIT_ERROR, f"cannot read {args.file}: {e}")
    if not rows:
        raise Fail(EXIT_ERROR, f"{args.file} has no samples")
    lo, hi = parse_time(args.from_, t0), parse_time(args.to, t0)
    sel = [r for r in rows if (lo is None or r[0] >= lo) and (hi is None or r[0] <= hi)]
    r = {"ok": bool(sel), "command": "summarize", "file": args.file, "log_start_unix": t0,
         "log_samples": len(rows)}
    r.update(analyze(sel, t0, args.burst_threshold, args.max_bursts))
    emit(r)
    return EXIT_OK if sel else EXIT_ERROR


# ---------------------------------------------------------------------------
# CLI

COMMANDS = ("scan", "info", "groups", "group", "trigger", "measure", "summarize")


def build_parser():
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("-a", "--address", default=argparse.SUPPRESS,
                        help="meter BLE address (default: $FNB48_ADDRESS, else the first FNB*/FNIRSI* found)")
    common.add_argument("--scan-timeout", type=float, default=argparse.SUPPRESS, help="seconds to scan (default 15)")
    common.add_argument("--connect-timeout", type=float, default=argparse.SUPPRESS,
                        help="seconds to find+connect (default 20)")
    common.add_argument("-q", "--quiet", action="store_true", default=argparse.SUPPRESS,
                        help="no '# ...' progress lines on stderr")

    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
                                 parents=[common])
    sub = ap.add_subparsers(dest="command", metavar="COMMAND")

    p = sub.add_parser("scan", parents=[common], help="list meters in range")
    p.add_argument("--all", action="store_true", help="list every BLE device, not just meters")

    sub.add_parser("info", parents=[common], help="device info, trigger state, energy groups and a live reading")
    sub.add_parser("groups", parents=[common], help="read every energy group's Wh/Ah")

    p = sub.add_parser("group", parents=[common], help="select or clear an energy group")
    p.add_argument("action", choices=["select", "clear"])
    p.add_argument("group", type=int)
    p.add_argument("--confirm", action="store_true", help="required for clear")

    p = sub.add_parser("trigger", parents=[common], help="fast-charge trigger: state, set, off")
    p.add_argument("action", choices=["state", "set", "off"])
    p.add_argument("mode", nargs="?", choices=sorted(TRIGGER_MODES))
    p.add_argument("volts", nargs="?", type=float)
    p.add_argument("--confirm", action="store_true", help="required for set")

    p = sub.add_parser("measure", parents=[common], help="record V/A/W and print a JSON summary")
    p.add_argument("-d", "--duration", type=float, default=0,
                   help="seconds to record after the start condition (default 0 = until SIGINT/SIGTERM)")
    p.add_argument("-o", "--out", help="write every sample to this file ('-' = stdout; summary then goes to stderr)")
    p.add_argument("--format", choices=["csv", "jsonl"], help="sample file format (default: from extension, else csv)")
    p.add_argument("--fast", action="store_true",
                   help="use the ~8-10 Hz 1 mV/1 mA stream instead of the ~4 Hz 0.1 mV/0.1 mA one")
    p.add_argument("--start-above", type=float, metavar="A", help="wait until current >= A before recording")
    p.add_argument("--start-below", type=float, metavar="A", help="wait until current <= A before recording")
    p.add_argument("--start-hold", type=float, default=0, metavar="S", help="start condition must hold S seconds")
    p.add_argument("--start-timeout", type=float, default=0, metavar="S",
                   help="give up (exit 5) if the start condition isn't met in S seconds")
    p.add_argument("--stop-above", type=float, metavar="A", help="stop once current >= A")
    p.add_argument("--stop-below", type=float, metavar="A", help="stop once current <= A")
    p.add_argument("--stop-hold", type=float, default=0, metavar="S", help="stop condition must hold S seconds")
    p.add_argument("--burst-threshold", type=float, metavar="A", help="report runs of current >= A in the summary")
    p.add_argument("--max-bursts", type=int, default=100, help="max bursts listed in the summary (default 100)")
    p.add_argument("--no-reconnect", action="store_true", help="stop instead of reconnecting if the link drops")
    p.add_argument("--reconnect-timeout", type=float, default=120, metavar="S",
                   help="give up reconnecting after S seconds (default 120, 0 = never)")

    p = sub.add_parser("summarize", parents=[common], help="statistics for a log written by measure")
    p.add_argument("file")
    p.add_argument("--from", dest="from_", metavar="T", help="window start: seconds into the log, or unix time")
    p.add_argument("--to", metavar="T", help="window end: seconds into the log, or unix time")
    p.add_argument("--burst-threshold", type=float, metavar="A", help="report runs of current >= A")
    p.add_argument("--max-bursts", type=int, default=100)
    return ap


def main(argv=None):
    global QUIET
    argv = sys.argv[1:] if argv is None else argv
    if not any(a in COMMANDS for a in argv) and not any(a in ("-h", "--help") for a in argv):
        argv = ["measure", "--out", "-"] + argv  # legacy: ./fnb48.py [-a ADDR] [--fast] > log.csv
    args = build_parser().parse_args(argv)
    for k, v in (("scan_timeout", 15.0), ("connect_timeout", 20.0), ("quiet", False)):
        if not hasattr(args, k):
            setattr(args, k, v)
    QUIET = args.quiet
    fn = globals()["cmd_" + args.command]
    try:
        return asyncio.run(fn(args))
    except Fail as e:
        emit({"ok": False, "error": str(e), "exit_code": e.code})
        return e.code
    except KeyboardInterrupt:
        return 130
    except Exception as e:
        msg = f"{type(e).__name__}: {e}"
        emit({"ok": False, "error": msg, "exit_code": EXIT_CONNECTION})
        return EXIT_CONNECTION


if __name__ == "__main__":
    sys.exit(main())
