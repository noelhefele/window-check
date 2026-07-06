#!/usr/bin/env python3
"""window_check — should I open the windows?

Reads indoor CO2 + temperature/humidity from an Aranet4 (BLE), outdoor air
quality from WAQI (aqicn.org, US AQI scale), and weather/wind/pollen from
Open-Meteo, then decides whether to open, close, flush, or free-cool — and
manages the Levoit purifiers accordingly. Runs unattended via launchd.

Config:  ~/.config/window_check/config.json
State:   ~/.config/window_check/state.json
"""

import json
import os
import sys
import time
import math
import subprocess

CONFIG_DIR = os.path.expanduser("~/.config/window_check")
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")
STATE_PATH = os.path.join(CONFIG_DIR, "state.json")

REQUIRED = {
    "aranet_address": str,
    "waqi_token": str,
    "latitude": (int, float),
    "longitude": (int, float),
    "timezone": str,
    "window_orientations": list,
    "thresholds": dict,
    "pm25_alert_cooldown_hours": (int, float),
    "flush_alert_cooldown_hours": (int, float),
    "levoit_nudge_cooldown_hours": (int, float),
}
REQUIRED_THRESHOLDS = {
    "co2_alert", "co2_all_clear", "dew_point_f", "aqi_clean", "o3", "pm25",
    "free_cooling_indoor_min_f", "free_cooling_delta_f",
    "free_cooling_close_within_f", "free_cooling_done_below_f",
    "flush_indoor_rh_max", "flush_dew_delta_f", "flush_outdoor_temp_min_f",
    "pollen_grass", "pollen_birch", "pollen_ragweed", "wind_calm_mph",
}

# Compass bearing (degrees, met. "from" convention) for each window facing.
ORIENT_BEARING = {
    "north": 0, "northeast": 45, "east": 90, "southeast": 135,
    "south": 180, "southwest": 225, "west": 270, "northwest": 315,
    "n": 0, "ne": 45, "e": 90, "se": 135,
    "s": 180, "sw": 225, "w": 270, "nw": 315,
}
_COMPASS = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]
# Arrow points where the air is going (opposite the "from" direction).
_ARROWS = {"N": "↓", "NE": "↙", "E": "←", "SE": "↖",
           "S": "↑", "SW": "↗", "W": "→", "NW": "↘"}


def notify(message, title="Window check"):
    """Fire a macOS notification. Best-effort; never raises."""
    safe = message.replace('"', "'")
    try:
        subprocess.run(
            ["osascript", "-e",
             f'display notification "{safe}" with title "{title}"'],
            check=False,
        )
    except Exception as e:  # osascript missing / not on a Mac
        print(f"[notify failed] {e}", file=sys.stderr)


def die_config(msg):
    """Config is unusable: tell the user loudly and stop."""
    full = f"Config problem: {msg}"
    print(f"[window_check] {full}", file=sys.stderr)
    notify(f"{full} — see {CONFIG_PATH}", title="Window check BROKEN")
    sys.exit(1)


def load_config():
    if not os.path.exists(CONFIG_PATH):
        die_config(f"file not found at {CONFIG_PATH}")
    try:
        with open(CONFIG_PATH) as f:
            cfg = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        die_config(f"cannot parse {CONFIG_PATH}: {e}")

    if not isinstance(cfg, dict):
        die_config("top-level JSON must be an object")

    for key, typ in REQUIRED.items():
        if key not in cfg:
            die_config(f"missing required key '{key}'")
        if not isinstance(cfg[key], typ) or isinstance(cfg[key], bool):
            die_config(f"key '{key}' has wrong type (expected {typ})")

    missing_thr = REQUIRED_THRESHOLDS - set(cfg["thresholds"])
    if missing_thr:
        die_config(f"thresholds missing: {', '.join(sorted(missing_thr))}")
    for k in REQUIRED_THRESHOLDS:
        v = cfg["thresholds"][k]
        if not isinstance(v, (int, float)) or isinstance(v, bool):
            die_config(f"threshold '{k}' must be a number")

    if not cfg["window_orientations"]:
        die_config("window_orientations is empty")
    if not cfg["waqi_token"].strip():
        die_config("waqi_token is empty — get one at "
                   "https://aqicn.org/data-platform/token/")
    return cfg


def load_state():
    default = {"co2_alert_active": False, "last_pm25_alert_ts": 0.0,
               "cooling_open": False, "last_flush_alert_ts": 0.0,
               "last_levoit_nudge_ts": 0.0}
    try:
        with open(STATE_PATH) as f:
            s = json.load(f)
        if not isinstance(s, dict):
            raise ValueError
        default.update({k: s[k] for k in default if k in s})
    except (FileNotFoundError, json.JSONDecodeError, OSError, ValueError):
        pass  # corrupt/missing state → start fresh, don't crash the tool
    return default


def save_state(state):
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f)
    os.replace(tmp, STATE_PATH)  # atomic


def dew_point_f(temp_f, rh):
    """Magnus-formula dew point from temperature (F) and relative humidity (%)."""
    rh = max(1.0, min(100.0, rh))
    t_c = (temp_f - 32) * 5.0 / 9.0
    a, b = 17.625, 243.04
    gamma = math.log(rh / 100.0) + (a * t_c) / (b + t_c)
    td_c = (b * gamma) / (a - gamma)
    return td_c * 9.0 / 5.0 + 32


def compass(deg):
    return _COMPASS[int((deg % 360) / 45 + 0.5) % 8]


def _ang_dist(a, b):
    d = abs((a - b) % 360)
    return min(d, 360 - d)


def wind_guidance(orientations, wind_deg, mph, calm_mph):
    """Cross-ventilation guidance from wind source bearing + speed."""
    names = list(orientations)
    if mph < calm_mph:
        return f"open {' & '.join(names)} both, expect lazy flow"
    known = [(o, ORIENT_BEARING[o.lower()]) for o in names
             if o.lower() in ORIENT_BEARING]
    if len(known) < 2:
        return f"open your {' & '.join(names)} windows"
    known.sort(key=lambda ob: _ang_dist(wind_deg, ob[1]))
    intake, exhaust = known[0][0], known[-1][0]
    return f"{intake} windows intake, {exhaust} cracked as exhaust"


def main():
    cfg = load_config()
    thr = cfg["thresholds"]
    lat, lon = cfg["latitude"], cfg["longitude"]
    tz = cfg["timezone"]
    orientations = cfg["window_orientations"]
    now = time.time()

    # --- indoor CO2 + temp/RH (BLE) ---
    import aranet4
    try:
        reading = aranet4.client.get_current_readings(cfg["aranet_address"])
        co2 = reading.co2
        tin_c = reading.temperature   # °C
        rh_in = reading.humidity      # %
        if co2 is None:
            raise ValueError("Aranet returned no CO2 value")
    except Exception as e:
        # Transient BLE miss (device off/out of range) shouldn't spam.
        print(f"sensor read failed: {e}", file=sys.stderr)
        sys.exit(0)

    # --- outdoor: WAQI (AQI/PM2.5/O3) + Open-Meteo (weather, wind, pollen) ---
    import requests
    try:
        waqi = requests.get(
            f"https://api.waqi.info/feed/geo:{lat};{lon}/",
            params={"token": cfg["waqi_token"]}, timeout=20).json()
        if waqi.get("status") != "ok":
            raise RuntimeError(
                f"WAQI status={waqi.get('status')} ({waqi.get('data')})")
        wdata = waqi["data"]
        iaqi = wdata.get("iaqi", {})

        def sub(key):
            try:
                return iaqi[key]["v"]
            except (KeyError, TypeError):
                return None

        raw_aqi = wdata.get("aqi")
        if isinstance(raw_aqi, (int, float)) and not isinstance(raw_aqi, bool):
            aqi = int(raw_aqi)
        elif isinstance(raw_aqi, str) and raw_aqi.lstrip("-").isdigit():
            aqi = int(raw_aqi)
        else:
            aqi = None
        pm25 = sub("pm25")   # AQI scale
        o3 = sub("o3")       # AQI scale

        wx = requests.get(
            "https://api.open-meteo.com/v1/forecast",
            params={"latitude": lat, "longitude": lon,
                    "current": ("temperature_2m,relative_humidity_2m,"
                                "precipitation,wind_speed_10m,wind_direction_10m"),
                    "temperature_unit": "fahrenheit", "wind_speed_unit": "mph",
                    "timezone": tz}, timeout=20).json()["current"]
        pol = requests.get(
            "https://air-quality-api.open-meteo.com/v1/air-quality",
            params={"latitude": lat, "longitude": lon,
                    "current": "grass_pollen,birch_pollen,ragweed_pollen",
                    "timezone": tz}, timeout=20).json().get("current", {})
    except Exception as e:
        # Transient network / API hiccup shouldn't spam.
        print(f"outdoor data fetch failed: {e}", file=sys.stderr)
        sys.exit(0)

    # Outdoor scalars
    tout = round(wx["temperature_2m"])
    rh_out = wx["relative_humidity_2m"]
    dp = round(dew_point_f(tout, rh_out))       # outdoor dew point (F)
    precip = wx.get("precipitation") or 0
    wind_mph = round(wx.get("wind_speed_10m") or 0)
    wind_deg = wx.get("wind_direction_10m") or 0
    wdir, warrow = compass(wind_deg), _ARROWS[compass(wind_deg)]
    raining = precip > 0

    # Indoor scalars (-1 is the aranet4 "absent" sentinel)
    tin_f = round(tin_c * 9 / 5 + 32) if tin_c not in (None, -1) else None
    rh_in = rh_in if rh_in not in (None, -1) else None
    dp_in = (round(dew_point_f(tin_f, rh_in))
             if tin_f is not None and rh_in is not None else None)

    # Pollen (Open-Meteo pollen is Europe-only; None elsewhere → never blocks)
    pollen_vals = {"grass": pol.get("grass_pollen"),
                   "birch": pol.get("birch_pollen"),
                   "ragweed": pol.get("ragweed_pollen")}
    pollen_thr = {"grass": thr["pollen_grass"], "birch": thr["pollen_birch"],
                  "ragweed": thr["pollen_ragweed"]}
    pollen_hits = [k for k, v in pollen_vals.items()
                   if v is not None and v > pollen_thr[k]]
    pollen_high = bool(pollen_hits)

    # Gates
    pollutants_ok = (aqi is not None and aqi <= thr["aqi_clean"]
                     and (o3 is None or o3 < thr["o3"])
                     and (pm25 is None or pm25 < thr["pm25"]))
    aq_clean = pollutants_ok and not pollen_high
    open_allowed = aq_clean and not raining     # may we advise open/flush at all
    comfy = dp <= thr["dew_point_f"]            # incoming air feels good

    # --- instrument line ---
    it = f"{tin_f}" if tin_f is not None else "?"
    ih = f"{round(rh_in)}" if rh_in is not None else "?"
    ia = f"{aqi}" if aqi is not None else "?"
    print(f"⌂ {it}° {ih}% {co2}ppm │ ◌ {tout}° dew{dp}° AQI{ia} │ "
          f"{wdir}{warrow} {wind_mph}mph")

    state = load_state()
    changed = False
    spoke = False

    def verdict(line):
        nonlocal spoke
        spoke = True
        print(f"⇒ {line}")

    def guide():
        return wind_guidance(orientations, wind_deg, wind_mph, thr["wind_calm_mph"])

    # --- Smoke: PM2.5 unhealthy → hard close + purifiers HIGH (rate-limited) ---
    if pm25 is not None and pm25 >= thr["pm25"]:
        if now - state["last_pm25_alert_ts"] >= cfg["pm25_alert_cooldown_hours"] * 3600:
            body = ("Close up, smoke outside. Levoit back on: both units HIGH, "
                    "no timer — runs until the close-up clears.")
            verdict(body)
            notify(body, title="Smoke — close")
            state["last_pm25_alert_ts"] = now
            changed = True
        else:
            verdict("Smoke holding — stay shut, Levoit HIGH.")

    # --- Free cooling: hot inside, cool/clean/dry/comfortable out → open up ---
    if tin_f is not None:
        cool_ok = (tin_f >= thr["free_cooling_indoor_min_f"]
                   and tout <= tin_f - thr["free_cooling_delta_f"]
                   and open_allowed and comfy)
        if not state["cooling_open"]:
            if cool_ok:
                body = (f"Hot inside, cool out — open up, cut the AC. {guide()}. "
                        f"Levoit off while open.")
                verdict(body)
                notify(body, title="Free cooling")
                state["cooling_open"] = True
                changed = True
        elif tin_f < thr["free_cooling_done_below_f"]:
            verdict(f"Free cooling done — inside {tin_f}°; latch cleared.")
            state["cooling_open"] = False
            changed = True
        elif tout >= tin_f - thr["free_cooling_close_within_f"]:
            body = "Outside caught up — close up, back to AC. Levoit back on."
            verdict(body)
            notify(body, title="Close up")
            state["cooling_open"] = False
            changed = True
        else:
            verdict(f"Free cooling holding — inside {tin_f}°, out {tout}°.")

    # --- Dry-air flush: bone dry inside, damper out → short flush (throttled) ---
    if rh_in is not None and dp_in is not None:
        flush_ok = (rh_in <= thr["flush_indoor_rh_max"]
                    and dp >= dp_in + thr["flush_dew_delta_f"]
                    and tout >= thr["flush_outdoor_temp_min_f"]
                    and open_allowed)
        if flush_ok:
            if now - state["last_flush_alert_ts"] >= cfg["flush_alert_cooldown_hours"] * 3600:
                body = (f"Bone dry inside ({round(rh_in)}%) — 10–15 min flush to "
                        f"add moisture, then close before the heat bleeds. "
                        f"{guide()}. Levoit off while open.")
                verdict(body)
                notify(body, title="Flush")
                state["last_flush_alert_ts"] = now
                changed = True
            else:
                verdict(f"Flush conditions hold ({round(rh_in)}%) — holding.")

    # --- CO2 with hysteresis latch ---
    if co2 >= thr["co2_alert"]:
        if open_allowed and comfy:
            body = f"CO₂ high and it's clean out — open up. {guide()}. Levoit off while open."
            title = "Open up"
        elif pollen_high:
            body = "CO₂ high but it's a pollen day — run the Levoit, keep shut. MEDIUM, both rooms."
            title = "Pollen day"
        elif o3 is not None and o3 >= thr["o3"]:
            body = "CO₂ high and ozone's up — keep shut, run the Levoit."
            title = "Ozone day"
        elif raining:
            body = "CO₂ high but it's raining — keep shut, run the Levoit."
            title = "Close up"
        else:
            body = "CO₂ high, outside won't help — keep shut, run the Levoit."
            title = "Close up"
        if not state["co2_alert_active"]:
            verdict(body)
            notify(body, title=title)
            state["co2_alert_active"] = True
            changed = True
        else:
            verdict(f"CO₂ still {co2}ppm — already flagged, holding.")
    elif co2 < thr["co2_all_clear"] and state["co2_alert_active"]:
        state["co2_alert_active"] = False  # reset latch below all-clear
        changed = True

    # --- Moderate-band Levoit nudge (log only, windows presumed shut) ---
    if (pm25 is not None and 50 <= pm25 < 100 and not state["cooling_open"]
            and not spoke):
        if now - state["last_levoit_nudge_ts"] >= cfg["levoit_nudge_cooldown_hours"] * 3600:
            verdict(f"PM2.5 AQI {pm25}, windows shut — worth running the Levoit: "
                    f"LOW, 2h timer is plenty.")
            state["last_levoit_nudge_ts"] = now
            changed = True

    # --- Fallback status line ---
    if not spoke:
        if raining:
            verdict("raining — windows stay shut.")
        elif pollen_high:
            verdict(f"pollen high ({', '.join(pollen_hits)}) — windows stay shut.")
        else:
            verdict(f"Fresh — CO₂ {co2}ppm, nothing to do.")

    if changed:
        save_state(state)


if __name__ == "__main__":
    main()
