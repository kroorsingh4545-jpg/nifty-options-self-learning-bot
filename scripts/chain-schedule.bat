@echo off
rem Double-click in the MORNING of a trading day (before 09:20), next to the PAPER bot:
rem takes option-chain snapshots at the times in research\research.yaml (chain.schedule,
rem default 09:20, 11:00, 13:30, 15:20 IST), then closes by itself after the last one.
rem Research layer only: about 5 read-only Upstox calls per snapshot time, on research's
rem own small rate budget (a tenth of the bot's). It never starts, stops or talks to the bot,
rem never touches trading settings and places no orders. Closing this window early is safe.
setlocal EnableExtensions
cd /d "%~dp0.." || (echo Could not open the project folder "%~dp0..". & pause & exit /b 1)
title NIFTY research - chain snapshots

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

"%PY%" -m research chain-schedule --during-session
if errorlevel 1 (
  echo.
  echo Finished WITH PROBLEMS: read the messages above. Nothing was changed in the bot.
  pause
  exit /b 1
)
echo.
echo Done for today.
pause
exit /b 0
