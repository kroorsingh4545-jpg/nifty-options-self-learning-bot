@echo off
rem Shared steps for start-office.bat / start-home.bat. Do not run directly:
rem the caller has already changed to the project folder and set PY.
rem   1. runtime check --connect: stop here if any check FAILs
rem   2. start the PAPER bot in its own window, unless one is already running
rem   3. wait 30 s, start the dashboard in another window (unless already up)
rem   4. open http://127.0.0.1:8501
if not defined PY (echo Run start-office.bat or start-home.bat, not this file. & pause & exit /b 1)
title NIFTY PAPER launcher (%WHERE%)
echo Project folder: %CD%
echo Python:         %PY%
echo.

if not exist "%PY%" (
  echo STOPPED: Python was not found at
  echo   %PY%
  echo Create the environment first ^(see README, Setup^).
  pause
  exit /b 1
)

echo === 1/4  Pre-flight checks ^(runtime check --connect^) ===
set "CHECK_OUT=%TEMP%\nifty-bot-check-%RANDOM%.txt"
"%PY%" -m runtime check --connect > "%CHECK_OUT%" 2>&1
set "CHECK_RC=%ERRORLEVEL%"
type "%CHECK_OUT%"
findstr /l /c:"[FAIL]" /c:"CONFIG ERROR" "%CHECK_OUT%" >nul && set "CHECK_RC=1"
del "%CHECK_OUT%" >nul 2>&1
if not "%CHECK_RC%"=="0" (
  echo.
  echo ============================================================
  echo  STOPPED: a pre-flight check FAILED ^(see the [FAIL] lines^).
  echo  Nothing was started. Fix it, then double-click again.
  echo ============================================================
  pause
  exit /b 1
)
echo All checks passed.
echo.

echo === 2/4  PAPER bot ===
call :bot_running
if "%BOT_RUNNING%"=="1" (
  echo A PAPER bot is already running on this PC - NOT starting a second one.
  goto dashboard
)
start "NIFTY PAPER bot (%WHERE%)" /d "%CD%" cmd /k ""%PY%" -m runtime paper"
echo PAPER bot started in its own window. Waiting 30 seconds before the dashboard...
timeout /t 30 /nobreak >nul

:dashboard
echo.
echo === 3/4  Dashboard ===
netstat -ano -p TCP | findstr /r /c:":8501 .*LISTENING" >nul
if not errorlevel 1 (
  echo The dashboard is already running.
) else (
  start "NIFTY dashboard (%WHERE%)" /d "%CD%" cmd /k ""%PY%" -m streamlit run dashboard/app.py"
  echo Dashboard started in its own window.
  timeout /t 5 /nobreak >nul
)

echo.
echo === 4/4  Browser ===
start "" "http://127.0.0.1:8501"
echo Opened http://127.0.0.1:8501 . This window closes in 10 seconds.
timeout /t 10 >nul
exit /b 0

rem ---------------------------------------------------------------------------
rem BOT_RUNNING=1 when a "python -m runtime paper" process exists on this PC, or
rem when the bot's heartbeat is fresh (runtime health; also covers Docker).
:bot_running
set "BOT_RUNNING=0"
powershell -NoProfile -ExecutionPolicy Bypass -Command "if (Get-CimInstance Win32_Process | Where-Object { $_.Name -like 'python*' -and $_.CommandLine -match '-m\s+runtime\s+paper' }) { exit 0 } else { exit 1 }"
if not errorlevel 1 set "BOT_RUNNING=1"
if "%BOT_RUNNING%"=="1" exit /b 0
"%PY%" -m runtime health >nul 2>&1
if not errorlevel 1 set "BOT_RUNNING=1"
exit /b 0
