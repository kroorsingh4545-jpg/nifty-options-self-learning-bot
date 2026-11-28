@echo off
rem Double-click AFTER 15:40 on the OFFICE PC: daily research update (research only, places no orders).
rem Uses the Python in C:\Users\Prince\venvs\nifty-bot (outside the project). The steps are in daily-research.bat.
setlocal
cd /d "%~dp0.." || (echo Could not open the project folder "%~dp0..". & pause & exit /b 1)
set "PY=C:\Users\Prince\venvs\nifty-bot\Scripts\python.exe"
set "NIFTY_PC=office"
call "%~dp0daily-research.bat"
exit /b %ERRORLEVEL%
