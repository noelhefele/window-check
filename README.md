# window-check

Tells you when to open (or close) your windows, based on indoor CO₂ and outdoor
air quality. Runs unattended on macOS via launchd and pings you with a
notification only when there's something to do.

## What it does

Every 30 minutes it reads:

- **Indoor CO₂** from an [Aranet4](https://aranet.com/products/aranet4/) sensor over Bluetooth
- **Outdoor air quality** (AQI, PM2.5, O₃) from [WAQI](https://aqicn.org) — US AQI scale
- **Temperature & humidity** (for dew point) from [Open-Meteo](https://open-meteo.com)

…then notifies you when:

- CO₂ is high **and** outside is clean & comfortable → **open the windows**
- CO₂ is high but outside is poor → **run the purifier, keep them closed**
- Outdoor PM2.5 spikes → **close the windows**
- Hot inside, cooler & clean & comfortable outside → **free cooling: open up,
  cut the AC** (and it tells you when to close back up)
- Bone-dry inside, damper outside → **short flush to add some moisture**

All the "open" triggers gate comfort on the **outdoor dew point** (≤ your
threshold) — that's what decides whether incoming air actually feels good.

It uses hysteresis so it won't nag: one CO₂ alert until levels recover (drop
below the all-clear), the free-cooling advice latches until it's done, PM2.5
alerts at most once every 2 hours, dry-flush at most once every 6, and "all
good" is silent (stdout only).

## Requirements

- macOS with Python 3
- An **Aranet4** CO₂ sensor
- A free **WAQI API token** — request one at <https://aqicn.org/data-platform/token/>
- Python packages: `aranet4`, `requests`

## Setup

1. `pip install aranet4 requests`
2. `mkdir -p ~/.config/window_check`
3. Copy `config.example.json` → `~/.config/window_check/config.json` and fill in:
   - `aranet_address` — your Aranet4's BLE address/UUID
   - `waqi_token` — your WAQI token
   - `latitude` / `longitude` — your location
   - `window_orientations` — which windows to name in alerts
   - `thresholds` — tune to taste. **All air-quality thresholds are on the US
     AQI scale** (`aqi_clean`, `o3`, `pm25`); `co2_*` are ppm; `dew_point_f` is °F.
4. Test it: `python3 window_check.py`

The script fails loudly (macOS notification + non-zero exit) if the config is
missing or malformed.

## Scheduling (launchd)

Create a LaunchAgent that runs `window_check.py` every `1800` seconds with
`RunAtLoad`, sending stdout/stderr to `~/.config/window_check/log.txt`, then:

```sh
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.you.windowcheck.plist
launchctl kickstart -k gui/$(id -u)/com.you.windowcheck   # run once now
```

### macOS Bluetooth note

macOS aborts a bare Python interpreter the instant it touches Bluetooth if the
process lacks an `NSBluetoothAlwaysUsageDescription` usage string. The reliable
fix is to run the script through a small app bundle (or framework `Python.app`)
that declares that key; it then works cleanly under launchd's GUI domain.

## Privacy

Your real `config.json` (WAQI token + location), plus `state.json` and
`log.txt`, live under `~/.config/window_check/` and are **git-ignored** — only
the placeholder `config.example.json` is tracked here.
