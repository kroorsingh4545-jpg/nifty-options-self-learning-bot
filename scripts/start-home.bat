@echo off
rem Double-click start for the HOME PC: checks, PAPER bot, dashboard, browser.
rem Uses the project's own .venv (created with: python -m venv .venv).
setlocal
cd /d "%~dp0.." || (echo Could not open the project folder "%~dp0..". & pause & exit /b 1)
set "PY=%CD%\.venv\Scripts\python.exe"
set "WHERE=home"
call "%~dp0start-common.cmd"
exit /b %ERRORLEVEL%
