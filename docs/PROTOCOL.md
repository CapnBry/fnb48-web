# FNIRSI FNB48 BLE protocol

Reverse engineered from `FNIRSI Bluetooth APP old（Android） v1.0.apk` (package `com.uct`, classes
`BleDetailActivity`, `CmdUtli`, `CrcTool`) and verified against a live FNB48 (FW 2.60, SN 044133).
The same app also handles the FNB38 and FNIRSI C1, so they likely use the same protocol.

## Transport

The meter advertises as `FNB48-<serial>` and uses a generic BLE-UART module.

| Role   | Service  | Characteristic | Properties    |
|--------|----------|----------------|---------------|
| Notify | `ffe0`   | `ffe4`         | notify        |
| Write  | `ffe5`   | `ffe9`         | write / write-without-response |

(16-bit UUIDs on the Bluetooth base: `0000xxxx-0000-1000-8000-00805f9b34fb`.)

No pairing or bonding is needed. The meter stays silent until it gets a command. A single notification can hold
several frames (45 bytes seen), and a frame may be split across notifications, so the receiver must buffer and
reassemble them.

### Android service-discovery bug

The BLE module (Device Information: manufacturer `RFstar`, model `RSBRS02ABR`, firmware `Tv5.11u_20200817_EP`)
breaks the ATT MTU rules. On connect, it sends its own MTU request of 23. It then answers the first service-discovery
request with a 68-byte packet, although the MTU is still 23. Recent Android versions start discovery as soon as the
link is up, before the client's MTU exchange (which ends at 252), so they hit this and log:

```
gatt_client_handle_server_rsp: invalid response pkt size: 68, PDU size: 23
```

Discovery then returns no services, and Android keeps that empty result for the rest of the connection. This is why
the original app "connects but shows no data" on newer phones: it looks up service `ffe0`, gets nothing, and stops.
Linux/BlueZ works because it exchanges the MTU before discovering.

Workaround (in `web/fnb48.js`): if no services are found, disconnect and reconnect after about 500 ms. Android keeps
the radio link up for about a second after the last client disconnects, so the new connection reuses the link at
MTU 252 and discovery succeeds. With a gap shorter than about 200 ms Chrome reuses its stale connection, and with a
gap over about a second the link is gone and the problem repeats.

## Frame format

```
AA  CMD  LEN  PAYLOAD[LEN]  CHK
```

* `AA`: start byte
* `CMD`: command (host → meter: `0x8x`; meter → host: `0x0x`)
* `LEN`: payload length
* `CHK`: **low byte** of CRC-16/XMODEM (poly `0x1021`, init `0x0000`) over every preceding byte, including `AA`

All multi-byte integers are **little-endian**.

Examples: `AA 81 00 F4` (info), `AA 82 00 A7` (start), `AA 84 00 ..` (stop), `AA 88 01 01 36` (select group 1).

## Host → meter commands

| CMD  | Payload               | Meaning | Reply |
|------|-----------------------|---------|-------|
| `81` | –                     | Get device info | `03` |
| `82` | –                     | Start streaming measurements | `04`–`08` continuously |
| `84` | –                     | Stop streaming | – |
| `85` | –                     | Get fast-charge trigger state | `09` |
| `86` | `mode, mV_lo, mV_hi`  | Trigger a fast-charge protocol (see below) | `01`/`02` |
| `87` | –                     | Stop fast-charge trigger | `01`/`02` |
| `88` | `group`               | Select the active energy-record group (1..N) | none observed |
| `89` | `group`               | Clear an energy-record group | – |
| `8A` | –                     | Read every group's accumulated Wh/Ah | N × `0A` |

Trigger modes for `86` (as the original app offered them):

| mode | Protocol      | Voltages |
|------|---------------|----------|
| 0    | QC2.0         | 5, 9, 12, 20 V |
| 1    | QC3.0         | 3.0 – 20.0 V in 0.2 V steps |
| 2    | Huawei FCP    | 5, 9, 12 V |
| 3    | Samsung AFC   | 5, 9, 12 V |

The voltage is encoded as millivolts in a u16. After `01`/`02` the app sends `85` to refresh the state.

## Meter → host messages

| CMD  | Len | Layout | Notes |
|------|-----|--------|-------|
| `01` | 1   | `u8 cmd` | Reply to `86`/`87`, echoes the command byte. Probably ACK; the app treats `01` and `02` the same |
| `02` | 1   | `u8 cmd` | Probably NAK |
| `03` | 14  | `u16 model, u16 fw×100, u32 serial, u32 run, u8 groupCount, u8 currentGroup` | model 48 = FNB48, 38 = FNB38, 9 = C1. The app labels `run` as "Run:"; its meaning is unconfirmed. It doesn't change per connection. One meter reported 728, then later 0x7FFFF7DD (CRC valid), so the device can report a corrupt value |
| `04` | 12  | `i32 V×1e4, i32 A×1e4, i32 W×1e4` | Precise reading, about 4 Hz |
| `05` | 7   | `u32 R×1e4, u8 tempSign, u16 °C×10` | R is the equivalent load resistance (V/I); 99999999 means not available. tempSign > 0 is positive |
| `06` | 6   | `u16 D+ mV, u16 D− mV, u8 proto1, u8 proto2` | Detected protocol indices into the table below |
| `07` | 4   | `u16 V mV, i16 A mA` | Fast sample, used by the app's live chart (about 8–10 Hz) |
| `08` | 17  | `u8 group, u32 Wh×1e5, u32 Ah×1e5, u32 recordSeconds, u32 uptimeSeconds` | Active energy group, about 1 Hz |
| `09` | 1   | `u8 state` | Trigger state: 0 None, 1 QC2, 2 QC3, 3 FCP, 4 SCP, 5 AFC, 6 PD, 7 PD1, 8 VOOC, 9/10 SVOOC |
| `0A` | 9   | `u8 group, u32 Wh×1e5, u32 Ah×1e5` | One per group in reply to `8A` |

Detected-protocol table (`06`): 0 Unknown, 1 DCP 1.5A, 2 QC2.0 5V, 3 QC2.0 9V, 4 QC2.0 12V, 5 QC2.0 20V,
6 QC3.0, 7 Apple 2.1A, 8 Apple 2.4A, 9 Samsung 2.0A, 10 USB2.0 Full, 11 USB2.0 High, 12 FCP/AFC 9V,
13 FCP/AFC 12V, 14 Huawei SCP, 15 PD MTK.

The app estimates battery capacity per group as `Wh / cellVoltage × efficiency` (defaults: 3.7 V and 90%).

## Session

1. Connect, then enable notifications on `ffe4`.
2. Write `AA 81 00 F4` to `ffe9` to get `03` device info.
3. Write `AA 82 00 A7` to start the stream.
4. Before disconnecting, write `84` to stop it.

## Captured example

```
→ aa8100f4
← aa 03 0e 3000 0401 65ac0000 d8020000 0a 01 be      FNB48, FW 2.60, SN 044133, run 728, 10 groups, group 1
→ aa8200a7
← aa 06 06 0d00 1100 01 0b 32                        D+ 0.013 V, D− 0.017 V, DCP 1.5A / USB2.0 High
← aa 07 04 5d13 0000 88                              4.957 V, 0 mA
← aa 04 0c b8c10000 01000000 05000000 91             4.9592 V, 0.0001 A, 0.0005 W
← aa 05 07 0ca54501 01 1a01 4c                       2134.1452 Ω, +28.2 °C
← aa 08 11 01 43d94500 998e0d00 d0be0400 0e010000 b3 group 1, 45.77603 Wh, 8.88473 Ah, 310992 s, 270 s
```
