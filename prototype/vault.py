"""Authenticated storage for PDF and document archives."""

import os
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

MAGIC = b"FVAULT\x01"
DOCUMENT_MAGIC = b"FVAULT\x02"
SALT_SIZE = 16
KEY_SIZE = 32
NONCE_SIZE = 12
HEADER_SIZE = len(MAGIC) + SALT_SIZE + NONCE_SIZE
MAX_PDF = 20 * 1024 * 1024
MAX_DOCUMENT_DATA = 64 * 1024 * 1024
MAX_CONTAINER = HEADER_SIZE + MAX_DOCUMENT_DATA + 16


def derive_archive_key(password: str, salt: bytes) -> bytes:
    if not password:
        raise ValueError("Введите пароль архива.")
    return Scrypt(salt=salt, length=KEY_SIZE, n=2**17, r=8, p=1).derive(
        password.encode("utf-8")
    )


def archive_salt(data: bytes) -> bytes:
    if data[:len(MAGIC)] not in (MAGIC, DOCUMENT_MAGIC) or not HEADER_SIZE + 16 <= len(data) <= MAX_CONTAINER:
        raise ValueError("Неизвестный формат или поврежденный архив.")
    return data[len(MAGIC):len(MAGIC) + SALT_SIZE]


def read_archive_salt(path: Path) -> bytes:
    with path.open("rb") as stream:
        return archive_salt(stream.read(HEADER_SIZE + 16))


def _check_pdf(pdf: bytes) -> None:
    if not pdf.startswith(b"%PDF-") or len(pdf) > MAX_PDF:
        raise ValueError("Ожидается PDF размером до 20 МиБ.")


def group_for_directory(path: Path) -> int | None:
    directory = path.parent.stat()
    groups = {os.getgid(), *os.getgroups()}
    return directory.st_gid if (
        directory.st_mode & 0o077 == 0o070
        and directory.st_gid in groups
        and os.access(path.parent, os.W_OK | os.X_OK)
    ) else None


def configure_group(fd: int, group: int | None) -> None:
    if group is not None:
        if os.fstat(fd).st_gid != group:
            os.fchown(fd, -1, group)
        os.fchmod(fd, 0o660)


def _write_new(path: Path, data: bytes, *, group: int | None = None) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as stream:
            configure_group(stream.fileno(), group)
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

    def __init__(self, key: bytes, salt: bytes) -> None:
        self._salt = salt
        self._cipher = AESGCM(key)

    @property
    def salt(self) -> bytes:
        return self._salt

    @classmethod
    def create(cls, password: str) -> "UnlockedVault":
        if len(password) < 12:
            raise ValueError("Для нового архива нужен пароль от 12 символов.")
        salt = os.urandom(SALT_SIZE)
        return cls(derive_archive_key(password, salt), salt)

    def encrypt(self, payload: bytes, magic: bytes) -> bytes:
        nonce = os.urandom(NONCE_SIZE)
        header = magic + self._salt + nonce
        return header + self._cipher.encrypt(nonce, payload, header)

    @classmethod
    def open(cls, data: bytes, password: str) -> tuple["UnlockedVault", bytes, bytes]:
        return cls.open_with_key(data, derive_archive_key(password, archive_salt(data)))

    @classmethod
    def open_with_key(cls, data: bytes, key: bytes) -> tuple["UnlockedVault", bytes, bytes]:
        salt = archive_salt(data)
        if len(key) != KEY_SIZE:
            raise ValueError("Не удалось получить ключ архива.")
        header = data[:HEADER_SIZE]
        magic = header[:len(MAGIC)]
        unlocked = cls(key, salt)
        payload = unlocked._cipher.decrypt(header[-NONCE_SIZE:], data[HEADER_SIZE:], header)
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


def copy_verified(
    source: Path, destination: Path, password: str, *, restore_shared: bool = False
) -> None:
    """Authenticate the exact bytes to be copied, without replacing existing files."""
    data = read_container(source)
    magic, payload = decrypt(data, password)
    group = None
    if magic == DOCUMENT_MAGIC:
        from documents import decode_catalog
        *_, scope = decode_catalog(payload)
        if scope in ("personal", "business") and source.stat().st_uid != os.getuid():
            raise ValueError("Личный архив и архив ИП восстанавливает владелец файла.")
        if restore_shared and scope == "family":
            group = group_for_directory(destination)
    _write_new(destination, data, group=group)
