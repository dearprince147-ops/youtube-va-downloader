@echo off
rem ============================================================
rem  YouTube Downloader - Windows launcher
rem  Double-click this file to start the app.
rem
rem  On first run it will:
rem    * install Python automatically if it is missing
rem    * add a "yt" command to PATH (run from Win+R or any shell)
rem    * launch app.py in Windows Terminal using PowerShell
rem ============================================================
setlocal EnableExtensions
title YouTube Downloader

set "APP_DIR=%~dp0"
if "%APP_DIR:~-1%"=="\" set "APP_DIR=%APP_DIR:~0,-1%"

rem ============================================================
rem  1. FIND OR INSTALL PYTHON
rem ============================================================
set "PY="
for /f "delims=" %%i in ('where python 2^>nul') do if not defined PY set "PY=%%i"
if not defined PY for /f "delims=" %%i in ('where py 2^>nul') do if not defined PY set "PY=py"

if not defined PY (
    echo.
    echo  Python was not found - installing it now...
    echo.
    call :InstallPython
    rem  If we get here, Python install failed - stop.
    exit /b 1
)

rem Re-check after a possible install
set "PY="
for /f "delims=" %%i in ('where python 2^>nul') do if not defined PY set "PY=%%i"
if not defined PY for /f "delims=" %%i in ('where py 2^>nul') do if not defined PY set "PY=py"
if not defined PY set "PY=python"

rem ============================================================
rem  2. ADD "yt" TO PATH (once, silently after the first run)
rem ============================================================
call :InstallPathEntry %1

rem ============================================================
rem  3. LAUNCH THE APP
rem ============================================================
rem  Windows Terminal if available (uses your own default profile's
rem  shell via powershell.exe), otherwise a plain PowerShell window.
rem  -NoExit keeps the window open if the app crashes.
where wt.exe >nul 2>&1
if not errorlevel 1 (
    start "" wt.exe -d "%APP_DIR%." powershell.exe -NoExit -NoLogo -ExecutionPolicy Bypass -Command "& '%PY%' 'app.py'"
) else (
    start "" powershell.exe -NoExit -NoLogo -ExecutionPolicy Bypass -Command "Set-Location -LiteralPath '%APP_DIR%'; & '%PY%' 'app.py'"
)
goto :eof

rem ============================================================
rem  Install Python via winget, falling back to the official
rem  installer download.
rem ============================================================
:InstallPython
where winget >nul 2>&1
if not errorlevel 1 (
    echo  Using winget to install Python...
    winget install --id Python.Python.3.12 -e --source winget ^
        --accept-source-agreements --accept-package-agreements
    if not errorlevel 1 (
        echo.
        echo  Python installed. This window will now relaunch the app
        echo  in a new terminal so it picks up the updated PATH.
        echo.
        timeout /t 3 >nul
        goto :RestartSelf
    )
)

echo  winget unavailable - downloading the official installer...
set "PYEXE=%TEMP%\python_setup.exe"
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "try { Invoke-WebRequest -UseBasicParsing -Uri 'https://www.python.org/ftp/python/3.12.8/python-3.12.8-amd64.exe' -OutFile '%PYEXE%' } catch { exit 1 }"
if not exist "%PYEXE%" (
    echo.
    echo  [X] Could not download Python automatically.
    echo      Please install it from https://www.python.org/downloads/
    echo      and tick "Add python.exe to PATH" during setup.
    echo.
    pause
    exit /b 1
)

echo  Running the Python installer (this may take a minute)...
"%PYEXE%" /quiet InstallAllUsers=0 PrependPath=1 Include_launcher=1
del "%PYEXE%" >nul 2>&1
echo.
echo  Python installed. Relaunching the app...
timeout /t 3 >nul
goto :RestartSelf

:RestartSelf
rem  Re-run this same .bat with the refreshed environment, then quit.
start "" "%~f0" --quiet
exit

rem ============================================================
rem  Create a "yt" command in the per-user PATH.
rem  Uses %LOCALAPPDATA%\Microsoft\WindowsApps, which already
rem  exists on every user's PATH - so no admin, no PATH edits.
rem  Silent on every run after the first.
rem ============================================================
:InstallPathEntry
set "SHIM_DIR=%LOCALAPPDATA%\Microsoft\WindowsApps"
if not exist "%SHIM_DIR%" mkdir "%SHIM_DIR%" 2>nul
if not exist "%SHIM_DIR%" exit /b 0

if exist "%SHIM_DIR%\yt.cmd" (
    rem  Already installed - refresh it in case the folder moved. Silent.
    > "%SHIM_DIR%\yt.cmd" echo @echo off
    >>"%SHIM_DIR%\yt.cmd" echo call "%APP_DIR%\yt.bat" --quiet
    exit /b 0
)

if /i "%~1"=="--quiet" exit /b 0
> "%SHIM_DIR%\yt.cmd" echo @echo off
>>"%SHIM_DIR%\yt.cmd" echo call "%APP_DIR%\yt.bat" --quiet

if exist "%SHIM_DIR%\yt.cmd" (
    echo.
    echo  ============================================================
    echo   Tip: type "yt" to launch this app from anywhere
    echo  ============================================================
    echo   Open a new terminal (or press Win+R) and type:   yt
    echo   That is it - no more clicking through folders.
    echo.
)
exit /b 0
