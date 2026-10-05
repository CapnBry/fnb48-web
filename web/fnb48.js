// FNIRSI FNB48 / FNB38 / C1 BLE protocol + Web Bluetooth driver.
// See PROTOCOL.md for the reverse-engineered frame format.

export const SERVICE_NOTIFY = 0xffe0;
export const CHAR_NOTIFY = 0xffe4;
export const SERVICE_WRITE = 0xffe5;
export const CHAR_WRITE = 0xffe9;

export const CMD = {
  INFO: 0x81,          // -> 0x03 device info
  START: 0x82,         // begin streaming 0x04..0x08
  STOP: 0x84,          // stop streaming
  TRIGGER_STATUS: 0x85, // -> 0x09
  TRIGGER: 0x86,       // [mode, mV lo, mV hi] -> 0x01/0x02
  TRIGGER_STOP: 0x87,  // -> 0x01/0x02
  SELECT_GROUP: 0x88,  // [group]
  CLEAR_GROUP: 0x89,   // [group]
  CAPACITY: 0x8a,      // -> 0x0A x groups
};

// Index tables taken from the original app (Global.sbxyhb / Global.cfxyh).
export const DETECTED_PROTOCOLS = [
  'Unknown', 'DCP 1.5A', 'QC2.0 5V', 'QC2.0 9V', 'QC2.0 12V', 'QC2.0 20V', 'QC3.0',
  'Apple 2.1A', 'Apple 2.4A', 'Samsung 2.0A', 'USB2.0 Full', 'USB2.0 High',
  'FCP/AFC 9V', 'FCP/AFC 12V', 'Huawei SCP', 'PD MTK',
];
export const TRIGGER_STATES = ['None', 'QC2', 'QC3', 'FCP', 'SCP', 'AFC', 'PD', 'PD1', 'VOOC', 'SVOOC', 'SVOOC'];

// Trigger modes accepted by CMD.TRIGGER and the voltages the original app offered.
export const TRIGGER_MODES = [
  { id: 0, name: 'QC2.0', volts: [5, 9, 12, 20] },
  { id: 1, name: 'QC3.0', volts: Array.from({ length: 86 }, (_, i) => +(3 + i * 0.2).toFixed(1)) },
  { id: 2, name: 'Huawei FCP', volts: [5, 9, 12] },
  { id: 3, name: 'Samsung AFC', volts: [5, 9, 12] },
];

const MODELS = { 9: 'FNIRSI C1', 38: 'FNB38', 48: 'FNB48' };

export function crc16xmodem(bytes) {
  let crc = 0;
  for (const b of bytes) {
    crc ^= b << 8;
    for (let i = 0; i < 8; i++) crc = crc & 0x8000 ? ((crc << 1) ^ 0x1021) & 0xffff : (crc << 1) & 0xffff;
  }
  return crc;
}

// Frame: AA <cmd> <len> <payload...> <low byte of CRC16-XMODEM over all preceding bytes>
export function buildFrame(cmd, payload = []) {
  const f = new Uint8Array(payload.length + 4);
  f[0] = 0xaa;
  f[1] = cmd;
  f[2] = payload.length;
  f.set(payload, 3);
  f[f.length - 1] = crc16xmodem(f.subarray(0, f.length - 1)) & 0xff;
  return f;
}

export function triggerPayload(mode, volts) {
  const mv = Math.round(volts * 1000);
  return [mode, mv & 0xff, (mv >> 8) & 0xff];
}

// Reassembles frames from an arbitrary chunked byte stream.
export class FrameParser {
  constructor() {
    this.buf = new Uint8Array(0);
    this.crcErrors = 0;
  }

  push(chunk) {
    const merged = new Uint8Array(this.buf.length + chunk.length);
    merged.set(this.buf);
    merged.set(chunk, this.buf.length);
    this.buf = merged;

    const frames = [];
    let i = 0;
    while (i < this.buf.length) {
      if (this.buf[i] !== 0xaa) { i++; continue; }
      if (i + 3 > this.buf.length) break;
      const n = this.buf[i + 2] + 4;
      if (i + n > this.buf.length) break;
      const f = this.buf.subarray(i, i + n);
      if ((crc16xmodem(f.subarray(0, n - 1)) & 0xff) === f[n - 1]) {
        frames.push({ cmd: f[1], payload: f.slice(3, n - 1) });
        i += n;
      } else {
        this.crcErrors++;
        i++;
      }
    }
    this.buf = this.buf.slice(i);
    return frames;
  }
}

// Turns a frame into a typed message object.
export function decode({ cmd, payload: p }) {
  const dv = new DataView(p.buffer, p.byteOffset, p.byteLength);
  const u16 = (o) => dv.getUint16(o, true);
  const i16 = (o) => dv.getInt16(o, true);
  const u32 = (o) => dv.getUint32(o, true);
  const i32 = (o) => dv.getInt32(o, true);
  try {
    switch (cmd) {
      case 0x01:
      case 0x02:
        return { type: cmd === 0x01 ? 'ack' : 'nak', for: p[0] };
      case 0x03:
        return {
          type: 'info', modelId: u16(0), model: MODELS[u16(0)] ?? `Unknown (${u16(0)})`,
          firmware: (u16(2) / 100).toFixed(2), serial: String(u32(4)).padStart(6, '0'),
          runCount: u32(8), groupCount: p[12], group: p[13],
        };
      case 0x04:
        return { type: 'power', voltage: i32(0) / 1e4, current: i32(4) / 1e4, power: i32(8) / 1e4 };
      case 0x05: {
        const raw = u32(0);
        return {
          type: 'aux', resistance: raw >= 99999999 ? null : raw / 1e4,
          temperature: (p[4] > 0 ? 1 : -1) * u16(5) / 10,
        };
      }
      case 0x06:
        return {
          type: 'dataLines', dPlus: u16(0) / 1000, dMinus: u16(2) / 1000,
          protocol1: DETECTED_PROTOCOLS[p[4]] ?? `#${p[4]}`, protocol2: DETECTED_PROTOCOLS[p[5]] ?? `#${p[5]}`,
        };
      case 0x07:
        return { type: 'sample', voltage: u16(0) / 1000, current: i16(2) / 1000 };
      case 0x08:
        return {
          type: 'energy', group: p[0], wh: u32(1) / 1e5, ah: u32(5) / 1e5,
          recordSeconds: u32(9), uptimeSeconds: u32(13),
        };
      case 0x09:
        return { type: 'triggerState', state: p[0], name: TRIGGER_STATES[p[0]] ?? `#${p[0]}` };
      case 0x0a:
        return { type: 'capacity', group: p[0], wh: u32(1) / 1e5, ah: u32(5) / 1e5 };
    }
  } catch (e) {
    return { type: 'malformed', cmd, error: String(e) };
  }
  return { type: 'unknown', cmd };
}

// Web Bluetooth connection. Emits 'message' (decoded), 'raw' (Uint8Array), 'status' (string),
// 'log' (string describing a connection step or error, for diagnostics).
const hex = (bytes) => [...bytes].map((b) => b.toString(16).padStart(2, '0')).join('');

export class FNB48 extends EventTarget {
  constructor() {
    super();
    this.device = null;
    this.writeChar = null;
    this.notifyChar = null;
    this.parser = new FrameParser();
    this.writeQueue = Promise.resolve();
    this.streaming = false;
    this.lastData = 0;
    this.watchdog = null;
    this.handleNotify = (e) => this.onNotify(e.target.value);
    this.handleDisconnect = () => this.onDisconnected();
    this.searchAbort = null;
  }

  static get supported() {
    return typeof navigator !== 'undefined' && !!navigator.bluetooth;
  }

  get connected() {
    return !!this.device?.gatt?.connected;
  }

  emit(type, detail) {
    this.dispatchEvent(new CustomEvent(type, { detail }));
  }

  log(msg) {
    this.emit('log', msg);
  }

  async request() {
    this.device = await navigator.bluetooth.requestDevice({
      filters: [{ namePrefix: 'FNB' }, { namePrefix: 'FNIRSI' }, { namePrefix: 'C1' }],
      optionalServices: [SERVICE_NOTIFY, SERVICE_WRITE],
    });
    this.device.addEventListener('gattserverdisconnected', this.handleDisconnect);
    this.log(`chose ${this.device.name ?? 'unnamed device'}`);
    return this.device;
  }

  forget() {
    this.device?.removeEventListener('gattserverdisconnected', this.handleDisconnect);
    this.device = null;
  }

  get searching() {
    return !!this.searchAbort;
  }

  cancelSearch() {
    this.searchAbort?.abort();
  }

  // BlueZ drops an unpaired device ~30 s after it last heard it advertise, after which gatt.connect()
  // fails with "Bluetooth Device is no longer in range". watchAdvertisements() makes the OS scan, which
  // brings the device back within a few seconds. Chrome on Linux never fires 'advertisementreceived' on
  // the old device object, so instead of waiting for that event, retry the connection while scanning.
  async searchAndConnect(timeoutMs) {
    const ac = new AbortController();
    this.searchAbort = ac;
    const pause = (ms) => new Promise((resolve) => {
      const t = setTimeout(resolve, ms);
      ac.signal.addEventListener('abort', () => { clearTimeout(t); resolve(); }, { once: true });
    });
    try {
      await this.device.watchAdvertisements({ signal: ac.signal });
      const deadline = Date.now() + timeoutMs;
      while (!ac.signal.aborted && Date.now() < deadline) {
        await pause(1000);
        if (ac.signal.aborted) break;
        try {
          const server = await this.device.gatt.connect();
          if (!ac.signal.aborted) return server;
          server.disconnect(); // cancelled while the connection was being set up
        } catch (e) {
          if (e.name !== 'NetworkError') throw e;
        }
      }
      throw ac.signal.aborted
        ? new DOMException('Search cancelled', 'AbortError')
        : new DOMException('Meter not found', 'TimeoutError');
    } finally {
      this.searchAbort = null;
      ac.abort();
    }
  }

  async connect() {
    if (!this.device) await this.request();
    this.emit('status', 'connecting');
    let server;
    try {
      server = await this.device.gatt.connect();
    } catch (e) {
      this.log(`gatt.connect failed: ${e.name}: ${e.message}`);
      if (e.name !== 'NetworkError') throw e;
      if (this.device.watchAdvertisements) {
        this.log('scanning for the meter');
        this.emit('status', 'searching');
        server = await this.searchAndConnect(60000);
        this.emit('status', 'connecting');
      } else {
        // No way to rescan for this device; re-pick it through the chooser while the click still counts.
        this.forget();
        if (navigator.userActivation && !navigator.userActivation.isActive) {
          throw new DOMException('Device needs to be chosen again', 'NeedsChooserError');
        }
        await this.request();
        this.emit('status', 'connecting');
        server = await this.device.gatt.connect();
      }
    }
    this.log('GATT connected');
    try {
      const [notifySvc, writeSvc] = await this.getServices(server);
      this.notifyChar = await notifySvc.getCharacteristic(CHAR_NOTIFY);
      this.writeChar = await writeSvc.getCharacteristic(CHAR_WRITE);
      const p = this.writeChar.properties;
      this.log(`characteristics found; write props: ${['write', 'writeWithoutResponse'].filter((k) => p[k]).join(', ')}`);
      this.notifyCount = 0;
      this.parser = new FrameParser();
      // Chrome may hand back the same characteristic object after a reconnect, so don't stack listeners.
      this.notifyChar.removeEventListener('characteristicvaluechanged', this.handleNotify);
      this.notifyChar.addEventListener('characteristicvaluechanged', this.handleNotify);
      await this.notifyChar.startNotifications();
      this.log('notifications enabled');
    } catch (e) {
      // Don't leave a half-set-up link open looking "connected".
      this.log(`setup failed: ${e.name}: ${e.message}`);
      this.device.gatt.disconnect();
      throw e;
    }
    this.emit('status', 'connected');
    await this.send(CMD.INFO);
    await this.startStreaming();
  }

  // Android discovers services as soon as the link comes up, while the ATT MTU is still 23. The meter's
  // BLE module answers that discovery with a 68-byte packet, which recent Android versions reject, so
  // discovery comes back empty and Android keeps serving that empty result for the connection. The MTU
  // exchange (to 252) completes right after. Reconnecting while Android still holds the radio link
  // (it lingers ~1 s after a disconnect) re-runs discovery on the same link at MTU 252, which works.
  // Too short a gap (<~200 ms) and Chrome reuses its stale client; too long and the link is gone.
  async getServices(server) {
    const lookup = async (srv) => {
      try {
        return [await srv.getPrimaryService(SERVICE_NOTIFY), await srv.getPrimaryService(SERVICE_WRITE)];
      } catch (e) {
        if (e.name !== 'NotFoundError') throw e;
        return null;
      }
    };
    let services = await lookup(server);
    for (const gap of [500, 800, 350]) {
      if (services) return services;
      this.log(`no services found (Android discovery failed); reconnecting on the same link after ${gap} ms`);
      services = await lookup(await this.reconnectOnSameLink(gap));
    }
    if (services) return services;
    throw new DOMException("Couldn't discover the meter's services", 'ServiceDiscoveryError');
  }

  async reconnectOnSameLink(gapMs) {
    // This disconnect is internal, so keep it from reaching onDisconnected / the UI.
    this.device.removeEventListener('gattserverdisconnected', this.handleDisconnect);
    try {
      const gone = new Promise((resolve) => {
        this.device.addEventListener('gattserverdisconnected', resolve, { once: true });
        setTimeout(resolve, 1000);
      });
      this.device.gatt.disconnect();
      await gone;
      await new Promise((resolve) => setTimeout(resolve, gapMs));
      return await this.device.gatt.connect();
    } finally {
      this.device.addEventListener('gattserverdisconnected', this.handleDisconnect);
    }
  }

  onNotify(value) {
    const bytes = new Uint8Array(value.buffer, value.byteOffset, value.byteLength);
    this.lastData = performance.now();
    if (++this.notifyCount <= 3) this.log(`notification #${this.notifyCount}: ${bytes.length} bytes ${hex(bytes)}`);
    this.emit('raw', bytes.slice());
    for (const frame of this.parser.push(bytes)) this.emit('message', decode(frame));
  }

  onDisconnected() {
    this.log('GATT disconnected');
    clearInterval(this.watchdog);
    this.streaming = false;
    this.emit('status', 'disconnected');
  }

  async disconnect() {
    if (this.connected) {
      try { await this.send(CMD.STOP); } catch { /* best effort */ }
      this.device.gatt.disconnect();
    }
  }

  // Web Bluetooth rejects overlapping GATT operations, so writes are serialised.
  send(cmd, payload = []) {
    const frame = buildFrame(cmd, payload);
    const op = this.writeQueue.then(async () => {
      const t0 = performance.now();
      try {
        await this.writeChar.writeValueWithResponse(frame);
        this.log(`sent ${hex(frame)} (${Math.round(performance.now() - t0)} ms)`);
      } catch (e) {
        this.log(`write ${hex(frame)} failed: ${e.name}: ${e.message}`);
        throw e;
      }
    });
    this.writeQueue = op.catch(() => {});
    return op;
  }

  async startStreaming() {
    this.streaming = true;
    this.lastData = performance.now();
    await this.send(CMD.START);
    clearInterval(this.watchdog);
    // Re-issue START if the stream goes quiet (e.g. after the meter is power-cycled on the same link).
    this.watchdog = setInterval(() => {
      if (this.streaming && this.connected && performance.now() - this.lastData > 3000) {
        this.lastData = performance.now();
        this.log(`no data for 3 s (${this.notifyCount} notifications so far), re-sending START`);
        this.send(CMD.START).catch(() => {});
      }
    }, 1000);
  }

  async stopStreaming() {
    this.streaming = false;
    clearInterval(this.watchdog);
    await this.send(CMD.STOP);
  }
}
