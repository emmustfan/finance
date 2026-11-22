"""Document cards and updates to the encrypted catalog."""

import base64
import fcntl
import hashlib
import json
import os
import stat
import tempfile
from dataclasses import asdict, dataclass, fields
from pathlib import Path

from vault import (
    DOCUMENT_MAGIC, MAGIC, MAX_DOCUMENT_DATA, MAX_PDF, _check_pdf, _write_new,
    decrypt, encrypt, read_container,
)

CARD_FIELDS = (
    ("title", "Название"), ("kind", "Тип"), ("number", "Номер"),
    ("date", "Дата"), ("expires", "Срок действия"), ("party", "Человек или организация"),
    ("tags", "Метки"), ("note", "Примечание"), ("cabinet", "Шкаф или место хранения"),
    ("folder", "Папка"), ("section", "Раздел"), ("position", "Номер в папке"),
)


@dataclass(frozen=True)
class Card:
    title: str
    kind: str = ""
    number: str = ""
    date: str = ""
    expires: str = ""
    party: str = ""
    tags: str = ""
    note: str = ""
    cabinet: str = ""
    folder: str = ""
    section: str = ""
    position: str = ""
    pdf: bytes | None = None

    def validate(self) -> None:
        if any(not isinstance(getattr(self, key), str) for key, _ in CARD_FIELDS):
            raise ValueError("Некорректные поля документа.")
        if not self.title.strip():
            raise ValueError("Введите название документа.")
        if self.pdf is None and not (self.cabinet.strip() or self.folder.strip()):
            raise ValueError("Приложите PDF или укажите место хранения оригинала.")
        if self.pdf is not None:
            _check_pdf(self.pdf)

    def matches(self, query: str) -> bool:
        searchable = " ".join(
            (self.title, self.number, self.party, self.kind, self.tags, self.folder)
        ).casefold()
        return all(term in searchable for term in query.casefold().split())

    @property
    def address(self) -> str:
        return " → ".join(filter(None, (self.cabinet, self.folder, self.section, self.position)))


def read_pdf(path: Path) -> bytes:
    with path.open("rb") as stream:
        pdf = stream.read(MAX_PDF + 1)
    _check_pdf(pdf)
    return pdf


def _encode_cards(cards: tuple[Card, ...]) -> bytes:
    items = []
    for card in cards:
        card.validate()
        item = asdict(card)
        if card.pdf is not None:
            item["pdf"] = base64.b64encode(card.pdf).decode("ascii")
        items.append(item)
    payload = json.dumps(items, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(payload) > MAX_DOCUMENT_DATA:
        raise ValueError("Архив превысил лимит 64 МиБ. Создайте отдельный архив.")
    return payload


def decode_cards(payload: bytes) -> tuple[Card, ...]:
    try:
        items = json.loads(payload)
        if not isinstance(items, list):
            raise ValueError("Ожидается список карточек.")
        cards = []
        expected = {field.name for field in fields(Card)}
        for item in items:
            if not isinstance(item, dict) or set(item) != expected:
                raise ValueError("Некорректная карточка документа.")
            if item["pdf"] is not None:
                item["pdf"] = base64.b64decode(item["pdf"], validate=True)
            card = Card(**item)
            card.validate()
            cards.append(card)
        return tuple(cards)
    except (ValueError, TypeError, UnicodeError) as exc:
        raise ValueError("Не удалось прочитать карточки документов.") from exc


@dataclass(frozen=True)
class Catalog:
    path: Path
    cards: tuple[Card, ...]
    revision: bytes
    legacy: bool = False

    @classmethod
    def open(cls, path: Path, password: str) -> "Catalog":
        return cls._from_container(path.resolve(), read_container(path), password)

    @classmethod
    def _from_container(cls, path: Path, data: bytes, password: str) -> "Catalog":
        magic, payload = decrypt(data, password)
        legacy = magic == MAGIC
        cards = (Card("Тестовый документ", pdf=payload),) if legacy else decode_cards(payload)
        return cls(path, cards, hashlib.sha256(data).digest(), legacy)

    @classmethod
    def create(cls, path: Path, password: str) -> "Catalog":
        path = path.parent.resolve() / path.name
        data = encrypt(_encode_cards(()), password, DOCUMENT_MAGIC)
        _write_new(path, data)
        return cls(path, (), hashlib.sha256(data).digest())

    def add(self, card: Card, password: str) -> "Catalog":
        if self.legacy:
            raise ValueError("Создайте новый архив документов для добавления карточек.")
        cards = (*self.cards, card)
        payload = _encode_cards(cards)
        lock_path = self.path.with_name(f".{self.path.name}.lock")
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "rb+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            mode = self.path.lstat()
            if not stat.S_ISREG(mode.st_mode) or mode.st_uid != os.getuid() or mode.st_mode & 0o077:
                raise ValueError("Для изменения нужен архив владельца с приватными правами доступа.")
            existing = read_container(self.path)
            if hashlib.sha256(existing).digest() != self.revision:
                raise ValueError("Архив изменился. Откройте его заново и повторите добавление.")
            decrypt(existing, password)
            data = encrypt(payload, password, DOCUMENT_MAGIC)
            with tempfile.NamedTemporaryFile(dir=self.path.parent, delete=False) as stream:
                temporary = Path(stream.name)
                try:
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
                    stream.close()
                    os.replace(temporary, self.path)
                finally:
                    temporary.unlink(missing_ok=True)
        return Catalog(self.path, cards, hashlib.sha256(data).digest())
