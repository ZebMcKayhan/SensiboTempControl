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
SENSIBO_API_KEY   = "PutYourAPIKeyHere"
SENSIBO_DEVICE_ID = ""          # leave blank → interactive device picker on startup

# ── Target room temperature ───────────────────────────────────────────────────
TARGET_ROOM_TEMP = 22.8          # °C  — adjust seasonally (e.g. 23 in deep winter)

# ── Regulation thresholds ─────────────────────────────────────────────────────
UNDERSHOOT_THRESHOLD = 0.4      # °C below target → increase AC set-point by 1
OVERSHOOT_THRESHOLD  = 0.4      # °C above target → decrease AC set-point by 1

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
LOG_FILE = "sensibo_reg.log"    # relative to script directory; created automatically

# =============================================================================
# END OF CONFIG — do not edit below unless you know what you're doing
# =============================================================================

import csv
import io
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
# Log file helpers
# ---------------------------------------------------------------------------
_log_lock = threading.Lock()
LOG_FIELDS = ["timestamp", "room_temp", "ac_set_temp", "delta", "ac_action", "fan_action", "changed"]

def _log_file_path() -> str:
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), LOG_FILE)

def append_log_row(row: dict):
    """Append one CSV row to the log file (thread-safe)."""
    path = _log_file_path()
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

def regulate(client: SensiboClient, device_id: str, allow_ac_change: bool) -> None:
    """
    Regulation cycle.  Reads current state from the Sensibo API, decides
    whether to adjust AC set-point and/or fan speed, applies any changes in a
    single API call, and appends a log row.

    *allow_ac_change* controls whether the AC set-point may be modified this
    cycle.  Even when False the sensor data is still read and logged so the
    dashboard stays current.
    """
    now = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    ac_action = "Error"
    fan_action = "Error"
    room_temp = None
    ac_set_temp = None
    delta = None
    changed = False   # True when any AC/fan command was actually sent

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

        update_shared_state(
            room_temp=room_temp,
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
            # 3. Temperature regulation
            delta = round(room_temp - TARGET_ROOM_TEMP, 2)
            log.info(
                "Room: %.1f°C  Target: %.1f°C  AC set: %d°C  Delta: %+.2f°C%s",
                room_temp, TARGET_ROOM_TEMP, ac_set_temp, delta,
                "" if allow_ac_change else "  [set-point locked — rate limit]",
            )

            # Decide desired new set-point (may be same as current)
            desired_temp = ac_set_temp
            if allow_ac_change:
                if delta < -UNDERSHOOT_THRESHOLD:
                    new_temp = min(ac_set_temp + 1, AC_TEMP_MAX)
                    if new_temp != ac_set_temp:
                        desired_temp = new_temp
                        ac_action = f"Increased set-point to {new_temp}°C"
                    else:
                        log.warning("Already at AC_TEMP_MAX (%d°C), cannot increase further", AC_TEMP_MAX)
                        ac_action = f"At max limit ({AC_TEMP_MAX}°C), no increase"
                elif delta > OVERSHOOT_THRESHOLD:
                    new_temp = max(ac_set_temp - 1, AC_TEMP_MIN)
                    if new_temp != ac_set_temp:
                        desired_temp = new_temp
                        ac_action = f"Decreased set-point to {new_temp}°C"
                    else:
                        log.warning("Already at AC_TEMP_MIN (%d°C), cannot decrease further", AC_TEMP_MIN)
                        ac_action = f"At min limit ({AC_TEMP_MIN}°C), no decrease"
                else:
                    ac_action = "No change"
            else:
                ac_action = "No change (rate limited)"

            # 4. Determine desired fan speed
            desired_fan = None   # None means "no fan change needed"
            target_set_for_fan = desired_temp   # use the new set-point for fan lookup
            if target_set_for_fan in FAN_SPEED_TABLE:
                target_fan_int = FAN_SPEED_TABLE[target_set_for_fan]
                target_fan_str = INT_TO_FAN.get(target_fan_int)
                if target_fan_str is None:
                    log.warning("Fan speed integer %d has no API mapping, skipping", target_fan_int)
                    fan_action = f"Invalid fan speed int {target_fan_int}"
                elif ac_fan != target_fan_str:
                    desired_fan = target_fan_str
                    fan_action = f"Fan → {target_fan_int} ({target_fan_str})"
                else:
                    fan_action = f"Fan unchanged ({target_fan_int})"
            else:
                fan_action = f"Fan unchanged (no table entry for {target_set_for_fan}°C)"
                log.info("No fan table entry for set-point %d°C, fan left as-is", target_set_for_fan)

            # 5. Apply changes — combine into a single API call if possible
            temp_changed = desired_temp != ac_set_temp
            fan_changed  = desired_fan is not None

            if temp_changed or fan_changed:
                updates: dict = {}
                if temp_changed:
                    updates["targetTemperature"] = desired_temp
                    log.info(
                        "Set-point %s: %d → %d°C",
                        "increased" if desired_temp > ac_set_temp else "decreased",
                        ac_set_temp, desired_temp,
                    )
                if fan_changed:
                    updates["fanLevel"] = desired_fan
                    log.info("Fan speed → %s", desired_fan)

                if temp_changed and fan_changed:
                    # Single combined call — avoids double beep
                    client.set_ac_state_fields(device_id, ac_state, updates)
                elif temp_changed:
                    client.set_ac_state_field(device_id, "targetTemperature", desired_temp)
                else:
                    client.set_ac_state_field(device_id, "fanLevel", desired_fan)

                # Update shared state to reflect applied changes
                if temp_changed:
                    ac_set_temp = desired_temp
                if fan_changed:
                    update_shared_state(ac_fan=desired_fan, ac_set_temp=ac_set_temp)
                else:
                    update_shared_state(ac_set_temp=ac_set_temp)

                changed = True

    except SensiboError as exc:
        log.error("Regulation cycle failed: %s", exc)
        ac_action = f"API Error: {exc}"
        fan_action = "N/A"

    # 6. Append log row (every cycle so log is complete)
    append_log_row({
        "timestamp": now,
        "room_temp": f"{room_temp:.1f}" if room_temp is not None else "N/A",
        "ac_set_temp": ac_set_temp if ac_set_temp is not None else "N/A",
        "delta": f"{delta:+.2f}" if delta is not None else "N/A",
        "ac_action": ac_action,
        "fan_action": fan_action,
        "changed": "yes" if changed else "no",
    })


def regulation_loop(client: SensiboClient, device_id: str) -> None:
    """
    Background thread: poll the sensor every READ_INTERVAL_MINUTES but only
    allow AC set-point changes once per CHECK_INTERVAL_MINUTES.
    """
    log.info(
        "Regulation loop started. Read every %d min, AC changes every %d min.",
        READ_INTERVAL_MINUTES, CHECK_INTERVAL_MINUTES,
    )
    last_ac_change_time: float = 0.0   # epoch seconds of last set-point change

    while True:
        now_epoch = time.time()
        minutes_since_last = (now_epoch - last_ac_change_time) / 60.0
        allow_ac_change = minutes_since_last >= CHECK_INTERVAL_MINUTES

        log.info(
            "--- Regulation cycle start (AC change %s) ---",
            "allowed" if allow_ac_change else f"locked for {CHECK_INTERVAL_MINUTES - minutes_since_last:.0f} more min",
        )

        # Remember set-point before the cycle to detect if it was changed
        state_before = get_shared_state()
        set_temp_before = state_before.get("ac_set_temp")

        regulate(client, device_id, allow_ac_change=allow_ac_change)

        state_after = get_shared_state()
        set_temp_after = state_after.get("ac_set_temp")
        if set_temp_after != set_temp_before:
            last_ac_change_time = time.time()

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
      &nbsp;·&nbsp; Next status refresh in <span id="countdown">300</span>s
    </p>
  </div>

  <!-- Recent events (changes only) -->
  <div class="card">
    <h2>Recent Events <span style="font-size:0.75rem;font-weight:400;color:#888">(changes only — see Full Log for all checks)</span></h2>
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

  <!-- Config summary -->
  <div class="card">
    <h2>Configuration (read-only)</h2>
    <table class="config-table">
      <tbody>
        <tr><td>TARGET_ROOM_TEMP</td><td>{{ target }} °C</td></tr>
        <tr><td>UNDERSHOOT_THRESHOLD</td><td>{{ cfg.UNDERSHOOT_THRESHOLD }} °C</td></tr>
        <tr><td>OVERSHOOT_THRESHOLD</td><td>{{ cfg.OVERSHOOT_THRESHOLD }} °C</td></tr>
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
let countdown = 300;
const countEl = document.getElementById('countdown');
setInterval(() => {
  countdown--;
  if (countEl) countEl.textContent = countdown;
  if (countdown <= 0) {
    countdown = 300;
    fetch('/api/status').then(r => r.json()).then(d => {
      const set = (id, v) => { const el = document.getElementById(id); if(el && v !== undefined) el.textContent = v; };
      set('room_temp',   d.room_temp   ?? '—');
      set('ac_set_temp', d.ac_set_temp ?? '—');
      set('ac_fan',      d.ac_fan      ?? '—');
      set('ac_mode',     d.ac_mode     ?? '—');
      set('last_checked',d.last_checked ?? '—');
      if (d.room_temp) {
        const delta = (parseFloat(d.room_temp) - {{ target }}).toFixed(1);
        const el = document.getElementById('delta');
        if (el) {
          el.textContent = (delta >= 0 ? '+' : '') + delta;
          el.className = 'value ' + (Math.abs(delta) < 0.3 ? 'delta-ok' : delta < 0 ? 'delta-cold' : 'delta-warm');
        }
      }
    });
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
        if delta < -UNDERSHOOT_THRESHOLD:
            delta_class = "delta-cold"
        elif delta > OVERSHOOT_THRESHOLD:
            delta_class = "delta-warm"

    return render_template_string(
        _DASHBOARD_TEMPLATE,
        s=s,
        rows=rows,
        target=TARGET_ROOM_TEMP,
        delta_class=delta_class,
        cfg={
            "UNDERSHOOT_THRESHOLD": UNDERSHOOT_THRESHOLD,
            "OVERSHOOT_THRESHOLD": OVERSHOOT_THRESHOLD,
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
