# data/

Runtime state shared by the two services. Everything here except this file is git-ignored.

| File | Written by | Read by |
|---|---|---|
| `robot_speed.json` | copilot API (speed commands), robot controller (start-up) | both |
| `robot_status.json` | robot controller | copilot API → dashboard, energy model |
| `step_tracking_data.json` | copilot API (step updates posted by the robot controller) | copilot API |
| `cycle_timing.json` | robot controller (measured), copilot API (speed changes) | copilot API |
| `robot_reset_command.json` | copilot API (fallback when the robot API is unreachable) | – |
| `voice_status.json` | voice assistant | copilot API → dashboard |
| `tia_cache_*.json`, `plc_tags.json`, `plc_hardware.json` | TIA Openness module | TIA Openness module (JSON cache mode) |

Set `COPILOT_DATA_DIR` to use a different folder.
