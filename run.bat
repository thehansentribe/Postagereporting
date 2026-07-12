@echo off
cd /d "%~dp0"
if not defined PORT set PORT=8080
if not defined HOST set HOST=0.0.0.0
if not exist ".venv\Scripts\python.exe" (
  py -3 -m venv .venv || python -m venv .venv
)
".venv\Scripts\python.exe" -m pip install -q -r requirements.txt
for /f "tokens=5" %%p in ('netstat -ano ^| findstr /r /c:":%PORT% .*LISTENING"') do (
  echo Port %PORT% is already in use (PID %%p) - stopping it...
  taskkill /F /PID %%p >nul 2>&1
)
for /f "tokens=2 delims=:" %%a in ('ipconfig ^| findstr /c:"IPv4 Address"') do (
  for /f "tokens=* delims= " %%b in ("%%a") do set LAN_IP=%%b
)
echo -------------------------------------------------------
echo  Postage Reporting is starting on port %PORT%
echo    This computer:   http://127.0.0.1:%PORT%
echo    Other computers: http://%LAN_IP%:%PORT%
echo  (Windows may prompt for a Firewall exception - click Allow access)
echo -------------------------------------------------------
start "" "http://127.0.0.1:%PORT%"
".venv\Scripts\python.exe" app.py
