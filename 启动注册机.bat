@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo Starting GPT Register WebUI...
del /f /q "%TEMP%\turb-gpt-free-register-web-*.lock" 2>nul

REM start keeper (minimized)
start "turb-webui-keeper" /MIN python -u _webui_keeper.py

REM wait for port
set /a n=0
:wait
timeout /t 1 /nobreak >nul
set /a n+=1
powershell -NoProfile -Command "try { (Invoke-WebRequest http://127.0.0.1:5000/login -UseBasicParsing -TimeoutSec 1).StatusCode } catch { exit 1 }" >nul 2>&1
if %errorlevel%==0 goto ok
if %n% geq 15 goto fail
goto wait

:ok
echo OK  http://127.0.0.1:5000
echo Auth code: read from .env WEBUI_AUTH_CODE
start http://127.0.0.1:5000/
exit /b 0

:fail
echo FAILED to start. See _webui_err.log
type _webui_err.log | more
pause
exit /b 1
