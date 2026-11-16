@echo off
rem Start the SolidWorks MCP bridge host on the interactive desktop.
rem Python: %SOLIDWORKS_MCP_PYTHON% if set, else the repo's .venv, else python on PATH.
setlocal
set "REPO=%~dp0.."
set "PY=%SOLIDWORKS_MCP_PYTHON%"
if "%PY%"=="" if exist "%REPO%\.venv\Scripts\python.exe" set "PY=%REPO%\.venv\Scripts\python.exe"
if "%PY%"=="" set "PY=python"
title SolidWorks MCP bridge host
cd /d "%REPO%"
"%PY%" -m bridge.host %*
if errorlevel 1 pause
