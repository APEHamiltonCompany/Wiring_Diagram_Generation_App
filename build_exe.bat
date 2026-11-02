@echo off
REM ====================================================================
REM  Wire Diagram Builder - Windows .exe build script
REM
REM  Double-click to run. Builds dist\WireDiagramBuilder.exe
REM  The window stays open at the end so you can read any warnings.
REM ====================================================================
setlocal EnableDelayedExpansion

REM Always run from the folder this script lives in.
cd /d "%~dp0"

set "WARNINGS="

REM ---- Pick the Python command: prefer "py" (Python Install Manager), else "python"
set "PY="
where py >nul 2>&1 && set "PY=py"
if not defined PY (
    where python >nul 2>&1 && set "PY=python"
)
if not defined PY (
    echo ERROR: Python was not found. Install it with:  py install 3.12
    goto :fail
)
echo Using Python command: %PY%
%PY% --version

echo.
echo === Rebuilding device database (data\drd.json) ===
%PY% app\drd.py
if errorlevel 1 (
    echo WARNING: could not rebuild drd.json; using the existing one.
    set "WARNINGS=!WARNINGS! [drd.json rebuild failed]"
)

echo.
echo === Installing build dependencies ===
%PY% -m pip install --upgrade pip
if errorlevel 1 set "WARNINGS=!WARNINGS! [pip upgrade failed]"
%PY% -m pip install pyinstaller openpyxl
if errorlevel 1 (
    echo ERROR: dependency installation failed.
    goto :fail
)

echo.
echo === Building executable with PyInstaller ===
%PY% -m PyInstaller --noconfirm --clean wirediagram.spec
if errorlevel 1 (
    echo ERROR: PyInstaller build failed.
    goto :fail
)

if not exist "dist\WireDiagramBuilder.exe" (
    echo ERROR: build finished but dist\WireDiagramBuilder.exe was not found.
    goto :fail
)

echo.
echo ====================================================================
echo  BUILD SUCCEEDED:  dist\WireDiagramBuilder.exe
if defined WARNINGS (
    echo  Warnings:!WARNINGS!
) else (
    echo  No warnings.
)
echo ====================================================================
pause
endlocal
exit /b 0

:fail
echo.
echo ====================================================================
echo  BUILD FAILED - scroll up to see the error above.
if defined WARNINGS echo  Earlier warnings:!WARNINGS!
echo ====================================================================
pause
endlocal
exit /b 1
