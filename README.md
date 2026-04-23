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

| Component             | Recommended                                               |
|-----------------------|-----------------------------------------------------------|
| Single-board computer | Raspberry Pi 4 (2 GB+) running Raspberry Pi OS (Bookworm) |
| SDR                   | RTL-SDR Blog v3 dongle                                    |
| Antenna               | QFH (Quadrifilar Helix) or V-Dipole tuned for 137 MHz     |
| Filter (optional)     | SAWbird+ NOAA / Meteor LNA                                |
| Cabling               | Low-loss coax (RG-58 or better), short run if possible    |
| Storage               | 16 GB+ SD card or external SSD                            |

## Software Requirements

* Raspberry Pi OS (or any Debian/Ubuntu derivative).
* Python **3.9 or newer**.
* The following CLI tools available on `$PATH`:
  * `rtl_fm`   – from the `rtl-sdr` package (records RF to PCM audio).
  * `sox`      – audio resampling to 11025 Hz for `wxtoimg`.
  * `wxtoimg`  – NOAA APT decoder.
  * `meteor-demod` (or `medet`) – Meteor-M2 LRPT decoder.

## Repository Layout

```text
Zedd-Satellite/
├── config/
│   └── settings.json         # Station Lat/Lon, SDR gain, TLE URLs, satellites
├── core/
│   ├── __init__.py
│   ├── tracker.py            # Fetches TLEs and computes upcoming passes
│   ├── capture.py            # Wraps rtl_fm to record a pass to WAV
│   └── decoder.py            # Decodes WAV -> PNG (NOAA / Meteor)
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
* `sdr` – `gain`, `sample_rate`, `ppm_correction`, `device_index`.
* `pass_filter` – `min_elevation_deg` (default `20`) and look-ahead window.
* `satellites` – per-satellite frequency (MHz), decoding mode
  (`NOAA_APT` / `METEOR_LRPT`), and the name used to look the bird up in
  the TLE catalog.
* `tle` – list of remote TLE URLs (e.g. CelesTrak weather group) and the
  local cache file path.

## Output

After a successful pass you will find, in `output/`:

* `NOAA_19_2025-04-23T18-12-04Z.wav` – the raw FM audio recording.
* `NOAA_19_2025-04-23T18-12-04Z.png` – the decoded image.

Logs are written to `logs/zedd-satellite.log` (rotated daily).

## License

MIT — see the repository `LICENSE` file for details.
