"""Local storage for archive keys sealed by the macOS Secure Enclave."""

import subprocess
import sys
from pathlib import Path

from PySide6.QtCore import QByteArray, QSettings

from vault import KEY_SIZE

MAX_SEALED_KEY = 4096


class TouchID:
    def __init__(self):
        self._helper = None
        if sys.platform == "darwin" and getattr(sys, "frozen", False):
            helper = Path(sys.executable).resolve().parents[1] / "Frameworks" / "FinanceTouchID"
            if helper.is_file():
                self._helper = helper
        self._settings = QSettings("org.familyfinance", "FinanceArchive")
        self._settings.setFallbacksEnabled(False)

    def _run(self, action: str, payload: bytes, timeout: int) -> bytes:
        if self._helper is None:
            raise OSError("Touch ID доступен в установленном приложении на совместимом Mac.")
        try:
            result = subprocess.run([str(self._helper), action], input=payload, capture_output=True, timeout=timeout)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise OSError("Не удалось запустить Touch ID.") from exc
        if result.returncode:
            raise OSError("Не удалось подтвердить вход с Touch ID.")
        return result.stdout

    def available(self) -> bool:
        try:
            self._run("available", b"", 5)
        except OSError:
            return False
        return True

    def _record(self, salt: bytes) -> bytes:
        value = self._settings.value(f"TouchID/{salt.hex()}")
        return bytes(value) if isinstance(value, QByteArray) and 0 < len(value) <= MAX_SEALED_KEY else b""

    def configured(self, salt: bytes) -> bool:
        return bool(self._record(salt))

    def enroll(self, salt: bytes, key: bytes) -> None:
        sealed = self._run("enroll", salt + key, 90)
        if not 0 < len(sealed) <= MAX_SEALED_KEY:
            raise OSError("Не удалось защитить ключ архива с Touch ID.")
        self._settings.setValue(f"TouchID/{salt.hex()}", QByteArray(sealed))
        self._sync()

    def unwrap(self, salt: bytes) -> bytes:
        sealed = self._record(salt)
        if not sealed:
            raise OSError("Вход с Touch ID для этого архива ещё не настроен.")
        key = self._run("unlock", salt + sealed, 90)
        if len(key) != KEY_SIZE:
            raise OSError("Не удалось получить ключ архива.")
        return key

    def forget(self, salt: bytes) -> None:
        self._settings.remove(f"TouchID/{salt.hex()}")
        self._sync()

    def _sync(self) -> None:
        self._settings.sync()
        if self._settings.status() != QSettings.Status.NoError:
            raise OSError("Не удалось сохранить настройку Touch ID на этом Mac.")
