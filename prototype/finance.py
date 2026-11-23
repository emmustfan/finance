"""Categories, cash-flow presentation, and local finance exports."""

import csv
import json
from collections import defaultdict
from dataclasses import asdict, dataclass, replace
from io import StringIO

from debts import interest_category
from ledger import Ledger, Preview, _normalized
from review import Assignment, Review, period

DEFAULT_INCOME = ("Зарплата", "Подработка", "Подарки", "Проценты по вкладам", "Дивиденды", "Прочие доходы")
DEFAULT_EXPENSE = (
    "Продукты", "Кафе", "Транспорт", "Жильё → Аренда", "Коммунальные услуги", "Здоровье",
    "Образование", "Подписки", "Одежда", "Путешествия", "Экскурсии и развлечения",
    "Комиссии банка", "Налоги и взносы ИП",
    "Прочие расходы",
)
FLOW_LABELS = {"income": "Доход", "expense": "Расход", "transfer": "Перевод", "debt": "Долг"}
UNCATEGORIZED = "Без статьи"


@dataclass(frozen=True)
class Category:
    direction: int
    name: str
    archived: bool = False
    aliases: tuple[str, ...] = ()


DEFAULT_DIRECTORY = tuple(Category(1, value) for value in DEFAULT_INCOME) + tuple(
    Category(-1, value) for value in DEFAULT_EXPENSE
)


@dataclass(frozen=True)
class Directory:
    categories: tuple[Category, ...] = DEFAULT_DIRECTORY

    @classmethod
    def from_payload(cls, payload):
        return cls(tuple(Category(item["direction"], item["name"], item["archived"], tuple(item["aliases"]))
                         for item in payload))

    def to_payload(self):
        return [asdict(category) for category in self.categories]

    def validate(self):
        seen = set()
        for category in self.categories:
            values = (category.name, *category.aliases)
            if type(category.direction) is not int or category.direction not in (-1, 1) or type(category.archived) is not bool:
                raise ValueError("Некорректный вид статьи.")
            for value in values:
                if not isinstance(value, str) or not _normalized(value) or len(value) > 100:
                    raise ValueError("Название статьи должно содержать от 1 до 100 символов.")
                key = category.direction, _normalized(value)
                if key in seen:
                    raise ValueError("Статья с таким названием уже есть.")
                seen.add(key)

    def lookup(self, direction: int, name: str) -> Category | None:
        key = _normalized(name)
        return next((item for item in self.categories if item.direction == direction
                     and any(_normalized(text) == key for text in (item.name, *item.aliases))), None)

    def effective(self, ledger: Ledger, review: Review) -> tuple[Category, ...]:
        names = {}
        for item in self.categories:
            for name in (item.name, *item.aliases):
                names[item.direction, _normalized(name)] = item
        result = list(self.categories)
        discovered = [(item.direction, item.category) for item in review.rules]
        discovered += [(-1, item.category) for item in review.recurring]
        discovered += [
            (1 if ledger.entries[item.operation - 1].amount > 0 else -1, item.category)
            for item in review.assignments
        ]
        for direction, name in discovered:
            key = direction, _normalized(name)
            if key not in names:
                item = Category(direction, name)
                names[key] = item
                result.append(item)
        return tuple(sorted(result, key=lambda item: (-item.direction, item.name.casefold())))

    def edit(self, ledger: Ledger, review: Review, direction: int, name: str,
             *, previous: str = "", archived=False) -> tuple["Directory", Review]:
        name = " ".join(name.split())
        existing = Directory(self.effective(ledger, review))
        old = existing.lookup(direction, previous) if previous else None
        duplicate = existing.lookup(direction, name)
        if duplicate is not None and duplicate != old:
            raise ValueError("Статья с таким названием уже есть.")
        aliases = tuple(
            value for value in (old.name, *old.aliases) if _normalized(value) != _normalized(name)
        ) if old else ()
        changed = Category(direction, name, archived, aliases)
        directory = Directory((*tuple(item for item in self.categories if item != old), changed))
        directory.validate()
        if old is None:
            return directory, review
        old_names = {_normalized(value) for value in (old.name, *old.aliases)}
        def rename(value, kind):
            return name if direction == kind and _normalized(value) in old_names else value
        assignments = tuple(replace(
            item, category=rename(item.category, 1 if ledger.entries[item.operation - 1].amount > 0 else -1)
        ) for item in review.assignments)
        rules = tuple(replace(item, category=rename(item.category, item.direction)) for item in review.rules)
        recurring = tuple(replace(item, category=rename(item.category, -1)) for item in review.recurring)
        return directory, replace(review, assignments=assignments, rules=rules, recurring=recurring)

    def suggestion(self, review: Review, entry):
        proposal = review.suggestion(entry)
        if proposal is None:
            return None
        name = " ".join(proposal.category.split())
        if not 1 <= len(name) <= 100:
            return None
        category = self.lookup(1 if entry.amount > 0 else -1, name)
        if category is None:
            return replace(proposal, category=name)
        return None if category.archived else replace(proposal, category=category.name)


def apply_statement(
    ledger: Ledger, review: Review, preview: Preview, confirmed: set[int],
    articles: dict[int, str], tags: dict[int, tuple[str, ...]],
) -> tuple[Ledger, Review]:
    posted = preview.apply(ledger, confirmed)
    if len(posted.entries) == len(ledger.entries):
        return posted, review
    numbers = {row.line: number for number, row in
               enumerate(preview.accepted_rows(confirmed), start=len(ledger.entries) + 1)}
    assigned = tuple(Assignment(number, articles[line]) for line, number in numbers.items() if articles.get(line))
    if assigned:
        review = review.assign(assigned)
    tagged = {number: tags[line] for line, number in numbers.items() if tags.get(line)}
    return posted, review.tag_operations(tagged) if tagged else review


@dataclass(frozen=True)
class Activity:
    number: int
    day: str
    account: int
    currency: str
    amount: int
    kind: str
    category: str
    description: str
    recipient: str
    fee: int = 0
    pair: int | None = None
    tags: tuple[str, ...] = ()
    principal: int = 0
    transfer_account: int | None = None

    @property
    def income(self) -> int:
        return max(0, self.amount - self.principal) if self.kind != "transfer" else 0

    @property
    def expense(self) -> int:
        return max(0, -self.amount - self.principal) if self.kind != "transfer" else self.fee

    @property
    def expense_category(self) -> str:
        return "Комиссии банка" if self.fee else self.category


def activity(ledger: Ledger, review: Review, start: str, end: str, currency="", *, tag="") -> tuple[Activity, ...]:
    period(start, end)
    currencies = {item.account: item.currency for item in ledger.openings}
    assigned = review.assignments_by_operation()
    linked = {number: pair for pair in review.transfers for number in (pair.debit, pair.credit)}
    targets = review.transfer_targets()
    debt_moves = review.debts.movements()
    tags = review.tags_by_operation()
    tag_key = _normalized(tag)
    rows = []
    for number, entry in enumerate(ledger.entries, 1):
        code = currencies[entry.account]
        row_tags = tags.get(number, ())
        if (not start <= entry.day <= end or (currency and code != currency)
                or (tag_key and not any(_normalized(name) == tag_key for name in row_tags))):
            continue
        pair = linked.get(number)
        transfer_account = targets.get(number)
        kind = "income" if entry.amount > 0 else "expense"
        category = assigned[number].category if number in assigned else UNCATEGORIZED
        if pair:
            other = pair.credit if number == pair.debit else pair.debit
            transfer_account = ledger.entries[other - 1].account
            kind, category = "transfer", "Внутренний перевод"
        elif transfer_account is not None:
            kind, category = "transfer", "Перевод · ждёт пару"
        principal = 0
        if number in debt_moves:
            kind = "debt"
            principal, movement = debt_moves[number]
            category = interest_category(entry.amount) if abs(entry.amount) > principal else movement
        rows.append(Activity(
            number, entry.day, entry.account, code, entry.amount, kind, category, entry.description,
            entry.recipient, pair.fee if pair and number == pair.debit else 0, pair.debit if pair else None,
            row_tags, principal, transfer_account,
        ))
    return tuple(sorted(rows, key=lambda row: (row.day, row.number), reverse=True))


def flow_totals(rows: tuple[Activity, ...]) -> tuple[int, int]:
    return sum(row.income for row in rows), sum(row.expense for row in rows)


def flow_breakdown(rows: tuple[Activity, ...]) -> dict[tuple[str, str, str], int]:
    result = defaultdict(int)
    for row in rows:
        if row.income:
            result[row.currency, "income", row.category] += row.income
        if row.expense:
            result[row.currency, "expense", row.expense_category] += row.expense
    return dict(result)


def spreadsheet_safe(value: str) -> str:
    if value.lstrip().startswith(("=", "+", "-", "@")) or value.startswith(("\t", "\r", "\n")):
        return "'" + value
    return value


def export_text(rows: tuple[Activity, ...], start: str, end: str, *, kind: str, details=False,
                include_tags=False) -> str:
    if kind not in ("json", "csv"):
        raise ValueError("Выберите формат JSON или CSV.")
    wallets = {number: f"Счёт {i}" for i, number in enumerate(sorted({row.account for row in rows}), 1)}
    pairs = {number: index for index, number in enumerate(sorted({row.pair for row in rows if row.pair}), 1)}
    monthly = defaultdict(lambda: [0, 0])
    operations = []
    for row in reversed(rows):
        month = monthly[row.day[:7], row.currency]
        month[0] += row.income
        month[1] += row.expense
        record = {
            "date": row.day, "wallet": wallets[row.account], "currency": row.currency, "type": row.kind,
            "category": row.category, "amount_minor": row.amount, "income_minor": row.income,
            "expense_minor": row.expense, "transfer_fee_minor": row.fee, "transfer_pair": pairs.get(row.pair),
            "debt_principal_minor": row.principal if row.kind == "debt" else None,
        }
        if include_tags:
            record["tags"] = list(row.tags) if kind == "json" else ", ".join(row.tags)
        if details:
            record.update(description=row.description, recipient=row.recipient)
        operations.append(record)
    if kind == "json":
        payload = {
            "period": {"from": start, "to": end}, "minor_unit_scale": 100,
            "monthly_totals": [
                {"month": key[0], "currency": key[1], "income_minor": values[0],
                 "expense_minor": values[1], "net_minor": values[0] - values[1]}
                for key, values in sorted(monthly.items())
            ],
            "category_totals": [
                {"currency": key[0], "type": key[1], "category": key[2], "amount_minor": amount}
                for key, amount in sorted(flow_breakdown(rows).items())
            ],
            "operations": operations,
        }
        return json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    fields = ("date", "wallet", "currency", "type", "category", "amount_minor", "income_minor",
              "expense_minor", "transfer_fee_minor", "transfer_pair", "debt_principal_minor")
    fields += ("tags",) if include_tags else ()
    fields += ("description", "recipient") if details else ()
    stream = StringIO(newline="")
    writer = csv.DictWriter(stream, fields)
    writer.writeheader()
    for record in operations:
        writer.writerow({key: spreadsheet_safe(value) if isinstance(value, str) else value
                         for key, value in record.items()})
    return stream.getvalue()
