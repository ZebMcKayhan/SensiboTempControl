# Sensibo Temperature Regulator

A Python script that maintains a steady **room temperature** using a [Sensibo](https://sensibo.com/) smart AC controller. Because the AC unit's own temperature sensor sits up near the ceiling or roof, it can read quite differently from the actual room temperature. This script uses the **Sensibo pod's built-in room sensor** as the true reference and automatically nudges the AC set-point up or down to compensate — without you having to touch the remote.

It also adjusts **fan speed** automatically based on the AC's current set-point, so the fan runs harder when the unit is working against cold outdoor temperatures and softer when conditions are mild.

A built-in **web dashboard** lets you monitor what the script is doing from any browser on your network.

---

## How it works

### The problem

A heat pump measures temperature at the unit itself, which is typically mounted high on a wall or near the ceiling. In cold weather the ceiling is significantly warmer than the floor where you actually sit. The AC thinks the room is warm enough and reduces output, but the room still feels cold.

### The solution

Every 30 minutes (configurable) the script:

1. **Reads** the room temperature from the Sensibo pod's sensor (at desk/sofa height).
2. **Reads** the AC's current set-point and fan speed via the Sensibo API.
3. **Compares** the measured room temperature to your `TARGET_ROOM_TEMP`.
4. **Adjusts** the AC set-point by ±1 °C if the room is too cold or too warm:

```
room_temp < TARGET − 0.5 °C  →  AC set-point +1 °C  (room too cold)
room_temp > TARGET + 0.6 °C  →  AC set-point −1 °C  (room too warm)
otherwise                    →  no change
```

5. **Adjusts fan speed** based on the (possibly updated) AC set-point using your fan-speed table.
6. **Logs** every cycle to a file and updates the web dashboard.

### Key design decisions

- **Stateless** — each cycle reads fresh data from the API and makes decisions purely on current readings. Nothing is stored between cycles.
- **Heat-mode only** — the script does nothing if the AC is off or in any mode other than `heat`.
- **Safe limits** — the AC set-point is never pushed below `AC_TEMP_MIN` (20 °C) or above `AC_TEMP_MAX` (27 °C).
- **Fan speed is optional** — if the current AC set-point does not appear in your fan-speed table, the fan is left exactly as it is.
- **Slow reaction is intentional** — room temperature has a time constant of many hours. Checking every 30 minutes avoids overshooting and ignores transient events like the unit defrosting.

### Fan speed table

You define a dictionary that maps AC set-point temperatures to fan speeds (integers 1–6):

| Integer | Sensibo fan level |
|---------|-------------------|
| 1 | Quiet |
| 2 | Low |
| 3 | Average Low (`medium_low`) |
| 4 | Average (`medium`) |
| 5 | Average High (`medium_high`) |
| 6 | High |

Example: when the AC is told to heat to 24 °C (needing to work harder), the fan runs at speed 6 (High). At 20 °C it runs at 4 (Average).

---

## Files

```
SensiboTempReg/
├── sensibo_temp_reg.py   ← single script, all logic inside
├── requirements.txt      ← Python dependencies
├── README.md             ← this file
└── sensibo_reg.log       ← created automatically on first run
```

---

## Requirements

- Python 3.10 or newer
- A [Sensibo](https://sensibo.com/) pod connected to your AC
- A Sensibo API key (free, see below)

---

## Installation

### 1. Get your Sensibo API key

1. Log in to [home.sensibo.com](https://home.sensibo.com).
2. Go to **My Account** (top-right menu).
3. Scroll down to **API Keys**.
4. Click **Generate Key** and copy the key.

### 2. Clone or download the project

```powershell
# If you have git:
git clone https://github.com/ZebMcKayhan/SensiboTempControl SensiboTempReg
cd SensiboTempReg

# Or just download sensibo_temp_reg.py and requirements.txt into a folder.
```

### 3. Install dependencies

```powershell
python -m pip install -r requirements.txt
```

This installs `flask` and `requests`. Both are small and have no heavy dependencies.

### 4. Configure the script

Open `sensibo_temp_reg.py` in any text editor. All settings are at the **very top of the file** between the two `===` banners — you will not need to touch anything else.

```python
# ── Sensibo API ───────────────────────────────────────────────────────────────
SENSIBO_API_KEY   = "paste_your_api_key_here"
SENSIBO_DEVICE_ID = ""          # leave blank on first run (see below)

# ── Target room temperature ───────────────────────────────────────────────────
TARGET_ROOM_TEMP = 22           # °C — increase to 23 in deep winter if needed

# ── Regulation thresholds ─────────────────────────────────────────────────────
UNDERSHOOT_THRESHOLD = 0.5      # °C below target → increase AC set-point by 1
OVERSHOOT_THRESHOLD  = 0.6      # °C above target → decrease AC set-point by 1

# ── AC set-point limits ───────────────────────────────────────────────────────
AC_TEMP_MIN = 20                # °C — never set AC below this
AC_TEMP_MAX = 27                # °C — never set AC above this

# ── Fan-speed table  (AC set-point → fan speed 1–6) ─────────────────────────
FAN_SPEED_TABLE = {
    20: 2,
    21: 2,
    22: 2,
    23: 3,
    24: 4,
    25: 5,
    26: 6,
}

# ── Polling ───────────────────────────────────────────────────────────────────
CHECK_INTERVAL_MINUTES = 30

# ── Web server ────────────────────────────────────────────────────────────────
WEB_HOST     = "0.0.0.0"
WEB_PORT     = 8080
WEB_USERNAME = "admin"
WEB_PASSWORD = "changeme"       # ← change this!
SESSION_SECRET_KEY = "change-this-to-a-random-string"  # any random text
```

> **Tip:** `SESSION_SECRET_KEY` is used to sign login cookies. Set it to any long random string — it just needs to be consistent while the script is running.

---

## Running

```powershell
python sensibo_temp_reg.py
```

### First run (device picker)

If `SENSIBO_DEVICE_ID` is blank, the script queries your account and lets you choose:

```
=== Sensibo Device Picker ===
  1. Living Room  (ID: abCdeFg)
Select device [1-1]: 1

Using: Living Room (abCdeFg)
Tip: set SENSIBO_DEVICE_ID = "abCdeFg" in the config to skip this step next time.
```

Paste the printed ID into `SENSIBO_DEVICE_ID` in the config so you won't be prompted again.

### Console output

```
2026-10-10 21:30:00 [INFO] Regulation loop started. Interval: 30 min
2026-10-10 21:30:00 [INFO] Web dashboard: http://localhost:8080
2026-10-10 21:30:00 [INFO] --- Regulation cycle start ---
2026-10-10 21:30:01 [INFO] Room: 21.4°C  Target: 22.0°C  AC set: 22°C  Delta: -0.60°C
2026-10-10 21:30:01 [INFO] Set-point increased: 22 → 23°C
2026-10-10 21:30:02 [INFO] Fan speed set to 3 (medium_low)
2026-10-10 21:30:02 [INFO] --- Next check in 30 min ---
```

### Web dashboard

Open a browser and go to:

```
http://<your-pc-ip>:8080
```

or `http://localhost:8080` if you are on the same machine. Log in with the username and password from the config.

The dashboard shows:

- **Current status** — room temperature, target, AC set-point, fan speed, mode, last checked time
- **Delta indicator** — colour-coded: 🔵 room too cold / 🟢 within range / 🔴 room too warm
- **Recent events** — last 20 regulation cycles
- **Configuration** — read-only display of all your settings
- **Full log** — available at `/log`, shows up to 500 historical entries

The status card refreshes automatically every 60 seconds.

---

## Running automatically on Windows startup (optional)

To have the script start automatically when Windows boots, create a scheduled task:

1. Open **Task Scheduler** → **Create Basic Task**
2. Set trigger: **When the computer starts**
3. Set action: **Start a program**
   - Program: `python`
   - Arguments: `sensibo_temp_reg.py`
   - Start in: `C:\path\to\SensiboTempReg`
4. Under **General**, tick **Run whether user is logged on or not**

Alternatively, wrap it in a `.bat` file:

```bat
@echo off
python C:\path\to\SensiboTempReg\sensibo_temp_reg.py
```

---

## Seasonal tuning

The main setting you will want to adjust as seasons change is `TARGET_ROOM_TEMP`:

| Season | Suggested `TARGET_ROOM_TEMP` |
|--------|------------------------------|
| Autumn / Spring | 21–22 °C |
| Winter | 22–23 °C |

You can also tune `FAN_SPEED_TABLE` if you find the fan is too loud or too quiet at certain set-points.

After any config change, restart the script for the new settings to take effect.

---

## Troubleshooting

| Symptom | Check |
|---------|-------|
| `Device not found in account` | Verify `SENSIBO_DEVICE_ID` matches the ID printed during first run |
| `API call failed after 3 attempts` | Check `SENSIBO_API_KEY` is correct and you have internet access |
| Script does nothing | Confirm the AC is **on** and set to **heat** mode — the script skips all other modes |
| Set-point stuck at limit | `AC_TEMP_MIN` / `AC_TEMP_MAX` is being hit; widen the range if intentional |
| Web dashboard not loading | Check `WEB_PORT` is not blocked by a firewall; try `http://localhost:8080` first |
