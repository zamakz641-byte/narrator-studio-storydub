@echo off
setlocal
cd /d "%~dp0"
title Narrator Studio - Dubroom

REM Runtime unique partagé avec Dubroom. Aucun venv local n'est recréé.
set "RUNTIME=D:\manhwa studio\anime_manga_manhua_dubbing_app\data\environments\tts-omnivoice-hq-torch28-v53\venv"
set "PY=%RUNTIME%\Scripts\python.exe"
set "PYW=%RUNTIME%\Scripts\pythonw.exe"
if not exist "%PY%" goto :runtime_error
if not exist "%PYW%" goto :runtime_error

:test_local
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
