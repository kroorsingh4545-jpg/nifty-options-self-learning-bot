@echo off
rem Double-click AFTER 15:40 on a trading day (or at the weekend): daily research update.
rem Research layer only (read-only market data). It never starts, stops or talks to the
rem PAPER bot, never touches trading settings and places no orders.
rem   1/9 history --to today   refresh NIFTY and India VIX candles (resumable, a few Upstox calls)
rem   2/9 summarize-quotes     copy the bot's recorded option quotes into 1-minute bars (no Upstox call)
rem   3/9 archive-quotes       copy the bot's raw 5-second quotes before their 30-day purge (no Upstox call)
rem   4/9 data-quality         audit of what the bot recorded, saved to logs\research\data-quality-YYYY-MM-DD.txt
rem   5/9 chain-snapshot       option-chain snapshot: nearest expiries + next monthly (a few Upstox calls)
rem   6/9 shadow               counterfactual outcome of every signal, saved to logs\research\shadow-YYYY-MM-DD.txt
rem   7/9 variants             forward shadow variants A and B next to the ORB, saved to logs\research\variants-YYYY-MM-DD.txt
rem   8/9 forecast             outcome rows + the next session's forecast (no Upstox call)
rem   9/9 report               saved to logs\research\report-YYYY-MM-DD.md (no Upstox call)
rem Research refuses Upstox calls on weekdays 08:30-15:40 IST, so this stops with a message if run too early.
setlocal EnableExtensions
cd /d "%~dp0.." || (echo Could not open the project folder "%~dp0..". & pause & exit /b 1)
title NIFTY research - daily update

rem Same Python as the start scripts: the project .venv (home PC), else the office PC environment.
if not defined PY if exist "%CD%\.venv\Scripts\python.exe" set "PY=%CD%\.venv\Scripts\python.exe"
if not defined PY set "PY=C:\Users\Prince\venvs\nifty-bot\Scripts\python.exe"
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

for /f %%d in ('powershell -NoProfile -Command "Get-Date -Format yyyy-MM-dd"') do set "TODAY=%%d"
if not exist "logs\research" mkdir "logs\research"
set "FAILED="

echo === 1/9  History ^(NIFTY + India VIX up to today^) ===
"%PY%" -m research history --to today
set "RC=%ERRORLEVEL%"
if "%RC%"=="3" (
  echo.
  echo ============================================================
  echo  STOPPED: too early. Research does not call Upstox on weekdays
  echo  08:30-15:40 IST. Double-click again after 15:40.
  echo ============================================================
  pause
  exit /b 3
)
if not "%RC%"=="0" set "FAILED=%FAILED% history"
echo.

echo === 2/9  Recorded option quotes ^(trading database, read-only^) ===
"%PY%" -m research summarize-quotes
if errorlevel 1 set "FAILED=%FAILED% summarize-quotes"
echo.

echo === 3/9  Raw quote archive ^(trading database, read-only^) ===
"%PY%" -m research archive-quotes
if errorlevel 1 set "FAILED=%FAILED% archive-quotes"
echo.

echo === 4/9  Data quality ^(trading database, read-only^) ===
"%PY%" -m research data-quality > "logs\research\data-quality-%TODAY%.txt"
if errorlevel 1 (
  set "FAILED=%FAILED% data-quality"
) else (
  type "logs\research\data-quality-%TODAY%.txt"
)
echo.

echo === 5/9  Option-chain snapshot ===
"%PY%" -m research chain-snapshot
if errorlevel 1 set "FAILED=%FAILED% chain-snapshot"
echo.

echo === 6/9  Shadow outcomes ^(trading database and bot log, read-only^) ===
"%PY%" -m research shadow > "logs\research\shadow-%TODAY%.txt"
if errorlevel 1 (
  set "FAILED=%FAILED% shadow"
) else (
  type "logs\research\shadow-%TODAY%.txt"
)
echo.

echo === 7/9  Shadow variants A and B next to the current ORB ^(trading database read-only^) ===
"%PY%" -m research variants > "logs\research\variants-%TODAY%.txt"
if errorlevel 1 (
  set "FAILED=%FAILED% variants"
) else (
  type "logs\research\variants-%TODAY%.txt"
)
echo.

echo === 8/9  Forecasts ^(outcomes + next session^) ===
"%PY%" -m research forecast
if errorlevel 1 set "FAILED=%FAILED% forecast"
echo.

echo === 9/9  Report ===
"%PY%" -m research report > "logs\research\report-%TODAY%.md"
if errorlevel 1 (
  set "FAILED=%FAILED% report"
) else (
  echo Saved logs\research\report-%TODAY%.md
)
echo.

if defined FAILED (
  echo ============================================================
  echo  Finished WITH PROBLEMS in:%FAILED%
  echo  Read the messages above. Nothing was changed in the bot.
  echo ============================================================
  pause
  exit /b 1
)
echo ============================================================
echo  Done. Check the forecast lines above: tomorrow's forecast says
echo  whether it was recorded before the open.
echo ============================================================
pause
exit /b 0
