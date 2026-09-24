@echo off
chcp 65001 >nul
rem Двойной щелчок — откроется aihub в браузере. Закрой это окно, чтобы остановить.
cd /d "%~dp0"
where py >nul 2>nul && (py -3 -m aihub ui) || (python -m aihub ui)
if errorlevel 1 (
  echo.
  echo Не получилось запустить. Нужен Python 3.11+: https://www.python.org/downloads/
  pause
)
