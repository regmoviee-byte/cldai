#!/bin/sh
# Запускает aihub и открывает его в браузере. Ctrl+C — остановить.
cd "$(dirname "$0")" || exit 1
exec python3 -m aihub ui
