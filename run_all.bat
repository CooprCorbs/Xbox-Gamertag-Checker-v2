@echo off
cd /d "%~dp0"
echo Starting only the Discord bot. Use /hunt length:4 count:1000 in Discord.
echo To use the CLI instead, stop the bot first.
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" bot.py
) else (
    python bot.py
)
pause
