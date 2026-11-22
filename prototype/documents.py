"""Document cards and updates to the encrypted catalog."""

import base64
import fcntl
import hashlib
import json
import os
import stat
import tempfile
from dataclasses import asdict, dataclass, field, fields, replace
from pathlib import Path

from vault import (
    DOCUMENT_MAGIC, MAGIC, MAX_DOCUMENT_DATA, MAX_PDF, UnlockedVault, _check_pdf,
    _write_new, read_container,
)

CARD_FIELDS = (
    ("title", "Название"), ("kind", "Тип"), ("number", "Номер на оригинале"),
    ("date", "Дата"), ("expires", "Срок действия"), ("party", "Человек или организация"),
    ("tags", "Теги"), ("note", "Примечание"), ("cabinet", "Шкаф или место хранения"),
    ("folder", "Папка"), ("section", "Раздел"), ("position", "Номер в папке"),
)

DEFAULT_KINDS = (
    "Паспорт", "Свидетельство", "Удостоверение", "Водительские права",
    "Страховой полис", "Договор", "Квитанция", "Справка", "Выписка",
    "Доверенность", "Медицинский документ", "Документ на имущество",
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
    archive_number: int = 0

    @property
    def archive_label(self) -> str:
        return f"{self.archive_number:04d}" if self.archive_number else ""

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
            (self.title, self.number, self.party, self.kind, self.tags, self.folder,
             self.archive_label)
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
        required = {field.name for field in fields(Card)} - {"archive_number"}
        numbers = set()
        for index, item in enumerate(items, start=1):
            if not isinstance(item, dict) or set(item) - {"archive_number"} != required:
                raise ValueError("Некорректная карточка документа.")
            number = item.setdefault("archive_number", index)
            if type(number) is not int or number < 1 or number in numbers:
                raise ValueError("Некорректный номер карточки.")
            numbers.add(number)
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
    _unlocked: UnlockedVault = field(repr=False, compare=False)

    @classmethod
    def open(cls, path: Path, password: str) -> "Catalog":
        path = path.resolve()
        data = read_container(path)
        unlocked, magic, payload = UnlockedVault.open(data, password)
        cards = (Card("Тестовый документ", pdf=payload, archive_number=1),) if magic == MAGIC else decode_cards(payload)
        return cls(path, cards, hashlib.sha256(data).digest(), unlocked)

    @classmethod
    def create(cls, path: Path, password: str, *, cards: tuple[Card, ...] = ()) -> "Catalog":
        path = path.parent.resolve() / path.name
        cards = tuple(replace(card, archive_number=index) for index, card in enumerate(cards, start=1))
        unlocked = UnlockedVault.create(password)
        data = unlocked.encrypt(_encode_cards(cards), DOCUMENT_MAGIC)
        _write_new(path, data)
        return cls(path, cards, hashlib.sha256(data).digest(), unlocked)

    def add(self, card: Card) -> "Catalog":
        number = max((existing.archive_number for existing in self.cards), default=0) + 1
        card = replace(card, archive_number=number, tags=", ".join(tag_names(card.tags)))
        return self._save((*self.cards, card))

    @property
    def available_kinds(self) -> tuple[str, ...]:
        kinds = {kind.casefold(): kind for kind in DEFAULT_KINDS}
        for card in self.cards:
            if card.kind:
                kinds.setdefault(card.kind.casefold(), card.kind)
        return tuple(kinds[key] for key in sorted(kinds))

    @property
    def available_tags(self) -> tuple[str, ...]:
        names = {}
        for card in self.cards:
            for name in tag_names(card.tags):
                names.setdefault(name.casefold(), name)
        return tuple(names[key] for key in sorted(names))

    def set_tags(self, index: int, value: str) -> "Catalog":
        if not 0 <= index < len(self.cards):
            raise ValueError("Документ не найден.")
        return self.update(index, replace(self.cards[index], tags=value))

    def update(self, index: int, card: Card) -> "Catalog":
        if not 0 <= index < len(self.cards):
            raise ValueError("Документ не найден.")
        card = replace(
            card, archive_number=self.cards[index].archive_number,
            tags=", ".join(tag_names(card.tags)),
        )
        cards = (*self.cards[:index], card, *self.cards[index + 1:])
        return self._save(cards)

    def _read_current(self) -> bytes:
        data = read_container(self.path)
        if hashlib.sha256(data).digest() != self.revision:
            raise ValueError("Архив изменился. Откройте его заново и повторите действие.")
        return data

    def backup(self, destination: Path) -> None:
        _write_new(destination, self._read_current())

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

    def _save(self, cards: tuple[Card, ...]) -> "Catalog":
        self._check_editable()
        payload = _encode_cards(cards)
        lock_path = self.path.with_name(f".{self.path.name}.lock")
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "rb+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            self._check_editable()
            self._read_current()
            data = self._unlocked.encrypt(payload, DOCUMENT_MAGIC)
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
        return replace(self, cards=cards, revision=hashlib.sha256(data).digest())
