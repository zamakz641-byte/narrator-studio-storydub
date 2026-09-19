@echo off
setlocal EnableDelayedExpansion
cd /d "%~dp0"
title Narrator Studio - DEBUG

REM Detecte automatiquement un runtime WebView2 physiquement valide.
set "WEBVIEW2_BROWSER_EXECUTABLE_FOLDER="
for /f "usebackq delims=" %%V in (`powershell -NoProfile -ExecutionPolicy Bypass -Command "$r='C:\Program Files (x86)\Microsoft\EdgeWebView\Application'; if (Test-Path $r) { Get-ChildItem $r -Directory -ErrorAction SilentlyContinue ^| Where-Object { Test-Path (Join-Path $_.FullName 'msedgewebview2.exe') } ^| Sort-Object { [version]$_.Name } -Descending ^| Select-Object -First 1 -ExpandProperty FullName }"`) do set "WEBVIEW2_BROWSER_EXECUTABLE_FOLDER=%%V"

if defined WEBVIEW2_BROWSER_EXECUTABLE_FOLDER (
  echo [WebView2] Runtime valide force : !WEBVIEW2_BROWSER_EXECUTABLE_FOLDER!
) else (
  echo [AVERTISSEMENT] Aucun runtime WebView2 valide trouve.
)

set "DUBROOM_PY=D:\manhwa studio\anime_manga_manhua_dubbing_app\data\environments\tts-omnivoice-hq-torch28-v53\venv\Scripts\python.exe"

if exist "%DUBROOM_PY%" (
  "%DUBROOM_PY%" app.py
  set "APP_RC=!ERRORLEVEL!"
  echo.
  echo [DEBUG] Code de sortie : !APP_RC!
  pause
  exit /b !APP_RC!
)

echo [ERREUR] Runtime Python Dubroom introuvable.
pause
exit /b 1
