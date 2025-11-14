@echo off
REM Installs the optional voice assistant dependencies (SpeechRecognition + PyAudio) into the project venv.
cd /d "%~dp0.."

if exist venv (
    echo Activating virtual environment...
    call venv\Scripts\activate.bat
)

pip install -r requirements-voice.txt

echo.
echo NOTE: PyAudio publishes Windows wheels for Python 3.8-3.13. On newer Python versions it
echo       needs the PortAudio build tools, or use a 3.13 virtual environment for the voice assistant.
echo.
echo The voice assistant starts automatically with the copilot API (scripts\start_app.bat).
echo Say "copilot" followed by your command, e.g. "copilot, what is my current speed?"
pause
