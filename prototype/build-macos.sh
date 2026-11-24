#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

if [[ "$(uname -s)" != Darwin ]]; then
    echo "Для сборки .pkg требуется macOS." >&2
    exit 1
fi

macos_version=$(sw_vers -productVersion)
if (( ${macos_version%%.*} < 13 )); then
    echo "Этот установщик требует macOS 13 или новее." >&2
    exit 1
fi

if command -v python3.12 >/dev/null; then
    python_command=python3.12
    runtime=.venv
elif command -v python3.14 >/dev/null; then
    python_command=python3.14
    runtime=.venv-3.14
else
    echo "Для сборки требуется Python 3.12 или 3.14." >&2
    exit 1
fi

"$python_command" -m venv "$runtime"
"$runtime/bin/python" -m pip --disable-pip-version-check install --only-binary=:all: -r requirements.txt PyInstaller==6.18.0
architecture=$("$runtime/bin/python" -c 'import platform; print(platform.machine())')
mkdir -p build
xcrun swiftc -parse-as-library -O -target "${architecture}-apple-macos13.0" \
    touch_id.swift -o build/FinanceTouchID
"$runtime/bin/python" -m PyInstaller --noconfirm --windowed \
    --name FinanceArchive --osx-bundle-identifier org.familyfinance.archive \
    --specpath build --workpath build --distpath dist \
    --add-binary "$(pwd)/build/FinanceTouchID:." \
    --collect-all pypdfium2_raw app.py

stage=$(mktemp -d)
trap 'rm -rf "$stage"' EXIT
payload="$stage/payload"
mkdir "$payload"
ditto dist/FinanceArchive.app "$payload/FinanceArchive.app"
pkgbuild --analyze --root "$payload" "$stage/components.plist"
# Keep Installer from updating the build copy or another copy of the app on the Mac.
/usr/libexec/PlistBuddy -c 'Set :0:BundleIsRelocatable false' "$stage/components.plist"
pkgbuild --root "$payload" --component-plist "$stage/components.plist" --install-location /Applications \
    --identifier org.familyfinance.archive --version 1.5.0 \
    dist/FinanceArchive.pkg
