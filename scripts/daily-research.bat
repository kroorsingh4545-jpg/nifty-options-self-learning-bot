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
rem
rem Which Python (same environments as start-office.bat / start-home.bat):
rem   daily-research-office.bat  -> C:\Users\Prince\venvs\nifty-bot  (sets NIFTY_PC=office, then calls this file)
rem   daily-research-home.bat    -> the project's .venv              (sets NIFTY_PC=home, then calls this file)
rem   this file double-clicked   -> picks by itself: the office environment if it exists and starts,
rem                                 else the project's .venv. A .venv copied from the other PC (the project
rem                                 folder travels on a drive) exists but does not start, so it is never trusted
rem                                 on sight: every candidate is test-started first.
setlocal EnableExtensions
cd /d "%~dp0.." || (echo Could not open the project folder "%~dp0..". & pause & exit /b 1)
title NIFTY research - daily update

set "OFFICE_PY=C:\Users\Prince\venvs\nifty-bot\Scripts\python.exe"
set "HOME_PY=%CD%\.venv\Scripts\python.exe"
if defined NIFTY_PC goto python_chosen
set "PY="
if exist "%OFFICE_PY%" call :try_python "%OFFICE_PY%" office
if not defined PY if exist "%HOME_PY%" call :try_python "%HOME_PY%" home
if not defined PY (
  echo STOPPED: no working Python found. Tried:
  echo   office PC: %OFFICE_PY%
  echo   home PC:   %HOME_PY%
  echo Create the environment first ^(see README, Setup^).
  pause
  exit /b 1
)

:python_chosen
echo Project folder: %CD%
echo Python:         %PY%  ^(%NIFTY_PC% PC^)
echo.
if not exist "%PY%" (
  echo STOPPED: Python was not found at
  echo   %PY%
  echo Create the environment first ^(see README, Setup^).
  pause
  exit /b 1
)
"%PY%" -c "import research" >nul 2>&1
if errorlevel 1 (
  echo STOPPED: Python at
  echo   %PY%
  echo does not start or cannot load the project. A .venv copied from the other PC does this:
  echo use daily-research-office.bat on the office PC and daily-research-home.bat at home.
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

rem ---------------------------------------------------------------------------
rem :try_python <python.exe> <office|home> sets PY and NIFTY_PC only if that Python
rem starts and can load the project (cheap: imports the research package only).
:try_python
"%~1" -c "import research" >nul 2>&1
if errorlevel 1 exit /b 0
set "PY=%~1"
set "NIFTY_PC=%~2"
exit /b 0
