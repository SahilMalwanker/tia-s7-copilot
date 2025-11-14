# Hardware and PLC interface

<p align="center"><img src="assets/hardware.svg" alt="Wiring overview: operator PC, S7-1500 rack with I/O modules, field devices and three uArm Swift Pro robots" width="100%"></p>

## Bill of materials

| Qty | Component | Article / model | Role in the cell |
|---:|---|---|---|
| 1 | S7-1500 CPU 1511-1 PN | 6ES7 511-1AK02-0AB0, firmware V2.9 | Runs the ladder program and exposes data block DB1 |
| 1 | Analog output module AQ 8xU/I HS | 6ES7 532-5HF00-0AB0 (slot 2) | Configured, not used by the software |
| 1 | Digital input module DI 16x24VDC BA | 6ES7 521-1BH10-0AA0 (slot 3) | Proximity sensors, emergency-stop inputs |
| 1 | Digital output module DQ 8x24VDC/2A HF | 6ES7 522-1BF00-0AB0 (slot 4) | Conveyor drives, robot status outputs |
| 1 | 24 V DC power supply | SITOP PSU6200 | Field and module supply |
| 1 | S7-1500 mounting rail | 6ES7 590-1xxx0-0AA0 | Rack |
| 2 | Conveyor belts, 24 V DC motor | Festo MPS conveyor modules | Carry the PCB holder between the arms |
| 4 | Proximity sensors, 24 V DC | – | Detect the holder at points A–D |
| 3 | Desktop robot arms, 4 DoF, 500 g payload, ±0.2 mm repeatability | uArm Swift Pro, USB serial | Pick, hold and assemble (see below) |
| 1 | Operator PC (Windows) | Python 3.10+, optionally TIA Portal V19 | Runs both services and the dashboard |

## Robot arms

| Arm | Default port | Tool | Job in the cycle | Home position (x, y, z mm) |
|---|---|---|---|---|
| Arm 1 | `COM3` | vacuum pump | picks a PCB holder and puts it on conveyor 1 (two alternating sequences) | (180, 0, 0) |
| Arm 2 | `COM5` | gripper | takes the holder at point B, holds it for assembly, sets it on conveyor 2 | (160, 0, 80) |
| Arm 3 | `COM4` | vacuum pump | brings the PCB from the tray and mounts it on the holder | (130, 0, 120) |

Windows can assign different COM ports after a reboot, so the mapping is fixed in configuration
(`ARM1_PORT`, `ARM2_PORT` and `ARM3_PORT` in `.env`). Arms are driven with Cartesian
coordinates through the uArm Python SDK. Each waypoint was tuned on the real cell. The robot
controller reads the speed from `robot_speed.json` before every move, so speed changes apply
from the next motion step without a restart.

## Assembly cycle

<p align="center"><img src="assets/cell-cycle.svg" alt="Animated replay of one assembly cycle" width="100%"></p>

| # | Event | Signal |
|---|---|---|
| 1 | Start: the PLC requests Arm 1 | `DB1.DBX10.0` rising edge |
| 2 | Arm 1 picks a PCB holder and places it on conveyor 1 | – |
| 3 | Proximity sensor 1 sees the holder, conveyor 1 runs | `%I0.0`, timer preset `DB1.DBD12` |
| 4 | Proximity sensor 2 at point B, Arm 2 grips the holder | `%I0.1`, `DB1.DBX10.1` |
| 5 | Arm 2 reaches waypoint 4, Arm 3 launches with the PCB | position trigger (190, 210, 120) |
| 6 | Arms 2 and 3 mount the PCB, then Arm 2 places the assembly on conveyor 2 | position trigger (165, −5, 180) |
| 7 | Proximity sensor 3 sees the assembly, conveyor 2 runs | `%I0.2`, timer preset `DB1.DBD16` |
| 8 | Proximity sensor 4: cycle complete, production counter + 1, Arm 1 starts again | `%I0.3`, `DB1.DBW2`, `DB1.DBX10.2` |

## PLC prerequisites

The services talk to the CPU through **Snap7** (S7 protocol over ISO-on-TCP, port 102) using
absolute addresses. Configure the CPU in TIA Portal:

1. **Protection & Security → Connection mechanisms:** enable *Permit access with PUT/GET
   communication from remote partner*.
2. **DB1 → Properties → Attributes:** clear *Optimized block access* so the offsets below are fixed.
3. Put the PC on the PLC subnet and set `PLC_IP`, `PLC_RACK` (0) and `PLC_SLOT` (1 for S7-1500) in `.env`.

## Data block DB1 `Daten_ai`

| Address | Name | Type | Used by the software |
|---|---|---|---|
| `DBX0.0` | `Start` | Bool | written by `start` (copilot API) |
| `DBX0.1` | `Stop` | Bool | written by `stop` / `reset` / emergency words. The robot controller watches it as the emergency-stop line |
| `DBW2` | `Total_prod` | Int | read: production counter |
| `DBX4.0` | `Conveyor_1` | Bool | read: conveyor 1 running |
| `DBX4.1` | `Conveyor_2` | Bool | read: conveyor 2 running |
| `DBD6` | `Robot_speed` | DInt | – |
| `DBX10.0` | `arm_1` | Bool | read: starts Arm 1 (rising edge) |
| `DBX10.1` | `arm_2` | Bool | read: starts Arm 2 (rising edge) |
| `DBX10.2` | `arm_3` | Bool | read: end-of-line trigger, starts Arm 1 (rising edge) |
| `DBX10.3` … `DBX10.6` | `Proximity_Sensor1` … `4` | Bool | read: sensor mirror for the dashboard |
| `DBD12` | `Conveyor1_DynamicTimer` | DInt (ms) | written by speed changes: 8000 − 50·speed %, clamped to 3000–8000 |
| `DBD16` | `Conveyor3_DynamicTimer` | DInt (ms) | written by speed changes: 9000 − 40·speed %, clamped to 5000–9000 |
| `DBX20.0` | `PCB_Detection` | Bool | – |
| `DBD22` | `Global_Robot_Speed` | Real | best-effort write of `speed % * 5` for an external simulation model |

The timer-write helper reads back and compares both presets. File updates, timer writes and
the simulation-speed write are separate operations, so a partial update remains possible.
Verify timer and robot state rather than treating a chat confirmation as hardware proof.

## I/O map

| Address | Tag | Direction | Meaning |
|---|---|---|---|
| `%I0.0` … `%I0.3` | `Prox_sensor_1` … `4` | input | holder detected at points A–D |
| `%I0.4` … `%I0.6` | `Em1` … `Em3` | input | emergency inputs |
| `%I0.7` | `Emergency_button` | input | emergency-stop push button |
| `%Q16.1` … `%Q16.3` | `Robot_1` … `3` | output | copy the arm request bits so that OPC UA clients such as a digital twin can see them. The arms themselves are driven over USB |
| `%Q16.4` / `%Q16.5` | `Conveyor_1` / `Conveyor_1_slow` | output | conveyor 1 drive |
| `%Q16.6` / `%Q16.7` | `Conveyor_2` / `Conveyor_2_slow` | output | conveyor 2 drive |
| `%Q15.1` … `%Q15.7`, `%Q16.0` | `*_seq2`, `*_PS` | output | sequence mirror signals for a simulation model connected over OPC UA |
| `%M0.0` | `Tag_start` | memory | latched "line running" |
| `%M0.1` … `%M1.1` | `Tag_conveyor_*`, `Tag_arm_*`, `Sequence_2`, `Tag_Auto_Reset`, `Tag_Cycle_Complete` | memory | sequence state |
| `%ID200` | `Global_Robot_Speed` | input double word | Real |

## Ladder logic (excerpt)

The first networks of the PLC program:

| Network | Logic | Purpose |
|---|---|---|
| 1 | `"Daten_ai".Start` AND NOT `"Daten_ai".Stop` → `Tag_start` (latched) | line running |
| 2 | `Tag_start` → `"Daten_ai".arm_1` | request Arm 1 |
| 3 | `Prox_sensor_2` AND NOT `Prox_sensor_3` AND `Tag_start` → `Tag_arm_2` (latched) | holder at point B, request Arm 2 |

The PLC project itself is not part of this repository. These tables describe everything the
Python services expect, so you can rebuild the same interface on another S7-1500.
