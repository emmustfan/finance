#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

if [[ "$(uname -s)" != Darwin ]]; then
    echo "Для сборки .pkg требуется macOS." >&2
    exit 1
fi

macos_version=$(sw_vers -productVersion)
if (( ${macos_version%%.*} < 13 )); then
    echo "Эта сборка PDFium требует macOS 13 или новее." >&2
    exit 1
fi

python3.12 -m venv .venv
.venv/bin/python -m pip install --only-binary=:all: -r requirements.txt PyInstaller==6.18.0
.venv/bin/python -m PyInstaller --noconfirm --windowed \
    --name FinanceArchive --osx-bundle-identifier org.familyfinance.archive \
    --specpath build --workpath build --distpath dist \
    --collect-all pypdfium2_raw app.py

stage=$(mktemp -d)
trap 'rm -rf "$stage"' EXIT
ditto dist/FinanceArchive.app "$stage/FinanceArchive.app"
pkgbuild --root "$stage" --install-location /Applications \
    --identifier org.familyfinance.archive --version 0.1.0 \
    dist/FinanceArchive.pkg
