@echo off
rem Start the discord-minutes recording bot. Double-click to run.
rem Stop it with Ctrl+C or by closing this window.
title discord-minutes bot
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
set PYTHONUNBUFFERED=1

set "POETRY=poetry"
where poetry >nul 2>&1 || set "POETRY=%APPDATA%\Python\Scripts\poetry.exe"

"%POETRY%" run minutes-bot

rem Keep the window open so errors can be read.
echo.
echo Bot stopped.
pause
