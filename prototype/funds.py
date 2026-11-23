"""Fund balances and deterministic allocation of posted income."""

import re
from dataclasses import asdict, dataclass, replace
from datetime import date

from ledger import Ledger, _day
from review import Review, operation

FUND_KINDS = ("Обязательные платежи", "Подушка", "Цель", "Инвестиции", "Налоговый резерв ИП")
MODES = {"fixed": "Фиксированная сумма", "percent": "Процент", "goal": "До цели", "remainder": "Весь остаток"}


@dataclass(frozen=True)
class Fund:
    title: str
    kind: str
    currency: str
    goal: int | None = None
    number: int = 0
    deadline: str = ""


def goal_gap(fund: Fund, saved: int, today: date) -> tuple[int, int | None]:
    if fund.goal is None:
        raise ValueError("Сначала укажите сумму цели.")
    gap = max(0, fund.goal - saved)
    if not gap or not fund.deadline:
        return gap, None
    deadline = date.fromisoformat(fund.deadline)
    if deadline < today:
        return gap, None
    months = (deadline.year - today.year) * 12 + deadline.month - today.month + 1
    return gap, (gap + months - 1) // months


@dataclass(frozen=True)
class Movement:
    fund: int
    amount: int
    command: str
    operation: int | None = None
    day: str = ""

    def effective_day(self, ledger: Ledger) -> str:
        return operation(ledger, self.operation).day if self.operation is not None else self.day


@dataclass(frozen=True)
class Line:
    fund: int
    mode: str
    value: int = 0


@dataclass(frozen=True)
class Template:
    currency: str
    lines: tuple[Line, ...]


@dataclass(frozen=True)
class Funds:
    funds: tuple[Fund, ...] = ()
    movements: tuple[Movement, ...] = ()
    templates: tuple[Template, ...] = ()

    def to_payload(self) -> dict:
        payload = asdict(self)
        for fund in payload["funds"]:
            if not fund["deadline"]:
                del fund["deadline"]
        for move in payload["movements"]:
            if not move["day"]:
                del move["day"]
        return payload

    @classmethod
    def from_payload(cls, payload: dict) -> "Funds":
        return cls(
            funds=tuple(Fund(**item) for item in payload["funds"]),
            movements=tuple(Movement(**item) for item in payload["movements"]),
            templates=tuple(Template(item["currency"], tuple(Line(**line) for line in item["lines"]))
                            for item in payload["templates"]),
        )

    def fund(self, number: int) -> Fund:
        found = next((item for item in self.funds if item.number == number), None)
        if found is None:
            raise ValueError("Фонд не найден.")
        return found

    def balances(self) -> dict[int, int]:
        values = dict.fromkeys((fund.number for fund in self.funds), 0)
        for move in self.movements:
            values[move.fund] += move.amount
        return values

    def reserved(self) -> dict[str, int]:
        values = {}
        amounts = self.balances()
        for fund in self.funds:
            values[fund.currency] = values.get(fund.currency, 0) + amounts[fund.number]
        return values

    def check_templates(self) -> None:
        currencies = set()
        for template in self.templates:
            if template.currency in currencies:
                raise ValueError("На валюту используется один шаблон.")
            currencies.add(template.currency)
            numbers = set()
            for idx, line in enumerate(template.lines):
                fund = self.fund(line.fund)
                if fund.currency != template.currency or line.fund in numbers or line.mode not in MODES:
                    raise ValueError("Каждый фонд валюты может входить в шаблон один раз.")
                numbers.add(line.fund)
                if type(line.value) is not int or line.value < 0:
                    raise ValueError("Некорректное значение правила распределения.")
                if line.mode == "percent" and not 0 < line.value <= 10_000:
                    raise ValueError("Процент должен быть больше нуля и не выше 100.")
                if line.mode == "fixed" and not line.value:
                    raise ValueError("Введите ненулевую фиксированную сумму.")
                if line.mode == "goal" and fund.goal is None:
                    raise ValueError("Для правила «До цели» задайте цель фонда.")
                if line.mode == "remainder" and idx != len(template.lines) - 1:
                    raise ValueError("Правило «Весь остаток» должно быть последним.")

    def validate(self, ledger: Ledger, review: Review) -> None:
        numbers = set()
        for fund in self.funds:
            if (
                type(fund.number) is not int or fund.number < 1 or fund.number in numbers
                or not isinstance(fund.title, str) or not fund.title.strip() or fund.kind not in FUND_KINDS
                or not isinstance(fund.deadline, str)
                or not isinstance(fund.currency, str) or not re.fullmatch("[A-Z]{3}", fund.currency)
                or (fund.goal is not None and (type(fund.goal) is not int or fund.goal <= 0))
            ):
                raise ValueError("Заполните название, вид, валюту и положительную цель фонда.")
            if fund.deadline:
                if fund.goal is None:
                    raise ValueError("Чтобы задать срок, укажите сумму цели.")
                _day(fund.deadline)
            numbers.add(fund.number)
        self.check_templates()
        balances = dict.fromkeys(numbers, 0)
        commands = {}
        movements = set()
        used = {}
        linked = review.fixed_operations
        for move in self.movements:
            fund = self.fund(move.fund)
            if type(move.amount) is not int or not move.amount or not isinstance(move.command, str) or not move.command:
                raise ValueError("Движение фонда должно содержать ненулевую сумму.")
            if not isinstance(move.day, str) or (move.operation is not None and move.day):
                raise ValueError("Дата движения определяется связанной операцией.")
            if move.day:
                _day(move.day)
            source = move.operation, move.day
            if move.command in commands and source != commands[move.command]:
                raise ValueError("Движения одной команды должны иметь общую дату и основание.")
            key = (move.command, move.fund)
            if key in movements:
                raise ValueError("Фонд уже затронут этой командой.")
            movements.add(key)
            commands[move.command] = source
            balances[fund.number] += move.amount
            if balances[fund.number] < 0:
                raise ValueError("В фонде недостаточно средств для этого действия.")
            if move.operation is not None:
                entry = operation(ledger, move.operation)
                if move.operation in linked:
                    raise ValueError("Операция уже учтена в фондах. Сначала отмените движение фонда.")
                if ledger.opening(entry.account).currency != fund.currency or (entry.amount > 0) != (move.amount > 0):
                    raise ValueError("Валюта и направление операции должны совпадать с движением фонда.")
                command, amount = used.get(move.operation, (move.command, 0))
                if command != move.command:
                    raise ValueError("Операция уже учтена в фондах.")
                used[move.operation] = (command, amount + move.amount)
        for number, (_, amount) in used.items():
            entry = operation(ledger, number)
            if (entry.amount > 0 and amount > entry.amount) or (entry.amount < 0 and amount != entry.amount):
                raise ValueError("Движения фонда превышают сумму операции или покрывают только часть оплаты.")

    def save_fund(self, fund: Fund) -> "Funds":
        if not fund.number:
            fund = replace(fund, number=max((item.number for item in self.funds), default=0) + 1)
            return replace(self, funds=(*self.funds, fund))
        previous = self.fund(fund.number)
        if previous.currency != fund.currency and any(move.fund == fund.number for move in self.movements):
            raise ValueError("Валюта фонда с движениями сохраняется.")
        return replace(self, funds=tuple(fund if old.number == fund.number else old for old in self.funds))

    def set_template(self, template: Template) -> "Funds":
        old = tuple(item for item in self.templates if item.currency != template.currency)
        updated = replace(self, templates=(*old, template))
        updated.check_templates()
        return updated

    def plan(self, currency: str, incoming: int, available: int) -> tuple[tuple[int, int], ...]:
        template = next((item for item in self.templates if item.currency == currency and item.lines), None)
        if template is None:
            raise ValueError("Настройте шаблон распределения для этой валюты.")
        budget = min(incoming, max(0, available))
        if budget <= 0:
            raise ValueError("Нет свободной суммы для распределения.")
        left = budget
        current = self.balances()
        moves = []
        for line in template.lines:
            fund = self.fund(line.fund)
            if line.mode == "fixed":
                amount = line.value
            elif line.mode == "percent":
                amount = (incoming * line.value + 5000) // 10_000
            elif line.mode == "goal":
                amount = max(0, fund.goal - current[fund.number])
            else:
                amount = left
            amount = min(amount, left)
            if amount:
                moves.append((fund.number, amount))
                left -= amount
        return tuple(moves)

    def apply(
        self, command: str, moves: tuple[tuple[int, int], ...], operation_number: int | None = None,
        *, day: str | None = None,
    ) -> "Funds":
        if any(item.command == command for item in self.movements):
            return self
        if operation_number is not None and any(item.operation == operation_number for item in self.movements):
            raise ValueError("Операция уже учтена в фондах.")
        if operation_number is not None and day is not None:
            raise ValueError("Для связанного движения используется дата операции.")
        day = _day(day if day is not None else date.today().isoformat()) if operation_number is None else ""
        return replace(self, movements=(
            *self.movements, *(Movement(number, amount, command, operation_number, day) for number, amount in moves),
        ))

    def date_movement(self, command: str, day: str) -> "Funds":
        day = _day(day)
        if not any(move.command == command and move.operation is None for move in self.movements):
            raise ValueError("Выберите ручное движение фонда.")
        return replace(self, movements=tuple(
            replace(move, day=day) if move.command == command else move for move in self.movements
        ))

    def undo(self, command: str) -> "Funds":
        return replace(self, movements=tuple(move for move in self.movements if move.command != command))
