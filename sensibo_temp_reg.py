"""
Sensibo Temperature Regulator
==============================
Maintains a target room temperature by adjusting the AC set-point via the
Sensibo API, using the Sensibo pod's built-in room sensor as the reference.
Also manages fan speed based on the AC set-point.

Includes a simple web dashboard for monitoring and log viewing.

Run:
    python sensibo_temp_reg.py

On first run (or if SENSIBO_DEVICE_ID is blank) you will be prompted to pick
a device from your account.
"""

# =============================================================================
# ██████╗  ██████╗ ███╗   ██╗███████╗██╗ ██████╗
# ██╔════╝██╔═══██╗████╗  ██║██╔════╝██║██╔════╝
# ██║     ██║   ██║██╔██╗ ██║█████╗  ██║██║  ███╗
# ██║     ██║   ██║██║╚██╗██║██╔══╝  ██║██║   ██║
# ╚██████╗╚██████╔╝██║ ╚████║██║     ██║╚██████╔╝
#  ╚═════╝ ╚═════╝ ╚═╝  ╚═══╝╚═╝     ╚═╝ ╚═════╝
# Edit all your settings here — nowhere else needed.
# =============================================================================

# ── Sensibo API ───────────────────────────────────────────────────────────────
SENSIBO_API_KEY   = "YOUR_API_KEY_HERE"
SENSIBO_DEVICE_ID = ""          # leave blank → interactive device picker on startup

# ── Target room temperature ───────────────────────────────────────────────────
TARGET_ROOM_TEMP = 22.8          # °C  — adjust seasonally (e.g. 23 in deep winter)

# ── Regulation thresholds ─────────────────────────────────────────────────────
FAST_THRESHOLD = 0.4            # °C deviation from target (±) for fast regulator
SLOW_THRESHOLD = 0.2            # °C deviation of EMA average from target (±) for slow regulator
SLOW_TAU       = 180            # minutes — EMA filter time constant

# ── AC set-point limits ───────────────────────────────────────────────────────
AC_TEMP_MIN = 20                # °C — never set AC below this
AC_TEMP_MAX = 27                # °C — never set AC above this

# ── Fan-speed table  (AC set-point [°C] → fan speed integer 1–6) ─────────────
# Only temperatures listed here will trigger a fan-speed change.
# If the current AC set-point is NOT in this dict, fan speed is left unchanged.
#
# Integer mapping:
#   1 = Quiet      2 = Low     3 = Average Low
#   4 = Average    5 = Avg High  6 = High
#   (7 = Auto and 8 = Strong are intentionally excluded)
FAN_SPEED_TABLE = {
    20: 4,
    21: 4,
    22: 5,
    23: 5,
    24: 6,
    25: 6,
    26: 6,
}

# ── Polling ───────────────────────────────────────────────────────────────────
READ_INTERVAL_MINUTES  = 5      # minutes between sensor read + regulation checks
CHECK_INTERVAL_MINUTES = 30     # minimum minutes between AC set-point changes

# ── Web server ────────────────────────────────────────────────────────────────
WEB_HOST     = "0.0.0.0"
WEB_PORT     = 8585
WEB_USERNAME = "admin"
WEB_PASSWORD = "changeme"
SESSION_SECRET_KEY = "change-this-to-a-random-string"  # used to sign session cookies

# ── Log file ──────────────────────────────────────────────────────────────────
LOG_FILE = "/var/log/sensibo/sensibo_reg.log"   # relative to script directory; created automatically

# =============================================================================
# END OF CONFIG — do not edit below unless you know what you're doing
# =============================================================================

import csv
import io
import math
import os
import sys
import time
import threading
import logging
from datetime import datetime
from functools import wraps

import requests
from flask import (
    Flask, render_template_string, request, redirect,
    url_for, session, jsonify, Response
)

# ---------------------------------------------------------------------------
# Logging setup (console)
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("sensibo_reg")

# Map integer fan speeds to Sensibo API string values
INT_TO_FAN = {
    1: "quiet",
    2: "low",
    3: "medium_low",
    4: "medium",
    5: "medium_high",
    6: "high",
}

# ---------------------------------------------------------------------------
# Shared state (written by regulation loop, read by web server)
# ---------------------------------------------------------------------------
_state_lock = threading.Lock()
_latest_state: dict = {}  # last successful API read

def update_shared_state(**kwargs):
    with _state_lock:
        _latest_state.update(kwargs)

def get_shared_state() -> dict:
    with _state_lock:
        return dict(_latest_state)


# ---------------------------------------------------------------------------
# EMA temperature filter (integrator)
# ---------------------------------------------------------------------------
_ema_lock = threading.Lock()
_ema_avg_temp: float | None = None
_last_sample_epoch: float | None = None

def update_ema_filter(room_temp: float, now_epoch: float) -> float:
    """
    Integrates room temperature into the exponential moving average (EMA) filter:
      AvgTemp = Temp * ALPHA + AvgTemp * (1 - ALPHA)
      ALPHA = 1 - exp(-duration_min / SLOW_TAU)
    On startup / first sample, ALPHA = 1 so AvgTemp is initialized to room_temp.
    """
    global _ema_avg_temp, _last_sample_epoch
    with _ema_lock:
        if _ema_avg_temp is None or _last_sample_epoch is None:
            _ema_avg_temp = float(room_temp)
        else:
            duration_min = max(0.0, (now_epoch - _last_sample_epoch) / 60.0)
            alpha = 1.0 - math.exp(-duration_min / SLOW_TAU)
            _ema_avg_temp = room_temp * alpha + _ema_avg_temp * (1.0 - alpha)
        _last_sample_epoch = now_epoch
        return _ema_avg_temp


# ---------------------------------------------------------------------------
# Log file helpers
# ---------------------------------------------------------------------------
_log_lock = threading.Lock()
LOG_FIELDS = ["timestamp", "room_temp", "ac_set_temp", "delta", "ac_action", "fan_action", "changed"]

def _log_file_path() -> str:
    # If explicit path exists, use it directly
    if os.path.exists(LOG_FILE):
        return LOG_FILE
    script_dir = os.path.dirname(os.path.abspath(__file__))
    # Check if a file with the same basename exists in the script directory
    local_path = os.path.join(script_dir, os.path.basename(LOG_FILE))
    if os.path.exists(local_path):
        return local_path
    if os.path.isabs(LOG_FILE):
        return LOG_FILE
    return os.path.join(script_dir, LOG_FILE)

def append_log_row(row: dict):
    """Append one CSV row to the log file (thread-safe)."""
    path = _log_file_path()
    log_dir = os.path.dirname(path)
    if log_dir:
        os.makedirs(log_dir, exist_ok=True)
    write_header = not os.path.exists(path)
    with _log_lock:
        with open(path, "a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=LOG_FIELDS)
            if write_header:
                writer.writeheader()
            writer.writerow(row)

def read_log_rows(max_rows: int = 200) -> list[dict]:
    """Return up to *max_rows* most-recent log rows, newest first."""
    path = _log_file_path()
    if not os.path.exists(path):
        return []
    with _log_lock:
        with open(path, "r", newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
    return list(reversed(rows[-max_rows:]))

def read_event_rows(max_rows: int = 50) -> list[dict]:
    """
    Return up to *max_rows* most-recent log rows where something changed
    (changed == 'yes'), newest first.  Falls back to all rows if the
    'changed' column is absent (old log files without that column).
    """
    path = _log_file_path()
    if not os.path.exists(path):
        return []
    with _log_lock:
        with open(path, "r", newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
    # If old log file has no 'changed' column, return everything
    if rows and "changed" not in rows[0]:
        return list(reversed(rows[-max_rows:]))
    event_rows = [r for r in rows if r.get("changed") == "yes"]
    return list(reversed(event_rows[-max_rows:]))


def read_chart_data(view: str = "24h", date_str: str | None = None) -> dict:
    """
    Return chart points and metadata for the requested view and date.

    Views:
      - '24h': rolling 24 hours ending now
      - 'day': single calendar day (00:00 to 24:00)
      - 'week': Monday 00:00 to Sunday 24:00
      - 'month': 1st of month 00:00 to last day of month 24:00
    """
    from datetime import timedelta
    import calendar

    today = datetime.now().date()
    if date_str:
        try:
            d = datetime.strptime(date_str, "%Y-%m-%d").date()
        except ValueError:
            d = today
    else:
        d = today

    now = datetime.now()
    can_go_next = False

    if view == "day":
        start_dt = datetime(d.year, d.month, d.day, 0, 0, 0)
        end_dt = datetime(d.year, d.month, d.day, 23, 59, 59)
        label = d.strftime("%A, %d %b %Y")
        can_go_next = (d < today)
    elif view == "week":
        # Monday is weekday 0
        monday = d - timedelta(days=d.weekday())
        sunday = monday + timedelta(days=6)
        start_dt = datetime(monday.year, monday.month, monday.day, 0, 0, 0)
        end_dt = datetime(sunday.year, sunday.month, sunday.day, 23, 59, 59)
        iso_year, iso_week, _ = monday.isocalendar()
        label = f"Week {iso_week}, {iso_year} ({monday.strftime('%d %b')} - {sunday.strftime('%d %b')})"
        today_monday = today - timedelta(days=today.weekday())
        can_go_next = (monday < today_monday)
    elif view == "month":
        last_day = calendar.monthrange(d.year, d.month)[1]
        start_dt = datetime(d.year, d.month, 1, 0, 0, 0)
        end_dt = datetime(d.year, d.month, last_day, 23, 59, 59)
        label = d.strftime("%B %Y")
        can_go_next = (d.year < today.year or (d.year == today.year and d.month < today.month))
    else:  # "24h"
        view = "24h"
        end_dt = now
        start_dt = now - timedelta(hours=24)
        label = "Last 24 Hours"
        can_go_next = False

    path = _log_file_path()
    if not os.path.exists(path):
        return {
            "target_temp": TARGET_ROOM_TEMP,
            "view": view,
            "date": d.strftime("%Y-%m-%d"),
            "label": label,
            "start_iso": start_dt.strftime("%Y-%m-%dT%H:%M:%S"),
            "end_iso": end_dt.strftime("%Y-%m-%dT%H:%M:%S"),
            "can_go_next": can_go_next,
            "points": [],
        }

    with _log_lock:
        with open(path, "r", newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))

    # Filter rows in [start_dt, end_dt] with valid numeric temperatures
    raw_points = []
    for r in rows:
        ts_str = r.get("timestamp", "")
        try:
            ts = datetime.fromisoformat(ts_str)
        except ValueError:
            continue
        if not (start_dt <= ts <= end_dt):
            continue
        try:
            rt = float(r["room_temp"])
            ac = float(r["ac_set_temp"])
        except (ValueError, KeyError):
            continue
        raw_points.append({"ts": ts, "room_temp": rt, "ac_set_temp": ac})

    # Deduplicate by timestamp (last logged entry wins) and sort chronologically
    deduped = {}
    for pt in raw_points:
        deduped[pt["ts"]] = pt
    sorted_points = [deduped[k] for k in sorted(deduped.keys())]

    # Downsampling for week and month if row count is large
    if view == "week" and len(sorted_points) > 400:
        bucket_seconds = 30 * 60  # 30-minute buckets (~336 points max per week)
    elif view == "month" and len(sorted_points) > 500:
        bucket_seconds = 2 * 3600  # 2-hour buckets (~372 points max per month)
    else:
        bucket_seconds = None

    if bucket_seconds and sorted_points:
        points = []
        import itertools
        def bucket_key(pt):
            diff = (pt["ts"] - start_dt).total_seconds()
            return int(diff // bucket_seconds)

        for b_idx, group in itertools.groupby(sorted_points, key=bucket_key):
            g_list = list(group)
            avg_rt = round(sum(p["room_temp"] for p in g_list) / len(g_list), 1)
            last_ac = g_list[-1]["ac_set_temp"]
            b_ts = start_dt + timedelta(seconds=(b_idx + 0.5) * bucket_seconds)
            points.append({
                "timestamp": b_ts.strftime("%Y-%m-%dT%H:%M:%S"),
                "room_temp": avg_rt,
                "ac_set_temp": last_ac,
            })
    else:
        points = [
            {
                "timestamp": p["ts"].strftime("%Y-%m-%dT%H:%M:%S"),
                "room_temp": p["room_temp"],
                "ac_set_temp": p["ac_set_temp"],
            }
            for p in sorted_points
        ]

    return {
        "target_temp": TARGET_ROOM_TEMP,
        "view": view,
        "date": d.strftime("%Y-%m-%d"),
        "label": label,
        "start_iso": start_dt.strftime("%Y-%m-%dT%H:%M:%S"),
        "end_iso": end_dt.strftime("%Y-%m-%dT%H:%M:%S"),
        "can_go_next": can_go_next,
        "points": points,
    }


def read_chart_rows(span_hours: int = 24, offset_periods: int = 0) -> list[dict]:
    """
    Return log rows within a rolling time window, oldest first, for charting.

    *span_hours*     — width of the window in hours (default 24 = "last 24 h").
    *offset_periods* — shift the window backwards by this many full span_hours
                       periods (0 = current/live window, 1 = previous period, …).
                       Reserved for future Day/Week/Month selector support.

    Only rows with numeric room_temp and ac_set_temp are included (N/A rows
    from API errors are skipped so the chart stays clean).
    """
    from datetime import timedelta

    path = _log_file_path()
    if not os.path.exists(path):
        return []

    now = datetime.now()
    window_end   = now   - timedelta(hours=span_hours * offset_periods)
    window_start = window_end - timedelta(hours=span_hours)

    with _log_lock:
        with open(path, "r", newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))

    result = []
    for r in rows:
        ts_str = r.get("timestamp", "")
        try:
            ts = datetime.fromisoformat(ts_str)
        except ValueError:
            continue
        if not (window_start <= ts <= window_end):
            continue
        # Skip rows where temperature values are not numeric
        try:
            float(r["room_temp"])
            float(r["ac_set_temp"])
        except (ValueError, KeyError):
            continue
        result.append(r)

    return result  # oldest first


# ---------------------------------------------------------------------------
# Sensibo API client
# ---------------------------------------------------------------------------
SENSIBO_API_BASE = "https://home.sensibo.com/api/v2"

class SensiboError(Exception):
    pass

class SensiboClient:
    def __init__(self, api_key: str):
        self.api_key = api_key
        self.session = requests.Session()
        self.session.params = {"apiKey": api_key}  # type: ignore[assignment]

    def _request(self, method: str, path: str, **kwargs) -> dict:
        url = SENSIBO_API_BASE + path
        last_exc: Exception | None = None
        for attempt in range(3):
            try:
                resp = self.session.request(method, url, timeout=15, **kwargs)
                if resp.status_code >= 500:
                    raise SensiboError(f"Server error {resp.status_code}")
                resp.raise_for_status()
                data = resp.json()
                if data.get("status") != "success":
                    raise SensiboError(f"API error: {data}")
                return data
            except (requests.RequestException, SensiboError) as exc:
                last_exc = exc
                if attempt < 2:
                    wait = 2 ** attempt
                    log.warning("API call failed (attempt %d/3): %s — retrying in %ds", attempt + 1, exc, wait)
                    time.sleep(wait)
        raise SensiboError(f"API call failed after 3 attempts: {last_exc}") from last_exc

    def list_devices(self) -> list[dict]:
        data = self._request("GET", "/users/me/pods", params={"fields": "id,room"})
        return data.get("result", [])

    def get_pod_data(self, device_id: str) -> dict:
        """
        Fetch current acState and measurements in one API call.
        Both are embedded directly in the pod object — there is no separate
        working /acStates or /measurements endpoint for current readings.
        """
        data = self._request(
            "GET", "/users/me/pods",
            params={"fields": "id,acState,measurements"},
        )
        for pod in data.get("result", []):
            if pod.get("id") == device_id:
                return pod
        raise SensiboError(f"Device '{device_id}' not found in account")

    def set_ac_state_fields(self, device_id: str, current_state: dict, updates: dict) -> None:
        """
        Apply *updates* to the AC state in a single API call.

        Sends the full acState with the requested fields overridden.  This
        avoids multiple round-trips (and multiple AC beeps) when we need to
        change both targetTemperature and fanLevel simultaneously.
        """
        new_state = dict(current_state)
        new_state.update(updates)
        self._request(
            "POST",
            f"/pods/{device_id}/acStates",
            json={"acState": new_state},
        )

    def set_ac_state_field(self, device_id: str, field: str, value) -> None:
        """Change a single field of the AC state (e.g. targetTemperature, fanLevel)."""
        self._request(
            "PATCH",
            f"/pods/{device_id}/acStates/{field}",
            json={"newValue": value},
        )

    def pick_device_interactive(self) -> str:
        """Print available devices and prompt the user to choose one."""
        devices = self.list_devices()
        if not devices:
            raise SensiboError("No devices found on this account.")
        print("\n=== Sensibo Device Picker ===")
        for i, d in enumerate(devices, 1):
            name = d.get("room", {}).get("name", "Unknown")
            print(f"  {i}. {name}  (ID: {d['id']})")
        while True:
            try:
                choice = int(input(f"Select device [1-{len(devices)}]: "))
                if 1 <= choice <= len(devices):
                    chosen = devices[choice - 1]
                    dev_id = chosen["id"]
                    name = chosen.get("room", {}).get("name", "Unknown")
                    print(f"\nUsing: {name} ({dev_id})")
                    print(f"Tip: set SENSIBO_DEVICE_ID = \"{dev_id}\" in the config to skip this step next time.\n")
                    return dev_id
            except ValueError:
                pass
            print("Invalid choice, try again.")


# ---------------------------------------------------------------------------
# Regulation loop
# ---------------------------------------------------------------------------

def apply_ac_change(
    client: SensiboClient,
    device_id: str,
    ac_state: dict,
    current_ac_temp: int,
    direction: int,
    current_fan: str,
) -> tuple[int, str, str, bool]:
    """
    Applies an AC set-point adjustment (+1 or -1), enforces AC_TEMP_MIN/MAX limits,
    looks up fan speed from FAN_SPEED_TABLE, and sends a single combined API call if needed.

    Returns:
        (new_ac_temp, ac_action, fan_action, changed)
    """
    if direction > 0:
        new_temp = min(current_ac_temp + 1, AC_TEMP_MAX)
        if new_temp != current_ac_temp:
            desired_temp = new_temp
            ac_action = f"Increased set-point to {new_temp}°C"
        else:
            log.warning("Already at AC_TEMP_MAX (%d°C), cannot increase further", AC_TEMP_MAX)
            ac_action = f"At max limit ({AC_TEMP_MAX}°C), no increase"
            desired_temp = current_ac_temp
    else:
        new_temp = max(current_ac_temp - 1, AC_TEMP_MIN)
        if new_temp != current_ac_temp:
            desired_temp = new_temp
            ac_action = f"Decreased set-point to {new_temp}°C"
        else:
            log.warning("Already at AC_TEMP_MIN (%d°C), cannot decrease further", AC_TEMP_MIN)
            ac_action = f"At min limit ({AC_TEMP_MIN}°C), no decrease"
            desired_temp = current_ac_temp

    desired_fan = None
    target_set_for_fan = desired_temp
    if target_set_for_fan in FAN_SPEED_TABLE:
        target_fan_int = FAN_SPEED_TABLE[target_set_for_fan]
        target_fan_str = INT_TO_FAN.get(target_fan_int)
        if target_fan_str is None:
            log.warning("Fan speed integer %d has no API mapping, skipping", target_fan_int)
            fan_action = f"Invalid fan speed int {target_fan_int}"
        elif current_fan != target_fan_str:
            desired_fan = target_fan_str
            fan_action = f"Fan → {target_fan_int} ({target_fan_str})"
        else:
            fan_action = f"Fan unchanged ({target_fan_int})"
    else:
        fan_action = f"Fan unchanged (no table entry for {target_set_for_fan}°C)"
        log.info("No fan table entry for set-point %d°C, fan left as-is", target_set_for_fan)

    temp_changed = desired_temp != current_ac_temp
    fan_changed = desired_fan is not None
    changed = False

    if temp_changed or fan_changed:
        updates: dict = {}
        if temp_changed:
            updates["targetTemperature"] = desired_temp
            log.info(
                "Set-point %s: %d → %d°C",
                "increased" if desired_temp > current_ac_temp else "decreased",
                current_ac_temp, desired_temp,
            )
        if fan_changed:
            updates["fanLevel"] = desired_fan
            log.info("Fan speed → %s", desired_fan)

        if temp_changed and fan_changed:
            client.set_ac_state_fields(device_id, ac_state, updates)
        elif temp_changed:
            client.set_ac_state_field(device_id, "targetTemperature", desired_temp)
        else:
            client.set_ac_state_field(device_id, "fanLevel", desired_fan)

        if temp_changed:
            current_ac_temp = desired_temp
        if fan_changed:
            update_shared_state(ac_fan=desired_fan, ac_set_temp=current_ac_temp)
        else:
            update_shared_state(ac_set_temp=current_ac_temp)

        changed = True

    return current_ac_temp, ac_action, fan_action, changed


def regulate(
    client: SensiboClient,
    device_id: str,
    fast_rate_limited: bool,
    slow_rate_limited: bool,
) -> tuple[bool, str | None]:
    """
    Regulation cycle. Reads current state from the Sensibo API, updates
    the EMA temperature filter, evaluates the fast regulator and (if fast is happy
    and not rate-limited) evaluates the slow regulator. Applies any changes
    in a single API call and appends a log row.

    Returns:
        (changed, regulator_source) where regulator_source is 'fast', 'slow', or None.
    """
    now = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    now_epoch = time.time()
    ac_action = "Error"
    fan_action = "Error"
    room_temp = None
    avg_temp = None
    ac_set_temp = None
    delta = None
    changed = False
    regulator_source: str | None = None

    try:
        # 1. Read current AC state and room measurement (single API call)
        pod = client.get_pod_data(device_id)
        ac_state = pod.get("acState", {})
        measurement = pod.get("measurements", {})

        room_temp = measurement.get("temperature")
        ac_set_temp = ac_state.get("targetTemperature")
        ac_mode = ac_state.get("mode", "").lower()
        ac_on = ac_state.get("on", False)
        ac_fan = ac_state.get("fanLevel", "")

        if room_temp is None:
            raise SensiboError("No temperature reading in measurements")
        if ac_set_temp is None:
            raise SensiboError("No targetTemperature in AC state")

        # All samples integrated in EMA filter, even during rate-limits or when AC is off
        avg_temp = update_ema_filter(room_temp, now_epoch)

        update_shared_state(
            room_temp=room_temp,
            avg_temp=round(avg_temp, 2),
            ac_set_temp=ac_set_temp,
            ac_mode=ac_mode,
            ac_on=ac_on,
            ac_fan=ac_fan,
            last_checked=now,
        )

        # 2. Only operate in heat mode
        if not ac_on or ac_mode != "heat":
            reason = "off" if not ac_on else f"mode={ac_mode}"
            log.info("Skipped — AC is %s (not in heat mode)", reason)
            ac_action = f"Skipped ({reason})"
            fan_action = "Skipped"
            delta = None
        else:
            # 3. Dual temperature regulation
            delta = round(room_temp - TARGET_ROOM_TEMP, 2)
            log.info(
                "Room: %.1f°C  Avg: %.2f°C  Target: %.1f°C  AC set: %d°C  Delta: %+.2f°C",
                room_temp, avg_temp, TARGET_ROOM_TEMP, ac_set_temp, delta,
            )

            # Determine fast regulator trigger
            fast_direction = 0
            if delta < -FAST_THRESHOLD:
                fast_direction = 1
            elif delta > FAST_THRESHOLD:
                fast_direction = -1

            if fast_direction != 0:
                if fast_rate_limited:
                    ac_action = "No change (rate limited)"
                    fan_action = "Fan unchanged"
                    log.info(
                        "Fast regulator wants %s but is rate-limited",
                        "increase" if fast_direction > 0 else "decrease",
                    )
                else:
                    regulator_source = "fast"
                    ac_set_temp, ac_action, fan_action, changed = apply_ac_change(
                        client, device_id, ac_state, ac_set_temp, fast_direction, ac_fan
                    )
            else:
                # Fast regulator is happy with no change
                if fast_rate_limited:
                    ac_action = "No change (rate limited)"
                    fan_action = "Fan unchanged"
                    log.info("Fast regulator is happy, but fast rate-limit is active — slow regulator skipped")
                else:
                    # Fast is happy and not rate-limited -> evaluate slow regulator
                    slow_direction = 0
                    if avg_temp < TARGET_ROOM_TEMP - SLOW_THRESHOLD:
                        slow_direction = 1
                    elif avg_temp > TARGET_ROOM_TEMP + SLOW_THRESHOLD:
                        slow_direction = -1

                    if slow_direction != 0:
                        if slow_rate_limited:
                            ac_action = "No change (rate limited)"
                            fan_action = "Fan unchanged"
                            log.info(
                                "Slow regulator wants %s but is rate-limited",
                                "increase" if slow_direction > 0 else "decrease",
                            )
                        else:
                            regulator_source = "slow"
                            ac_set_temp, ac_action, fan_action, changed = apply_ac_change(
                                client, device_id, ac_state, ac_set_temp, slow_direction, ac_fan
                            )
                    else:
                        ac_action = "No change"
                        fan_action = "Fan unchanged"

    except SensiboError as exc:
        log.error("Regulation cycle failed: %s", exc)
        ac_action = f"API Error: {exc}"
        fan_action = "N/A"

    # 4. Append log row (every cycle so log is complete)
    append_log_row({
        "timestamp": now,
        "room_temp": f"{room_temp:.1f}" if room_temp is not None else "N/A",
        "ac_set_temp": ac_set_temp if ac_set_temp is not None else "N/A",
        "delta": f"{delta:+.2f}" if delta is not None else "N/A",
        "ac_action": ac_action,
        "fan_action": fan_action,
        "changed": "yes" if changed else "no",
    })

    return changed, regulator_source


def regulation_loop(client: SensiboClient, device_id: str) -> None:
    """
    Background thread: poll sensor every READ_INTERVAL_MINUTES.
    Fast regulator enforces CHECK_INTERVAL_MINUTES cooldown.
    Slow regulator enforces 2 * CHECK_INTERVAL_MINUTES cooldown.
    """
    log.info(
        "Regulation loop started. Read every %d min, Fast min %d min, Slow min %d min.",
        READ_INTERVAL_MINUTES, CHECK_INTERVAL_MINUTES, 2 * CHECK_INTERVAL_MINUTES,
    )
    last_fast_change_time: float = 0.0   # epoch seconds of last fast set-point change
    last_slow_change_time: float = 0.0   # epoch seconds of last slow set-point change

    while True:
        now_epoch = time.time()
        fast_elapsed = (now_epoch - last_fast_change_time) / 60.0
        slow_elapsed = (now_epoch - last_slow_change_time) / 60.0

        fast_rate_limited = fast_elapsed < CHECK_INTERVAL_MINUTES
        slow_rate_limited = slow_elapsed < (2 * CHECK_INTERVAL_MINUTES)

        fast_status = "allowed" if not fast_rate_limited else f"locked {CHECK_INTERVAL_MINUTES - fast_elapsed:.0f}m"
        slow_status = "allowed" if not slow_rate_limited else f"locked {2 * CHECK_INTERVAL_MINUTES - slow_elapsed:.0f}m"

        log.info(
            "--- Regulation cycle start [Fast: %s, Slow: %s] ---",
            fast_status, slow_status,
        )

        # Remember set-point before the cycle to detect if it was changed
        state_before = get_shared_state()
        set_temp_before = state_before.get("ac_set_temp")

        changed, regulator_source = regulate(
            client,
            device_id,
            fast_rate_limited=fast_rate_limited,
            slow_rate_limited=slow_rate_limited,
        )

        state_after = get_shared_state()
        set_temp_after = state_after.get("ac_set_temp")

        now_done = time.time()
        # On startup (first run), state_before had no ac_set_temp (set_temp_before is None).
        # We preserve existing startup behavior where rate limit cooldown is initiated on startup:
        if set_temp_before is None:
            last_fast_change_time = now_done
            last_slow_change_time = now_done
        elif changed and set_temp_after != set_temp_before:
            if regulator_source == "slow":
                last_slow_change_time = now_done
                last_fast_change_time = now_done
            elif regulator_source == "fast":
                last_fast_change_time = now_done
                # Fast regulator does not reset slow regulator rate limit

        log.info("--- Next read in %d min ---", READ_INTERVAL_MINUTES)
        time.sleep(READ_INTERVAL_MINUTES * 60)



# ---------------------------------------------------------------------------
# Flask web dashboard
# ---------------------------------------------------------------------------
app = Flask(__name__)
app.secret_key = SESSION_SECRET_KEY

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("logged_in"):
            return redirect(url_for("login_page", next=request.url))
        return f(*args, **kwargs)
    return decorated

# ── HTML templates (inline, no extra files needed) ────────────────────────────

_BASE_STYLE = """
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: system-ui, sans-serif; background: #f0f2f5; color: #222; }
  a { color: #2563eb; text-decoration: none; }
  a:hover { text-decoration: underline; }
  .navbar {
    background: #1e3a5f; color: #fff; padding: 0.75rem 1.5rem;
    display: flex; align-items: center; justify-content: space-between;
  }
  .navbar h1 { font-size: 1.1rem; letter-spacing: 0.02em; }
  .navbar a { color: #93c5fd; font-size: 0.9rem; }
  .container { max-width: 960px; margin: 2rem auto; padding: 0 1rem; }
  .card {
    background: #fff; border-radius: 10px; padding: 1.25rem 1.5rem;
    box-shadow: 0 1px 4px rgba(0,0,0,.12); margin-bottom: 1.25rem;
  }
  .card h2 { font-size: 1rem; font-weight: 600; color: #1e3a5f; margin-bottom: 0.75rem; }
  .status-grid {
    display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 0.75rem;
  }
  .stat { text-align: center; }
  .stat .label { font-size: 0.72rem; text-transform: uppercase; color: #666; letter-spacing: .05em; }
  .stat .value { font-size: 1.8rem; font-weight: 700; color: #1e3a5f; }
  .stat .unit  { font-size: 0.85rem; color: #555; }
  .delta-ok   { color: #16a34a; }
  .delta-cold { color: #2563eb; }
  .delta-warm { color: #dc2626; }
  table { width: 100%; border-collapse: collapse; font-size: 0.85rem; }
  th { background: #1e3a5f; color: #fff; padding: 0.4rem 0.75rem; text-align: left; }
  td { padding: 0.35rem 0.75rem; border-bottom: 1px solid #e5e7eb; }
  tr:hover td { background: #f9fafb; }
  .config-table td:first-child { font-family: monospace; color: #7c3aed; }
  .btn {
    display: inline-block; padding: 0.4rem 1rem; border-radius: 6px;
    background: #2563eb; color: #fff; font-size: 0.85rem; border: none; cursor: pointer;
  }
  .btn:hover { background: #1d4ed8; }
  .login-wrap {
    display: flex; align-items: center; justify-content: center; min-height: 100vh;
  }
  .login-card { background: #fff; padding: 2rem; border-radius: 12px; width: 320px; box-shadow: 0 2px 8px rgba(0,0,0,.15); }
  .login-card h1 { text-align: center; color: #1e3a5f; margin-bottom: 1.5rem; font-size: 1.2rem; }
  .form-group { margin-bottom: 1rem; }
  .form-group label { display: block; font-size: 0.82rem; color: #555; margin-bottom: 0.25rem; }
  .form-group input {
    width: 100%; padding: 0.4rem 0.6rem; border: 1px solid #d1d5db;
    border-radius: 6px; font-size: 0.9rem;
  }
  .error { color: #dc2626; font-size: 0.82rem; margin-bottom: 0.75rem; }
</style>
"""

_LOGIN_TEMPLATE = """
<!doctype html>
<html lang="en">
<head><meta charset="utf-8"><title>Login — Sensibo Reg</title>""" + _BASE_STYLE + """</head>
<body>
<div class="login-wrap">
  <div class="login-card">
    <h1>🌡 Sensibo Reg</h1>
    {% if error %}<p class="error">{{ error }}</p>{% endif %}
    <form method="post">
      <div class="form-group">
        <label>Username</label>
        <input name="username" autofocus>
      </div>
      <div class="form-group">
        <label>Password</label>
        <input name="password" type="password">
      </div>
      <button class="btn" style="width:100%">Log in</button>
    </form>
  </div>
</div>
</body></html>
"""

_DASHBOARD_TEMPLATE = """
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Sensibo Reg — Dashboard</title>
  """ + _BASE_STYLE + """
  <style>
    /* Chart styles */
    .chart-header {
      display: flex; align-items: center; justify-content: space-between;
      flex-wrap: wrap; gap: 0.5rem; margin-bottom: 0.75rem;
    }
    .chart-header h2 { margin-bottom: 0; }
    .view-btn-group {
      display: inline-flex; border-radius: 6px; overflow: hidden;
      border: 1px solid #cbd5e1; background: #f8fafc;
    }
    .view-btn {
      background: transparent; border: none; padding: 0.35rem 0.75rem;
      font-size: 0.8rem; font-weight: 500; color: #475569; cursor: pointer;
      border-right: 1px solid #cbd5e1; transition: background .15s, color .15s;
    }
    .view-btn:last-child { border-right: none; }
    .view-btn:hover { background: #e2e8f0; }
    .view-btn.active { background: #2563eb; color: #fff; }
    .chart-nav {
      display: flex; align-items: center; justify-content: center;
      gap: 1rem; margin-bottom: 0.75rem; background: #f8fafc;
      padding: 0.4rem 0.75rem; border-radius: 6px; border: 1px solid #e2e8f0;
    }
    .nav-btn {
      background: #fff; border: 1px solid #cbd5e1; border-radius: 4px;
      width: 28px; height: 28px; display: inline-flex; align-items: center;
      justify-content: center; cursor: pointer; font-size: 1.1rem; line-height: 1;
      color: #1e3a5f; transition: background .15s, border-color .15s;
    }
    .nav-btn:hover:not(:disabled) { background: #e2e8f0; border-color: #94a3b8; }
    .nav-btn:disabled { opacity: 0.35; cursor: not-allowed; }
    .nav-title { font-size: 0.88rem; font-weight: 600; color: #1e3a5f; min-width: 220px; text-align: center; }
    .chart-wrap { position: relative; width: 100%; }
    .chart-wrap svg { width: 100%; height: 260px; display: block; overflow: visible; }
    .chart-legend {
      display: flex; gap: 1.25rem; flex-wrap: wrap;
      margin-top: 0.6rem; font-size: 0.78rem; color: #555;
    }
    .legend-item { display: flex; align-items: center; gap: 0.35rem; }
    .legend-swatch { width: 28px; height: 3px; border-radius: 2px; }
    .legend-dashed { border-top: 2px dashed; height: 0; width: 28px; }
    .chart-empty { color: #aaa; font-size: 0.85rem; padding: 1rem 0; text-align: center; }
  </style>
</head>
<body>
<div class="navbar">
  <h1>🌡 Sensibo Temperature Regulator</h1>
  <span><a href="/log">Full Log</a> &nbsp;|&nbsp; <a href="/logout">Logout</a></span>
</div>
<div class="container">

  <!-- Status card -->
  <div class="card" id="status-card">
    <h2>Current Status</h2>
    <div class="status-grid">
      <div class="stat">
        <div class="label">Room Temp</div>
        <div class="value" id="room_temp">{{ s.room_temp if s.room_temp else '—' }}</div>
        <div class="unit">°C</div>
      </div>
      <div class="stat">
        <div class="label">Average Temp</div>
        <div class="value" id="avg_temp">{{ '%.2f'|format(s.avg_temp) if s.avg_temp is defined and s.avg_temp is not none else '—' }}</div>
        <div class="unit">°C</div>
      </div>
      <div class="stat">
        <div class="label">Target Temp</div>
        <div class="value">{{ target }}</div>
        <div class="unit">°C</div>
      </div>
      <div class="stat">
        <div class="label">AC Set-Point</div>
        <div class="value" id="ac_set_temp">{{ s.ac_set_temp if s.ac_set_temp else '—' }}</div>
        <div class="unit">°C</div>
      </div>
      <div class="stat">
        <div class="label">Fan Speed</div>
        <div class="value" id="ac_fan" style="font-size:1.2rem">{{ s.ac_fan if s.ac_fan else '—' }}</div>
      </div>
      <div class="stat">
        <div class="label">Mode</div>
        <div class="value" id="ac_mode" style="font-size:1.2rem">{{ s.ac_mode if s.ac_mode else '—' }}</div>
      </div>
      <div class="stat">
        <div class="label">Delta</div>
        <div class="value {{ delta_class }}" id="delta">
          {% if s.room_temp and s.room_temp != 'N/A' %}
            {{ '%+.1f'|format(s.room_temp - target) }}
          {% else %}—{% endif %}
        </div>
        <div class="unit">°C</div>
      </div>
    </div>
    <p style="font-size:0.75rem;color:#888;margin-top:0.75rem">
      Last checked: <span id="last_checked">{{ s.last_checked if s.last_checked else 'Not yet' }}</span>
      &nbsp;·&nbsp; Next refresh in <span id="countdown">300</span>s
    </p>
  </div>

  <!-- Temperature Chart -->
  <div class="card">
    <div class="chart-header">
      <h2 id="chartTitle">Last 24 Hours</h2>
      <div class="view-btn-group">
        <button type="button" class="view-btn active" data-view="24h">Last 24h</button>
        <button type="button" class="view-btn" data-view="day">Day</button>
        <button type="button" class="view-btn" data-view="week">Week</button>
        <button type="button" class="view-btn" data-view="month">Month</button>
      </div>
    </div>
    <div class="chart-nav" id="chartNav" style="display:none">
      <button type="button" class="nav-btn" id="navPrev" title="Previous">‹</button>
      <span class="nav-title" id="navTitle"></span>
      <button type="button" class="nav-btn" id="navNext" title="Next">›</button>
    </div>
    <div class="chart-wrap">
      <svg id="tempChart" viewBox="0 0 900 240" preserveAspectRatio="none"
           aria-label="Temperature chart"></svg>
      <p class="chart-empty" id="chartEmpty" style="display:none">No data for this period.</p>
    </div>
    <div class="chart-legend">
      <div class="legend-item">
        <div class="legend-swatch" style="background:#2563eb"></div>
        <span>Room Temp</span>
      </div>
      <div class="legend-item">
        <div class="legend-swatch" style="background:#f59e0b"></div>
        <span>AC Set-Point</span>
      </div>
      <div class="legend-item">
        <div class="legend-dashed" style="border-color:#16a34a"></div>
        <span>Target Temp</span>
      </div>
    </div>
  </div>

  <!-- Recent events (changes only) -->
  <div class="card">
    <h2>Recent Events <span style="font-size:0.75rem;font-weight:400;color:#888">(changes only — see Full Log for all checks)</span></h2>
    <div id="events-wrap">
    {% if rows %}
    <table>
      <thead><tr>
        <th>Timestamp</th><th>Room °C</th><th>AC Set °C</th>
        <th>Delta</th><th>AC Action</th><th>Fan Action</th>
      </tr></thead>
      <tbody>
      {% for r in rows %}
      <tr>
        <td>{{ r.timestamp }}</td>
        <td>{{ r.room_temp }}</td>
        <td>{{ r.ac_set_temp }}</td>
        <td>{{ r.delta }}</td>
        <td>{{ r.ac_action }}</td>
        <td>{{ r.fan_action }}</td>
      </tr>
      {% endfor %}
      </tbody>
    </table>
    {% else %}
    <p style="color:#888;font-size:0.85rem">No changes logged yet.</p>
    {% endif %}
    </div>
  </div>

  <!-- Config summary -->
  <div class="card">
    <h2>Configuration (read-only)</h2>
    <table class="config-table">
      <tbody>
        <tr><td>TARGET_ROOM_TEMP</td><td>{{ target }} °C</td></tr>
        <tr><td>FAST_THRESHOLD</td><td>{{ cfg.FAST_THRESHOLD }} °C (± threshold for fast regulator)</td></tr>
        <tr><td>SLOW_THRESHOLD</td><td>{{ cfg.SLOW_THRESHOLD }} °C (± threshold for slow regulator)</td></tr>
        <tr><td>SLOW_TAU</td><td>{{ cfg.SLOW_TAU }} min (EMA filter time constant)</td></tr>
        <tr><td>AC_TEMP_MIN</td><td>{{ cfg.AC_TEMP_MIN }} °C</td></tr>
        <tr><td>AC_TEMP_MAX</td><td>{{ cfg.AC_TEMP_MAX }} °C</td></tr>
        <tr><td>READ_INTERVAL_MINUTES</td><td>{{ cfg.READ_INTERVAL_MINUTES }} min (sensor polling)</td></tr>
        <tr><td>CHECK_INTERVAL_MINUTES</td><td>{{ cfg.CHECK_INTERVAL_MINUTES }} min (min between AC changes)</td></tr>
        <tr><td>FAN_SPEED_TABLE</td><td>{{ cfg.FAN_SPEED_TABLE }}</td></tr>
      </tbody>
    </table>
  </div>

</div>

<script>
// ── Chart renderer & Time-Window Controller ──────────────────────────────────
(function () {
  const SVG_NS = 'http://www.w3.org/2000/svg';
  const W = 900, H = 240;
  const PAD = { top: 14, right: 16, bottom: 36, left: 44 };

  let currentView = '24h';
  let currentDate = new Date();

  function makeSvgEl(tag, attrs) {
    const el = document.createElementNS(SVG_NS, tag);
    for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, v);
    return el;
  }

  function formatIsoDate(d) {
    const y = d.getFullYear();
    const m = String(d.getMonth() + 1).padStart(2, '0');
    const day = String(d.getDate()).padStart(2, '0');
    return `${y}-${m}-${day}`;
  }

  function getMonday(d) {
    const dt = new Date(d);
    const day = dt.getDay(); // 0 is Sun, 1 is Mon
    const diff = dt.getDate() - day + (day === 0 ? -6 : 1);
    return new Date(dt.getFullYear(), dt.getMonth(), diff);
  }

  function updateViewButtons() {
    document.querySelectorAll('.view-btn').forEach(btn => {
      if (btn.dataset.view === currentView) {
        btn.classList.add('active');
      } else {
        btn.classList.remove('active');
      }
    });
  }

  function setView(newView) {
    if (newView === currentView) return;
    const oldView = currentView;
    currentView = newView;

    if (newView === '24h') {
      currentDate = new Date();
    } else if (newView === 'day') {
      if (oldView === '24h') {
        currentDate = new Date();
      } else if (oldView === 'week') {
        currentDate = getMonday(currentDate);
      } else if (oldView === 'month') {
        currentDate = new Date(currentDate.getFullYear(), currentDate.getMonth(), 1);
      }
    } else if (newView === 'week') {
      if (oldView === '24h') {
        currentDate = new Date();
      } else if (oldView === 'day') {
        // Keep currentDate (the week that day is a part of)
      } else if (oldView === 'month') {
        currentDate = new Date(currentDate.getFullYear(), currentDate.getMonth(), 1);
      }
    } else if (newView === 'month') {
      if (oldView === '24h') {
        currentDate = new Date();
      } else if (oldView === 'day') {
        // Keep currentDate (month that day is part of)
      } else if (oldView === 'week') {
        currentDate = getMonday(currentDate);
      }
    }

    updateViewButtons();
    loadChart();
  }

  function navigate(step) {
    if (currentView === 'day') {
      currentDate.setDate(currentDate.getDate() + step);
    } else if (currentView === 'week') {
      currentDate.setDate(currentDate.getDate() + (step * 7));
    } else if (currentView === 'month') {
      currentDate = new Date(currentDate.getFullYear(), currentDate.getMonth() + step, 1);
    }
    loadChart();
  }

  function renderChart(data) {
    const svg = document.getElementById('tempChart');
    const emptyMsg = document.getElementById('chartEmpty');

    while (svg.firstChild) svg.removeChild(svg.firstChild);

    const pts = (data && data.points) ? data.points : [];
    const targetTemp = (data && data.target_temp !== undefined) ? data.target_temp : 22.8;
    const view = (data && data.view) ? data.view : currentView;

    if (pts.length === 0) {
      emptyMsg.style.display = '';
    } else {
      emptyMsg.style.display = 'none';
    }
    svg.style.display = '';

    // Parse start and end timestamps into local epoch ms
    const tStart = data.start_iso ? new Date(data.start_iso).getTime() : (Date.now() - 24 * 3600 * 1000);
    const tEnd   = data.end_iso   ? new Date(data.end_iso).getTime()   : Date.now();

    // Value domain: snap to whole degrees (floor min, ceil max), 1°C ticks
    let vMin, vMax;
    if (pts.length > 0) {
      const allVals = pts.flatMap(p => [p.room_temp, p.ac_set_temp]).concat([targetTemp]);
      vMin = Math.floor(Math.min(...allVals));
      vMax = Math.ceil(Math.max(...allVals));
    } else {
      vMin = Math.floor(targetTemp - 2);
      vMax = Math.ceil(targetTemp + 2);
    }
    if (vMin === vMax) {
      vMin -= 1;
      vMax += 1;
    }

    const plotW = W - PAD.left - PAD.right;
    const plotH = H - PAD.top  - PAD.bottom;

    function tx(epochMs) {
      if (tEnd <= tStart) return PAD.left;
      return PAD.left + ((epochMs - tStart) / (tEnd - tStart)) * plotW;
    }
    function ty(val) {
      return PAD.top + plotH - ((val - vMin) / (vMax - vMin)) * plotH;
    }

    // ── Grid lines & Y-axis labels (1°C per tick) ─────────────────────────
    for (let val = vMin; val <= vMax; val++) {
      const y = ty(val);
      svg.appendChild(makeSvgEl('line', {
        x1: PAD.left, y1: y, x2: W - PAD.right, y2: y,
        stroke: '#e5e7eb', 'stroke-width': '1',
      }));
      const lbl = makeSvgEl('text', {
        x: PAD.left - 5, y: y + 4,
        'text-anchor': 'end', 'font-size': '11', fill: '#888',
      });
      lbl.textContent = String(val);
      svg.appendChild(lbl);
    }

    // ── X-axis ticks & grid lines per view ─────────────────────────────────
    if (view === '24h') {
      const tStartDate = new Date(tStart);
      let firstHour = new Date(tStartDate);
      firstHour.setMinutes(0, 0, 0, 0);
      firstHour.setTime(firstHour.getTime() + 3600 * 1000);
      if (firstHour.getHours() % 2 !== 0) {
        firstHour.setTime(firstHour.getTime() + 3600 * 1000);
      }
      for (let t = firstHour.getTime(); t < tEnd; t += 3600 * 1000) {
        const x = tx(t);
        const hr = new Date(t).getHours();
        const isEven = (hr % 2 === 0);
        if (isEven) {
          svg.appendChild(makeSvgEl('line', {
            x1: x, y1: PAD.top, x2: x, y2: H - PAD.bottom,
            stroke: '#e5e7eb', 'stroke-width': '1',
          }));
          const lbl = makeSvgEl('text', {
            x: x, y: H - PAD.bottom + 14,
            'text-anchor': 'middle', 'font-size': '11', fill: '#888',
          });
          lbl.textContent = hr.toString().padStart(2, '0') + ':00';
          svg.appendChild(lbl);
        } else {
          svg.appendChild(makeSvgEl('line', {
            x1: x, y1: H - PAD.bottom, x2: x, y2: H - PAD.bottom + 4,
            stroke: '#cbd5e1', 'stroke-width': '1',
          }));
        }
      }
    } else if (view === 'day') {
      // 00:00 to 24:00 (24 hours)
      for (let hr = 0; hr <= 24; hr++) {
        const t = tStart + hr * 3600 * 1000;
        const x = tx(t);
        const isEven = (hr % 2 === 0);
        if (isEven) {
          svg.appendChild(makeSvgEl('line', {
            x1: x, y1: PAD.top, x2: x, y2: H - PAD.bottom,
            stroke: '#e5e7eb', 'stroke-width': '1',
          }));
          const lbl = makeSvgEl('text', {
            x: x, y: H - PAD.bottom + 14,
            'text-anchor': 'middle', 'font-size': '11', fill: '#888',
          });
          lbl.textContent = hr.toString().padStart(2, '0') + ':00';
          svg.appendChild(lbl);
        } else {
          svg.appendChild(makeSvgEl('line', {
            x1: x, y1: H - PAD.bottom, x2: x, y2: H - PAD.bottom + 4,
            stroke: '#cbd5e1', 'stroke-width': '1',
          }));
        }
      }
    } else if (view === 'week') {
      // Monday 00:00 to Sunday 24:00 (7 days)
      const dayNames = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];
      for (let i = 0; i < 7; i++) {
        const dayStart = tStart + i * 24 * 3600 * 1000;
        const x = tx(dayStart);
        // Major tick: 00:00 every day
        svg.appendChild(makeSvgEl('line', {
          x1: x, y1: PAD.top, x2: x, y2: H - PAD.bottom,
          stroke: '#e5e7eb', 'stroke-width': '1',
        }));
        const dDate = new Date(dayStart);
        const lbl = makeSvgEl('text', {
          x: x + 4, y: H - PAD.bottom + 14,
          'text-anchor': 'start', 'font-size': '11', fill: '#888',
        });
        lbl.textContent = dayNames[i] + ' ' + dDate.getDate();
        svg.appendChild(lbl);

        // Minor tick: 12:00 noon every day
        const noon = dayStart + 12 * 3600 * 1000;
        const noonX = tx(noon);
        svg.appendChild(makeSvgEl('line', {
          x1: noonX, y1: H - PAD.bottom, x2: noonX, y2: H - PAD.bottom + 4,
          stroke: '#cbd5e1', 'stroke-width': '1',
        }));
      }
    } else if (view === 'month') {
      // 1st 00:00 to last day 24:00
      const mStart = new Date(tStart);
      const year = mStart.getFullYear();
      const month = mStart.getMonth();
      const daysInMonth = new Date(year, month + 1, 0).getDate();

      for (let d = 1; d <= daysInMonth; d++) {
        const dTime = new Date(year, month, d).getTime();
        const x = tx(dTime);
        if (d % 2 === 0) {
          // Major tick: even days at 00:00
          svg.appendChild(makeSvgEl('line', {
            x1: x, y1: PAD.top, x2: x, y2: H - PAD.bottom,
            stroke: '#e5e7eb', 'stroke-width': '1',
          }));
          const lbl = makeSvgEl('text', {
            x: x, y: H - PAD.bottom + 14,
            'text-anchor': 'middle', 'font-size': '11', fill: '#888',
          });
          lbl.textContent = String(d);
          svg.appendChild(lbl);
        } else {
          // Minor tick: odd days at 00:00
          svg.appendChild(makeSvgEl('line', {
            x1: x, y1: H - PAD.bottom, x2: x, y2: H - PAD.bottom + 4,
            stroke: '#cbd5e1', 'stroke-width': '1',
          }));
        }
      }
    }

    // ── Clip rect so lines don't overflow plot area ───────────────────────
    const clipId = 'chartClip';
    const defs = makeSvgEl('defs', {});
    const clip = makeSvgEl('clipPath', { id: clipId });
    clip.appendChild(makeSvgEl('rect', {
      x: PAD.left, y: PAD.top, width: plotW, height: plotH,
    }));
    defs.appendChild(clip);
    svg.appendChild(defs);

    // Segmented polyline builder (breaks across missing data gaps)
    function buildPolylineSegments(series, color, dasharray, maxGapMs) {
      if (series.length < 1) return;

      const chunks = [];
      let curChunk = [series[0]];
      for (let i = 1; i < series.length; i++) {
        if (series[i][0] - series[i - 1][0] > maxGapMs) {
          chunks.push(curChunk);
          curChunk = [];
        }
        curChunk.push(series[i]);
      }
      if (curChunk.length > 0) chunks.push(curChunk);

      for (const chunk of chunks) {
        if (chunk.length === 1) {
          const [t, v] = chunk[0];
          svg.appendChild(makeSvgEl('circle', {
            cx: tx(t), cy: ty(v), r: '2.5',
            fill: color,
            'clip-path': 'url(#' + clipId + ')',
          }));
          continue;
        }
        const pointsStr = chunk.map(([t, v]) => tx(t).toFixed(1) + ',' + ty(v).toFixed(1)).join(' ');
        const attrs = {
          points: pointsStr,
          fill: 'none',
          stroke: color,
          'stroke-width': '2',
          'stroke-linejoin': 'miter',
          'stroke-linecap': 'square',
          'clip-path': 'url(#' + clipId + ')',
        };
        if (dasharray) attrs['stroke-dasharray'] = dasharray;
        svg.appendChild(makeSvgEl('polyline', attrs));
      }
    }

    function parseTS(s) {
      return new Date(s).getTime();
    }

    // Target temp: horizontal dashed line across full width
    const targetY = ty(targetTemp);
    svg.appendChild(makeSvgEl('line', {
      x1: PAD.left, y1: targetY, x2: W - PAD.right, y2: targetY,
      stroke: '#16a34a', 'stroke-width': '2',
      'stroke-dasharray': '7,4',
      'clip-path': 'url(#' + clipId + ')',
    }));

    if (pts.length > 0) {
      const roomSeries = pts.map(p => [parseTS(p.timestamp), p.room_temp]);
      const acSeries   = pts.map(p => [parseTS(p.timestamp), p.ac_set_temp]);

      let maxGapMs = 30 * 60 * 1000;
      if (view === 'week') maxGapMs = 2 * 3600 * 1000;
      if (view === 'month') maxGapMs = 6 * 3600 * 1000;

      buildPolylineSegments(acSeries,   '#f59e0b', null, maxGapMs);
      buildPolylineSegments(roomSeries, '#2563eb', null, maxGapMs);
    }

    // Axes borders
    svg.appendChild(makeSvgEl('line', {
      x1: PAD.left, y1: PAD.top, x2: PAD.left, y2: H - PAD.bottom,
      stroke: '#cbd5e1', 'stroke-width': '1',
    }));
    svg.appendChild(makeSvgEl('line', {
      x1: PAD.left, y1: H - PAD.bottom, x2: W - PAD.right, y2: H - PAD.bottom,
      stroke: '#cbd5e1', 'stroke-width': '1',
    }));
  }

  function loadChart() {
    const url = `/api/chart?view=${currentView}&date=${formatIsoDate(currentDate)}`;
    fetch(url)
      .then(r => r.json())
      .then(data => {
        const chartNav = document.getElementById('chartNav');
        const navTitle = document.getElementById('navTitle');
        const navNext = document.getElementById('navNext');
        const chartTitle = document.getElementById('chartTitle');

        if (currentView === '24h') {
          chartNav.style.display = 'none';
          chartTitle.textContent = 'Last 24 Hours';
        } else {
          chartNav.style.display = 'flex';
          chartTitle.textContent = currentView.charAt(0).toUpperCase() + currentView.slice(1);
          navTitle.textContent = data.label || '';
          navNext.disabled = !data.can_go_next;
        }

        renderChart(data);
      })
      .catch(() => {});
  }

  // Button handlers
  document.querySelectorAll('.view-btn').forEach(btn => {
    btn.addEventListener('click', () => setView(btn.dataset.view));
  });
  document.getElementById('navPrev').addEventListener('click', () => navigate(-1));
  document.getElementById('navNext').addEventListener('click', () => navigate(1));

  // Initial load
  loadChart();

  // Expose loadChart for auto-refresh
  window._loadChart = loadChart;
})();

// ── Events table rebuilder ────────────────────────────────────────────────────
function rebuildEventsTable(rows) {
  const wrap = document.getElementById('events-wrap');
  if (!wrap) return;
  if (!rows || rows.length === 0) {
    wrap.innerHTML = '<p style="color:#888;font-size:0.85rem">No changes logged yet.</p>';
    return;
  }
  const cols = ['timestamp','room_temp','ac_set_temp','delta','ac_action','fan_action'];
  const heads = ['Timestamp','Room °C','AC Set °C','Delta','AC Action','Fan Action'];
  let html = '<table><thead><tr>' +
    heads.map(h => '<th>' + h + '</th>').join('') +
    '</tr></thead><tbody>';
  for (const r of rows) {
    html += '<tr>' + cols.map(c => '<td>' + (r[c] ?? '') + '</td>').join('') + '</tr>';
  }
  html += '</tbody></table>';
  wrap.innerHTML = html;
}

// ── Countdown / auto-refresh loop ─────────────────────────────────────────────
let countdown = 300;
const countEl = document.getElementById('countdown');
setInterval(() => {
  countdown--;
  if (countEl) countEl.textContent = countdown;
  if (countdown <= 0) {
    countdown = 300;

    // Refresh status
    fetch('/api/status').then(r => r.json()).then(d => {
      const set = (id, v) => { const el = document.getElementById(id); if(el && v !== undefined) el.textContent = v; };
      set('room_temp',   d.room_temp   ?? '—');
      set('avg_temp',    (d.avg_temp !== undefined && d.avg_temp !== null) ? Number(d.avg_temp).toFixed(2) : '—');
      set('ac_set_temp', d.ac_set_temp ?? '—');
      set('ac_fan',      d.ac_fan      ?? '—');
      set('ac_mode',     d.ac_mode     ?? '—');
      set('last_checked',d.last_checked ?? '—');
      if (d.room_temp) {
        const delta = (parseFloat(d.room_temp) - {{ target }}).toFixed(1);
        const el = document.getElementById('delta');
        if (el) {
          el.textContent = (delta >= 0 ? '+' : '') + delta;
          el.className = 'value ' + (Math.abs(delta) < {{ cfg.FAST_THRESHOLD }} ? 'delta-ok' : delta < 0 ? 'delta-cold' : 'delta-warm');
        }
      }
    }).catch(() => {});

    // Refresh chart (maintains currently selected view and date)
    if (window._loadChart) window._loadChart();

    // Refresh events table
    fetch('/api/events').then(r => r.json()).then(rebuildEventsTable).catch(() => {});
  }
}, 1000);
</script>
</body></html>
"""



_LOG_TEMPLATE = """
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Sensibo Reg — Full Log</title>
  """ + _BASE_STYLE + """
</head>
<body>
<div class="navbar">
  <h1>🌡 Sensibo Temperature Regulator — Log</h1>
  <span><a href="/">Dashboard</a> &nbsp;|&nbsp; <a href="/logout">Logout</a></span>
</div>
<div class="container">
  <div class="card">
    <h2>Full Event Log (newest first, max 500 rows)</h2>
    {% if rows %}
    <table>
      <thead><tr>
        <th>Timestamp</th><th>Room °C</th><th>AC Set °C</th>
        <th>Delta</th><th>AC Action</th><th>Fan Action</th>
      </tr></thead>
      <tbody>
      {% for r in rows %}
      <tr>
        <td>{{ r.timestamp }}</td>
        <td>{{ r.room_temp }}</td>
        <td>{{ r.ac_set_temp }}</td>
        <td>{{ r.delta }}</td>
        <td>{{ r.ac_action }}</td>
        <td>{{ r.fan_action }}</td>
      </tr>
      {% endfor %}
      </tbody>
    </table>
    {% else %}
    <p style="color:#888;font-size:0.85rem">No log entries yet.</p>
    {% endif %}
  </div>
</div>
</body></html>
"""

# ── Flask routes ──────────────────────────────────────────────────────────────

@app.route("/login", methods=["GET", "POST"])
def login_page():
    error = None
    if request.method == "POST":
        if (request.form.get("username") == WEB_USERNAME and
                request.form.get("password") == WEB_PASSWORD):
            session["logged_in"] = True
            return redirect(request.args.get("next") or url_for("dashboard"))
        error = "Invalid username or password."
    return render_template_string(_LOGIN_TEMPLATE, error=error)

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login_page"))

@app.route("/")
@login_required
def dashboard():
    s = get_shared_state()
    rows = read_event_rows(max_rows=20)   # only rows where something changed

    delta_class = "delta-ok"
    if s.get("room_temp") and s["room_temp"] != "N/A":
        delta = float(s["room_temp"]) - TARGET_ROOM_TEMP
        if delta < -FAST_THRESHOLD:
            delta_class = "delta-cold"
        elif delta > FAST_THRESHOLD:
            delta_class = "delta-warm"

    return render_template_string(
        _DASHBOARD_TEMPLATE,
        s=s,
        rows=rows,
        target=TARGET_ROOM_TEMP,
        delta_class=delta_class,
        cfg={
            "FAST_THRESHOLD": FAST_THRESHOLD,
            "SLOW_THRESHOLD": SLOW_THRESHOLD,
            "SLOW_TAU": SLOW_TAU,
            "AC_TEMP_MIN": AC_TEMP_MIN,
            "AC_TEMP_MAX": AC_TEMP_MAX,
            "READ_INTERVAL_MINUTES": READ_INTERVAL_MINUTES,
            "CHECK_INTERVAL_MINUTES": CHECK_INTERVAL_MINUTES,
            "FAN_SPEED_TABLE": FAN_SPEED_TABLE,
        },
    )


@app.route("/api/status")
@login_required
def api_status():
    s = get_shared_state()
    return jsonify(s)


@app.route("/api/chart")
@login_required
def api_chart():
    """
    Return chart data for the requested time window.

    Query parameters:
      view — '24h' (default), 'day', 'week', 'month'
      date — 'YYYY-MM-DD' anchor date (default: today)
    """
    view = request.args.get("view", "").strip().lower()
    if not view:
        # Fallback if span_hours was supplied
        if request.args.get("span_hours"):
            view = "24h"
        else:
            view = "24h"
    elif view not in ("24h", "day", "week", "month"):
        view = "24h"

    date_str = request.args.get("date")
    data = read_chart_data(view=view, date_str=date_str)
    return jsonify(data)


@app.route("/api/events")
@login_required
def api_events():
    """Return the most-recent event rows (changed=='yes') as JSON for live table refresh."""
    rows = read_event_rows(max_rows=20)
    return jsonify(rows)


@app.route("/log")
@login_required
def log_view():
    rows = read_log_rows(max_rows=500)
    return render_template_string(_LOG_TEMPLATE, rows=rows)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    global SENSIBO_DEVICE_ID

    if SENSIBO_API_KEY == "YOUR_API_KEY_HERE":
        print("ERROR: Please set SENSIBO_API_KEY in the config section at the top of this file.")
        sys.exit(1)

    client = SensiboClient(SENSIBO_API_KEY)

    # Device picker
    device_id = SENSIBO_DEVICE_ID.strip()
    if not device_id:
        try:
            device_id = client.pick_device_interactive()
        except SensiboError as exc:
            print(f"ERROR: {exc}")
            sys.exit(1)

    # Start regulation loop in a daemon thread
    reg_thread = threading.Thread(
        target=regulation_loop,
        args=(client, device_id),
        daemon=True,
        name="regulation-loop",
    )
    reg_thread.start()

    # Start Flask (blocking, main thread)
    log.info("Web dashboard: http://localhost:%d", WEB_PORT)
    log.info("Login with username '%s'", WEB_USERNAME)
    app.run(host=WEB_HOST, port=WEB_PORT, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()
