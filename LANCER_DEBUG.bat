@echo off
setlocal
cd /d "%~dp0"
set "DUBROOM_PY=D:\manhwa studio\anime_manga_manhua_dubbing_app\data\environments\tts-omnivoice-hq-torch28-v53\venv\Scripts\python.exe"
if exist "%DUBROOM_PY%" (
  "%DUBROOM_PY%" app.py
  pause
  exit /b %ERRORLEVEL%
)
echo [ERREUR] Runtime Python Dubroom introuvable.
pause
exit /b 1
