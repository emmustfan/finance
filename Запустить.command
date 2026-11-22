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

export PATH="/Library/Frameworks/Python.framework/Versions/3.14/bin:/Library/Frameworks/Python.framework/Versions/3.12/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"
python_command=""
for candidate in python3.14 python3.12; do
    if command -v "$candidate" >/dev/null && "$candidate" -c 'import tkinter' 2>/dev/null; then
        python_command="$candidate"
        break
    fi
done
if [[ -z "$python_command" ]]; then
    echo "Установите Python 3.14 или 3.12 с поддержкой Tk, затем снова откройте этот файл."
    open 'https://www.python.org/downloads/macos/'
    wait_for_enter
    exit 1
fi

runtime=".venv"
if [[ "$python_command" == python3.14 ]]; then
    runtime=".venv-3.14"
fi
if [[ ! -x "$runtime/bin/python" ]]; then
    "$python_command" -m venv "$runtime"
fi

requirements="prototype/requirements.txt"
stamp="$runtime/finance-requirements.txt"
if ! cmp -s "$requirements" "$stamp"; then
    echo "Устанавливаю зависимости для первого запуска…"
    "$runtime/bin/python" -m pip --disable-pip-version-check install --only-binary=:all: -r "$requirements"
    cp "$requirements" "$stamp"
fi

"$runtime/bin/python" prototype/app.py
