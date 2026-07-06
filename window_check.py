#!/usr/bin/env python3
"""window_check — should I open the windows?

Reads indoor CO2 from an Aranet4 (BLE), outdoor air quality from WAQI
(aqicn.org, AQI-scale, needs a token), and temperature/humidity from Open-Meteo
(for dew point), then decides whether opening the windows would help. Runs
unattended via launchd every 30 minutes.

All tunables live in ~/.config/window_check/config.json.
Alert de-duplication state lives in ~/.config/window_check/state.json.
"""

import json
import os
import sys
import time
import math
import subprocess
from datetime import datetime

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
}
REQUIRED_THRESHOLDS = {
    "co2_alert", "co2_all_clear", "dew_point_f", "aqi_clean", "o3", "pm25",
}


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
    default = {"co2_alert_active": False, "last_pm25_alert_ts": 0.0}
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


def main():
    cfg = load_config()
    thr = cfg["thresholds"]
    lat, lon = cfg["latitude"], cfg["longitude"]
    tz = cfg["timezone"]
    orient = " & ".join(cfg["window_orientations"])
    now = time.time()
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")

    # --- indoor CO2 (BLE) ---
    import aranet4
    try:
        reading = aranet4.client.get_current_readings(cfg["aranet_address"])
        co2 = reading.co2
        if co2 is None:
            raise ValueError("Aranet returned no CO2 value")
    except Exception as e:
        # A transient BLE miss (device off/out of range) shouldn't spam
        # notifications every 30 min. Log and exit quietly.
        print(f"[{stamp}] sensor read failed: {e}", file=sys.stderr)
        sys.exit(0)

    # --- outdoor air quality (WAQI, AQI-scale) + weather (Open-Meteo) ---
    # WAQI reports the overall AQI and per-pollutant sub-indices, all on the
    # US AQI scale (0-500), so thresholds are AQI numbers, not concentrations.
    import requests
    try:
        waqi = requests.get(
            f"https://api.waqi.info/feed/geo:{lat};{lon}/",
            params={"token": cfg["waqi_token"]},
            timeout=20,
        ).json()
        if waqi.get("status") != "ok":
            raise RuntimeError(
                f"WAQI status={waqi.get('status')} ({waqi.get('data')})")
        data = waqi["data"]
        iaqi = data.get("iaqi", {})

        def sub(key):  # per-pollutant AQI sub-index, or None if not reported
            try:
                return iaqi[key]["v"]
            except (KeyError, TypeError):
                return None

        raw_aqi = data.get("aqi")
        if isinstance(raw_aqi, (int, float)) and not isinstance(raw_aqi, bool):
            aqi = int(raw_aqi)
        elif isinstance(raw_aqi, str) and raw_aqi.lstrip("-").isdigit():
            aqi = int(raw_aqi)
        else:
            aqi = None  # station reporting "-" / no overall AQI
        pm25 = sub("pm25")  # AQI scale
        o3 = sub("o3")      # AQI scale
        station = data.get("city", {}).get("name", "?")

        wx = requests.get(
            "https://api.open-meteo.com/v1/forecast",
            params={"latitude": lat, "longitude": lon,
                    "current": "temperature_2m,relative_humidity_2m",
                    "temperature_unit": "fahrenheit", "timezone": tz},
            timeout=20,
        ).json()["current"]
    except Exception as e:
        # Transient network / API hiccup shouldn't spam notifications.
        print(f"[{stamp}] outdoor data fetch failed: {e}", file=sys.stderr)
        sys.exit(0)

    tout = round(wx["temperature_2m"])
    rh = wx["relative_humidity_2m"]
    dp = round(dew_point_f(tout, rh))

    aqi_s = aqi if aqi is not None else "n/a"
    pm25_s = pm25 if pm25 is not None else "n/a"
    o3_s = round(o3) if o3 is not None else "n/a"
    outside = (f"AQI {aqi_s}, PM2.5 {pm25_s} (AQI), O3 {o3_s} (AQI), "
               f"{tout}F, dew point {dp}F")
    print(f"[{stamp}] Indoor CO2: {co2} ppm  |  Outside: {outside}  "
          f"[WAQI: {station}]")

    # Require a valid overall AQI before ever recommending "open"; missing
    # per-pollutant sub-indices don't block (overall AQI already caps them).
    open_ok = (aqi is not None and aqi <= thr["aqi_clean"]
               and (o3 is None or o3 < thr["o3"])
               and (pm25 is None or pm25 < thr["pm25"])
               and dp <= thr["dew_point_f"])

    state = load_state()
    changed = False

    # --- PM2.5 close-up alert (independent of CO2, rate-limited) ---
    cooldown = cfg["pm25_alert_cooldown_hours"] * 3600
    if pm25 is not None and pm25 >= thr["pm25"]:
        if now - state["last_pm25_alert_ts"] >= cooldown:
            msg = (f"CLOSE your {orient} windows — outdoor PM2.5 is unhealthy "
                   f"(PM2.5 AQI {pm25}, overall AQI {aqi_s}).")
            print("=>", msg)
            notify(msg)
            state["last_pm25_alert_ts"] = now
            changed = True
        else:
            print(f"=> PM2.5 high (AQI {pm25}) but alerted recently — suppressed.")

    # --- CO2 alert with hysteresis latch ---
    if co2 >= thr["co2_alert"]:
        if open_ok:
            msg = (f"OPEN your {orient} windows — CO2 {co2} ppm, and outside "
                   f"is clean & comfortable ({outside}).")
        else:
            msg = (f"CO2 is high ({co2} ppm) but outside isn't great "
                   f"({outside}) — run the purifier, keep windows closed.")
        if not state["co2_alert_active"]:
            print("=>", msg)
            notify(msg)
            state["co2_alert_active"] = True
            changed = True
        else:
            print(f"=> [CO2 still {co2}, already alerted — suppressed] {msg}")
    else:
        if co2 < thr["co2_all_clear"] and state["co2_alert_active"]:
            state["co2_alert_active"] = False  # reset latch below all-clear
            changed = True
        if pm25 is None or pm25 < thr["pm25"]:
            print(f"=> All good — CO2 {co2} ppm is fresh. No need to open.")

    if changed:
        save_state(state)


if __name__ == "__main__":
    main()
