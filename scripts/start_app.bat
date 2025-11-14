@echo off
REM One-click start (Windows): venv + dependencies, robot controller (:8001), copilot API + dashboard (:8000), browser.
setlocal
cd /d "%~dp0.."
set PYTHONUTF8=1

REM Step 1: Create virtual environment if not exists
if not exist venv (
    where py >nul 2>nul && (py -3 -m venv venv) || (python -m venv venv)
)

REM Step 2: Activate virtual environment
call venv\Scripts\activate.bat

REM Step 3: Install dependencies (TIA Openness and voice extras are optional)
python -m pip install --upgrade pip
pip install -r requirements.txt
pip install -r requirements-tia.txt || echo TIA Openness support not installed - continuing in Snap7-only mode
pip install -r requirements-voice.txt || echo Voice dependencies not installed - continuing without the voice assistant

REM Step 4: Run the robot controller (in its own window)
start "TIA S7 Copilot - Robot Controller" cmd /k "call venv\Scripts\activate.bat && python -m tia_s7_copilot.robot_controller"

REM Step 5: Wait a few seconds for the robot controller to connect to the PLC and arms
ping 127.0.0.1 -n 8 > nul

REM Step 6: Run the copilot API + dashboard (in its own window)
start "TIA S7 Copilot - API" cmd /k "call venv\Scripts\activate.bat && python -m uvicorn tia_s7_copilot.api:app --host 0.0.0.0 --port 8000"

REM Step 7: Wait for the server, then open the dashboard
ping 127.0.0.1 -n 6 > nul
start http://localhost:8000/

echo All done! The robot controller and the copilot API are running and the dashboard is open.
pause
