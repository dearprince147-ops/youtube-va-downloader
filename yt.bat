@echo off
REM ═══════════════════════════════════════════════════════════════
REM  YouTube Downloader - Windows launcher (yt.bat)
REM
REM  Double-click to run the app inside Windows Terminal using
REM  PowerShell. First run offers to add a "yt" command to your
REM  PATH so you can press Win+R, type "yt", hit Enter.
REM
REM  Works as a normal user. Admin is only requested for the
REM  optional fallback that edits the machine-wide PATH.
REM ═══════════════════════════════════════════════════════════════
setlocal EnableExtensions

REM -- App folder = the folder this .bat lives in -----------------
set "APP_DIR=%~dp0"
if "%APP_DIR:~-1%"=="\" set "APP_DIR=%APP_DIR:~0,-1%"
set "SCRIPT=app.py"
set "APP_TITLE=YouTube Downloader"

REM -- Make sure Python is available ------------------------------
set "PY_EXE="
where python >nul 2>&1
if not errorlevel 1 set "PY_EXE=python"
if not defined PY_EXE (
    where py >nul 2>&1
    if not errorlevel 1 set "PY_EXE=py"
)
if not defined PY_EXE (
    echo.
    echo  [X] Python was not found on your PATH.
    echo      Install it from https://www.python.org/downloads/
    echo      and tick "Add Python to PATH" during setup.
    echo.
    pause
    exit /b 1
)

REM -- Pick the launcher: Windows Terminal, else plain PowerShell -
set "WT="
where wt >nul 2>&1
if not errorlevel 1 set "WT=wt"

set "PS_CMD=Set-Location -LiteralPath '%APP_DIR%'; & '%PY_EXE%' '%SCRIPT%'"

if defined WT (
    start "" wt -w 0 new-tab --title "%APP_TITLE%" --suppressApplicationTitle powershell -NoExit -NoLogo -ExecutionPolicy Bypass -Command "%PS_CMD%"
) else (
    start "" powershell -NoExit -NoLogo -ExecutionPolicy Bypass -Command "%PS_CMD%"
)

REM ═══════════════════════════════════════════════════════════════
REM  First-run setup: offer the "yt" PATH shortcut (optional)
REM ═══════════════════════════════════════════════════════════════
REM  If we were invoked as the "yt" command itself, skip the prompt.
if /i "%~1"=="--nopath" goto :done

REM  Already installed? Nothing to do.
if exist "%LOCALAPPDATA%\Microsoft\WindowsApps\yt.cmd" goto :done

echo.
echo  -----------------------------------------------------------
echo   Optional setup: type "yt" from anywhere
echo  -----------------------------------------------------------
echo   Adds a small "yt" command to your PATH so you can press
echo   Win+R, type  yt  and hit Enter to launch the app in
echo   Windows Terminal. No admin rights required.
echo.
choice /c YN /n /m "  Add the 'yt' command now? [Y/N] "
if errorlevel 2 goto :done

REM -- Install a shim into WindowsApps (already on every PATH) ---
set "SHIM_DIR=%LOCALAPPDATA%\Microsoft\WindowsApps"
if not exist "%SHIM_DIR%" mkdir "%SHIM_DIR%" 2>nul
> "%SHIM_DIR%\yt.cmd" echo @echo off 2>nul
>>"%SHIM_DIR%\yt.cmd" echo call "%APP_DIR%\yt.bat" --nopath 2>nul

REM  Verify the shim actually landed; if not, use the elevated fallback.
if exist "%SHIM_DIR%\yt.cmd" (
    echo.
    echo  [OK] Done! Open a NEW terminal, then run:  yt
    echo       Or press Win+R, type  yt  and press Enter.
) else (
    echo.
    echo  [!] Could not write the user shortcut. Falling back to a
    echo      machine-wide PATH entry ^(needs admin approval^)...
    call :ElevatedPath
)

timeout /t 4 >nul
goto :done

REM ═══════════════════════════════════════════════════════════════
REM  Fallback: add APP_DIR to the MACHINE PATH (needs admin).
REM  Uses a temp .ps1 to avoid fragile nested quoting.
REM ═══════════════════════════════════════════════════════════════
:ElevatedPath
set "PS1=%TEMP%\yt_path_%RANDOM%.ps1"
> "%PS1%" echo $d = '%APP_DIR%'
>>"%PS1%" echo $p = [Environment]::GetEnvironmentVariable('Path','Machine')
>>"%PS1%" echo if ($p -split ';' -notcontains $d) {
>>"%PS1%" echo     [Environment]::SetEnvironmentVariable('Path', ($p.TrimEnd(';') + ';' + $d), 'Machine')
>>"%PS1%" echo     Write-Host '  [OK] Added to system PATH.'
>>"%PS1%" echo } else {
>>"%PS1%" echo     Write-Host '  [OK] Already on system PATH.'
>>"%PS1%" echo }

powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "Start-Process powershell -Verb RunAs -Wait -ArgumentList '-NoProfile','-ExecutionPolicy','Bypass','-File','%PS1%'"

del "%PS1%" >nul 2>&1
echo.
echo  Open a NEW terminal, then run:  yt
timeout /t 5 >nul
goto :eof

:done
endlocal
