# Zedd-Satellite

A GIS implementation of Zedd-Weather.

`Zedd-Satellite` (a.k.a. **Z-weather-satellite**) is a Raspberry Pi based ground
station that automatically tracks, captures, and decodes imagery from polar
orbiting weather satellites (NOAA 15/18/19 APT and Meteor-M2 LRPT) using an
RTL-SDR dongle.

The project is composed of a small Python orchestration layer that:

1. Downloads up-to-date TLE (Two-Line Element) orbital data.
2. Computes upcoming satellite passes for the configured ground station.
3. Schedules captures at the exact AOS (Acquisition Of Signal) time.
4. Drives `rtl_fm` (and `sox`) to record the pass to a `.wav` file.
5. Routes the recording to `wxtoimg` (NOAA APT) or `meteor-demod`
   (Meteor LRPT) to produce the final `.png` image.

---

## Hardware Requirements

| Component             | Recommended                                                 |
|-----------------------|-------------------------------------------------------------|
| Single-board computer | Raspberry Pi 5 (8 GB) running Raspberry Pi OS (Bookworm)    |
| Cooling               | Official Pi 5 active cooler (mandatory under decode load)   |
| PSU                   | Official 27 W USB-C PD power supply                         |
| Storage               | NVMe HAT + 256 GB NVMe (primary) + microSD (mirror)         |
| SDR (primary)         | RTL-SDR Blog v3 dongle (TCXO + bias-tee)                    |
| SDR (redundant)       | A second RTL-SDR Blog v3 listed in `sdr.device_indexes`     |
| LNA + filter          | Nooelec SAWbird+ NOAA (137 MHz, bias-tee powered)           |
| Antenna               | QFH (Quadrifilar Helix) tuned for 137 MHz                   |
| Cabling               | RG-58 (≤5 m) or LMR-240 (longer); SMA jumpers               |
| Lightning protection  | Gas-discharge surge arrestor (DC-pass for bias-tee)         |
| GPS receiver          | u-blox 7/8 USB dongle (used by `gpsd` and chrony)           |
| LoRa transceiver      | Waveshare / RAK SX1262 LoRa HAT (region-correct ISM band)   |
| Out-of-band antenna   | 433 / 868 / 915 MHz vertical or Yagi for the LoRa HAT       |

## Software Requirements

* Raspberry Pi OS (or any Debian/Ubuntu derivative).
* Python **3.9 or newer**.
* The following CLI tools available on `$PATH`:
  * `rtl_fm`   – from the `rtl-sdr` package (records RF to PCM audio).
  * `rtl_biast` – toggles the dongle bias-tee that powers the LNA.
  * `sox`      – audio resampling for the decoders.
  * `wxtoimg`  – primary NOAA APT decoder.
  * `noaa-apt` – open-source fallback NOAA decoder (redundancy).
  * `meteor-demod` – primary Meteor-M2 LRPT decoder.
  * `medet`    – fallback Meteor decoder (redundancy).
  * `gpsd` + `gpsd-clients` – exposes the USB GPS receiver to the daemon.
  * `vcgencmd` (preinstalled) – Pi throttle / under-voltage status.

## Repository Layout

```text
Zedd-Satellite/
├── config/
│   └── settings.json         # Station Lat/Lon, SDR gain, TLE URLs, satellites
├── core/
│   ├── __init__.py
│   ├── tracker.py            # Fetches TLEs and computes upcoming passes
│   ├── capture.py            # Wraps rtl_fm to record a pass to WAV
│   ├── decoder.py            # Decodes WAV -> PNG (NOAA / Meteor)
│   ├── gps.py                # Real GPS reader (gpsd primary, NMEA serial fallback)
│   ├── lora.py               # SX1262 LoRa HAT driver (heartbeat / payload RX)
│   ├── health.py             # Pi 5 thermal / throttle / disk / SDR monitor
│   └── storage.py            # Mirrors output to redundant directories
├── scripts/
│   ├── update_tle.sh         # Downloads the latest TLE files
│   └── setup_env.sh          # Installs apt + python dependencies
├── logs/                     # Runtime log files
├── output/                   # Captured WAVs and decoded PNGs
├── main.py                   # Daemon entry point (scheduler loop)
├── requirements.txt          # Python dependencies
└── README.md
```

## Quick Start

```bash
# 1. Clone the repo on the Raspberry Pi
git clone https://github.com/Zedd-Weather/Zedd-Satellite.git
cd Zedd-Satellite

# 2. Install system + python dependencies
bash scripts/setup_env.sh

# 3. Edit your station coordinates and SDR settings
nano config/settings.json

# 4. Pull fresh TLEs (cron this every 12 h in production)
bash scripts/update_tle.sh

# 5. Run the daemon (foreground)
python3 main.py
```

To run as a background service, create a `systemd` unit that executes
`python3 /opt/Zedd-Satellite/main.py` with the project directory as
`WorkingDirectory`.

## Configuration

All tunable parameters live in `config/settings.json`:

* `station` – latitude, longitude, elevation (m) of your antenna.
* `sdr` – `gain`, `sample_rate`, `ppm_correction`, plus
  `device_indexes` (list, primary + redundant dongles tried in order)
  and `bias_tee` (powers the SAWbird+ NOAA LNA up the coax via
  `rtl_biast`).
* `pass_filter` – `min_elevation_deg` (default `20`) and look-ahead window.
* `satellites` – per-satellite frequency (MHz), decoding mode
  (`NOAA_APT` / `METEOR_LRPT`), and the name used to look the bird up in
  the TLE catalog.
* `tle` – list of remote TLE URLs (e.g. CelesTrak weather group) and the
  local cache file path.
* `decoder` – primary binary plus `noaa_fallbacks` / `meteor_fallbacks`
  lists. Each fallback is tried in order if the primary fails.
* `storage.mirror_dirs` – list of redundant directories (e.g. an
  external SSD and the microSD) that every captured WAV / decoded PNG
  is mirrored into.
* `gps` – real-hardware GPS configuration. When `enabled` the daemon
  reads a fix on startup (gpsd first, raw serial NMEA as fallback) and,
  if `discipline_station` is true, overrides `station.*` with the live
  coordinates. A clock-drift warning is logged if the system clock
  differs from GPS time by more than `max_clock_drift_s`.
* `lora` – SX1262 LoRa HAT settings (SPI bus, GPIO pins, RF parameters,
  heartbeat cadence). When `enabled` the daemon transmits a compact
  JSON health snapshot every `heartbeat_period_s` seconds.
* `health` – thresholds for CPU temperature warnings and free-disk
  warnings, plus the snapshot cadence used when LoRa is disabled.

## Output

After a successful pass you will find, in `output/`:

* `NOAA_19_2025-04-23T18-12-04Z.wav` – the raw FM audio recording.
* `NOAA_19_2025-04-23T18-12-04Z.png` – the decoded image.

Logs are written to `logs/zedd-satellite.log` (rotated daily).

## License

MIT — see the repository `LICENSE` file for details.
