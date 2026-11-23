"""Accounts, investments and property recorded in the encrypted archive."""

import re
from dataclasses import dataclass
from datetime import date

CATEGORIES = {
    "account": "Деньги на счетах",
    "investment": "Инвестиции",
    "property": "Имущество",
}
KINDS = {
    "account": ("Текущий счёт", "Накопительный счёт", "Наличные", "Вклад", "Счёт ИП"),
    "investment": ("Акции", "Облигации", "Фонд", "Брокерский счёт", "ИИС", "Доля в бизнесе"),
    "property": ("Квартира", "Дом", "Земельный участок", "Автомобиль"),
}
STATUSES = {"active": "Действует", "inactive": "Закрыт или выбыл"}


def parse_amount(text: str) -> int | None:
    text = text.strip().replace("\u00a0", " ").replace("\u202f", " ").replace("−", "-")
    if not text:
        return None
    match = re.fullmatch(r"([+-]?)([0-9]+|[0-9]{1,3}(?: [0-9]{3})+)(?:[.,]([0-9]{1,2}))?", text)
    if match is None:
        raise ValueError("Введите сумму с точностью до двух знаков, например 125 500,50.")
    digits = match[2].replace(" ", "")
    if len(digits) > 15:
        raise ValueError("В целой части суммы допустимо до 15 цифр.")
    cents = int(digits) * 100 + int((match[3] or "").ljust(2, "0"))
    return -cents if match[1] == "-" else cents


def amount_text(amount: int) -> str:
    units, cents = divmod(abs(amount), 100)
    sign = "−" if amount < 0 else ""
    return sign + f"{units:,}".replace(",", "\u202f") + f",{cents:02d}"


@dataclass(frozen=True)
class Asset:
    title: str
    category: str
    owner: str
    kind: str = ""
    organization: str = ""
    status: str = "active"
    amount: int | None = None
    currency: str = "RUB"
    as_of: str = ""
    access_pointer: str = ""
    document_numbers: tuple[int, ...] = ()
    asset_number: int = 0
    ticker: str = ""
    quantity: str = ""
    purchase_cost: int | None = None

    def validate(self, documents: set[int]) -> None:
        if not all(isinstance(value, str) for value in (
            self.title, self.category, self.owner, self.kind, self.organization,
            self.status, self.currency, self.as_of, self.access_pointer, self.ticker, self.quantity,
        )):
            raise ValueError("В карточке актива есть некорректные поля.")
        if not self.title.strip() or not self.owner.strip():
            raise ValueError("Укажите название и владельца.")
        if self.category not in CATEGORIES or self.status not in STATUSES:
            raise ValueError("Выберите категорию и статус.")
        if not re.fullmatch(r"[A-Z]{3}", self.currency):
            raise ValueError("Укажите три буквы валюты, например RUB.")
        if self.amount is not None:
            if type(self.amount) is not int or abs(self.amount) >= 10**17:
                raise ValueError("Некорректная сумма.")
            if self.category != "account" and self.amount < 0:
                raise ValueError("Оценка актива должна быть неотрицательной.")
            if not self.as_of:
                raise ValueError("Укажите дату, на которую известна сумма.")
        if self.as_of:
            try:
                if date.fromisoformat(self.as_of).isoformat() != self.as_of:
                    raise ValueError()
            except ValueError as exc:
                raise ValueError("Укажите дату в формате ГГГГ-ММ-ДД.") from exc
        if self.ticker or self.quantity or self.purchase_cost is not None:
            if self.category != "investment":
                raise ValueError("Тикер, количество и затраты относятся к инвестиционной позиции.")
            if self.quantity and (
                not re.fullmatch(r"[0-9]{1,12}(?:[.,][0-9]{1,8})?", self.quantity)
                or not any(character in "123456789" for character in self.quantity)
            ):
                raise ValueError("Количество должно быть положительным числом, например 10 или 2,5.")
            if self.purchase_cost is not None and (
                type(self.purchase_cost) is not int or not 0 <= self.purchase_cost < 10**17
            ):
                raise ValueError("Укажите неотрицательную стоимость покупки.")
        if (
            not all(type(number) is int and number in documents for number in self.document_numbers)
            or len(set(self.document_numbers)) != len(self.document_numbers)
        ):
            raise ValueError("Выберите документы из этого архива.")

    @property
    def valuation(self) -> str:
        return f"{amount_text(self.amount)} {self.currency}" if self.amount is not None else "Сумма не указана"

    def matches(self, query: str) -> bool:
        searchable = " ".join((
            self.title, self.owner, self.kind, self.organization, self.ticker, CATEGORIES[self.category],
        )).casefold()
        return all(part in searchable for part in query.casefold().split())


def validate_assets(assets: tuple[Asset, ...], documents: set[int]) -> None:
    numbers = set()
    for asset in assets:
        number = asset.asset_number
        if type(number) is not int or number < 1 or number in numbers:
            raise ValueError("Некорректный номер актива.")
        numbers.add(number)
        asset.validate(documents)


def totals_by_category(assets: tuple[Asset, ...]) -> dict[str, dict[str, int]]:
    totals: dict[str, dict[str, int]] = {category: {} for category in CATEGORIES}
    for asset in assets:
        if asset.amount is not None and asset.status == "active":
            by_currency = totals[asset.category]
            by_currency[asset.currency] = by_currency.get(asset.currency, 0) + asset.amount
    return totals
