#!/usr/bin/env python3
"""window_check — should I open the windows?

Reads indoor CO2 + temperature/humidity from an Aranet4 (BLE), outdoor air
quality from WAQI (aqicn.org, US AQI scale), weather/wind from Open-Meteo, and
pollen from Google's Pollen API (0-5 index), then decides whether to open,
close, flush, or free-cool — and manages the Levoit purifiers accordingly.
Also a quiet interoception backstop for indoor comfort. Runs via launchd.

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
    "google_pollen_key": str,
    "notify_sound": str,
    "notify_sound_urgent": str,
    "latitude": (int, float),
    "longitude": (int, float),
    "timezone": str,
    "window_orientations": list,
    "thresholds": dict,
    "banner_glyphs": dict,
    "heating_season_months": list,
    "pm25_alert_cooldown_hours": (int, float),
    "flush_alert_cooldown_hours": (int, float),
    "levoit_nudge_cooldown_hours": (int, float),
    "intero_alert_cooldown_hours": (int, float),
    "co2_urgent_repeat_minutes": (int, float),
    "ble_timeout_seconds": (int, float),
}
REQUIRED_THRESHOLDS = {
    "co2_alert", "co2_all_clear", "co2_urgent", "dew_point_f",
    "dew_point_breeze_f", "breeze_mph", "aqi_clean", "o3", "pm25",
    "free_cooling_indoor_min_f", "free_cooling_delta_f",
    "free_cooling_close_within_f", "free_cooling_done_below_f",
    "flush_indoor_rh_max", "flush_dew_delta_f", "flush_outdoor_temp_min_f",
    "pollen_grass", "pollen_tree", "pollen_weed", "wind_calm_mph",
    "intero_hot_f", "intero_dew_f", "intero_cold_f", "levoit_banner_pm25",
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

_GLYPHS = {}              # populated from config; maps banner title → glyph
_SOUND = "Glass"          # default notification sound (from config)
_SOUND_URGENT = "Sosumi"  # urgent-tier sound: CO2 ≥ 2000 / smoke (from config)
_first_notify = True      # clear stale delivered banners once per run
_played = set()           # sound names already played this run (dedupe chimes)


def _play_sound(name):
    """Play a named system sound via afplay (blocking, ~1-2s). macOS 26 drops
    the deprecated notification API's own sound, so we play it ourselves."""
    for base in ("/System/Library/Sounds",
                 os.path.expanduser("~/Library/Sounds"), "/Library/Sounds"):
        path = os.path.join(base, f"{name}.aiff")
        if os.path.exists(path):
            try:
                subprocess.run(["afplay", path], timeout=6,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except Exception:
                pass
            return


def notify(message, title="Window check", urgent=False):
    """Post a notification named 'WindowCheck' via the app bundle (osascript
    fallback), and play its sound ourselves via afplay — macOS 26 ignores the
    deprecated API's setSoundName. Title is prefixed with its config glyph;
    urgent states (CO2-urgent / smoke) use the urgent sound.

    Advice is self-invalidating, so before this run's first banner we clear our
    previously-delivered ones — Notification Center shows only the current
    verdict instead of a growing stack of stale cards."""
    global _first_notify
    disp = f"{_GLYPHS.get(title, '')} {title}".strip()
    snd = _SOUND_URGENT if urgent else _SOUND
    if snd not in _played:          # one chime per sound per run, not per banner
        _played.add(snd)
        _play_sound(snd)
    try:
        from Foundation import (NSUserNotification, NSUserNotificationCenter,
                                NSRunLoop, NSDate)
        center = NSUserNotificationCenter.defaultUserNotificationCenter()
        if center is not None:
            if _first_notify:
                center.removeAllDeliveredNotifications()
                _first_notify = False
            n = NSUserNotification.alloc().init()
            n.setTitle_(disp)
            n.setInformativeText_(message)
            center.deliverNotification_(n)
            # brief run-loop tick so the center actually delivers before exit
            NSRunLoop.currentRunLoop().runUntilDate_(
                NSDate.dateWithTimeIntervalSinceNow_(0.3))
            return
    except Exception:
        pass  # not in a bundle / API unavailable → fall back
    safe, st = message.replace('"', "'"), disp.replace('"', "'")
    try:
        subprocess.run(["osascript", "-e",
                        f'display notification "{safe}" with title "{st}"'],
                       check=False)
    except Exception as e:
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
               "last_levoit_nudge_ts": 0.0, "last_co2_urgent_ts": 0.0,
               "last_intero_hot_ts": 0.0, "last_intero_muggy_ts": 0.0,
               "last_intero_cold_ts": 0.0, "last_advisory_date": "",
               "sensor_fail_count": 0}
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


def fetch_pollen(cfg, lat, lon, thr):
    """Google Pollen API (0-5 index). Fail soft: returns (high, hits, known).
    On missing key or any error, pollen is treated as unknown (never blocks)."""
    key = cfg.get("google_pollen_key", "").strip()
    if not key:
        print("pollen: no google_pollen_key set — treating as unknown",
              file=sys.stderr)
        return False, [], False
    try:
        import requests
        pj = requests.get(
            "https://pollen.googleapis.com/v1/forecast:lookup",
            params={"key": key, "location.latitude": lat,
                    "location.longitude": lon, "days": 1},
            timeout=20).json()
        if "error" in pj:
            raise RuntimeError(pj["error"].get("message", "API error"))
        daily = pj.get("dailyInfo", [])
        if not daily:
            return False, [], False
        types = {t.get("code"): (t.get("indexInfo") or {}).get("value")
                 for t in daily[0].get("pollenTypeInfo", [])}
        vals = {"grass": types.get("GRASS"), "tree": types.get("TREE"),
                "weed": types.get("WEED")}
        limits = {"grass": thr["pollen_grass"], "tree": thr["pollen_tree"],
                  "weed": thr["pollen_weed"]}
        hits = [k for k, v in vals.items() if v is not None and v > limits[k]]
        return bool(hits), hits, True
    except Exception as e:
        print(f"pollen fetch failed (soft): {e}", file=sys.stderr)
        return False, [], False


def fetch_forecast(lat, lon, tz):
    """Today's forecast peaks from Open-Meteo hourly pollutant AQIs.
    Returns (o3_max, o3_peak_hour, pm25_max) — all None on any failure
    (fail-soft: no forecast just means no advisory banner)."""
    try:
        import requests
        h = requests.get(
            "https://air-quality-api.open-meteo.com/v1/air-quality",
            params={"latitude": lat, "longitude": lon,
                    "hourly": "us_aqi_ozone,us_aqi_pm2_5",
                    "forecast_days": 1, "timezone": tz},
            timeout=20).json().get("hourly", {})
        times = h.get("time", [])
        o3s = h.get("us_aqi_ozone", [])
        pms = [v for v in h.get("us_aqi_pm2_5", []) if v is not None]
        o3_valid = [(v, t) for v, t in zip(o3s, times) if v is not None]
        if not o3_valid and not pms:
            return None, None, None
        o3_max, o3_peak = max(o3_valid) if o3_valid else (None, None)
        peak_hr = None
        if o3_peak:
            hr = int(o3_peak[11:13])
            peak_hr = ("%d%s" % (hr % 12 or 12, "am" if hr < 12 else "pm"))
        return o3_max, peak_hr, (max(pms) if pms else None)
    except Exception as e:
        print(f"forecast fetch failed (soft): {e}", file=sys.stderr)
        return None, None, None


def main():
    cfg = load_config()
    thr = cfg["thresholds"]
    lat, lon = cfg["latitude"], cfg["longitude"]
    tz = cfg["timezone"]
    orientations = cfg["window_orientations"]
    global _SOUND, _SOUND_URGENT, _first_notify
    _GLYPHS.clear()
    _GLYPHS.update(cfg["banner_glyphs"])
    _SOUND = cfg["notify_sound"]
    _SOUND_URGENT = cfg["notify_sound_urgent"]
    _first_notify = True
    _played.clear()
    now = time.time()

    # --- indoor CO2 + temp/RH (BLE) ---
    # Hard timeout via SIGALRM: BLE can wedge (adapter/peripheral state), and a
    # launchd agent must never hang forever holding the Aranet's one connection.
    import signal
    import aranet4

    def _on_alarm(signum, frame):
        raise TimeoutError(f"no Aranet response in {cfg['ble_timeout_seconds']}s")

    signal.signal(signal.SIGALRM, _on_alarm)
    signal.alarm(int(cfg["ble_timeout_seconds"]))
    try:
        reading = aranet4.client.get_current_readings(cfg["aranet_address"])
        co2 = reading.co2
        tin_c = reading.temperature   # °C
        rh_in = reading.humidity      # %
        if co2 is None:
            raise ValueError("Aranet returned no CO2 value")
    except Exception as e:
        # Sensor down must NOT blind the outdoor alerts (smoke, advisory,
        # filter-on need no CO2). Run degraded: indoor triggers skip.
        print(f"sensor read failed: {e}", file=sys.stderr)
        co2 = tin_c = rh_in = None
    finally:
        signal.alarm(0)

    # --- outdoor: WAQI (AQI/PM2.5/O3) + Open-Meteo (weather, wind) ---
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
    except Exception as e:
        print(f"outdoor data fetch failed: {e}", file=sys.stderr)
        sys.exit(0)

    # Pollen + today's forecast peaks (both fail-soft — never abort the run)
    pollen_high, pollen_hits, pollen_known = fetch_pollen(cfg, lat, lon, thr)
    fc_o3, fc_o3_peak, fc_pm25 = fetch_forecast(lat, lon, tz)

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

    # Gates
    pollutants_ok = (aqi is not None and aqi <= thr["aqi_clean"]
                     and (o3 is None or o3 < thr["o3"])
                     and (pm25 is None or pm25 < thr["pm25"]))
    aq_clean = pollutants_ok and not pollen_high
    open_allowed = aq_clean and not raining     # may we advise open/flush at all
    # Wind-comfort modifier: a breeze makes a higher dew point tolerable.
    dew_gate = (thr["dew_point_breeze_f"] if wind_mph >= thr["breeze_mph"]
                else thr["dew_point_f"])
    comfy = dp <= dew_gate                       # incoming air feels good
    smoke = pm25 is not None and pm25 >= thr["pm25"]

    # --- instrument line ---
    it = f"{tin_f}" if tin_f is not None else "?"
    ih = f"{round(rh_in)}" if rh_in is not None else "?"
    ia = f"{aqi}" if aqi is not None else "?"
    ic = f"{co2}" if co2 is not None else "?"
    print(f"⌂ {it}° {ih}% {ic}ppm │ ◌ {tout}° dew{dp}° AQI{ia} │ "
          f"{wdir}{warrow} {wind_mph}mph")

    state = load_state()
    changed = False
    spoke = False

    # --- persistent sensor failure → one heads-up after ~3h of silence ---
    if co2 is None:
        state["sensor_fail_count"] = state.get("sensor_fail_count", 0) + 1
        changed = True
        if state["sensor_fail_count"] == 6:
            notify("Aranet unreachable for ~3 hours — is it away from home "
                   "(or battery dead)? Outdoor alerts still running; indoor "
                   "ones resume when it's back in range.",
                   title="Sensor offline")
    elif state.get("sensor_fail_count", 0):
        state["sensor_fail_count"] = 0
        changed = True

    def verdict(line):
        nonlocal spoke
        spoke = True
        print(f"⇒ {line}")

    def guide():
        return wind_guidance(orientations, wind_deg, wind_mph, thr["wind_calm_mph"])

    # --- Smoke: PM2.5 unhealthy → hard close + purifiers HIGH (rate-limited) ---
    if smoke:
        if now - state["last_pm25_alert_ts"] >= cfg["pm25_alert_cooldown_hours"] * 3600:
            body = ("Close up, smoke outside. Levoit back on: both units HIGH, "
                    "no timer — runs until the close-up clears.")
            verdict(body)
            notify(body, title="Smoke", urgent=True)
            state["last_pm25_alert_ts"] = now
            changed = True
        else:
            verdict("Smoke holding — stay shut, Levoit HIGH.")

    # --- Forecast advisory: one heads-up banner per day, morning onward, when
    #     today's predicted O3 or PM2.5 peak crosses the unhealthy line. Geared
    #     to planning fieldwork, not just windows. ---
    today = time.strftime("%Y-%m-%d")
    fc_o3_bad = fc_o3 is not None and fc_o3 >= thr["o3"]
    fc_pm_bad = fc_pm25 is not None and fc_pm25 >= thr["pm25"]
    if ((fc_o3_bad or fc_pm_bad) and state["last_advisory_date"] != today
            and time.localtime().tm_hour >= 6):
        if fc_o3_bad and fc_pm_bad:
            body = (f"Air advisory today — ozone to AQI {round(fc_o3)} "
                    f"(~{fc_o3_peak}), PM2.5 to {round(fc_pm25)}. Morning "
                    f"fieldwork only; windows shut; Levoit LOW all day.")
        elif fc_o3_bad:
            body = (f"Ozone advisory today — forecast AQI {round(fc_o3)}, "
                    f"peaking ~{fc_o3_peak}. Park time this morning; windows "
                    f"shut through the afternoon. HEPA won't catch ozone.")
        else:
            body = (f"PM2.5 advisory today — forecast AQI {round(fc_pm25)}. "
                    f"Mask for the park, windows shut, Levoit LOW all day.")
        verdict(body)
        notify(body, title="Air advisory")
        state["last_advisory_date"] = today
        changed = True

    # --- Free cooling: hot inside, cool/clean/dry/comfortable out → open up ---
    # When free cooling is engaged (advised, holding, or just closed), the
    # interoception "Warm — AC on" nudge defers: the windows ARE the answer,
    # and the close-up verdict already says "back to AC".
    free_cool_engaged = False
    if tin_f is not None:
        cool_ok = (tin_f >= thr["free_cooling_indoor_min_f"]
                   and tout <= tin_f - thr["free_cooling_delta_f"]
                   and open_allowed and comfy)
        free_cool_engaged = cool_ok or state["cooling_open"]
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
        elif not aq_clean:
            # Air went bad while the windows are open — close regardless of
            # temperature. (Previously only PM2.5 >= 100 could force this.)
            state["cooling_open"] = False
            changed = True
            if smoke:
                verdict("Smoke while open — latch cleared (smoke banner covers it).")
            else:
                if pollen_high:
                    why = f"pollen ({', '.join(pollen_hits)})"
                elif o3 is not None and o3 >= thr["o3"]:
                    why = f"ozone AQI {round(o3)}"
                else:
                    why = f"AQI {aqi if aqi is not None else '?'}"
                body = f"Air went bad ({why}) — close up. Levoit back on."
                verdict(body)
                notify(body, title="Close up")
        elif tout >= tin_f - thr["free_cooling_close_within_f"]:
            body = (f"Outside caught up ({tout}° vs {tin_f}° inside) — close up, "
                    f"back to AC. Levoit back on.")
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

    # --- CO2: urgent tier (repeats, overrides latch) then normal hysteresis ---
    # (whole chain skips when the sensor is offline: co2 is None)
    if co2 is not None and co2 >= thr["co2_urgent"] and not smoke:
        if now - state["last_co2_urgent_ts"] >= cfg["co2_urgent_repeat_minutes"] * 60:
            body = (f"CO₂ {co2} — you're thinking through soup. Open something "
                    f"NOW, any window, air quality secondary.")
            verdict(body)
            notify(body, title="CO₂ urgent", urgent=True)
            state["last_co2_urgent_ts"] = now
            changed = True
        else:
            verdict(f"CO₂ {co2} — urgent, alerted recently, holding.")
        state["co2_alert_active"] = True  # so the normal tier won't re-fire below
    elif co2 is not None and co2 >= thr["co2_alert"]:
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
    elif (co2 is not None and co2 < thr["co2_all_clear"]
          and state["co2_alert_active"]):
        state["co2_alert_active"] = False  # reset latch below all-clear
        changed = True
    if (co2 is not None and co2 < thr["co2_urgent"]
            and state["last_co2_urgent_ts"]):
        state["last_co2_urgent_ts"] = 0.0  # fresh urgent episode alerts at once
        changed = True

    # --- Interoception backstop: quiet indoor-comfort banners (4h/condition) ---
    intero_cd = cfg["intero_alert_cooldown_hours"] * 3600
    heating = time.localtime().tm_mon in cfg["heating_season_months"]

    def intero(active, tskey, msg, title):
        nonlocal changed, spoke
        if active:
            if now - state[tskey] >= intero_cd:
                spoke = True
                print(f"⇒ {msg}")
                notify(msg, title=title)
                state[tskey] = now
                changed = True
        elif state[tskey]:
            state[tskey] = 0.0  # clear silently on recovery
            changed = True

    if tin_f is not None:
        intero(tin_f >= thr["intero_hot_f"] and not free_cool_engaged,
               "last_intero_hot_ts",
               f"It's {tin_f}F in here — AC on", "Warm")
        intero(tin_f <= thr["intero_cold_f"] and heating, "last_intero_cold_ts",
               f"Cold in here — {tin_f}F", "Cold")
    if dp_in is not None:
        intero(dp_in >= thr["intero_dew_f"], "last_intero_muggy_ts",
               f"Muggy inside, dew {dp_in}F — AC will pull it", "Muggy")

    # --- Elevated-PM Levoit prompts (windows presumed shut). Closed windows
    #     still leak PM2.5, so upper-moderate gets a real banner; the mild
    #     band stays a log-only nudge. Same throttle for both. ---
    if (pm25 is not None and 50 <= pm25 < 100 and not state["cooling_open"]
            and not spoke):
        if now - state["last_levoit_nudge_ts"] >= cfg["levoit_nudge_cooldown_hours"] * 3600:
            if pm25 >= thr["levoit_banner_pm25"]:
                body = (f"PM2.5 AQI {pm25} outside seeps in even shut — "
                        f"Levoit LOW, 2h timer.")
                verdict(body)
                notify(body, title="Filter on")
            else:
                verdict(f"PM2.5 AQI {pm25}, windows shut — worth running the "
                        f"Levoit: LOW, 2h timer is plenty.")
            state["last_levoit_nudge_ts"] = now
            changed = True

    # --- Fallback status line ---
    if not spoke:
        if raining:
            verdict("raining — windows stay shut.")
        elif pollen_high:
            verdict(f"pollen high ({', '.join(pollen_hits)}) — windows stay shut.")
        else:
            verdict(f"Fresh — CO₂ {co2}ppm, nothing to do." if co2 is not None
                    else "Outdoor watch only — Aranet offline, indoor triggers blind.")

    if changed:
        save_state(state)


if __name__ == "__main__":
    main()
