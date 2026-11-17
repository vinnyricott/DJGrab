@echo off
rem Double-click to install DJ Grab. Downloads the setup script and runs it (no admin rights needed).
title Installing DJ Grab
echo Installing DJ Grab. This takes a few minutes the first time...
powershell -NoProfile -ExecutionPolicy Bypass -Command "$ProgressPreference='SilentlyContinue'; $f=Join-Path $env:TEMP 'djgrab-setup.ps1'; Invoke-WebRequest 'https://raw.githubusercontent.com/vinnyricott/DJGrab/main/win/setup.ps1' -OutFile $f; & $f"
if errorlevel 1 (
  echo.
  echo Something went wrong. Check your internet connection and try again.
  pause
)
