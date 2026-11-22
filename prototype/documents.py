"""Document cards and updates to the encrypted catalog."""

import base64
import fcntl
import hashlib
import json
import os
import stat
import tempfile
from dataclasses import asdict, dataclass, fields, replace
from pathlib import Path

from vault import (
    DOCUMENT_MAGIC, MAGIC, MAX_DOCUMENT_DATA, MAX_PDF, _check_pdf, _write_new,
    decrypt, encrypt, read_container,
)

CARD_FIELDS = (
    ("title", "Название"), ("kind", "Тип"), ("number", "Номер"),
    ("date", "Дата"), ("expires", "Срок действия"), ("party", "Человек или организация"),
    ("tags", "Теги"), ("note", "Примечание"), ("cabinet", "Шкаф или место хранения"),
    ("folder", "Папка"), ("section", "Раздел"), ("position", "Номер в папке"),
)


def tag_names(value: str) -> tuple[str, ...]:
    if not isinstance(value, str):
        raise ValueError("Теги должны быть текстом.")
    names = {}
    for part in value.split(","):
        name = part.strip()
        if name:
            names.setdefault(name.casefold(), name)
    return tuple(names.values())


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

    def matches(self, query: str, *, tag: str = "") -> bool:
        tag = tag.casefold()
        if tag and not any(name.casefold() == tag for name in tag_names(self.tags)):
            return False
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

    @classmethod
    def open(cls, path: Path, password: str) -> "Catalog":
        return cls._from_container(path.resolve(), read_container(path), password)

    @classmethod
    def _from_container(cls, path: Path, data: bytes, password: str) -> "Catalog":
        magic, payload = decrypt(data, password)
        cards = (Card("Тестовый документ", pdf=payload),) if magic == MAGIC else decode_cards(payload)
        return cls(path, cards, hashlib.sha256(data).digest())

    @classmethod
    def create(cls, path: Path, password: str, *, cards: tuple[Card, ...] = ()) -> "Catalog":
        path = path.parent.resolve() / path.name
        data = encrypt(_encode_cards(cards), password, DOCUMENT_MAGIC)
        _write_new(path, data)
        return cls(path, cards, hashlib.sha256(data).digest())

    def add(self, card: Card, password: str) -> "Catalog":
        card = replace(card, tags=", ".join(tag_names(card.tags)))
        return self._save((*self.cards, card), password)

    @property
    def available_tags(self) -> tuple[str, ...]:
        names = {}
        for card in self.cards:
            for name in tag_names(card.tags):
                names.setdefault(name.casefold(), name)
        return tuple(names[key] for key in sorted(names))

    def set_tags(self, index: int, value: str, password: str) -> "Catalog":
        if not 0 <= index < len(self.cards):
            raise ValueError("Документ не найден.")
        card = replace(self.cards[index], tags=", ".join(tag_names(value)))
        cards = (*self.cards[:index], card, *self.cards[index + 1:])
        return self._save(cards, password)

    @property
    def editable(self) -> bool:
        try:
            mode = self.path.lstat()
        except OSError:
            return False
        return (
            stat.S_ISREG(mode.st_mode)
            and mode.st_uid == os.getuid()
            and bool(mode.st_mode & stat.S_IWUSR)
            and not mode.st_mode & 0o077
            and os.access(self.path.parent, os.W_OK | os.X_OK)
        )

    def _check_editable(self) -> None:
        if not self.editable:
            raise ValueError("Для изменения нужен архив владельца с приватными правами доступа.")

    def _save(self, cards: tuple[Card, ...], password: str) -> "Catalog":
        self._check_editable()
        payload = _encode_cards(cards)
        lock_path = self.path.with_name(f".{self.path.name}.lock")
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "rb+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            self._check_editable()
            existing = read_container(self.path)
            if hashlib.sha256(existing).digest() != self.revision:
                raise ValueError("Архив изменился. Откройте его заново и повторите изменение.")
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
