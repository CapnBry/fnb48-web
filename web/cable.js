// Guided cable resistance test. Pure logic with no DOM or Bluetooth, driven by the meter's precise readings.
//
// The meter sits upstream of a resistive load, and measures V and I together, so V/I is the total resistance
// after the meter whatever the charger's voltage does:
//   baseline:  charger -> meter -> load           V/I = R_load
//   cable:     charger -> meter -> cable -> load  V/I = R_load + R_cable
// The cable's resistance is the difference. It doesn't work with a constant-current load, because then the
// meter's V/I doesn't depend on what's downstream.

export const CABLE_TUNING = {
  LOAD_A: 0.1,            // current at or above this means a load is connected
  UNPLUG_A: 0.02,         // current below this means the load has been unplugged
  SETTLE_S: 2,            // ignore this long after a load is connected
  WINDOW_S: 6,            // average over this long
  MIN_SAMPLES: 12,        // fewest samples to accept in a window
  MIN_DEVIATION_SAMPLES: 4, // fewest samples to show a deviation from
  MAX_GAP_S: 2,           // a longer gap in the readings restarts the capture
  STABLE_OHM: 0.001,      // the window's two halves must agree this closely
  TIMEOUT_S: 20,          // accept the reading anyway after this long, and flag it as drifting
  NEAR_ZERO_OHM: 0.005,   // results smaller than this get a warning
  HIGH_OHM: 1,            // results bigger than this get a warning
};
const T = CABLE_TUNING;

const ratio = (samples) => {
  let v = 0, a = 0;
  for (const s of samples) { v += s.v; a += s.a; }
  return { r: v / a, v: v / samples.length, a: a / samples.length };
};

// Averages one connection's readings, once they've settled.
export class Capture {
  constructor() { this.restart(); }

  restart() {
    this.startT = null;
    this.lastT = null;
    this.samples = [];
  }

  // Returns { status: 'waiting' | 'settling' | 'measuring' | 'stabilizing' | 'done', deviation, result }.
  // deviation is how far the window's later half is from its earlier half, in ohms, or null with too few
  // samples to tell. A reading is accepted once the window is full and this is within +/- STABLE_OHM.
  push(t, v, a) {
    if (a < T.LOAD_A || (this.lastT !== null && t - this.lastT > T.MAX_GAP_S)) this.restart();
    if (a < T.LOAD_A) return { status: 'waiting', deviation: null };
    this.lastT = t;
    this.startT ??= t;
    if (t - this.startT < T.SETTLE_S) return { status: 'settling', deviation: null };

    this.samples.push({ t, v, a });
    while (t - this.samples[0].t > T.WINDOW_S) this.samples.shift();
    const span = t - this.samples[0].t;
    const half = this.samples.length >> 1;
    const deviation = this.samples.length < T.MIN_DEVIATION_SAMPLES
      ? null
      : ratio(this.samples.slice(half)).r - ratio(this.samples.slice(0, half)).r;
    if (span < T.WINDOW_S * 0.95 || this.samples.length < T.MIN_SAMPLES) return { status: 'measuring', deviation };

    const stable = Math.abs(deviation) <= T.STABLE_OHM;
    if (!stable && t - this.startT < T.TIMEOUT_S) return { status: 'stabilizing', deviation };
    return {
      status: 'done',
      deviation,
      result: { ...ratio(this.samples), stable, driftPerMin: deviation / (span / 2) * 60 },
    };
  }
}

// Steps: idle -> baseline -> cable -> result.
export class CableTest {
  constructor() { this.reset(); }

  reset() {
    this.step = 'idle';
    this.baseline = null;
    this.cable = null;
    this.capture = null;
    this.awaitingUnplug = false;
    this.status = null;
    this.deviation = null;
    this.live = null;
  }

  get capturing() { return this.step === 'baseline' || this.step === 'cable'; }

  begin(step, awaitingUnplug) {
    this.step = step;
    this.capture = new Capture();
    this.awaitingUnplug = awaitingUnplug;
    this.status = 'waiting';
    this.deviation = null;
  }

  start() {
    this.reset();
    this.begin('baseline', false);
  }

  push(t, v, a) {
    this.live = { v, a, r: a >= T.LOAD_A ? v / a : null };
    if (!this.capturing) return;
    if (this.awaitingUnplug) {
      if (a >= T.UNPLUG_A) return;
      this.awaitingUnplug = false;
    }
    const { status, deviation, result } = this.capture.push(t, v, a);
    this.status = status;
    this.deviation = deviation;
    if (status !== 'done') return;
    if (this.step === 'baseline') {
      this.baseline = result;
      this.begin('cable', true);
    } else {
      this.cable = result;
      this.capture = null;
      this.step = 'result';
    }
  }

  get ohms() { return this.cable.r - this.baseline.r; }

  // Things worth flagging about the result, as codes for the page to word.
  get warnings() {
    const out = [];
    const ohms = this.ohms;
    if (ohms < -T.NEAR_ZERO_OHM) out.push({ code: 'negative' });
    else if (ohms < T.NEAR_ZERO_OHM) out.push({ code: 'nearZero' });
    if (ohms > T.HIGH_OHM) out.push({ code: 'high' });
    const drifting = [this.baseline, this.cable].find((m) => !m.stable);
    if (drifting) out.push({ code: 'drifting', perMin: drifting.driftPerMin });
    return out;
  }
}
