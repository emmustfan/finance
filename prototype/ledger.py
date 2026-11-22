"""Exact-cent account ledger, CSV preview and balance reconciliation."""

import csv
import hashlib
import io
import re
from collections import Counter
from dataclasses import asdict, dataclass, replace
from datetime import date, datetime
from pathlib import Path

from assets import Asset, parse_amount

MAX_CSV = 10 * 1024 * 1024
MAX_ROWS = 20_000
ALIASES = {
    "date": ("date", "operation date", "дата", "дата операции", "дата платежа"),
    "amount": ("amount", "sum", "сумма", "сумма операции", "сумма платежа"),
    "description": ("description", "назначение платежа", "описание", "назначение", "операция"),
    "identifier": ("operation id", "transaction id", "id", "id операции", "идентификатор операции"),
    "currency": ("currency", "валюта", "валюта операции", "валюта платежа"),
    "account": ("account", "account number", "счёт", "счет", "номер счета", "номер счёта"),
    "balance": ("balance", "остаток", "баланс", "остаток после операции", "остаток на счете"),
}
NEEDED = ("date", "amount", "description")


def _normalized(text: str) -> str:
    return " ".join(text.casefold().split())


def _day(text: str) -> str:
    try:
        parsed = date.fromisoformat(text)
        if parsed.isoformat() == text:
            return text
    except ValueError:
        pass
    raise ValueError("Укажите дату в формате ГГГГ-ММ-ДД.")


def _statement_day(text: str) -> str:
    try:
        return datetime.fromisoformat(text).date().isoformat()
    except ValueError:
        pass
    for format_string in ("%d.%m.%Y", "%d.%m.%Y %H:%M", "%d.%m.%Y %H:%M:%S"):
        try:
            return datetime.strptime(text, format_string).date().isoformat()
        except ValueError:
            continue
    raise ValueError("Не удалось прочитать дату операции.")


def _asset_accounts(assets: tuple[Asset, ...]) -> dict[int, Asset]:
    return {asset.asset_number: asset for asset in assets if asset.category == "account"}


@dataclass(frozen=True)
class Opening:
    account: int
    day: str
    amount: int
    currency: str
    reference: str = ""


@dataclass(frozen=True)
class Entry:
    account: int
    day: str
    amount: int
    description: str
    external_id: str = ""
    source: str = "manual"

    @property
    def fingerprint(self) -> tuple[str, int, str]:
        return self.day, self.amount, _normalized(self.description)


@dataclass(frozen=True)
class BankBalance:
    account: int
    day: str
    amount: int


@dataclass(frozen=True)
class ImportedFile:
    account: int
    digest: str


@dataclass(frozen=True)
class Ledger:
    openings: tuple[Opening, ...] = ()
    entries: tuple[Entry, ...] = ()
    files: tuple[ImportedFile, ...] = ()
    balances: tuple[BankBalance, ...] = ()

    def to_payload(self) -> dict:
        return asdict(self)

    @classmethod
    def from_payload(cls, payload: dict) -> "Ledger":
        types = {"openings": Opening, "entries": Entry, "files": ImportedFile, "balances": BankBalance}
        if not isinstance(payload, dict) or payload.keys() != types.keys():
            raise ValueError("Некорректные данные журнала операций.")
        return cls(**{name: tuple(model(**item) for item in payload[name]) for name, model in types.items()})

    def validate(self, assets: tuple[Asset, ...]) -> None:
        accounts = _asset_accounts(assets)
        openings = {}
        for opening in self.openings:
            asset = accounts.get(opening.account)
            if (
                type(opening.account) is not int or opening.account in openings or asset is None
                or opening.currency != asset.currency or type(opening.amount) is not int
                or not isinstance(opening.reference, str)
            ):
                raise ValueError("Учет требует существующего счёта с прежней валютой.")
            _day(opening.day)
            openings[opening.account] = opening
        identifiers = set()
        for entry in self.entries:
            if (
                type(entry.account) is not int or entry.account not in openings
                or _day(entry.day) <= openings[entry.account].day
                or type(entry.amount) is not int or not entry.amount
                or not isinstance(entry.description, str) or not entry.description.strip()
                or not isinstance(entry.external_id, str) or entry.source not in ("manual", "csv")
            ):
                raise ValueError("Операция должна иметь сумму, описание и дату после начального остатка.")
            if entry.external_id:
                identity = (entry.account, entry.external_id)
                if identity in identifiers:
                    raise ValueError("У счёта уже есть операция с таким ID банка.")
                identifiers.add(identity)
        keys = set()
        for file in self.files:
            key = (file.account, file.digest)
            if file.account not in openings or key in keys or not re.fullmatch(r"[a-f0-9]{64}", file.digest):
                raise ValueError("Некорректная история импорта.")
            keys.add(key)
        for balance in self.balances:
            if (
                balance.account not in openings or _day(balance.day) < openings[balance.account].day
                or type(balance.amount) is not int
            ):
                raise ValueError("Некорректный банковский остаток.")

    def opening(self, account: int) -> Opening | None:
        return next((opening for opening in self.openings if opening.account == account), None)

    def set_opening(self, opening: Opening) -> "Ledger":
        previous = self.opening(opening.account)
        if previous and any(entry.account == opening.account for entry in self.entries):
            if (opening.day, opening.amount) != (previous.day, previous.amount):
                raise ValueError("После записи операций можно менять только реквизит счёта.")
        openings = tuple(item for item in self.openings if item.account != opening.account)
        return replace(self, openings=(*openings, opening))

    def add_manual(self, entry: Entry) -> "Ledger":
        return replace(self, entries=(*self.entries, entry))

    def balance_on(self, account: int, day: str) -> int:
        opening = self.opening(account)
        if opening is None or _day(day) < opening.day:
            raise ValueError("Выберите дату начального остатка или позже.")
        return opening.amount + sum(
            entry.amount for entry in self.entries if entry.account == account and entry.day <= day
        )

    def latest_balance(self, account: int) -> tuple[int, str] | None:
        opening = self.opening(account)
        if opening is None:
            return None
        day = max(
            [opening.day, *(entry.day for entry in self.entries if entry.account == account)]
        )
        return self.balance_on(account, day), day


@dataclass(frozen=True)
class Statement:
    name: str
    digest: str
    columns: tuple[str, ...]
    rows: tuple[tuple[int, tuple[str, ...]], ...]

    def suggested_columns(self) -> dict[str, int]:
        normalized = [_normalized(column) for column in self.columns]
        mapping = {}
        for field, aliases in ALIASES.items():
            candidates = [index for index, column in enumerate(normalized) if column in aliases]
            if len(candidates) == 1:
                mapping[field] = candidates[0]
        return mapping

    def reference(self, mapping: dict[str, int]) -> str:
        column = mapping.get("account")
        if column is None:
            return ""
        values = {fields[column].strip() for _, fields in self.rows if column < len(fields) and fields[column].strip()}
        return next(iter(values)) if len(values) == 1 else ""


def read_statement(path: Path) -> Statement:
    with path.open("rb") as stream:
        raw = stream.read(MAX_CSV + 1)
    if len(raw) > MAX_CSV:
        raise ValueError("Размер одной выписки CSV ограничен 10 МиБ.")
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        text = raw.decode("utf-16")
    else:
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = raw.decode("cp1251")
    first_line = next((line for line in text.splitlines() if line.strip()), "")
    delimiter = max((";", ",", "\t"), key=lambda candidate: len(next(csv.reader([first_line], delimiter=candidate))))
    reader = csv.reader(io.StringIO(text, newline=""), delimiter=delimiter, strict=True)
    try:
        header = next((row for row in reader if any(value.strip() for value in row)), ())
        if len(header) < 3:
            raise ValueError("CSV должен начинаться с заголовка и иметь не менее трёх столбцов.")
        rows = []
        for row in reader:
            if not any(value.strip() for value in row):
                continue
            if len(rows) >= MAX_ROWS:
                raise ValueError("В одной выписке поддерживается до 20 000 операций.")
            rows.append((reader.line_num, tuple(value.strip() for value in row)))
    except csv.Error as exc:
        raise ValueError(f"Некорректный CSV около строки {reader.line_num}.") from exc
    if not rows:
        raise ValueError("В выписке нет строк с операциями.")
    return Statement(path.name, hashlib.sha256(raw).hexdigest(), tuple(header), tuple(rows))


@dataclass(frozen=True)
class PreviewRow:
    line: int
    state: str
    message: str
    entry: Entry | None = None


@dataclass(frozen=True)
class Preview:
    account: int
    digest: str
    rows: tuple[PreviewRow, ...]
    balance: BankBalance | None
    balance_warning: str = ""

    def apply(self, ledger: Ledger, confirmed: set[int]) -> Ledger:
        if ImportedFile(self.account, self.digest) in ledger.files:
            return ledger
        entries = tuple(
            row.entry for row in self.rows
            if row.entry is not None and (
                row.state == "new" or (row.state == "review" and row.line in confirmed)
            )
        )
        if not entries and self.balance is None:
            return ledger
        file = ImportedFile(self.account, self.digest)
        balances = ledger.balances if self.balance is None else (*ledger.balances, self.balance)
        return replace(ledger, entries=(*ledger.entries, *entries), files=(*ledger.files, file), balances=balances)


def preview_import(ledger: Ledger, account: int, statement: Statement, mapping: dict[str, int]) -> Preview:
    opening = ledger.opening(account)
    if opening is None:
        raise ValueError("Сначала задайте начальный остаток счёта.")
    if not all(field in mapping for field in NEEDED) or len(set(mapping.values())) != len(mapping):
        raise ValueError("Назначьте разные столбцы для даты, суммы и описания.")
    if ImportedFile(account, statement.digest) in ledger.files:
        rows = tuple(PreviewRow(line, "duplicate", "Файл уже импортирован") for line, _ in statement.rows)
        return Preview(account, statement.digest, rows, None)

    known_ids = {entry.external_id: entry for entry in ledger.entries if entry.account == account and entry.external_id}
    id_column = mapping.get("identifier")
    reserved_ids = {
        values[id_column] for _, values in statement.rows
        if id_column is not None and id_column < len(values) and values[id_column]
    }
    prior = [entry for entry in ledger.entries if entry.account == account and entry.external_id not in reserved_ids]
    known_matches = Counter(entry.fingerprint for entry in prior)
    unlabelled_matches = Counter(entry.fingerprint for entry in prior if not entry.external_id)
    seen_refs = set()
    if "account" in mapping:
        column = mapping["account"]
        seen_refs = {_normalized(values[column]) for _, values in statement.rows if column < len(values) and values[column]}
    if len(seen_refs) > 1:
        raise ValueError("В файле несколько реквизитов счёта. Разделите его на отдельные выписки.")
    if seen_refs and opening.reference and _normalized(opening.reference) not in seen_refs:
        raise ValueError("Реквизит в выписке отличается от реквизита выбранного счёта.")
    rows, reported = [], []
    for line, values in statement.rows:
        try:
            cells = {field: values[column] for field, column in mapping.items()}
            day = _statement_day(cells["date"])
            if day <= opening.day:
                raise ValueError("Дата операции должна быть позже даты начального остатка.")
            amount = parse_amount(cells["amount"])
            if not amount or not cells["description"]:
                raise ValueError("Заполните ненулевую сумму и описание.")
            if cells.get("currency") and cells["currency"].upper() != opening.currency:
                raise ValueError("Валюта операции отличается от валюты счёта.")
            external_id = cells.get("identifier", "")
            entry = Entry(account, day, amount, cells["description"], external_id, "csv")
            balance = parse_amount(cells.get("balance", ""))
        except (ValueError, IndexError) as exc:
            reason = str(exc) if isinstance(exc, ValueError) else "Не хватает столбцов."
            rows.append(PreviewRow(line, "error", reason))
            continue
        if external_id and external_id in known_ids:
            if known_ids[external_id].fingerprint == entry.fingerprint:
                rows.append(PreviewRow(line, "duplicate", "ID операции уже есть"))
            else:
                rows.append(PreviewRow(line, "error", "Этот ID уже существует с другими данными"))
                continue
        else:
            if external_id:
                known_ids[external_id] = entry
            matches = unlabelled_matches if external_id else known_matches
            if matches[entry.fingerprint]:
                known_matches[entry.fingerprint] -= 1
                if unlabelled_matches[entry.fingerprint]:
                    unlabelled_matches[entry.fingerprint] -= 1
                rows.append(PreviewRow(line, "review", "Совпадает дата, сумма и описание", entry))
            else:
                rows.append(PreviewRow(line, "new", "", entry))
        if balance is not None:
            reported.append((day, balance))
    bank, warning = None, ""
    if reported:
        last_day = max(day for day, _ in reported)
        amounts = {amount for day, amount in reported if day == last_day}
        if len(amounts) == 1:
            bank = BankBalance(account, last_day, next(iter(amounts)))
        else:
            warning = "В последний день указано несколько разных остатков. Сверьте остаток вручную."
    return Preview(account, statement.digest, tuple(rows), bank, warning)
