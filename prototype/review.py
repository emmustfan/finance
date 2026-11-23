"""Review of posted transactions. Confirmations never post a second transaction."""

from calendar import monthrange
from collections import defaultdict
from dataclasses import asdict, dataclass, replace
from datetime import date

from assets import Asset
from ledger import Entry, Ledger, _day, _normalized

TRANSFER_WORDS = ("перевод", "transfer", "пополнение вклада", "между счетами", "сбп")
RULE_FIELDS = {"description": "Описание", "recipient": "Получатель", "bank_category": "Категория банка"}
TAX_PAYMENT = "Налоги и взносы ИП"


@dataclass(frozen=True)
class Transfer:
    debit: int
    credit: int
    fee: int


@dataclass(frozen=True)
class Assignment:
    operation: int
    category: str
    taxable: bool = False


@dataclass(frozen=True)
class Rule:
    field: str
    pattern: str
    direction: int
    category: str


@dataclass(frozen=True)
class Recurring:
    name: str
    account: int
    recipient: str
    category: str
    expected: int
    month_day: int
    number: int = 0


@dataclass(frozen=True)
class Suggestion:
    category: str
    reason: str
    difference: int | None = None


def is_business(asset: Asset, scope: str) -> bool:
    return asset.category == "account" and (
        scope == "business" or _normalized(asset.kind) == "счёт ип" or _normalized(asset.kind) == "счет ип"
    )


def operation(ledger: Ledger, number: int) -> Entry:
    if type(number) is not int or not 1 <= number <= len(ledger.entries):
        raise ValueError("Операция не найдена.")
    return ledger.entries[number - 1]


def period(start: str, end: str) -> None:
    if _day(start) > _day(end):
        raise ValueError("Начало периода должно предшествовать окончанию.")


@dataclass(frozen=True)
class Review:
    transfers: tuple[Transfer, ...] = ()
    waiting: tuple[int, ...] = ()
    assignments: tuple[Assignment, ...] = ()
    rules: tuple[Rule, ...] = ()
    recurring: tuple[Recurring, ...] = ()

    def to_payload(self) -> dict:
        return asdict(self)

    @classmethod
    def from_payload(cls, payload: dict) -> "Review":
        return cls(
            transfers=tuple(Transfer(**item) for item in payload["transfers"]),
            waiting=tuple(payload["waiting"]),
            assignments=tuple(Assignment(**item) for item in payload["assignments"]),
            rules=tuple(Rule(**item) for item in payload["rules"]),
            recurring=tuple(Recurring(**item) for item in payload["recurring"]),
        )

    @property
    def linked(self) -> set[int]:
        return {number for pair in self.transfers for number in (pair.debit, pair.credit)}

    def assignments_by_operation(self) -> dict[int, Assignment]:
        return {item.operation: item for item in self.assignments}

    def validate(self, ledger: Ledger, assets: tuple[Asset, ...], scope: str) -> None:
        opening_by_account = {opening.account: opening for opening in ledger.openings}
        asset_by_account = {asset.asset_number: asset for asset in assets}
        if len(self.linked) != len(self.transfers) * 2:
            raise ValueError("Операция участвует только в одном переводе.")
        for pair in self.transfers:
            if type(pair.fee) is not int or self.transfer_fee(ledger, pair.debit, pair.credit) != pair.fee:
                raise ValueError("Некорректная комиссия перевода.")
        seen = self.linked
        for item in self.assignments:
            entry = operation(ledger, item.operation)
            if item.operation in seen or not isinstance(item.category, str) or not item.category.strip():
                raise ValueError("У операции может быть только одна подтверждённая статья.")
            if type(item.taxable) is not bool or (item.taxable and (
                entry.amount <= 0 or opening_by_account[entry.account].currency != "RUB"
                or not is_business(asset_by_account[entry.account], scope)
            )):
                raise ValueError("Налоговый резерв рассчитывается с рублёвых поступлений ИП.")
            seen.add(item.operation)
        if len(set(self.waiting)) != len(self.waiting):
            raise ValueError("Операция уже ожидает пару.")
        for number in self.waiting:
            operation(ledger, number)
            if number in self.linked:
                raise ValueError("Перевод уже связан.")
        for rule in self.rules:
            if rule.field not in RULE_FIELDS or rule.direction not in (-1, 1) or not (
                isinstance(rule.pattern, str) and isinstance(rule.category, str)
                and rule.pattern.strip() and rule.category.strip()
            ):
                raise ValueError("Укажите условие правила и статью.")
        schedule_ids = set()
        for item in self.recurring:
            if (
                type(item.number) is not int or item.number < 1 or item.number in schedule_ids
                or item.account not in opening_by_account or type(item.expected) is not int or item.expected <= 0
                or type(item.month_day) is not int or not 1 <= item.month_day <= 31
                or not all(isinstance(text, str) and text.strip() for text in
                           (item.name, item.recipient, item.category))
            ):
                raise ValueError("Заполните получателя, статью, счёт, сумму и день месяца от 1 до 31.")
            schedule_ids.add(item.number)

    @staticmethod
    def transfer_fee(ledger: Ledger, debit: int, credit: int) -> int:
        outgoing, incoming = operation(ledger, debit), operation(ledger, credit)
        if (
            outgoing.amount >= 0 or incoming.amount <= 0 or outgoing.account == incoming.account
            or ledger.opening(outgoing.account).currency != ledger.opening(incoming.account).currency
            or abs((date.fromisoformat(outgoing.day) - date.fromisoformat(incoming.day)).days) > 7
            or -outgoing.amount < incoming.amount
        ):
            raise ValueError("Для перевода выберите списание и зачисление своей валюты на разных счетах, в пределах 7 дней.")
        return -outgoing.amount - incoming.amount

    def transfer_candidates(self, ledger: Ledger, number: int) -> tuple[tuple[int, int, str], ...]:
        entry = operation(ledger, number)
        linked = self.linked
        if number in linked:
            return ()
        openings = {opening.account: opening for opening in ledger.openings}
        this_open = openings[entry.account]
        current_day = date.fromisoformat(entry.day)
        candidates = []
        for other_number, other in enumerate(ledger.entries, 1):
            if (
                other_number in linked or other.account == entry.account or (other.amount > 0) == (entry.amount > 0)
                or openings[other.account].currency != this_open.currency
            ):
                continue
            days = abs((current_day - date.fromisoformat(other.day)).days)
            if days > 3:
                continue
            outgoing, incoming = (entry, other) if entry.amount < 0 else (other, entry)
            fee = -outgoing.amount - incoming.amount
            refer = (
                bool(entry.counterparty and openings[other.account].reference)
                and _normalized(entry.counterparty) == _normalized(openings[other.account].reference)
            ) or (
                bool(other.counterparty and this_open.reference)
                and _normalized(other.counterparty) == _normalized(this_open.reference)
            )
            text = _normalized(f"{entry.description} {other.description}")
            evidence = refer or any(word in text for word in TRANSFER_WORDS)
            if fee < 0 or (fee and (not evidence or fee > max(100, incoming.amount // 20))):
                continue
            reason = f"{'Реквизит совпал · ' if refer else ''}Одна валюта · разница дат {days} дн."
            reason += " · равные суммы" if not fee else " · разница сумм как комиссия"
            candidates.append((not refer, not evidence, days, fee, other_number, reason))
        candidates.sort()
        return tuple((number, fee, reason) for _, _, _, fee, number, reason in candidates)

    def wait_for_transfer(self, ledger: Ledger, number: int) -> "Review":
        operation(ledger, number)
        if number in self.linked or number in self.waiting:
            return self
        assignments = tuple(item for item in self.assignments if item.operation != number)
        return replace(self, waiting=(*self.waiting, number), assignments=assignments)

    def link_transfer(self, ledger: Ledger, first: int, second: int) -> "Review":
        outgoing, incoming = (first, second) if operation(ledger, first).amount < 0 else (second, first)
        fee = self.transfer_fee(ledger, outgoing, incoming)
        pair = Transfer(outgoing, incoming, fee)
        if pair in self.transfers:
            return self
        touched = {outgoing, incoming}
        if touched & self.linked:
            raise ValueError("Одна из операций уже входит в другой перевод.")
        return replace(
            self, transfers=(*self.transfers, pair), waiting=tuple(num for num in self.waiting if num not in touched),
            assignments=tuple(item for item in self.assignments if item.operation not in touched),
        )

    def unlink(self, number: int) -> "Review":
        return replace(
            self, transfers=tuple(pair for pair in self.transfers if number not in (pair.debit, pair.credit)),
            waiting=tuple(item for item in self.waiting if item != number),
        )

    def assign(self, items: tuple[Assignment, ...]) -> "Review":
        if any(item.operation in self.linked for item in items):
            raise ValueError("Связанные переводы не относятся к доходам и расходам.")
        assignments = self.assignments_by_operation()
        for item in items:
            assignments[item.operation] = item
        confirmed = set(assignments)
        return replace(self, assignments=tuple(assignments.values()),
                       waiting=tuple(number for number in self.waiting if number not in confirmed))

    def learn(self, rule: Rule) -> "Review":
        rules = tuple(item for item in self.rules if (item.field, _normalized(item.pattern), item.direction)
                      != (rule.field, _normalized(rule.pattern), rule.direction))
        return replace(self, rules=(*rules, rule))

    def save_recurring(self, item: Recurring) -> "Review":
        if not item.number:
            item = replace(item, number=max((old.number for old in self.recurring), default=0) + 1)
        return replace(self, recurring=(*tuple(old for old in self.recurring if old.number != item.number), item))

    def suggestion(self, entry: Entry) -> Suggestion | None:
        direction = 1 if entry.amount > 0 else -1
        for item in self.recurring:
            if direction == -1 and entry.account == item.account and self.matches_schedule(entry, item):
                return Suggestion(item.category, f"Регулярный платёж: {item.name}", -entry.amount - item.expected)
        for rule in reversed(self.rules):
            if direction == rule.direction and _normalized(rule.pattern) in _normalized(getattr(entry, rule.field)):
                return Suggestion(rule.category, f"{RULE_FIELDS[rule.field]}: {rule.pattern}")
        if entry.bank_category:
            return Suggestion(entry.bank_category, "Категория из выписки")
        if direction == -1:
            description = _normalized(f"{entry.description} {entry.recipient}")
            builtins = (
                (("аренда", "аренду"), "Жильё → Аренда"),
                (("комиссия", "commission"), "Комиссии банка"),
                (("продукты", "супермаркет"), "Продукты"),
                (("кофе", "coffee", "кафе"), "Кафе"),
                (("налог", "фнс", "страховые взносы"), TAX_PAYMENT),
            )
            for patterns, category in builtins:
                if word := next((word for word in patterns if word in description), ""):
                    return Suggestion(category, f"Описание или получатель: «{word}»")
        return None

    @staticmethod
    def matches_schedule(entry: Entry, item: Recurring) -> bool:
        return entry.amount < 0 and entry.account == item.account and _normalized(item.recipient) in _normalized(
            f"{entry.description} {entry.recipient}"
        )

    def calendar(self, ledger: Ledger, month: str) -> tuple[tuple[str, Recurring, Entry | None], ...]:
        first = date.fromisoformat(month + "-01")
        last = monthrange(first.year, first.month)[1]
        used = self.linked
        rows = []
        plans = sorted(
            ((first.replace(day=min(item.month_day, last)).isoformat(), item) for item in self.recurring),
            key=lambda plan: (plan[0], plan[1].number),
        )
        for due, item in plans:
            payments = (
                (number, entry) for number, entry in enumerate(ledger.entries, 1)
                if number not in used and entry.day[:7] == month and self.matches_schedule(entry, item)
            )
            match = min(payments, key=lambda payment: (
                abs((date.fromisoformat(payment[1].day) - date.fromisoformat(due)).days),
                abs(-payment[1].amount - item.expected),
            ), default=None)
            actual = None
            if match:
                number, actual = match
                used.add(number)
            rows.append((due, item, actual))
        return tuple(rows)

    def report(self, ledger: Ledger, start: str, end: str) -> dict[tuple[str, str, str], int]:
        period(start, end)
        totals = defaultdict(int)
        currencies = {item.account: item.currency for item in ledger.openings}
        assigned = self.assignments_by_operation()
        linked = self.linked
        fees = {pair.debit: pair.fee for pair in self.transfers if pair.fee}
        for number, entry in enumerate(ledger.entries, 1):
            if not start <= entry.day <= end:
                continue
            currency = currencies[entry.account]
            if number in linked:
                if number in fees:
                    totals[currency, "Расходы", "Комиссии банка"] += fees[number]
                continue
            category = assigned[number].category if number in assigned else "Без подтверждённой статьи"
            totals[currency, "Доходы" if entry.amount > 0 else "Расходы", category] += abs(entry.amount)
        return dict(totals)

    def tax_reserve(self, ledger: Ledger, start: str, end: str) -> tuple[int, int]:
        period(start, end)
        revenue = sum(
            entry.amount for item in self.assignments if item.taxable
            and start <= (entry := operation(ledger, item.operation)).day <= end
        )
        return revenue, (revenue * 6 + 50) // 100
