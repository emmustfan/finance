"""Loans and repayments matched to the same movements used by the account ledger."""

import re
from dataclasses import dataclass, replace

from ledger import Ledger, _day

DIRECTIONS = {"borrowed": "Я занял", "lent": "Я одолжил"}
ISSUE_SIGN = {"borrowed": 1, "lent": -1}


@dataclass(frozen=True)
class Debt:
    title: str
    direction: str
    currency: str
    day: str
    amount: int
    due: str = ""
    issue: int | None = None
    number: int = 0


@dataclass(frozen=True)
class Repayment:
    debt: int
    operation: int
    principal: int


@dataclass(frozen=True)
class Debts:
    items: tuple[Debt, ...] = ()
    payments: tuple[Repayment, ...] = ()

    @classmethod
    def from_payload(cls, payload: dict) -> "Debts":
        return cls(tuple(Debt(**item) for item in payload["items"]),
                   tuple(Repayment(**item) for item in payload["payments"]))

    def movements(self) -> dict[int, tuple[int, str]]:
        amounts = {
            item.issue: (item.amount, "Получение займа" if item.direction == "borrowed" else "Выдача займа")
            for item in self.items if item.issue is not None
        }
        directions = {item.number: item.direction for item in self.items}
        amounts.update({
            item.operation: (item.principal, "Погашение займа" if directions[item.debt] == "borrowed"
                             else "Возврат выданного займа") for item in self.payments
        })
        return amounts

    def balances(self) -> dict[int, int]:
        remaining = {item.number: item.amount for item in self.items}
        for item in self.payments:
            remaining[item.debt] -= item.principal
        return remaining

    def add(self, debt: Debt) -> "Debts":
        number = max((item.number for item in self.items), default=0) + 1
        return replace(self, items=(*self.items, replace(debt, number=number)))

    def update_terms(self, number: int, title: str, due: str) -> "Debts":
        return replace(self, items=tuple(
            replace(item, title=title, due=due) if item.number == number else item for item in self.items
        ))

    def validate(self, ledger: Ledger) -> None:
        currencies = {opening.account: opening.currency for opening in ledger.openings}
        loans, used = {}, set()

        def check_movement(number: int, debt: Debt, sign: int):
            if type(number) is not int or not 1 <= number <= len(ledger.entries) or number in used:
                raise ValueError("Одно денежное движение можно привязать только к одному долгу.")
            entry = ledger.entries[number - 1]
            if currencies[entry.account] != debt.currency or entry.amount * sign <= 0:
                raise ValueError("Валюта и направление движения должны совпадать с долгом.")
            used.add(number)
            return entry

        for item in self.items:
            if (
                type(item.number) is not int or item.number < 1 or item.number in loans
                or not isinstance(item.title, str) or not item.title.strip() or len(item.title) > 120
                or not isinstance(item.direction, str) or item.direction not in DIRECTIONS
                or not isinstance(item.currency, str) or not re.fullmatch(r"[A-Z]{3}", item.currency)
                or type(item.amount) is not int or not 0 < item.amount < 10**17
                or not isinstance(item.due, str)
            ):
                raise ValueError("Укажите имя или название долга, валюту и положительную сумму.")
            day = _day(item.day)
            if item.due and _day(item.due) < day:
                raise ValueError("Срок возврата должен быть не раньше даты долга.")
            if item.issue is not None:
                entry = check_movement(item.issue, item, ISSUE_SIGN[item.direction])
                if entry.day != day or abs(entry.amount) != item.amount:
                    raise ValueError("Дата и сумма выдачи должны совпадать с операцией.")
            loans[item.number] = item
        for item in self.payments:
            if type(item.debt) is not int or item.debt not in loans:
                raise ValueError("Долг для погашения не найден.")
            debt = loans[item.debt]
            entry = check_movement(item.operation, debt, -ISSUE_SIGN[debt.direction])
            if entry.day < debt.day:
                raise ValueError("День возврата не может быть раньше даты долга.")
            if type(item.principal) is not int or not 0 <= item.principal <= abs(entry.amount):
                raise ValueError("Основная сумма возврата должна быть от нуля до суммы платежа.")
        if any(value < 0 for value in self.balances().values()):
            raise ValueError("Возвраты превышают основной долг. Проверьте сумму без процентов.")


def interest_category(amount: int) -> str:
    return "Проценты по выданным займам" if amount > 0 else "Проценты по займам"
