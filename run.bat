@echo off
setlocal enabledelayedexpansion
title Kaviaarder

cd /d "%~dp0"

echo =======================================================
echo   Kaviaarder - Minimalist PDF Redaction Tool
echo =======================================================
echo.

:: Detect Python 3 interpreter
set "PY_CMD="
py -3 --version >nul 2>&1
if %errorlevel% equ 0 (
    set "PY_CMD=py -3"
) else (
    python --version >nul 2>&1
    if %errorlevel% equ 0 (
        set "PY_CMD=python"
    )
)

if "%PY_CMD%"=="" (
    echo [ERROR] Python 3 was not found on your system.
    echo Please install Python 3.10+ from https://www.python.org/
    echo.
    pause
    exit /b 1
)

:: Create virtual environment if it does not exist
if not exist ".venv\Scripts\python.exe" (
    echo [*] Creating virtual environment (.venv)...
    %PY_CMD% -m venv .venv
    if %errorlevel% neq 0 (
        echo [ERROR] Failed to create virtual environment.
        pause
        exit /b 1
    )
    echo [*] Installing dependencies...
    .\.venv\Scripts\python.exe -m pip install --upgrade pip >nul 2>&1
    .\.venv\Scripts\python.exe -m pip install -r requirements.txt
    if %errorlevel% neq 0 (
        echo [ERROR] Failed to install dependencies.
        pause
        exit /b 1
    )
)

:: Start the application
echo [*] Starting Kaviaarder...
.\.venv\Scripts\python.exe app.py

if %errorlevel% neq 0 (
    echo.
    echo [ERROR] Application exited with an error.
    pause
)
