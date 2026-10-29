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
4. **Adjusts** the AC set-point by ±1 °C using a dual-regulator approach:
   - **Fast regulator** (instantaneous):
     ```
     room_temp < TARGET − FAST_THRESHOLD  →  AC set-point +1 °C  (room too cold)
     room_temp > TARGET + FAST_THRESHOLD  →  AC set-point −1 °C  (room too warm)
     ```
   - **Slow regulator** (EMA integrator, evaluated only when fast regulator is satisfied and not rate-limited):
     ```
     AvgTemp < TARGET − SLOW_THRESHOLD  →  AC set-point +1 °C
     AvgTemp > TARGET + SLOW_THRESHOLD  →  AC set-point −1 °C
     ```
   - Otherwise → no change.

5. **Adjusts fan speed** based on the (possibly updated) AC set-point using your fan-speed table.
6. **Logs** every cycle to a file and updates the web dashboard.

### Key design decisions

- **Dual-regulator control** — combines a fast regulator for immediate threshold excursions with an in-memory EMA filter for subtle long-term temperature drift, while keeping the on-disk log format clean and consistent.
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
├── sensibo_temp_reg.py          ← main regulator script and web server
├── sensibo_temp_reg_setting.json ← persistent configuration file (created automatically)
├── requirements.txt             ← Python dependencies
├── README.md                    ← this file
└── sensibo_reg.log              ← created automatically on first run
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

Configuration settings are stored in `sensibo_temp_reg_setting.json` in the same directory. If the file does not exist when starting `sensibo_temp_reg.py`, it is automatically created using the built-in defaults from the script.

You can set your `SENSIBO_API_KEY` in `sensibo_temp_reg_setting.json`:

```json
{
  "SENSIBO_API_KEY": "YOUR_API_KEY_HERE",
  "SENSIBO_DEVICE_ID": "",
  "ROOM_TEMP_OFFSET": -0.4,
  "TARGET_ROOM_TEMP": 22.8,
  "FAST_THRESHOLD": 0.4,
  "SLOW_THRESHOLD": 0.2,
  "SLOW_TAU": 120,
  "AC_TEMP_MIN": 20,
  "AC_TEMP_MAX": 27,
  "FAN_SPEED_TABLE": {
    "20": 4,
    "21": 4,
    "22": 5,
    "23": 5,
    "24": 6,
    "25": 6,
    "26": 6
  },
  "READ_INTERVAL_MINUTES": 5,
  "CHECK_INTERVAL_MINUTES": 30,
  "WEB_HOST": "0.0.0.0",
  "WEB_PORT": 8585,
  "WEB_USERNAME": "admin",
  "WEB_PASSWORD": "changeme",
  "SESSION_SECRET_KEY": "change-this-to-a-random-string",
  "LOG_FILE": "/var/log/sensibo/sensibo_reg.log"
}
```

- **`ROOM_TEMP_OFFSET`** (`-0.4` °C): The Sensibo sensor often reads ~0.4 °C too high. This offset is applied immediately to the raw reading (`adjusted_room_temp = sensibo_room_temp + ROOM_TEMP_OFFSET`) and used across all regulators, filters, and displays.
- **`TARGET_ROOM_TEMP`**: Can also be adjusted directly from the web dashboard using the `↑` and `↓` buttons in 0.1 °C increments.
- **Defaults & updates**: The Python script retains default values. If future script updates introduce new parameters, missing keys in your existing JSON file are automatically populated with defaults while fully preserving your existing custom settings.

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
Device ID 'abCdeFg' saved to sensibo_temp_reg_setting.json
```

The chosen device ID is automatically saved to `sensibo_temp_reg_setting.json` so you will not be prompted again on subsequent runs.

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

- **Current status** — room temperature, average temperature (EMA), target (with `↑` and `↓` buttons to adjust target temperature in 0.1 °C steps), AC set-point, fan speed, mode, last checked time
- **Delta indicator** — colour-coded: 🔵 room too cold / 🟢 within range / 🔴 room too warm
- **Recent events** — last 20 regulation cycles
- **Configuration** — display of settings loaded from `sensibo_temp_reg_setting.json`
- **Full log** — available at `/log`, shows up to 500 historical entries

The status card refreshes automatically every 300 seconds (or immediately when adjusting target temperature).

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

Target temperature adjustments made through the GUI take effect immediately without restarting the service and persist across restarts. For other manual edits in `sensibo_temp_reg_setting.json`, restart the script for the new settings to take effect.

---

## Troubleshooting

| Symptom | Check |
|---------|-------|
| `Device not found in account` | Verify `SENSIBO_DEVICE_ID` matches the ID printed during first run |
| `API call failed after 3 attempts` | Check `SENSIBO_API_KEY` is correct and you have internet access |
| Script does nothing | Confirm the AC is **on** and set to **heat** mode — the script skips all other modes |
| Set-point stuck at limit | `AC_TEMP_MIN` / `AC_TEMP_MAX` is being hit; widen the range if intentional |
| Web dashboard not loading | Check `WEB_PORT` is not blocked by a firewall; try `http://localhost:8080` first |
