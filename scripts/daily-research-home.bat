@echo off
rem Double-click AFTER 15:40 on the HOME PC: daily research update (research only, places no orders).
rem Uses the project's own .venv (created with: python -m venv .venv). The steps are in daily-research.bat.
setlocal
cd /d "%~dp0.." || (echo Could not open the project folder "%~dp0..". & pause & exit /b 1)
set "PY=%CD%\.venv\Scripts\python.exe"
set "NIFTY_PC=home"
call "%~dp0daily-research.bat"
exit /b %ERRORLEVEL%
