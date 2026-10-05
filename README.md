# FNB48 Web UI

A browser replacement for the FNIRSI FNB48 Android app. It talks to the meter directly over Web Bluetooth.
For the reverse-engineered protocol, see [PROTOCOL.md](PROTOCOL.md).

## Run it

```sh
python3 -m http.server 8000 --directory web
```

Then open <http://localhost:8000> in Chrome or Edge and click **Connect**.

* **Linux desktop:** Web Bluetooth is behind a flag. Enable `chrome://flags/#enable-experimental-web-platform-features`
  and restart the browser. BlueZ must be running.
* **Android:** Chrome supports Web Bluetooth natively, but the page has to come from HTTPS (for example GitHub Pages or any
  static host), because `localhost` on the phone isn't this PC.
  The first connection takes about 2 seconds longer than on desktop, because the page works around a bug in the meter's
  Bluetooth module that otherwise leaves Android with no services (see [PROTOCOL.md](PROTOCOL.md#android-service-discovery-bug)).
* Firefox and Safari don't support Web Bluetooth.

**Reconnecting on Linux:** BlueZ forgets an unpaired device about 30 s after it last heard it advertise, and Chrome then
reports "no longer in range". When that happens, Connect scans for up to 60 s (status **searching**, button **Cancel**)
and reconnects as soon as BlueZ hears the meter again, usually within 10–15 s. It only scans while that search is running.
To avoid the wait entirely, mark the meter as trusted once: run `bluetoothctl`, then `scan on`, then
`trust <meter address>`.

Everything that gets published lives in `web/`: plain static files with no build step and no external dependencies.
`fnb48.js` holds the protocol and Web Bluetooth driver, and `index.html` holds the UI.

## Install as an app (PWA)

The page is a Progressive Web App: it has a manifest (`web/manifest.webmanifest`), icons (`web/icons/`) and a service
worker (`web/sw.js`) that caches everything, so once installed it launches and runs with no network.

1. Host the `web/` folder on any static HTTPS host (see GitHub Pages below).
2. Open the URL in Chrome on your phone, then use **⋮ → Add to Home screen → Install**.
3. It now opens full-screen from its own icon, offline.

You can also skip hosting and install it over USB with Chrome port forwarding:

1. Turn on USB debugging on the phone and plug it into the PC.
2. On the PC, open `chrome://inspect/#devices` and forward port `8000` to `localhost:8000`.
3. With `python3 -m http.server 8000 --directory web` running on the PC, open `http://localhost:8000` on the phone and install it.

Because the service worker cached it, the installed app keeps working after you unplug.

**Updating:** the service worker fetches from the network first and only uses its cache when offline, so a normal
reload always picks up changes. Bump `APP_VERSION` in `web/version.js` on each release: it rebuilds the offline cache and is shown first in the
Diagnostics panel, so you can tell which version is running.

## Publishing on GitHub Pages

`.github/workflows/pages.yml` publishes only the `web/` folder on every push to `main`. The docs, the CLI and the
workflow itself are not published.

1. Push the repo to GitHub. On a free account the repo must be public for Pages to work.
2. In the repo, open **Settings → Pages**. Under "Build and deployment", set **Source** to **GitHub Actions**.
3. Push to `main`, or run the workflow by hand from the **Actions** tab. The site appears at
   `https://<user>.github.io/<repo>/`.

## Features

* Live voltage, current and power (0.1 mV / 0.1 mA), plus temperature, equivalent resistance, D+/D− and the detected charge protocol
* Live V/A/W trace with selectable windows (10 s to 1 h), min/max/avg, hover readout and freeze
* On-device energy groups: switch groups, clear a group, and read every group's Wh/Ah with a battery-capacity estimate
* Fast-charge trigger (QC2.0, QC3.0, FCP, AFC)
* Voltage and current alarms (highlight and/or beep)
* Record to CSV

## CLI alternative

`fnb48.py` logs readings as CSV using [bleak](https://github.com/hbldh/bleak):

```sh
pip install bleak
./fnb48.py > log.csv           # 0.1 mV/mA readings at ~4 Hz
./fnb48.py --fast > log.csv    # 1 mV/mA samples at ~8-10 Hz
```
