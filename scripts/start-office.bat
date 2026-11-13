@echo off
rem Double-click start for the OFFICE PC: checks, PAPER bot, dashboard, browser.
rem Uses the Python in C:\Users\Prince\venvs\nifty-bot (outside the project).
setlocal
cd /d "%~dp0.." || (echo Could not open the project folder "%~dp0..". & pause & exit /b 1)
set "PY=C:\Users\Prince\venvs\nifty-bot\Scripts\python.exe"
set "WHERE=office"
call "%~dp0start-common.cmd"
exit /b %ERRORLEVEL%
