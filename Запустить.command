#!/bin/bash
set -Eeuo pipefail

wait_for_enter() {
    if [[ -t 0 ]]; then
        read -r -p "Нажмите Enter, чтобы закрыть окно. " || true
    fi
}

trap 'echo "Запуск не завершился. Подробности выше."; wait_for_enter' ERR
cd "$(dirname "$0")"

if [[ "$(uname -s)" != Darwin ]]; then
    echo "Этот файл запуска предназначен для macOS."
    wait_for_enter
    exit 1
fi

version="$(sw_vers -productVersion)"
if (( ${version%%.*} < 13 )); then
    echo "Требуется macOS 13 или новее."
    wait_for_enter
    exit 1
fi

export PATH="/Library/Frameworks/Python.framework/Versions/3.12/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"
if ! command -v python3.12 >/dev/null || ! python3.12 -c 'import tkinter'; then
    echo "Установите Python 3.12 с поддержкой Tk, затем снова откройте этот файл."
    open 'https://www.python.org/downloads/macos/'
    wait_for_enter
    exit 1
fi

runtime=".venv"
if [[ ! -x "$runtime/bin/python" ]]; then
    python3.12 -m venv "$runtime"
fi

requirements="prototype/requirements.txt"
stamp="$runtime/finance-requirements.txt"
if ! cmp -s "$requirements" "$stamp"; then
    echo "Устанавливаю зависимости для первого запуска…"
    "$runtime/bin/python" -m pip --disable-pip-version-check install --only-binary=:all: -r "$requirements"
    cp "$requirements" "$stamp"
fi

"$runtime/bin/python" prototype/app.py
