"""Review of posted transactions. Confirmations never post a second transaction."""

from calendar import monthrange
from collections import defaultdict
from dataclasses import asdict, dataclass, replace
from datetime import date

from assets import Asset
from debts import Debts, interest_category
from ledger import Entry, Ledger, _day, _normalized, reference_matches

TRANSFER_WORDS = ("перевод", "transfer", "пополнение вклада", "между счетами", "сбп")
RULE_FIELDS = {"description": "Описание", "recipient": "Получатель", "bank_category": "Категория банка"}
TAX_PAYMENT = "Налоги и взносы ИП"


@dataclass(frozen=True)
class Transfer:
    debit: int
    credit: int
    fee: int


@dataclass(frozen=True)
class PendingTransfer:
    operation: int
    account: int


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


@dataclass(frozen=True)
class OperationTags:
    operation: int
    names: tuple[str, ...]


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
    tags: tuple[OperationTags, ...] = ()
    debts: Debts = Debts()
    pending_transfers: tuple[PendingTransfer, ...] = ()

    def to_payload(self) -> dict:
        payload = asdict(self)
        if not self.tags:
            del payload["tags"]
        if self.debts == Debts():
            del payload["debts"]
        if not self.pending_transfers:
            del payload["pending_transfers"]
        return payload

    @classmethod
    def from_payload(cls, payload: dict) -> "Review":
        return cls(
            transfers=tuple(Transfer(**item) for item in payload["transfers"]),
            waiting=tuple(payload["waiting"]),
            assignments=tuple(Assignment(**item) for item in payload["assignments"]),
            rules=tuple(Rule(**item) for item in payload["rules"]),
            recurring=tuple(Recurring(**item) for item in payload["recurring"]),
            tags=tuple(OperationTags(item["operation"], tuple(item["names"])) for item in payload.get("tags", ())),
            debts=Debts.from_payload(payload["debts"]) if "debts" in payload else Debts(),
            pending_transfers=tuple(PendingTransfer(**item) for item in payload.get("pending_transfers", ())),
        )

    @property
    def linked(self) -> set[int]:
        return {number for pair in self.transfers for number in (pair.debit, pair.credit)}

    def assignments_by_operation(self) -> dict[int, Assignment]:
        return {item.operation: item for item in self.assignments}

    @property
    def fixed_operations(self) -> set[int]:
        return self.linked | self.debts.movements().keys()

    def transfer_targets(self) -> dict[int, int]:
        return {item.operation: item.account for item in self.pending_transfers}

    @property
    def reserved_operations(self) -> set[int]:
        return self.fixed_operations | self.transfer_targets().keys()

    def tags_by_operation(self) -> dict[int, tuple[str, ...]]:
        return {item.operation: item.names for item in self.tags}

    def available_tags(self) -> tuple[str, ...]:
        names = {}
        for item in self.tags:
            for name in item.names:
                names.setdefault(_normalized(name), name)
        return tuple(names[key] for key in sorted(names))

    def tag_operations(self, values: dict[int, tuple[str, ...]], *, extend=False) -> "Review":
        available = {_normalized(name): name for name in self.available_tags()}
        updates = self.tags_by_operation()
        for number, names in values.items():
            tags = dict.fromkeys(updates.get(number, ()) if extend else ())
            for value in names:
                name = " ".join(value.split())
                if not name:
                    continue
                if len(name) > 80 or "," in name:
                    raise ValueError("Введите тег до 80 символов. Запятая разделяет теги.")
                tags[available.setdefault(_normalized(name), name)] = None
            if tags:
                updates[number] = tuple(tags)
            else:
                updates.pop(number, None)
        return replace(self, tags=tuple(OperationTags(number, names) for number, names in sorted(updates.items())))

    def validate(self, ledger: Ledger, assets: tuple[Asset, ...], scope: str) -> None:
        opening_by_account = {opening.account: opening for opening in ledger.openings}
        asset_by_account = {asset.asset_number: asset for asset in assets}
        if len(self.linked) != len(self.transfers) * 2:
            raise ValueError("Операция участвует только в одном переводе.")
        for pair in self.transfers:
            if type(pair.fee) is not int or self.transfer_fee(ledger, pair.debit, pair.credit) != pair.fee:
                raise ValueError("Некорректная комиссия перевода.")
        self.debts.validate(ledger)
        if self.linked & self.debts.movements().keys():
            raise ValueError("Операция по долгу уже связана с внутренним переводом.")
        fixed = self.fixed_operations
        targets = self.transfer_targets()
        seen = fixed | targets.keys()
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
            if number in fixed:
                raise ValueError("Движение уже связано с переводом или долгом.")
        if len(targets) != len(self.pending_transfers):
            raise ValueError("У перевода может быть только один второй счёт.")
        waiting = set(self.waiting)
        for item in self.pending_transfers:
            entry = operation(ledger, item.operation)
            asset = asset_by_account.get(item.account)
            if (
                type(item.account) is not int or item.operation not in waiting
                or asset is None or asset.category != "account" or asset.asset_number == entry.account
                or asset.currency != opening_by_account[entry.account].currency
            ):
                raise ValueError("Выберите другой счёт в той же валюте.")
        tagged = set()
        for item in self.tags:
            operation(ledger, item.operation)
            if item.operation in tagged or not item.names:
                raise ValueError("Повтор записи тегов операции.")
            keys = set()
            for name in item.names:
                if not isinstance(name, str) or not name.strip() or len(name) > 80 or "," in name:
                    raise ValueError("Некорректный тег операции.")
                key = _normalized(name)
                if key in keys:
                    raise ValueError("Тег уже добавлен к этой операции.")
                keys.add(key)
            tagged.add(item.operation)
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

    def transfer_candidates(
        self, ledger: Ledger, number: int, *, account: int | None = None,
        include_other=False, excluded: frozenset[int] = frozenset(),
    ) -> tuple[tuple[int, int, str], ...]:
        entry = operation(ledger, number)
        linked = self.fixed_operations | excluded
        if number in linked:
            return ()
        openings = {opening.account: opening for opening in ledger.openings}
        this_open = openings[entry.account]
        current_day = date.fromisoformat(entry.day)
        targets = self.transfer_targets()
        if account is None:
            account = targets.get(number)
        candidates = []
        for other_number, other in enumerate(ledger.entries, 1):
            if (
                other_number in linked or other.account == entry.account or (other.amount > 0) == (entry.amount > 0)
                or openings[other.account].currency != this_open.currency
                or (account is not None and other.account != account)
                or (other_number in targets and targets[other_number] != entry.account)
            ):
                continue
            days = abs((current_day - date.fromisoformat(other.day)).days)
            if days > (7 if include_other else 3):
                continue
            outgoing, incoming = (entry, other) if entry.amount < 0 else (other, entry)
            fee = -outgoing.amount - incoming.amount
            refer = reference_matches(entry.counterparty, openings[other.account].reference) or reference_matches(
                other.counterparty, this_open.reference
            )
            evidence = refer or any(word in _normalized(f"{entry.description} {other.description}")
                                    for word in TRANSFER_WORDS)
            preferred = days <= 3 and (
                fee == 0 or ((evidence or account is not None) and fee <= max(100, incoming.amount // 20))
            )
            if fee < 0 or (not include_other and not preferred):
                continue
            reason = (
                ("Реквизит совпал · " if refer else "")
                + ("Счёт уже выбран · " if other_number in targets else "")
                + ("в один день" if not days else f"разница {days} дн.")
            )
            if not fee:
                reason += " · суммы совпали"
            candidates.append((not preferred, not refer, other_number not in targets, not evidence,
                               days, fee, other_number, reason))
        candidates.sort()
        return tuple((number, fee, reason) for _, _, _, _, _, fee, number, reason in candidates)

    def wait_for_transfer(self, ledger: Ledger, number: int, account: int | None = None) -> "Review":
        entry = operation(ledger, number)
        if number in self.fixed_operations:
            return self
        pending = self.pending_transfers
        if account is not None:
            if type(account) is not int or account == entry.account:
                raise ValueError("Выберите другой счёт в той же валюте.")
            pending = (*(item for item in pending if item.operation != number), PendingTransfer(number, account))
        waiting = self.waiting if number in self.waiting else (*self.waiting, number)
        return replace(self, waiting=waiting, pending_transfers=pending,
                       assignments=tuple(item for item in self.assignments if item.operation != number))

    def link_transfer(self, ledger: Ledger, first: int, second: int) -> "Review":
        outgoing, incoming = (first, second) if operation(ledger, first).amount < 0 else (second, first)
        fee = self.transfer_fee(ledger, outgoing, incoming)
        pair = Transfer(outgoing, incoming, fee)
        if pair in self.transfers:
            return self
        touched = {outgoing, incoming}
        if touched & self.fixed_operations:
            raise ValueError("Одна из операций уже связана с переводом или долгом.")
        targets = self.transfer_targets()
        for number, other in ((outgoing, incoming), (incoming, outgoing)):
            if number in targets and targets[number] != operation(ledger, other).account:
                raise ValueError("Для одной из операций уже выбран другой счёт перевода.")
        return replace(
            self, transfers=(*self.transfers, pair), waiting=tuple(num for num in self.waiting if num not in touched),
            assignments=tuple(item for item in self.assignments if item.operation not in touched),
            pending_transfers=tuple(item for item in self.pending_transfers if item.operation not in touched),
        )

    def unlink(self, number: int) -> "Review":
        return replace(
            self, transfers=tuple(pair for pair in self.transfers if number not in (pair.debit, pair.credit)),
            waiting=tuple(item for item in self.waiting if item != number),
            pending_transfers=tuple(item for item in self.pending_transfers if item.operation != number),
        )

    def assign(self, items: tuple[Assignment, ...]) -> "Review":
        fixed = self.fixed_operations
        if any(item.operation in fixed for item in items):
            raise ValueError("Движения, связанные с переводом или долгом, имеют свою статью.")
        assignments = self.assignments_by_operation()
        for item in items:
            assignments[item.operation] = item
        confirmed = set(assignments)
        return replace(self, assignments=tuple(assignments.values()),
                       waiting=tuple(number for number in self.waiting if number not in confirmed),
                       pending_transfers=tuple(item for item in self.pending_transfers if item.operation not in confirmed))

    def classify(self, numbers: tuple[int, ...], category: str) -> "Review":
        if not category:
            targets = set(numbers)
            return replace(self, assignments=tuple(item for item in self.assignments if item.operation not in targets))
        assigned = self.assignments_by_operation()
        return self.assign(tuple(Assignment(number, category, number in assigned and assigned[number].taxable)
                                 for number in numbers))

    def with_debts(self, debts: Debts, *, attached: int | None = None) -> "Review":
        if attached in self.linked:
            raise ValueError("Сначала снимите связь с внутренним переводом.")
        return replace(self, debts=debts,
                       assignments=tuple(item for item in self.assignments if item.operation != attached),
                       waiting=tuple(item for item in self.waiting if item != attached),
                       pending_transfers=tuple(item for item in self.pending_transfers if item.operation != attached))

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
                (("кофе", "coffee", "кафе", "ресторан"), "Кафе"),
                (("такси", "метро"), "Транспорт"),
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
        used = self.reserved_operations
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
        pending = self.transfer_targets()
        debt_moves = self.debts.movements()
        fees = {pair.debit: pair.fee for pair in self.transfers if pair.fee}
        for number, entry in enumerate(ledger.entries, 1):
            if not start <= entry.day <= end:
                continue
            currency = currencies[entry.account]
            if number in linked:
                if number in fees:
                    totals[currency, "Расходы", "Комиссии банка"] += fees[number]
                continue
            if number in pending:
                continue
            if number in debt_moves:
                interest = abs(entry.amount) - debt_moves[number][0]
                if interest:
                    totals[currency, "Доходы" if entry.amount > 0 else "Расходы",
                           interest_category(entry.amount)] += interest
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
