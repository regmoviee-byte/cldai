#!/bin/bash
# Двойной щелчок — откроется aihub в браузере. Закрой это окно, чтобы остановить.
cd "$(dirname "$0")" || exit 1
python3 -m aihub ui || { echo; echo "Нужен Python 3.11+: https://www.python.org/downloads/"; read -r -p "Enter — закрыть"; }
