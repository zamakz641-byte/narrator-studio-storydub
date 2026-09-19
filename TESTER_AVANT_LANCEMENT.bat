@echo off
setlocal
cd /d "%~dp0"
set "PY=D:\manhwa studio\anime_manga_manhua_dubbing_app\data\environments\tts-omnivoice-hq-torch28-v53\venv\Scripts\python.exe"
if not exist "%PY%" (
  echo [ERREUR] Runtime Python Dubroom introuvable.
  pause
  exit /b 1
)
"%PY%" SELF_TEST.py
pause
