# Driving the FNB48 from an LLM agent

This document is for LLM agents (and scripts) that use `fnb48.py` to measure the power draw of a device plugged
into a FNIRSI FNB48 USB meter. Everything here is non-interactive: each command connects over Bluetooth LE, does
one job, disconnects, and prints one JSON document.

The wire protocol is documented separately in [PROTOCOL.md](PROTOCOL.md). You don't need it to use the script.

## Setup

* Run it as `uv run fnb48.py ...` (the script declares its `bleak` dependency inline), or `pip install bleak` and run
  `./fnb48.py ...`. Examples below say `fnb48.py`.
* The host needs a working Bluetooth adapter (on Linux, BlueZ running).
* Set `FNB48_ADDRESS=<address>` (or pass `-a <address>`) to skip the name scan. Get the address from `fnb48.py scan`.
  Even with an address, connecting takes about 3-8 s.
* **Only one client can be connected to the meter at a time.** While any `fnb48.py` process (or the web app or a
  phone) is connected, the meter stops advertising and every other command fails with exit code 3. Never run two
  commands at once. Before starting, check for leftovers with `pgrep -af fnb48.py`. On Linux,
  `bluetoothctl info <address> | grep Connected` shows whether something is holding the link.

## Output contract

* **stdout**: exactly one JSON document (pretty-printed) per run. Floats are rounded to 7 digits; unknown values are
  `null`. The only exception is `measure --out -`, where samples go to stdout and the JSON goes to stderr.
* **stderr**: human progress lines starting with `# ` (for example `# recording 30 s to log.csv`). `-q` silences them.
* Every result has `"ok": true|false`. On failure: `{"ok": false, "error": "<message>", "exit_code": N}`.

| Exit | Meaning |
|------|---------|
| 0 | Success |
| 1 | Generic error (bad log file, verify after `group` failed, bleak missing) |
| 2 | Usage error, including a missing `--confirm` |
| 3 | Meter not found: off, out of range, or already connected to another client |
| 4 | Connection failed or lost and couldn't be re-established |
| 5 | `measure` start condition not met before `--start-timeout` |
| 6 | Meter NAKed a trigger command |
| 130 | Interrupted |

Options that every command takes (before or after the command name):

| Option | Default | Meaning |
|--------|---------|---------|
| `-a, --address ADDR` | `$FNB48_ADDRESS`, else scan | Meter BLE address |
| `--scan-timeout S` | 15 | Seconds to scan by name when no address is given |
| `--connect-timeout S` | 20 | Seconds to find and connect to a known address |
| `-q, --quiet` | off | No `# ` lines on stderr |

## Commands

### `scan`

`fnb48.py scan [--all] [--scan-timeout S]` lists meters in range, strongest signal first:
`{"ok": true, "devices": [{"address": "BA:03:C8:07:70:4C", "name": "FNB48-044133", "rssi": -44}]}`.
It exits with 3 if none are found. `--all` lists every BLE device.

### `info`

`fnb48.py info` returns:

* `device`: `model`, `firmware`, `serial`, `group_count`, `current_group`, `run` (meaning unknown, sometimes garbage)
* `trigger`: fast-charge trigger state (`"None"` normally)
* `groups`: each on-device energy group's accumulated `Wh` and `Ah`
* `reading`: a single live reading of `voltage_V`, `current_A`, `power_W`, `resistance_ohm` (V/I, or `null`),
  `temp_C`, `dplus_V`, `dminus_V`, `protocols` (the two charge protocols the meter detected) and `active_group`
  (the active group's counters plus `record_s` and `uptime_s`)

Use this for a quick look ("is anything plugged in, and what is it drawing?").

### `measure`: the main tool

`fnb48.py measure [options]` records samples and then prints statistics.

| Option | Meaning |
|--------|---------|
| `-d, --duration S` | Record for S seconds after recording starts. The default of 0 means record until SIGINT/SIGTERM |
| `-o, --out FILE` | Write every sample to FILE (CSV, or JSONL if the name ends in `.jsonl`/`.ndjson`). `-` means stdout |
| `--format csv\|jsonl` | Override the format chosen from the file extension |
| `--fast` | Use the ~10 Hz stream (1 mV / 1 mA) instead of the default ~4 Hz stream (0.1 mV / 0.1 mA) |
| `--start-above A` / `--start-below A` | Don't record until current is >= / <= A amps |
| `--start-hold S` | The start condition must hold for S seconds |
| `--start-timeout S` | Give up (exit 5) if the start condition isn't met within S seconds |
| `--stop-above A` / `--stop-below A` | Stop once current is >= / <= A amps (`--duration` still caps the run) |
| `--stop-hold S` | The stop condition must hold for S seconds |
| `--burst-threshold A` | Add a `bursts` analysis: runs of samples with current >= A |
| `--max-bursts N` | How many bursts to list (default 100; the totals cover all of them) |
| `--no-reconnect` | Stop with exit 4 if the link drops, instead of reconnecting |
| `--reconnect-timeout S` | Give up reconnecting after S seconds (default 120; 0 means never) |

Without `--out`, samples are kept only in memory for the summary. **Always pass `--out`** when you may want to look
at a time window later. Samples are flushed to the file as they arrive, so you can read the file while the
capture is still running.

Sample file columns (CSV header, or JSONL keys):

```
unix_time,time_s,voltage_V,current_A,power_W,temp_C
1792258929.982,0.000,5.2334,0.0902,0.4723,nan
```

`unix_time` is the host clock when the sample arrived. `time_s` counts from the first recorded sample. `temp_C` is
`nan`/`null` until the first temperature message arrives.

Summary fields:

| Field | Meaning |
|-------|---------|
| `stop_reason` | `duration`, `signal`, `stop-condition`, `start-timeout` or `disconnected` |
| `mode`, `resolution` | `precise` (0.1 mV/mA) or `fast` (1 mV/mA) |
| `samples`, `rate_hz` | Sample count and the measured rate (precise ≈ 4.0 Hz, fast ≈ 9.9 Hz) |
| `start_unix`, `end_unix`, `duration_s` | Time span of the recorded samples |
| `voltage_V`, `current_A`, `power_W` | Each has `min`, `max`, `mean`, `median`, `p5`, `p95` and `stdev` over the samples |
| `avg_current_A`, `avg_power_W` | Time-weighted averages, which are better than the sample means when there are gaps |
| `energy_Wh`, `charge_mAh` | Trapezoidal integration over the samples, with gaps excluded |
| `meter_counter` | The meter's own energy counter for the same run: `delta_Wh`, `delta_mAh`, `over_s`. Useful as a cross-check (resolution 10 µWh / 10 µAh, updated at 1 Hz) |
| `temp_C` | The meter's temperature: `start`, `end`, `max` |
| `port` | `dplus_V`, `dminus_V`, and every pair of detected charge protocols seen (`protocols_seen`) |
| `gaps` | Holes in the data longer than 2 s (`from_s`, `to_s`, `length_s`), usually from a reconnect |
| `disconnects` | Number of link drops (the script reconnects automatically) |
| `start_condition` | `condition`, whether it was `met`, and `waited_s` |
| `bursts` | With `--burst-threshold`: `count`, `time_above_s`, `fraction_above`, `mean_current_above_A`, `mean_current_below_A`, `longest_s`, `mean_period_s` (average time between burst starts), and `list` (each burst's `start_s`, `duration_s`, `peak_A`, `mean_A` and `samples`) |
| `out` | Path of the sample file |

`measure` heals itself. If no data arrives for 5 s it restarts the stream, and after 15 s it reconnects.

### `summarize`

`fnb48.py summarize FILE [--from T] [--to T] [--burst-threshold A]` gives the same statistics as `measure` for a
saved log, optionally limited to a window. `T` is seconds into the log (`time_s`), or a unix time if it's > 1e9.
It needs no meter, so it can run while another capture is in progress. The result also includes `log_start_unix`
and `log_samples`.

### `groups` and `group`

* `fnb48.py groups` reads every energy group's `Wh`/`Ah` and reports `current_group`.
* `fnb48.py group select N` makes group N the active accumulator, then checks that it took effect.
* `fnb48.py group clear N --confirm` **erases** group N's stored totals. Without `--confirm` it refuses (exit 2)
  before connecting. Only clear a group when the user asked you to.

### `trigger`

* `fnb48.py trigger state` reports the fast-charge trigger state and a live reading.
* `fnb48.py trigger set MODE VOLTS --confirm` asks the charger for a higher voltage. MODE is `qc2` (5/9/12/20 V),
  `qc3` (3.0-20.0 V in 0.2 V steps), `fcp` or `afc` (5/9/12 V). The result has `ack`, the new `trigger` state, and a
  `reading` taken about 1 s later, so check `reading.voltage_V` to confirm the voltage changed.
* `fnb48.py trigger off` stops triggering.

**This puts more than 5 V on VBUS and can destroy the device under test.** Never use it unless the user
explicitly asks for it. Whether the trigger stays active after disconnecting hasn't been verified, so always run
`trigger off` afterwards and check that `reading.voltage_V` is back to about 5 V.

## Recipes

**Quick check:**

```sh
fnb48.py measure -q -d 10
```

Read `avg_current_A` and `current_A.max`.

**Capture around actions you perform** (the most common case for debugging):

```sh
fnb48.py measure -o /tmp/cap.csv > /tmp/cap.json 2>/tmp/cap.err &   # note the PID ($!)
# wait until recording has started (connecting takes 3-8 s):
until [ "$(wc -l < /tmp/cap.csv 2>/dev/null)" -gt 1 ]; do sleep 0.5; done
date +%s.%N    # T1: then do action A on the device
date +%s.%N    # T2: then do action B ...
kill -TERM <PID>; wait <PID>     # it disconnects cleanly and writes /tmp/cap.json
fnb48.py summarize /tmp/cap.csv --from T1 --to T2
```

Use absolute unix times from `date +%s.%N` for `--from`/`--to`, so you don't have to work out offsets. If you run
it through a tool that backgrounds commands for you and you don't have the PID, use
`pkill -TERM -f '[f]nb48.py measure'`. The brackets matter: a plain `pkill -f 'fnb48.py measure'` also matches,
and kills, any shell whose own command line contains that text. SIGTERM reaches the script through `uv run` too.
Don't use SIGKILL: the meter stays connected until the BLE link times out.

**Let the device settle, then measure idle current:**

```sh
fnb48.py measure -q --start-below 0.005 --start-hold 5 --start-timeout 120 -d 60 -o idle.csv
```

Exit 5 means it never dropped below 5 mA within 120 s, which is itself a finding.

**Periodic wakeups** (radio beacons, polling loops):

```sh
fnb48.py measure -q -d 120 -o wake.csv --burst-threshold 0.03
```

Look at `bursts.count`, `bursts.mean_period_s`, `bursts.longest_s` and the mean current above and below the
threshold. Pick the threshold between the idle and active levels; `current_A.p5` and `current_A.p95` from a first
run are good guides.

**Wait for an event, such as a boot or the start of charging:**

```sh
fnb48.py measure -q --start-above 0.1 --start-timeout 300 -d 30 -o boot.csv
```

**Compare before/after a change.** Run the same `measure -d N` under the same conditions before and after, and
compare `avg_current_A` / `energy_Wh`. Precise-mode noise on a steady load is about ±0.2 mA (`current_A.stdev`),
so treat smaller differences as noise.

## Limits and caveats

* **The sample rate is low.** It's about 4 Hz in precise mode and 10 Hz in fast mode. Whether each sample is an
  instantaneous reading or an average hasn't been verified, so assume that spikes shorter than about 100-250 ms can
  be missed or aliased. The `meter_counter` energy is integrated by the meter itself and catches what the samples miss, so if it
  disagrees noticeably with `energy_Wh`, the load has short spikes.
* Resolution: precise mode is 0.1 mA / 0.1 mV, fast mode is 1 mA / 1 mV. Use precise (the default) for sleep and
  idle currents.
* Timestamps are when the host received the sample. Expect a few tens of ms of BLE latency and jitter, so don't
  correlate events to better than about 0.25 s.
* `duration_s` is the span between the first and last sample, so it's slightly less than `--duration`.
* The detected-protocol fields are the meter's guess about the charger and data lines, and they often flip between
  values (`protocols_seen` lists every pair).
* Link drops are handled automatically. They appear as `disconnects` and `gaps`, and gaps are left out of the
  energy totals.

## Legacy mode

Running `fnb48.py [-a ADDR] [--fast] > log.csv` with no command still streams CSV to stdout until Ctrl-C (it's
equivalent to `measure --out -`). The CSV now has a leading `unix_time` column.
