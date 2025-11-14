# HTTP API

Both services are FastAPI apps and publish interactive OpenAPI docs:
<http://localhost:8000/docs> (copilot API) and <http://localhost:8001/docs> (robot controller).

> [!WARNING]
> The API has **no authentication**. Anyone who can reach it can start, stop and reconfigure the
> cell. Run it only on an isolated machine network; see [SECURITY.md](../SECURITY.md).

## Copilot API, port 8000

### Chat

| Method | Path | Description |
|---|---|---|
| `POST` | `/api/command` | Main entry point. Body `{"command": "set speed to 80%", "session_id": "tab-1"}` → `{"response": "<markdown>", "plc_status": {…}}` |
| `GET` | `/api/initial_greeting` | Greeting with the current cell state, shown when the page loads |
| `GET` | `/` | The dashboard (static files from `tia_s7_copilot/web`) |

```bash
curl -s localhost:8000/api/command -H "Content-Type: application/json" \
     -d '{"command": "what will my cycle time be at 90%?", "session_id": "demo"}'
```

### PLC and cell state

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/plc_status` | DB1 snapshot: `Start`, `Stop`, `Total_Prod`, conveyors, arms, proximity sensors |
| `GET` | `/api/prox_sensors` | Proximity sensors 1–4 (`DB1.DBX10.3`–`10.6`) |
| `GET` | `/api/robot_state` | Speed, per-arm movement and overall mode (`IDLE`, …) |
| `GET` | `/api/robot_running_status` | Contents of `robot_status.json` written by the robot controller |
| `GET` | `/api/robot_speed_percentage` | `{"speed_percentage": 65, "display_text": "65%"}` |
| `GET` | `/api/timer_status` | Current conveyor timers `DB1.DBD12` / `DB1.DBD16` |
| `POST` | `/api/update_speed_and_timers` | Body `{"speed": 80}`. Same path as a chat speed command (speed file + PLC timers) |
| `POST` | `/api/test_raw_timer_write` | Diagnostic: writes 7500 / 6000 ms to the timers and reads them back |

### Step tracking

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/step_status` | Current step and progress of `arm_1` … `arm_3` |
| `GET` | `/api/step_status_with_prox` | Same, plus proximity sensor states |
| `GET` | `/api/step_history` | Recent step transitions |
| `GET` | `/api/step_summary` | Active arms, overall progress, current operation |
| `GET` | `/api/assembly_status` | Assembly stage and next expected action |
| `GET` | `/api/plant_workflow_status` | Workflow position derived from the proximity sensors |
| `POST` | `/api/update_step?arm=1&step_name=PLACING&progress=85` | Called by the robot controller after each step |

### Production, energy and cost

Endpoints marked **LLM** ask the language model for an analysis and fall back to the rule-based
calculation if that fails.

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/energy_status` | Per-component power at the current speed and state |
| `GET` | `/api/energy_breakdown_detailed` | Energy per cycle by component, with confidence level |
| `GET` | `/api/intelligent_cost_analysis` | Energy cost per part, with confidence level |
| `GET` | `/api/dynamic_production_rate` | Cycle time, parts per hour and per 8-hour shift at the current speed |
| `GET` | `/api/cycle_timing` | Measured and expected cycle times |
| `GET` | `/api/intelligent_status` | Combined status summary |
| `GET` | `/api/production_metrics` | **LLM** production time breakdown |
| `GET` | `/api/cost_analysis` | **LLM** energy cost per part |
| `GET` | `/api/bottleneck_analysis` | **LLM** process bottleneck analysis |
| `GET` | `/api/maintenance_prediction` | **LLM** maintenance outlook |
| `GET` | `/api/ai_production_metrics` | **LLM** production breakdown (async variant) |
| `GET` | `/api/ai_cost_analysis` | **LLM** cost analysis (async variant) |
| `POST` | `/api/ai_speed_simulation` | **LLM** what-if for a speed increase, body `{"percentage": 20}`. Never changes the cell |
| `GET` | `/api/all_scenarios` | All 32 on/off combinations of 3 arms and 2 conveyors, with power (19–850 W) |
| `GET` | `/api/custom_scenarios?arms=1,0,1&conveyors=1,0` | Matching scenarios only. Wrong lengths return `400` |

### TIA Portal (Openness)

Apart from `/api/tia/status`, these endpoints return `503` when the TIA module is not available
(no pythonnet, no TIA Portal, or `TIA_OPENNESS_DISABLED=1`). See [tia-dual-mode.md](tia-dual-mode.md).

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/tia/status` | Connection mode (`ATTACHED`, `EXCLUSIVE`, `JSON_CACHE`), cache counts |
| `GET` | `/api/tia/tags` | All PLC tags with address and type |
| `GET` | `/api/tia/tags/search?q=prox` | Search tags by name |
| `GET` | `/api/tia/blocks` | Program blocks (OB, FB, FC, DB) |
| `GET` | `/api/tia/program_structure` | Block structure overview |
| `GET` | `/api/tia/hardware` | Rack, modules and article numbers |
| `GET` | `/api/tia/ip_address` | PLC IP addresses from the hardware configuration |
| `GET` | `/api/tia/project_info` | Project name and path, PLC name, TIA version, cache status |
| `GET` | `/api/tia/health` | Tag and block counts, documentation coverage, issues and recommendations |
| `POST` | `/api/tia/refresh` | Re-read everything and rewrite the JSON cache |

### Voice

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/voice/status` | Listening state, last voice command and reply |

## Robot controller, port 8001

| Method | Path | Description |
|---|---|---|
| `POST` | `/api/enable_system` | Robots start reacting to PLC triggers (sent on `start`) |
| `POST` | `/api/disable_system` | Robots ignore PLC triggers (sent on `stop` / emergency) |
| `GET` | `/api/system_status` | `{"system_enabled": bool, "emergency_stop_active": bool}` |
| `POST` | `/api/reset_robots` | Body `{"command": "RESET_TO_ORIGIN", "reset_all_arms": true, "clear_emergency_stop": true}`. Flushes all queues, switches tools off and drives every arm home at 30 % speed |
