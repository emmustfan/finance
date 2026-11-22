"""Versioned encrypted container for the stage-zero PDF prototype."""

from pathlib import Path
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

MAGIC = b"FVAULT\x01"
SALT_SIZE = 16
NONCE_SIZE = 12
HEADER_SIZE = len(MAGIC) + SALT_SIZE + NONCE_SIZE
MAX_PDF = 20 * 1024 * 1024
MAX_CONTAINER = HEADER_SIZE + MAX_PDF + 16


def _key(password: str, salt: bytes) -> bytes:
    if not password:
        raise ValueError("Введите пароль архива.")
    return Scrypt(salt=salt, length=32, n=2**17, r=8, p=1).derive(
        password.encode("utf-8")
    )


def _check_pdf(pdf: bytes) -> None:
    if not pdf.startswith(b"%PDF-") or len(pdf) > MAX_PDF:
        raise ValueError("Ожидается PDF размером до 20 МиБ.")


def _write_new(path: Path, data: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def create(path: Path, pdf: bytes, password: str) -> None:
    _check_pdf(pdf)
    if len(password) < 12:
        raise ValueError("Для нового архива нужен пароль от 12 символов.")
    salt = os.urandom(SALT_SIZE)
    nonce = os.urandom(NONCE_SIZE)
    header = MAGIC + salt + nonce
    cipher = AESGCM(_key(password, salt))
    _write_new(path, header + cipher.encrypt(nonce, pdf, header))


def _read(path: Path) -> bytes:
    with path.open("rb") as stream:
        return stream.read(MAX_CONTAINER + 1)


def _decrypt(data: bytes, password: str) -> bytes:
    if not HEADER_SIZE + 16 <= len(data) <= MAX_CONTAINER or not data.startswith(MAGIC):
        raise ValueError("Неизвестный формат или поврежденный архив.")
    header = data[:HEADER_SIZE]
    salt = header[len(MAGIC):len(MAGIC) + SALT_SIZE]
    nonce = header[-NONCE_SIZE:]
    pdf = AESGCM(_key(password, salt)).decrypt(nonce, data[HEADER_SIZE:], header)
    _check_pdf(pdf)
    return pdf


def open_pdf(path: Path, password: str) -> bytes:
    return _decrypt(_read(path), password)


def copy_verified(source: Path, destination: Path, password: str) -> None:
    """Authenticate the exact bytes to be copied, without replacing existing files."""
    data = _read(source)
    _decrypt(data, password)
    _write_new(destination, data)
