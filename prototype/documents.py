"""Document cards and updates to the encrypted catalog."""

import base64
import fcntl
import hashlib
import json
import os
import re
import stat
import tempfile
from dataclasses import asdict, dataclass, field, fields, replace
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageOps

from assets import Asset, validate_assets
from funds import Funds
from ledger import Ledger
from review import Review
from vault import (
    DOCUMENT_MAGIC, MAGIC, MAX_DOCUMENT_DATA, MAX_PDF, UnlockedVault, _check_pdf,
    _write_new, configure_group, group_for_directory, read_container,
)

CARD_FIELDS = (
    ("title", "Название"), ("kind", "Тип"), ("number", "Номер на оригинале"),
    ("date", "Дата"), ("expires", "Срок действия"), ("party", "Человек или организация"),
    ("tags", "Теги"), ("note", "Примечание"), ("cabinet", "Шкаф или место хранения"),
    ("folder", "Папка"), ("section", "Раздел"), ("position", "Номер в папке"),
)

SCOPES = {"family": "Семейный", "personal": "Личный", "business": "ИП", "legacy": "Архив"}

DEFAULT_KINDS = (
    "Паспорт", "Свидетельство", "Удостоверение", "Водительские права",
    "Страховой полис", "Договор", "Квитанция", "Справка", "Выписка",
    "Доверенность", "Медицинский документ", "Документ на имущество",
)

MAX_PHOTO = 12 * 1024 * 1024
MAX_PHOTO_PIXELS = 50_000_000
MAX_ATTACHMENT_BYTES = MAX_DOCUMENT_DATA * 3 // 4


@dataclass(frozen=True)
class Photo:
    name: str
    data: bytes = field(repr=False)

    def validate(self, *, verify=False) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("У фотографии должно быть имя файла.")
        if not isinstance(self.data, bytes) or len(self.data) > MAX_PHOTO:
            raise ValueError("Размер фотографии должен быть не больше 12 МиБ.")
        try:
            with Image.open(BytesIO(self.data), formats=["JPEG", "PNG"]) as image:
                if image.width * image.height > MAX_PHOTO_PIXELS:
                    raise ValueError("Размер фотографии должен быть не больше 50 мегапикселей.")
                if verify:
                    image.verify()
        except (OSError, SyntaxError, Image.DecompressionBombError) as exc:
            raise ValueError("Не удалось прочитать фотографию. Выберите JPEG или PNG.") from exc

    def render(self, size: tuple[int, int]) -> Image.Image:
        with Image.open(BytesIO(self.data), formats=["JPEG", "PNG"]) as image:
            image.draft(None, (max(size), max(size)))
            image.thumbnail((max(size), max(size)))
            oriented = ImageOps.exif_transpose(image)
            oriented.thumbnail(size)
            return oriented.convert("RGBA")


def read_photo(path: Path) -> Photo:
    with path.open("rb") as stream:
        photo = Photo(path.name, stream.read(MAX_PHOTO + 1))
    photo.validate(verify=True)
    return photo


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
    photos: tuple[Photo, ...] = ()

    @property
    def archive_label(self) -> str:
        return f"{self.archive_number:04d}" if self.archive_number else ""

    def validate(self) -> None:
        if any(not isinstance(getattr(self, key), str) for key, _ in CARD_FIELDS):
            raise ValueError("Некорректные поля документа.")
        if not self.title.strip():
            raise ValueError("Введите название документа.")
        if self.pdf is None and not self.photos and not (self.cabinet.strip() or self.folder.strip()):
            raise ValueError("Приложите PDF или фотографию либо укажите место хранения оригинала.")
        if self.pdf is not None:
            _check_pdf(self.pdf)
        for photo in self.photos:
            photo.validate()

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


def _encode_catalog(
    cards: tuple[Card, ...], assets: tuple[Asset, ...], ledger: Ledger,
    review: Review, funds: Funds, scope: str,
) -> bytes:
    attachment_bytes = sum(
        len(card.pdf or b"") + sum(len(photo.data) for photo in card.photos) for card in cards
    )
    if attachment_bytes > MAX_ATTACHMENT_BYTES:
        raise ValueError("Архив превысил лимит 64 МиБ. Создайте отдельный архив.")
    items = []
    for card in cards:
        card.validate()
        item = asdict(card)
        if card.pdf is not None:
            item["pdf"] = base64.b64encode(card.pdf).decode("ascii")
        if card.photos:
            item["photos"] = [
                {"name": photo.name, "data": base64.b64encode(photo.data).decode("ascii")}
                for photo in card.photos
            ]
        else:
            item.pop("photos")
        items.append(item)
    validate_assets(assets, {card.archive_number for card in cards})
    ledger.validate(assets)
    if scope not in SCOPES:
        raise ValueError("Неизвестная область архива.")
    review.validate(ledger, assets, scope)
    funds.validate(ledger, review)
    content = items
    if assets or scope != "legacy" or review != Review() or funds != Funds():
        content = {"cards": items, "assets": [asdict(asset) for asset in assets]}
        if ledger.openings:
            content["ledger"] = ledger.to_payload()
        if review != Review():
            content["review"] = review.to_payload()
        if funds != Funds():
            content["funds"] = funds.to_payload()
        if scope != "legacy":
            content["scope"] = scope
    payload = json.dumps(content, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(payload) > MAX_DOCUMENT_DATA:
        raise ValueError("Архив превысил лимит 64 МиБ. Создайте отдельный архив.")
    return payload


def decode_catalog(
    payload: bytes,
) -> tuple[tuple[Card, ...], tuple[Asset, ...], Ledger, Review, Funds, str]:
    try:
        content = json.loads(payload)
        assets = []
        ledger = Ledger()
        review, funds, scope = Review(), Funds(), "legacy"
        optional_sections = {"ledger", "review", "funds", "scope"}
        if isinstance(content, dict) and set(content) - optional_sections == {"cards", "assets"}:
            items = content["cards"]
            if "ledger" in content:
                ledger = Ledger.from_payload(content["ledger"])
            if "review" in content:
                review = Review.from_payload(content["review"])
            if "funds" in content:
                funds = Funds.from_payload(content["funds"])
            scope = content.get("scope", "legacy")
            if scope not in SCOPES:
                raise ValueError("Неизвестная область архива.")
            if not isinstance(content["assets"], list):
                raise ValueError("Ожидается список активов.")
            for item in content["assets"]:
                if not isinstance(item, dict) or not isinstance(item.get("document_numbers"), list):
                    raise ValueError("Некорректная карточка актива.")
                item["document_numbers"] = tuple(item["document_numbers"])
                assets.append(Asset(**item))
        else:
            items = content
        if not isinstance(items, list):
            raise ValueError("Ожидается список карточек.")
        cards = []
        optional = {"archive_number", "photos"}
        required = {field.name for field in fields(Card)} - optional
        numbers = set()
        for index, item in enumerate(items, start=1):
            if not isinstance(item, dict) or set(item) - optional != required:
                raise ValueError("Некорректная карточка документа.")
            number = item.setdefault("archive_number", index)
            if type(number) is not int or number < 1 or number in numbers:
                raise ValueError("Некорректный номер карточки.")
            numbers.add(number)
            if item["pdf"] is not None:
                item["pdf"] = base64.b64decode(item["pdf"], validate=True)
            if "photos" in item:
                photos = item["photos"]
                if not isinstance(photos, list):
                    raise ValueError("Некорректный список фотографий.")
                item["photos"] = tuple(
                    Photo(photo["name"], base64.b64decode(photo["data"], validate=True))
                    for photo in photos
                )
            card = Card(**item)
            card.validate()
            cards.append(card)
        validate_assets(tuple(assets), numbers)
        ledger.validate(tuple(assets))
        review.validate(ledger, tuple(assets), scope)
        funds.validate(ledger, review)
        return tuple(cards), tuple(assets), ledger, review, funds, scope
    except (ValueError, TypeError, KeyError, UnicodeError) as exc:
        raise ValueError("Не удалось прочитать карточки архива.") from exc


def _location_order(value: str) -> tuple:
    return tuple(
        (1, int(part)) if part.isdecimal() else (0, part)
        for part in re.split(r"(\d+)", value.casefold())
    )


@dataclass(frozen=True)
class Catalog:
    path: Path
    cards: tuple[Card, ...]
    revision: bytes
    _unlocked: UnlockedVault = field(repr=False, compare=False)
    assets: tuple[Asset, ...] = ()
    ledger: Ledger = Ledger()
    review: Review = Review()
    funds: Funds = Funds()
    scope: str = "legacy"

    @classmethod
    def open(cls, path: Path, password: str) -> "Catalog":
        path = path.resolve()
        data = read_container(path)
        unlocked, magic, payload = UnlockedVault.open(data, password)
        if magic == MAGIC:
            return cls(path, (Card("Тестовый документ", pdf=payload, archive_number=1),),
                       hashlib.sha256(data).digest(), unlocked)
        cards, assets, ledger, review, funds, scope = decode_catalog(payload)
        if scope in ("personal", "business") and path.stat().st_uid != os.getuid():
            raise ValueError("Личный архив и архив ИП открываются из учётной записи владельца.")
        return cls(path, cards, hashlib.sha256(data).digest(), unlocked, assets, ledger, review, funds, scope)

    @classmethod
    def create(
        cls, path: Path, password: str, *, cards: tuple[Card, ...] = (),
        assets: tuple[Asset, ...] = (), ledger: Ledger = Ledger(),
        review: Review = Review(), funds: Funds = Funds(), scope: str = "legacy",
    ) -> "Catalog":
        path = path.parent.resolve() / path.name
        cards = tuple(replace(card, archive_number=index) for index, card in enumerate(cards, start=1))
        unlocked = UnlockedVault.create(password)
        assets = tuple(replace(asset, asset_number=index) for index, asset in enumerate(assets, start=1))
        data = unlocked.encrypt(_encode_catalog(cards, assets, ledger, review, funds, scope), DOCUMENT_MAGIC)
        group = group_for_directory(path) if scope == "family" else None
        _write_new(path, data, group=group)
        return cls(path, cards, hashlib.sha256(data).digest(), unlocked, assets, ledger, review, funds, scope)

    def add(self, card: Card) -> "Catalog":
        number = max((existing.archive_number for existing in self.cards), default=0) + 1
        card = replace(card, archive_number=number, tags=", ".join(tag_names(card.tags)))
        return self._save((*self.cards, card), self.assets)

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

    def folder_inventory(self) -> dict[tuple[str, str], tuple[Card, ...]]:
        folders: dict[tuple[str, str], list[Card]] = {}
        names = {}
        for card in self.cards:
            cabinet, folder = card.cabinet.strip(), card.folder.strip()
            if folder:
                key = names.setdefault((cabinet.casefold(), folder.casefold()), (cabinet, folder))
                folders.setdefault(key, []).append(card)
        return {
            key: tuple(sorted(folders[key], key=lambda card: (
                _location_order(card.section), _location_order(card.position), card.archive_number,
            )))
            for key in sorted(folders, key=lambda pair: tuple(_location_order(part) for part in pair))
        }

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
        return self._save(cards, self.assets)

    def add_asset(self, asset: Asset) -> "Catalog":
        number = max((existing.asset_number for existing in self.assets), default=0) + 1
        return self._save(self.cards, (*self.assets, replace(asset, asset_number=number)))

    def update_asset(self, index: int, asset: Asset) -> "Catalog":
        if not 0 <= index < len(self.assets):
            raise ValueError("Актив не найден.")
        asset = replace(asset, asset_number=self.assets[index].asset_number)
        assets = (*self.assets[:index], asset, *self.assets[index + 1:])
        return self._save(self.cards, assets)

    def recorded_value(self, asset: Asset) -> Asset:
        latest = self.ledger.latest_balance(asset.asset_number)
        return replace(asset, amount=latest[0], as_of=latest[1]) if latest is not None else asset

    def save_ledger(self, ledger: Ledger) -> "Catalog":
        return self._save(self.cards, self.assets, ledger)

    def save_review(self, review: Review) -> "Catalog":
        return self._save(self.cards, self.assets, review=review)

    def save_funds(self, funds: Funds) -> "Catalog":
        return self._save(self.cards, self.assets, funds=funds)

    def liquid_totals(self) -> dict[str, int]:
        totals = {}
        for asset in self.assets:
            if asset.category == "account" and asset.status == "active":
                amount = self.recorded_value(asset).amount
                if amount is not None:
                    totals[asset.currency] = totals.get(asset.currency, 0) + amount
        return totals

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
        if not stat.S_ISREG(mode.st_mode):
            return False
        try:
            if self.shared_group is not None:
                return os.access(self.path, os.R_OK | os.W_OK)
        except OSError:
            return False
        return (
            mode.st_uid == os.getuid()
            and bool(mode.st_mode & stat.S_IWUSR)
            and not mode.st_mode & 0o077
            and os.access(self.path.parent, os.W_OK | os.X_OK)
        )

    @property
    def shared_group(self) -> int | None:
        if self.scope != "family":
            return None
        mode = self.path.lstat()
        group = group_for_directory(self.path)
        return group if group == mode.st_gid and stat.S_IMODE(mode.st_mode) == 0o660 else None

    def _check_editable(self) -> None:
        if not self.editable:
            raise ValueError("Для изменения нужен архив владельца с приватными правами доступа или семейный архив с доступом группы.")

    def _save(
        self, cards: tuple[Card, ...], assets: tuple[Asset, ...], ledger: Ledger | None = None,
        *, review: Review | None = None, funds: Funds | None = None,
    ) -> "Catalog":
        self._check_editable()
        ledger = self.ledger if ledger is None else ledger
        review = self.review if review is None else review
        funds = self.funds if funds is None else funds
        payload = _encode_catalog(cards, assets, ledger, review, funds, self.scope)
        group = self.shared_group
        lock_path = self.path.with_name(f".{self.path.name}.lock")
        flags = os.O_RDWR | os.O_NOFOLLOW
        try:
            fd = os.open(lock_path, flags | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            fd = os.open(lock_path, flags)
        else:
            try:
                configure_group(fd, group)
            except OSError:
                os.close(fd)
                lock_path.unlink(missing_ok=True)
                raise
        with os.fdopen(fd, "rb+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            self._check_editable()
            if self.shared_group != group:
                raise ValueError("Права семейного архива изменились. Откройте его заново.")
            self._read_current()
            data = self._unlocked.encrypt(payload, DOCUMENT_MAGIC)
            with tempfile.NamedTemporaryFile(dir=self.path.parent, delete=False) as stream:
                temporary = Path(stream.name)
                try:
                    configure_group(stream.fileno(), group)
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
                    stream.close()
                    os.replace(temporary, self.path)
                finally:
                    temporary.unlink(missing_ok=True)
        return replace(self, cards=cards, assets=assets, ledger=ledger, review=review, funds=funds,
                       revision=hashlib.sha256(data).digest())
