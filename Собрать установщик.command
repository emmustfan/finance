#!/bin/bash
set -Eeuo pipefail

trap 'echo "Сборка не завершилась. Подробности выше."; if [[ -t 0 ]]; then read -r -p "Нажмите Enter, чтобы закрыть окно. " || true; fi' ERR
cd "$(dirname "$0")"
export PATH="/Library/Frameworks/Python.framework/Versions/3.14/bin:/Library/Frameworks/Python.framework/Versions/3.12/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"
bash prototype/build-macos.sh
open -R prototype/dist/FinanceArchive.pkg
