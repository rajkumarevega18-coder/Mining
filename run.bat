@echo off
:: ─────────────────────────────────────────────────────────────────────────────
:: sagepub_runner\run.bat  —  Windows launcher for sage.py
::
:: Usage:
::   run.bat          → run part 1 (links-only phase)
::   run.bat 2        → run part 2
::   run.bat 1 --data-only  → run data enrichment for part 1
::   run.bat split 6  → split journals into 6 part files
:: ─────────────────────────────────────────────────────────────────────────────

:: Load sys_config.env (set environment variables)
for /f "usebackq delims=" %%L in ("sys_config.env") do (
    set "%%L"
)

:: Default to part 1 if no argument given
set PART=%1
if "%PART%"=="" set PART=1

python sage.py %*
