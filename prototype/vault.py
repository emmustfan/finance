"""Authenticated storage for PDF and document archives."""

import os
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

MAGIC = b"FVAULT\x01"
DOCUMENT_MAGIC = b"FVAULT\x02"
SALT_SIZE = 16
NONCE_SIZE = 12
HEADER_SIZE = len(MAGIC) + SALT_SIZE + NONCE_SIZE
MAX_PDF = 20 * 1024 * 1024
MAX_DOCUMENT_DATA = 64 * 1024 * 1024
MAX_CONTAINER = HEADER_SIZE + MAX_DOCUMENT_DATA + 16


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
    _write_new(path, encrypt(pdf, password, MAGIC))


def encrypt(payload: bytes, password: str, magic: bytes) -> bytes:
    return UnlockedVault.create(password).encrypt(payload, magic)


class UnlockedVault:
    """Keep the derived cipher in memory while the archive is open."""

    def __init__(self, password: str, salt: bytes) -> None:
        self._salt = salt
        self._cipher = AESGCM(_key(password, salt))

    @classmethod
    def create(cls, password: str) -> "UnlockedVault":
        if len(password) < 12:
            raise ValueError("Для нового архива нужен пароль от 12 символов.")
        return cls(password, os.urandom(SALT_SIZE))

    def encrypt(self, payload: bytes, magic: bytes) -> bytes:
        nonce = os.urandom(NONCE_SIZE)
        header = magic + self._salt + nonce
        return header + self._cipher.encrypt(nonce, payload, header)

    @classmethod
    def open(cls, data: bytes, password: str) -> tuple["UnlockedVault", bytes, bytes]:
        magic = data[:len(MAGIC)]
        if magic not in (MAGIC, DOCUMENT_MAGIC) or not HEADER_SIZE + 16 <= len(data) <= MAX_CONTAINER:
            raise ValueError("Неизвестный формат или поврежденный архив.")
        header = data[:HEADER_SIZE]
        salt = header[len(MAGIC):len(MAGIC) + SALT_SIZE]
        nonce = header[-NONCE_SIZE:]
        unlocked = cls(password, salt)
        payload = unlocked._cipher.decrypt(nonce, data[HEADER_SIZE:], header)
        if magic == MAGIC:
            _check_pdf(payload)
        return unlocked, magic, payload


def read_container(path: Path) -> bytes:
    with path.open("rb") as stream:
        return stream.read(MAX_CONTAINER + 1)


def decrypt(data: bytes, password: str) -> tuple[bytes, bytes]:
    _, magic, payload = UnlockedVault.open(data, password)
    return magic, payload


def open_pdf(path: Path, password: str) -> bytes:
    magic, payload = decrypt(read_container(path), password)
    if magic != MAGIC:
        raise ValueError("Ожидается тестовый архив с одним PDF.")
    return payload


def copy_verified(source: Path, destination: Path, password: str) -> None:
    """Authenticate the exact bytes to be copied, without replacing existing files."""
    data = read_container(source)
    magic, payload = decrypt(data, password)
    if magic == DOCUMENT_MAGIC:
        from documents import decode_cards
        decode_cards(payload)
    _write_new(destination, data)
