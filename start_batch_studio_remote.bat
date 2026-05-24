@echo off
setlocal
cd /d "%~dp0"

echo This starts ComfyPilot on 0.0.0.0 for LAN/tunnel access.
echo Use a strong access token if the service can be reached from outside your home network.
echo.
set /p BATCH_STUDIO_ACCESS_TOKEN=Access token: 

python app.py --public --port 8000 --access-token "%BATCH_STUDIO_ACCESS_TOKEN%"
