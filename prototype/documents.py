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
from datetime import date, datetime, timedelta
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageOps

from assets import Asset, validate_assets
from debts import ISSUE_SIGN, Debt, Repayment
from finance import Directory
from funds import Funds
from ledger import Entry, Ledger, Opening, _day
from review import Assignment, Review
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


def document_deadline(value: str) -> date | None:
    for pattern in ("%Y-%m-%d", "%d.%m.%Y"):
        try:
            return datetime.strptime(value.strip(), pattern).date()
        except ValueError:
            continue
    return None


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
            (self.title, self.number, self.party, self.kind, self.tags, self.date, self.expires, self.note,
             self.address, self.archive_label)
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
    review: Review, funds: Funds, directory: Directory, scope: str,
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
    directory.validate()
    content = items
    if assets or scope != "legacy" or review != Review() or funds != Funds() or directory != Directory():
        records = []
        for asset in assets:
            record = asdict(asset)
            for key in ("ticker", "quantity", "purchase_cost"):
                if record[key] is None or record[key] == "":
                    record.pop(key)
            records.append(record)
        content = {"cards": items, "assets": records}
        if ledger.openings:
            content["ledger"] = ledger.to_payload()
        if review != Review():
            content["review"] = review.to_payload()
        if review.tags or review.debts.items:
            content["format_revision"] = 2
        if funds != Funds():
            content["funds"] = funds.to_payload()
        if directory != Directory():
            content["directory"] = directory.to_payload()
        if scope != "legacy":
            content["scope"] = scope
    payload = json.dumps(content, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(payload) > MAX_DOCUMENT_DATA:
        raise ValueError("Архив превысил лимит 64 МиБ. Создайте отдельный архив.")
    return payload


def decode_catalog(
    payload: bytes,
) -> tuple[tuple[Card, ...], tuple[Asset, ...], Ledger, Review, Funds, Directory, str]:
    try:
        content = json.loads(payload)
        assets = []
        ledger = Ledger()
        review, funds, directory, scope = Review(), Funds(), Directory(), "legacy"
        optional_sections = {"ledger", "review", "funds", "directory", "scope", "format_revision"}
        if isinstance(content, dict) and set(content) - optional_sections == {"cards", "assets"}:
            items = content["cards"]
            if "format_revision" in content and content["format_revision"] != 2:
                raise ValueError("Для этого формата требуется более новая версия приложения.")
            if "ledger" in content:
                ledger = Ledger.from_payload(content["ledger"])
            if "review" in content:
                review = Review.from_payload(content["review"])
            if "funds" in content:
                funds = Funds.from_payload(content["funds"])
            if "directory" in content:
                directory = Directory.from_payload(content["directory"])
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
        directory.validate()
        return tuple(cards), tuple(assets), ledger, review, funds, directory, scope
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
    directory: Directory = Directory()

    @classmethod
    def open(cls, path: Path, password: str) -> "Catalog":
        path = path.resolve()
        data = read_container(path)
        unlocked, magic, payload = UnlockedVault.open(data, password)
        if magic == MAGIC:
            return cls(path, (Card("Тестовый документ", pdf=payload, archive_number=1),),
                       hashlib.sha256(data).digest(), unlocked)
        cards, assets, ledger, review, funds, directory, scope = decode_catalog(payload)
        if scope in ("personal", "business") and path.stat().st_uid != os.getuid():
            raise ValueError("Личный архив и архив ИП открываются из учётной записи владельца.")
        return cls(path, cards, hashlib.sha256(data).digest(), unlocked, assets, ledger, review, funds, scope, directory)

    @classmethod
    def create(
        cls, path: Path, password: str, *, cards: tuple[Card, ...] = (),
        assets: tuple[Asset, ...] = (), ledger: Ledger = Ledger(),
        review: Review = Review(), funds: Funds = Funds(), scope: str = "legacy",
        directory: Directory = Directory(),
    ) -> "Catalog":
        path = path.parent.resolve() / path.name
        cards = tuple(replace(card, archive_number=index) for index, card in enumerate(cards, start=1))
        unlocked = UnlockedVault.create(password)
        assets = tuple(replace(asset, asset_number=index) for index, asset in enumerate(assets, start=1))
        data = unlocked.encrypt(_encode_catalog(cards, assets, ledger, review, funds, directory, scope), DOCUMENT_MAGIC)
        group = group_for_directory(path) if scope == "family" else None
        _write_new(path, data, group=group)
        return cls(path, cards, hashlib.sha256(data).digest(), unlocked, assets, ledger, review, funds, scope, directory)

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
        staged = self.stage_asset(asset)
        return self._save(self.cards, staged.assets)

    def stage_asset(self, asset: Asset) -> "Catalog":
        number = max((existing.asset_number for existing in self.assets), default=0) + 1
        return replace(self, assets=(*self.assets, replace(asset, asset_number=number)))

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

    def save_directory(self, directory: Directory, review: Review) -> "Catalog":
        return self._save(self.cards, self.assets, review=review, directory=directory)

    def save_import(self, staged: "Catalog") -> "Catalog":
        if staged.path != self.path or staged.revision != self.revision:
            raise ValueError("Архив изменился. Откройте его заново и повторите импорт.")
        return self._save(self.cards, staged.assets, staged.ledger, review=staged.review, directory=staged.directory)

    def _manual_opening(self, ledger: Ledger, account: int, day: str, prior: int | None) -> Ledger:
        asset = next((asset for asset in self.assets if asset.asset_number == account
                      and asset.category == "account" and asset.status == "active"), None)
        if asset is None:
            raise ValueError("Выберите действующий счёт или кошелёк.")
        earliest = date.fromisoformat(_day(day))
        if earliest == date.min:
            raise ValueError("Выберите более позднюю дату операции.")
        opening = ledger.opening(account)
        if opening is not None:
            if day > opening.day:
                return ledger
            if opening.flexible:
                moved = replace(opening, day=(earliest - timedelta(days=1)).isoformat())
                return replace(ledger, openings=tuple(moved if item.account == account else item
                                                     for item in ledger.openings))
            raise ValueError(f"Вводите операции после {opening.day}: это дата начального остатка счёта.")
        if prior is None and asset.amount is not None and asset.as_of < day:
            return ledger.set_opening(Opening(account, asset.as_of, asset.amount, asset.currency))
        if prior is None and asset.amount is not None:
            raise ValueError("Укажите остаток непосредственно перед операцией: дата оценки счёта совпадает с ней или позже.")
        start = (earliest - timedelta(days=1)).isoformat()
        return ledger.set_opening(Opening(account, start, prior or 0, asset.currency,
                                         flexible=asset.amount is None and not prior))

    def record_manual(self, entry: Entry, category: str = "", *, prior: int | None = None,
                      number: int | None = None, taxable: bool = False,
                      fund: int | None = None, tags: tuple[str, ...] | None = None,
                      directory: Directory | None = None) -> "Catalog":
        if entry.source != "manual":
            raise ValueError("Ручной ввод принимает только ручные операции.")
        ledger, review = self.ledger, self.review
        if number is not None:
            if fund is not None:
                raise ValueError("Привяжите уже записанный расход через движения фонда.")
            if not 1 <= number <= len(ledger.entries):
                raise ValueError("Операция не найдена.")
            old = ledger.entries[number - 1]
            if old.source != "manual" or number in review.fixed_operations or any(
                move.operation == number for move in self.funds.movements
            ):
                raise ValueError("Для этой операции доступны статьи в разделе разбора. Денежные движения связаны с другими записями.")
        ledger = self._manual_opening(ledger, entry.account, entry.day, prior)
        if number is None:
            ledger = ledger.add_manual(entry)
            number = len(ledger.entries)
        else:
            ledger = replace(ledger, entries=(*ledger.entries[:number - 1], entry, *ledger.entries[number:]))
        category = " ".join(category.split())
        if category:
            if len(category) > 100:
                raise ValueError("Название статьи должно содержать не более 100 символов.")
            found = (self.directory if directory is None else directory).lookup(
                1 if entry.amount > 0 else -1, category
            )
            if found is not None:
                previous = review.assignments_by_operation().get(number)
                if found.archived and (previous is None or previous.category != found.name):
                    raise ValueError("Эта статья в архиве. Выберите действующую или верните её в справочнике.")
                category = found.name
            review = review.assign((Assignment(number, category, taxable),))
        else:
            review = replace(review, assignments=tuple(item for item in review.assignments
                                                       if item.operation != number))
        funds = self.funds
        if fund is not None:
            if entry.amount >= 0:
                raise ValueError("Из фонда можно списать только расход.")
            funds = funds.apply(f"expense:{number}", ((fund, entry.amount),), number)
        if tags is not None:
            review = review.tag_operations({number: tags})
        return self._save(self.cards, self.assets, ledger, review=review, funds=funds, directory=directory)

    def record_transfer(self, debit: Entry, credit: Entry, *, debit_prior: int | None = None,
                        credit_prior: int | None = None, tags: tuple[str, ...] = ()) -> "Catalog":
        ledger = self._manual_opening(self.ledger, debit.account, debit.day, debit_prior)
        ledger = self._manual_opening(ledger, credit.account, credit.day, credit_prior)
        debit_number = len(ledger.entries) + 1
        ledger = ledger.add_manual(debit).add_manual(credit)
        review = self.review.link_transfer(ledger, debit_number, debit_number + 1)
        if tags:
            review = review.tag_operations({debit_number: tags, debit_number + 1: tags})
        return self._save(self.cards, self.assets, ledger, review=review)

    def record_debt(self, debt: Debt, *, account: int | None = None, prior: int | None = None) -> "Catalog":
        ledger = self.ledger
        if account is not None:
            if debt.issue is not None or debt.direction not in ISSUE_SIGN:
                raise ValueError("Выберите один способ записи выдачи займа.")
            ledger = self._manual_opening(ledger, account, debt.day, prior)
            amount = debt.amount * ISSUE_SIGN[debt.direction]
            ledger = ledger.add_manual(Entry(account, debt.day, amount, debt.title, recipient=debt.title))
            debt = replace(debt, issue=len(ledger.entries))
        review = self.review.with_debts(self.review.debts.add(debt), attached=debt.issue)
        return self._save(self.cards, self.assets, ledger, review=review)

    def repay_debt(self, number: int, principal: int, *, entry: Entry | None = None,
                   operation_number: int | None = None, prior: int | None = None) -> "Catalog":
        ledger = self.ledger
        if entry is not None:
            if operation_number is not None or entry.source != "manual":
                raise ValueError("Выберите один способ записи возврата.")
            ledger = self._manual_opening(ledger, entry.account, entry.day, prior)
            ledger = ledger.add_manual(entry)
            operation_number = len(ledger.entries)
        debts = self.review.debts
        debts = replace(debts, payments=(*debts.payments, Repayment(number, operation_number, principal)))
        review = self.review.with_debts(debts, attached=operation_number)
        return self._save(self.cards, self.assets, ledger, review=review)

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
        *, review: Review | None = None, funds: Funds | None = None, directory: Directory | None = None,
    ) -> "Catalog":
        self._check_editable()
        ledger = self.ledger if ledger is None else ledger
        review = self.review if review is None else review
        funds = self.funds if funds is None else funds
        directory = self.directory if directory is None else directory
        payload = _encode_catalog(cards, assets, ledger, review, funds, directory, self.scope)
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
        return replace(self, cards=cards, assets=assets, ledger=ledger, review=review, funds=funds, directory=directory,
                       revision=hashlib.sha256(data).digest())
