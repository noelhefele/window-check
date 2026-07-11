# window-check

Tells you when to open (or close) your windows, based on indoor CO₂ and outdoor
air quality. Runs unattended on macOS via launchd and pings you with a
notification only when there's something to do.

## What it does

Every 30 minutes it reads:

- **Indoor CO₂, temperature & humidity** from an [Aranet4](https://aranet.com/products/aranet4/) sensor over Bluetooth
- **Outdoor air quality** (AQI, PM2.5, O₃) from [WAQI](https://aqicn.org) — US AQI scale
- **Weather, wind & pollen** from [Open-Meteo](https://open-meteo.com) (temp/humidity → dew point, wind for cross-ventilation, precipitation, grass/birch/ragweed pollen)

…then notifies you when:

- CO₂ is high **and** outside is clean & comfortable → **open up**
- CO₂ is high but outside is poor (ozone, pollen, rain…) → **keep shut, run the Levoit**
- Outdoor PM2.5 spikes → **close up, purifiers HIGH**
- Hot inside, cooler & clean & comfortable outside → **free cooling: open up,
  cut the AC** (and it tells you when to close back up)
- Bone-dry inside, damper outside → **short flush to add some moisture**

Open/flush verdicts carry **cross-ventilation guidance** from the live wind
(which window is intake, which is exhaust; both if it's calm), and each verdict
manages the **Levoit** purifiers (off while windows are open, back on when they
close, HIGH for smoke, MEDIUM for pollen, a LOW-timer nudge in the moderate PM2.5
band).

The **clean gate** blocks "open" advice on high AQI, ozone, PM2.5, **high pollen**
(grass/tree/weed over config thresholds, from Google's Pollen API on a 0–5 index),
or **rain** — logging why, without a banner. "Open" triggers gate comfort on the
**outdoor dew point** (raised a few degrees once there's a real breeze — moving air
feels cooler). At **CO₂ ≥ 2000** an urgent tier overrides every gate but smoke and
repeats until you ventilate.

A quiet **interoception backstop** watches indoor comfort directly: banners if it's
too hot, muggy, or (in heating season) too cold inside — at most once per 4h each.

It uses hysteresis so it won't nag: one CO₂ alert until levels recover, the
free-cooling advice latches until it's done, PM2.5 alerts at most once every 2h,
dry-flush every 6h, the Levoit nudge every 4h, and "all good" is silent.

Each run logs a one-line instrument readout followed by the verdict; banners are
posted as **WindowCheck** (configurable in System Settings ▸ Notifications) with a
one-glyph state title and a short system sound (`notify_sound`, default *Blow* —
the urgent tier and smoke use `notify_sound_urgent`, default *Basso*). Advice is
self-invalidating, so each run's banner **supersedes** the previous one —
Notification Center shows the current verdict, not a growing stack of stale cards:

```
⌂ 83° 64% 526ppm │ ◌ 71° dew67° AQI55 │ E← 4mph
⇒ PM2.5 AQI 55, windows shut — worth running the Levoit: LOW, 2h timer is plenty.
```

> **Pollen:** uses Google's Pollen API (free tier, US coverage). If the key is
> missing or the call fails, pollen is treated as *unknown* and never blocks the
> other triggers (fail-soft).

## Requirements

- macOS with Python 3
- An **Aranet4** CO₂ sensor
- A free **WAQI API token** — <https://aqicn.org/data-platform/token/>
- A **Google Pollen API key** (optional; free tier) — <https://developers.google.com/maps/documentation/pollen>
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
