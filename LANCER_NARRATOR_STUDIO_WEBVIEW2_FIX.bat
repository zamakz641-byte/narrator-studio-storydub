@echo off
setlocal
cd /d "%~dp0"
title Narrator Studio - Dubroom

REM Detecte automatiquement la version WebView2 la plus recente
REM qui contient reellement msedgewebview2.exe.
set "WEBVIEW2_BROWSER_EXECUTABLE_FOLDER="
for /f "usebackq delims=" %%V in (`powershell -NoProfile -ExecutionPolicy Bypass -Command "$r='C:\Program Files (x86)\Microsoft\EdgeWebView\Application'; if (Test-Path $r) { Get-ChildItem $r -Directory -ErrorAction SilentlyContinue ^| Where-Object { Test-Path (Join-Path $_.FullName 'msedgewebview2.exe') } ^| Sort-Object { [version]$_.Name } -Descending ^| Select-Object -First 1 -ExpandProperty FullName }"`) do set "WEBVIEW2_BROWSER_EXECUTABLE_FOLDER=%%V"

if defined WEBVIEW2_BROWSER_EXECUTABLE_FOLDER (
    echo [WebView2] Runtime valide force : %WEBVIEW2_BROWSER_EXECUTABLE_FOLDER%
) else (
    echo [AVERTISSEMENT] Aucun runtime WebView2 valide trouve.
)

REM Runtime unique partage avec Dubroom. Aucun venv local n'est recree.
set "RUNTIME=D:\manhwa studio\anime_manga_manhua_dubbing_app\data\environments\tts-omnivoice-hq-torch28-v53\venv"
set "PY=%RUNTIME%\Scripts\python.exe"
set "PYW=%RUNTIME%\Scripts\pythonw.exe"

if not exist "%PY%" goto :runtime_error
if not exist "%PYW%" goto :runtime_error

"%PY%" "%~dp0SELF_TEST.py" > "%~dp0self_test.log" 2>&1
if errorlevel 1 goto :preflight_error

start "" "%PYW%" "%~dp0app.py"
exit /b 0

:preflight_error
echo ============================================================
echo  NARRATOR STUDIO - PREFLIGHT ECHEC
echo ============================================================
type "%~dp0self_test.log"
echo.
pause
exit /b 4

:runtime_error
echo [ERREUR] Runtime Dubroom introuvable : %RUNTIME%
pause
exit /b 2
