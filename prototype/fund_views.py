"""Fund balances, allocation templates and the household overview."""

from datetime import date
from functools import partial
from uuid import uuid4

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog, QHBoxLayout, QInputDialog, QLineEdit, QMessageBox, QSpinBox,
    QVBoxLayout, QWidget,
)

from appearance import ChoiceBox, area, button, label
from assets import CATEGORIES, amount_text, parse_amount
from funds import FUND_KINDS, MODES, Fund, Line, Template
from ledger_views import DetailsDialog, table
from review_views import close_bar, fill_table, inputs


def cash_rows(catalog):
    cash, reserved = catalog.liquid_totals(), catalog.funds.reserved()
    return [
        (currency, amount_text(cash.get(currency, 0)), amount_text(reserved.get(currency, 0)),
         amount_text(cash.get(currency, 0) - reserved.get(currency, 0)))
        for currency in sorted(cash.keys() | reserved.keys())
    ]


def reserve_text(catalog, start, end):
    revenue, reserve = catalog.review.tax_reserve(catalog.ledger, start, end)
    return f"ИП · Облагаемые поступления {amount_text(revenue)} RUB · Резерв 6%: {amount_text(reserve)} RUB"


class TemplateDialog(QDialog):
    def __init__(self, parent, state, currency):
        super().__init__(parent)
        self.updated = None
        self.setWindowTitle(f"Шаблон распределения · {currency}")
        self.resize(660, 510)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.addWidget(label(f"Шаблон распределения · {currency}", "screenTitle"))
        layout.addWidget(label(
            "Процент считается от выбранного поступления. Правила выполняются по порядку, «Весь остаток» — последним.",
            "muted", wrap=True,
        ))
        panel = QWidget()
        panel.setObjectName("formContent")
        rows = QVBoxLayout(panel)
        template = next((item for item in state.templates if item.currency == currency), Template(currency, ()))
        existing = {line.fund: (position, line) for position, line in enumerate(template.lines, 1)}
        widgets = []
        for fund in state.funds:
            if fund.currency != currency:
                continue
            row = QHBoxLayout()
            row.addWidget(label(fund.title, wrap=True), 1)
            mode = ChoiceBox()
            mode.addItem("Пропустить", "")
            for key, value in MODES.items():
                mode.addItem(value, key)
            value = QLineEdit()
            value.setPlaceholderText("Сумма / %")
            value.setMaximumWidth(102)
            order = QSpinBox()
            order.setRange(1, 100000)
            order.setAccessibleName(f"Порядок: {fund.title}")
            order.setValue(len(widgets) + 1)
            if fund.number in existing:
                position, line = existing[fund.number]
                mode.setCurrentIndex(mode.findData(line.mode))
                if line.value:
                    value.setText(amount_text(line.value))
                order.setValue(position)
            for item in (mode, value, order):
                row.addWidget(item)
            rows.addLayout(row)
            widgets.append((fund, mode, value, order))
        rows.addStretch(1)
        layout.addWidget(area(panel), 1)
        def save():
            try:
                lines = []
                for fund, mode, value, order in widgets:
                    rule = mode.currentData()
                    if rule:
                        amount = (parse_amount(value.text()) or 0) if rule in ("fixed", "percent") else 0
                        lines.append((order.value(), Line(fund.number, rule, amount)))
                ordered = tuple(item for _, item in sorted(lines, key=lambda item: item[0]))
                self.updated = state.set_template(Template(currency, ordered))
            except ValueError as exc:
                QMessageBox.warning(self, "Шаблон", str(exc))
                return
            self.accept()
        close_bar(self, layout, save)


class FundsDialog(QDialog):
    def __init__(self, parent, catalog):
        super().__init__(parent)
        self.catalog = catalog
        self.setWindowTitle("Фонды и цели")
        self.resize(900, 660)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.addWidget(label("Фонды и цели", "screenTitle"))
        self.summary = table(("Валюта", "На счетах", "В фондах", "Свободно"), 3)
        self.summary.setMaximumHeight(130)
        layout.addWidget(self.summary)
        self.grid = table(("Фонд", "Вид", "Валюта", "Накоплено", "Цель", "До цели"), 0)
        layout.addWidget(self.grid, 1)
        self.tax = label(style="muted", wrap=True)
        layout.addWidget(self.tax)
        top = QHBoxLayout()
        for caption, callback in (
            ("+ Фонд", self.edit), ("Изменить фонд…", partial(self.edit, edit=True)),
            ("Шаблон…", self.template), ("Распределить поступление…", self.allocate),
        ):
            item = button(caption, callback, primary=caption.startswith("+"))
            item.setEnabled(catalog.editable)
            top.addWidget(item)
        top.addStretch(1)
        layout.addLayout(top)
        bottom = QHBoxLayout()
        for caption, callback in (
            ("Внести / освободить…", self.adjust), ("Учесть оплату из фонда…", self.pay),
            ("Движения фонда…", self.history),
        ):
            item = button(caption, callback)
            if caption != "Движения фонда…":
                item.setEnabled(catalog.editable)
            bottom.addWidget(item)
        bottom.addStretch(1)
        bottom.addWidget(button("Закрыть", self.accept))
        layout.addLayout(bottom)
        self.refresh()

    def refresh(self):
        number = self.grid.currentRow()
        fill_table(self.summary, cash_rows(self.catalog))
        funds = self.catalog.funds
        amounts = funds.balances()
        fill_table(self.grid, [
            (item.title, item.kind, item.currency, amount_text(amounts[item.number]),
             amount_text(item.goal) if item.goal is not None else "—",
             amount_text(max(0, item.goal - amounts[item.number])) if item.goal is not None else "—")
            for item in funds.funds
        ])
        if funds.funds:
            self.grid.selectRow(max(0, min(number, len(funds.funds) - 1)))
        year = max((entry.day for entry in self.catalog.ledger.entries), default=date.today().isoformat())[:4]
        self.tax.setText(reserve_text(self.catalog, f"{year}-01-01", f"{year}-12-31"))

    def selected(self):
        row = self.grid.currentRow()
        return self.catalog.funds.funds[row] if 0 <= row < len(self.catalog.funds.funds) else None

    def save(self, funds):
        try:
            if funds != self.catalog.funds:
                self.catalog = self.catalog.save_funds(funds)
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, "Фонды", str(exc))
            return False
        self.refresh()
        return True

    def edit(self, *, edit=False):
        old = self.selected() if edit else None
        if edit and old is None:
            return
        dialog = QDialog(self)
        layout, form, fields = inputs(dialog, "Фонд", (
            ("title", "Название", old.title if old else ""),
            ("currency", "Валюта", old.currency if old else "RUB"),
            ("goal", "Цель (необязательно)", amount_text(old.goal) if old and old.goal else ""),
            ("deadline", "Срок (ГГГГ-ММ-ДД)", old.deadline if old else ""),
        ))
        kind = ChoiceBox()
        kind.addItems(FUND_KINDS)
        if old:
            kind.setCurrentText(old.kind)
        form.addRow("Вид", kind)
        def commit():
            try:
                goal = parse_amount(fields["goal"].text())
                fund = Fund(fields["title"].text().strip(), kind.currentText(),
                            fields["currency"].text().strip().upper(), goal, old.number if old else 0,
                            fields["deadline"].text().strip())
                if self.save(self.catalog.funds.save_fund(fund)):
                    dialog.accept()
            except ValueError as exc:
                QMessageBox.warning(dialog, "Фонд", str(exc))
        close_bar(dialog, layout, commit)
        dialog.exec()

    def template(self):
        currencies = sorted({fund.currency for fund in self.catalog.funds.funds})
        if not currencies:
            QMessageBox.information(self, "Шаблон", "Сначала создайте фонд.")
            return
        currency, ok = QInputDialog.getItem(self, "Шаблон", "Валюта", currencies, editable=False)
        if ok:
            dialog = TemplateDialog(self, self.catalog.funds, currency)
            if dialog.exec() == QDialog.DialogCode.Accepted:
                self.save(dialog.updated)

    def available(self, currency):
        return self.catalog.liquid_totals().get(currency, 0) - self.catalog.funds.reserved().get(currency, 0)

    def choose_operation(self, *, income, currency=None):
        ledger = self.catalog.ledger
        used = {item.operation for item in self.catalog.funds.movements} | self.catalog.review.reserved_operations
        names = {asset.asset_number: asset.title for asset in self.catalog.assets}
        choices = {}
        for number, entry in reversed(tuple(enumerate(ledger.entries, 1))):
            code = ledger.opening(entry.account).currency
            if number in used or (entry.amount > 0) != income or (currency and code != currency):
                continue
            caption = f"№ {number} · {entry.day} · {amount_text(entry.amount)} {code} · {names[entry.account]} · {entry.description}"
            choices[caption] = (number, entry, code)
        if not choices:
            QMessageBox.information(self, "Операции", "Нет подходящих проведённых операций.")
            return None
        selected, ok = QInputDialog.getItem(self, "Операция", "Выберите проведённую операцию",
                                            list(choices), editable=False)
        return choices[selected] if ok else None

    def allocate(self):
        item = self.choose_operation(income=True)
        if item is None:
            return
        number, entry, currency = item
        funds = self.catalog.funds
        try:
            moves = funds.plan(currency, entry.amount, self.available(currency))
            if not moves:
                raise ValueError("По шаблону ничего не распределяется. Проверьте правила и цели.")
            text = "\n".join(f"{funds.fund(key).title}: {amount_text(value)} {currency}" for key, value in moves)
            text += f"\n\nСвободно после распределения: {amount_text(self.available(currency) - sum(value for _, value in moves))} {currency}"
            if QMessageBox.question(self, "Подтвердить распределение", text) == QMessageBox.StandardButton.Yes:
                self.save(funds.apply(f"income:{number}", moves, number))
        except ValueError as exc:
            QMessageBox.warning(self, "Распределение", str(exc))

    def adjust(self):
        fund = self.selected()
        if fund is None:
            return
        values = DetailsDialog(self, fund.title, "Введите положительную сумму для пополнения или отрицательную для освобождения резерва.", (
            ("amount", f"Сумма, {fund.currency}", "", "0,00"),
            ("day", "Дата движения", date.today().isoformat(), "ГГГГ-ММ-ДД"),
        )).values()
        if values is None:
            return
        try:
            amount = parse_amount(values["amount"])
            if not amount:
                raise ValueError("Введите ненулевую сумму.")
            if amount > max(0, self.available(fund.currency)):
                raise ValueError("Свободных денег в этой валюте недостаточно.")
            self.save(self.catalog.funds.apply(uuid4().hex, ((fund.number, amount),), day=values["day"]))
        except ValueError as exc:
            QMessageBox.warning(self, "Фонд", str(exc))

    def pay(self):
        fund = self.selected()
        if fund is None or (item := self.choose_operation(income=False, currency=fund.currency)) is None:
            return
        number, entry, currency = item
        text = f"Списать из фонда «{fund.title}» {amount_text(-entry.amount)} {currency} по уже проведённой операции?"
        if QMessageBox.question(self, "Оплата из фонда", text) == QMessageBox.StandardButton.Yes:
            self.save(self.catalog.funds.apply(f"expense:{number}", ((fund.number, entry.amount),), number))

    def history(self, *, window_parent=None):
        fund = self.selected()
        if fund is None:
            return
        dialog = QDialog(window_parent if window_parent is not None else self)
        dialog.setWindowTitle("Движения фонда")
        dialog.resize(740, 450)
        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.addWidget(label(fund.title, "screenTitle"))
        moves = ()
        grid = table(("Дата", "Сумма", "Основание"), 2)
        entries = self.catalog.ledger.entries
        layout.addWidget(grid, 1)

        def refresh():
            nonlocal moves
            row = grid.currentRow()
            moves = tuple(move for move in self.catalog.funds.movements if move.fund == fund.number)
            fill_table(grid, [
                (move.effective_day(self.catalog.ledger) or "—", amount_text(move.amount),
                 f"№ {move.operation} · {entries[move.operation - 1].description}" if move.operation else "Ручное движение")
                for move in moves
            ])
            if moves:
                grid.selectRow(max(0, min(row, len(moves) - 1)))
            selection_changed()

        def selection_changed():
            row = grid.currentRow()
            selected = self.catalog.editable and 0 <= row < len(moves)
            change_day.setEnabled(selected and moves[row].operation is None)
            undo_move.setEnabled(selected)

        def date_move():
            row = grid.currentRow()
            move = moves[row]
            text, ok = QInputDialog.getText(dialog, "Дата движения", "Введите дату в формате ГГГГ-ММ-ДД",
                                            text=move.day)
            if ok:
                try:
                    updated = self.catalog.funds.date_movement(move.command, text.strip())
                except ValueError as exc:
                    QMessageBox.warning(dialog, "Дата движения", str(exc))
                    return
                if self.save(updated):
                    refresh()

        def undo():
            row = grid.currentRow()
            consent = QMessageBox.question(dialog, "Отмена", "Отменить выбранное движение во всех затронутых фондах?")
            if consent == QMessageBox.StandardButton.Yes and self.save(self.catalog.funds.undo(moves[row].command)):
                refresh()

        actions = QHBoxLayout()
        change_day = button("Указать дату…", date_move)
        undo_move = button("Отменить движение…", undo)
        actions.addWidget(change_day)
        actions.addWidget(undo_move)
        actions.addStretch(1)
        actions.addWidget(button("Готово", dialog.accept, primary=True))
        layout.addLayout(actions)
        grid.itemSelectionChanged.connect(selection_changed)
        refresh()
        dialog.exec()


class OverviewDialog(QDialog):
    def __init__(self, parent, catalog):
        super().__init__(parent)
        self.setWindowTitle("Обзор")
        self.resize(910, 660)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.addWidget(label("Обзор", "screenTitle"))
        cash = table(("Валюта", "Деньги на счетах", "В фондах", "Свободно"), 3)
        cash.setMaximumHeight(126)
        fill_table(cash, cash_rows(catalog))
        layout.addWidget(cash)
        layout.addWidget(label("Счета, инвестиции и имущество", "sectionTitle"))
        assets = table(("Категория", "Название", "Сумма", "Дата остатка или оценки"), 1)
        known = [catalog.recorded_value(asset) for asset in catalog.assets if asset.status == "active"]
        fill_table(assets, [(CATEGORIES[asset.category], asset.title, asset.valuation, asset.as_of or "—")
                            for asset in known])
        layout.addWidget(assets, 1)
        layout.addWidget(label("Ближайшие регулярные платежи", "sectionTitle"))
        calendar = table(("Плановая дата", "Платёж", "План"), 1)
        today = date.today()
        month = today.strftime("%Y-%m")
        following = today.replace(year=today.year + (today.month == 12), month=today.month % 12 + 1, day=1)
        schedule = (*catalog.review.calendar(catalog.ledger, month),
                    *catalog.review.calendar(catalog.ledger, following.strftime("%Y-%m")))
        fill_table(calendar, [(day, rule.name, f"{amount_text(rule.expected)} {catalog.ledger.opening(rule.account).currency}")
                              for day, rule, actual in schedule if actual is None])
        layout.addWidget(calendar, 1)
        close_bar(self, layout)
