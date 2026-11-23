"""Confirmation of transfers, categories and planned monthly payments."""

from datetime import date, timedelta
from functools import partial

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox, QDialog, QFormLayout, QHBoxLayout, QInputDialog, QLineEdit, QMessageBox, QTableWidgetItem,
    QVBoxLayout,
)

from appearance import ChoiceBox, button, label
from assets import amount_text, parse_amount
from ledger import _normalized
from ledger_views import table
from review import Assignment, Recurring, Rule, RULE_FIELDS, TAX_PAYMENT, TRANSFER_WORDS, is_business, period


def fill_table(widget, rows):
    widget.setRowCount(len(rows))
    for row, values in enumerate(rows):
        for column, value in enumerate(values):
            item = QTableWidgetItem(str(value))
            item.setToolTip(str(value))
            widget.setItem(row, column, item)


def close_bar(dialog, layout, action=None, caption="Сохранить"):
    row = QHBoxLayout()
    row.addStretch(1)
    row.addWidget(button("Отмена" if action else "Закрыть", dialog.reject))
    if action:
        row.addWidget(button(caption, action, primary=True))
    layout.addLayout(row)


def inputs(dialog, title, fields):
    dialog.setWindowTitle(title)
    dialog.setMinimumWidth(510)
    layout = QVBoxLayout(dialog)
    layout.setContentsMargins(20, 18, 20, 18)
    layout.addWidget(label(title, "screenTitle"))
    form = QFormLayout()
    edits = {}
    for key, name, value in fields:
        edit = QLineEdit(str(value))
        edits[key] = edit
        form.addRow(name, edit)
    layout.addLayout(form)
    return layout, form, edits


class CategoryDialog(QDialog):
    def __init__(self, parent, catalog, number):
        super().__init__(parent)
        self.result_review = None
        entry = catalog.ledger.entries[number - 1]
        review = catalog.review
        previous = review.assignments_by_operation().get(number)
        suggestion = review.suggestion(entry)
        category = previous.category if previous else suggestion.category if suggestion else ""
        layout, form, edits = inputs(self, "Подтвердить статью", (("category", "Статья", category),))
        if suggestion:
            layout.addWidget(label(suggestion.reason, "muted", wrap=True))
        self.taxable = QCheckBox("Облагаемое поступление ИП для резерва 6%")
        asset = next(asset for asset in catalog.assets if asset.asset_number == entry.account)
        self.taxable.setVisible(entry.amount > 0 and asset.currency == "RUB" and is_business(asset, catalog.scope))
        self.taxable.setChecked(bool(previous and previous.taxable))
        layout.addWidget(self.taxable)
        self.rule = ChoiceBox()
        self.rule.addItem("Запомнить только эту операцию", "")
        for field, title in RULE_FIELDS.items():
            value = getattr(entry, field)
            if value:
                self.rule.addItem(f"{title}: {value}", field)
        form.addRow("Правило на будущее", self.rule)
        def save():
            value = edits["category"].text().strip()
            if not value:
                QMessageBox.warning(self, "Статья", "Введите статью дохода или расхода.")
                return
            updated = review.assign((Assignment(number, value, self.taxable.isChecked()),))
            field = self.rule.currentData()
            if field:
                updated = updated.learn(Rule(field, getattr(entry, field), 1 if entry.amount > 0 else -1, value))
            self.result_review = updated
            self.accept()
        close_bar(self, layout, save, "Подтвердить")


class RecurringDialog(QDialog):
    def __init__(self, parent, catalog, month):
        super().__init__(parent)
        self.catalog = catalog
        self.setWindowTitle("Регулярные платежи")
        self.resize(840, 520)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.addWidget(label("Регулярные платежи", "screenTitle"))
        controls = QHBoxLayout()
        controls.addWidget(label("Месяц"))
        self.month = QLineEdit(month)
        self.month.setPlaceholderText("ГГГГ-ММ")
        controls.addWidget(self.month)
        controls.addWidget(button("Показать", self.refresh))
        add = button("+ Платёж", self.add, primary=True)
        add.setEnabled(catalog.editable and bool(catalog.ledger.openings))
        controls.addWidget(add)
        edit = button("Изменить…", partial(self.add, edit=True))
        edit.setEnabled(catalog.editable)
        controls.addWidget(edit)
        layout.addLayout(controls)
        self.grid = table(("Дата", "Платёж", "План", "Факт", "Разница"), 1)
        layout.addWidget(self.grid, 1)
        close_bar(self, layout)
        self.refresh()

    def refresh(self):
        try:
            self.rows = self.catalog.review.calendar(self.catalog.ledger, self.month.text().strip())
        except ValueError:
            QMessageBox.warning(self, "Регулярные платежи", "Введите месяц в формате ГГГГ-ММ.")
            return
        fill_table(self.grid, [
            (due, item.name, amount_text(item.expected),
             f"{amount_text(-actual.amount)} · {actual.day}" if actual else "Запланирован",
             amount_text(-actual.amount - item.expected) if actual else "—")
            for due, item, actual in self.rows
        ])

    def add(self, *, edit=False):
        row = self.grid.currentRow()
        if edit and row < 0:
            return
        old = self.rows[row][1] if edit else None
        popup = QDialog(self)
        layout, form, edits = inputs(popup, "Регулярный платёж", (
            ("name", "Название", old.name if old else ""),
            ("recipient", "Получатель / фраза в описании", old.recipient if old else ""),
            ("category", "Статья", old.category if old else ""),
            ("amount", "Ожидаемая сумма", amount_text(old.expected) if old else ""),
            ("day", "День месяца", old.month_day if old else ""),
        ))
        account = ChoiceBox()
        accounts = {asset.asset_number: asset for asset in self.catalog.assets}
        for opening in self.catalog.ledger.openings:
            account.addItem(f"{accounts[opening.account].title} · {opening.currency}", opening.account)
        if old:
            account.setCurrentIndex(account.findData(old.account))
        form.addRow("Счёт списания", account)
        def save():
            try:
                values = {key: edit.text().strip() for key, edit in edits.items()}
                item = Recurring(values["name"], account.currentData(), values["recipient"], values["category"],
                                 parse_amount(values["amount"]), int(values["day"]), old.number if old else 0)
                self.catalog = self.catalog.save_review(self.catalog.review.save_recurring(item))
            except (OSError, ValueError) as exc:
                QMessageBox.warning(popup, "Регулярный платёж", str(exc))
                return
            popup.accept()
        close_bar(popup, layout, save)
        if popup.exec() == QDialog.DialogCode.Accepted:
            self.refresh()


class ReviewDialog(QDialog):
    def __init__(self, parent, catalog):
        super().__init__(parent)
        self.catalog = catalog
        self.names = {asset.asset_number: asset.title for asset in catalog.assets}
        self.setWindowTitle("Разбор операций")
        self.resize(1050, 685)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.addWidget(label("Разбор операций", "screenTitle"))
        last = max((entry.day for entry in catalog.ledger.entries), default=date.today().isoformat())
        controls = QHBoxLayout()
        first = (date.fromisoformat(last) - timedelta(days=44)).isoformat()
        self.start, self.end = QLineEdit(first), QLineEdit(last)
        for title, edit in (("С", self.start), ("По", self.end)):
            controls.addWidget(label(title))
            controls.addWidget(edit)
        controls.addWidget(button("Показать период", self.refresh))
        controls.addStretch(1)
        controls.addWidget(button("Отчёт и резерв ИП…", self.report))
        controls.addWidget(button("Регулярные платежи…", self.recurring))
        layout.addLayout(controls)
        self.grid = table(("№", "Дата", "Счёт", "Сумма", "Операция", "Статья / статус", "Причина"), 4)
        self.grid.horizontalHeader().setSectionResizeMode(
            6, self.grid.horizontalHeader().ResizeMode.Interactive
        )
        self.grid.setColumnWidth(6, 230)
        layout.addWidget(self.grid, 1)
        actions = QHBoxLayout()
        for title, callback in (
            ("Статья…", self.category), ("Перевод…", self.transfer), ("Пара по номеру…", self.manual_transfer),
            ("Ждать вторую сторону", self.waiting), ("Снять связь или ожидание", self.unlink),
            ("Подтвердить предложения…", self.batch),
        ):
            widget = button(title, callback)
            widget.setEnabled(catalog.editable)
            actions.addWidget(widget)
        actions.addStretch(1)
        actions.addWidget(button("Закрыть", self.accept))
        layout.addLayout(actions)
        self.numbers = []
        self.refresh()

    def bounds(self):
        start, end = self.start.text().strip(), self.end.text().strip()
        period(start, end)
        return start, end

    def refresh(self):
        try:
            start, end = self.bounds()
        except ValueError as exc:
            QMessageBox.warning(self, "Период", str(exc))
            return
        selected = self.selected()
        review, ledger = self.catalog.review, self.catalog.ledger
        assigned, linked = review.assignments_by_operation(), review.linked
        self.numbers = sorted(
            (number for number, entry in enumerate(ledger.entries, 1) if start <= entry.day <= end),
            key=lambda number: (ledger.entries[number - 1].day, number), reverse=True,
        )
        data = []
        wait = set(review.waiting)
        for number in self.numbers:
            entry = ledger.entries[number - 1]
            reason = ""
            if number in linked:
                status = "Связанный перевод"
            elif number in assigned:
                status = assigned[number].category
                reason = "Подтверждено" + (" · Облагаемое ИП" if assigned[number].taxable else "")
            elif number in wait or any(word in _normalized(entry.description) for word in TRANSFER_WORDS):
                status = "Возможный перевод"
                reason = ("Ожидает вторую сторону" if not review.transfer_candidates(ledger, number)
                          else "Доступны пары · «Перевод…»")
            elif suggestion := review.suggestion(entry):
                status = suggestion.category
                reason = suggestion.reason
                if suggestion.difference:
                    reason += f" · Разница {amount_text(suggestion.difference)}"
            else:
                status = "Без статьи"
            data.append((number, entry.day, self.names[entry.account], amount_text(entry.amount),
                         entry.description, status, reason))
        fill_table(self.grid, data)
        if selected in self.numbers:
            self.grid.selectRow(self.numbers.index(selected))
        elif self.numbers:
            self.grid.selectRow(0)

    def selected(self):
        row = self.grid.currentRow()
        return self.numbers[row] if 0 <= row < len(self.numbers) else None

    def save(self, review):
        try:
            if review != self.catalog.review:
                self.catalog = self.catalog.save_review(review)
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, "Операция не выполнена", str(exc))
            return
        self.refresh()

    def category(self):
        number = self.selected()
        if number is None or number in self.catalog.review.linked:
            return
        dialog = CategoryDialog(self, self.catalog, number)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.save(dialog.result_review)

    def transfer(self):
        number = self.selected()
        if number is None or number in self.catalog.review.linked:
            return
        ledger, review = self.catalog.ledger, self.catalog.review
        candidates = review.transfer_candidates(ledger, number)
        if not candidates:
            self.waiting()
            return
        dialog = QDialog(self)
        dialog.setWindowTitle("Связать перевод")
        dialog.resize(780, 420)
        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.addWidget(label("Выберите вторую сторону перевода", "screenTitle"))
        grid = table(("Дата", "Счёт", "Сумма", "Описание", "Комиссия", "Причина"), 5)
        data = []
        for other, fee, reason in candidates:
            entry = ledger.entries[other - 1]
            data.append((entry.day, self.names[entry.account], amount_text(entry.amount),
                         entry.description, amount_text(fee), reason))
        fill_table(grid, data)
        grid.selectRow(0)
        layout.addWidget(grid, 1)
        close_bar(dialog, layout, dialog.accept, "Подтвердить перевод")
        if dialog.exec() == QDialog.DialogCode.Accepted and grid.currentRow() >= 0:
            self.save(review.link_transfer(ledger, number, candidates[grid.currentRow()][0]))

    def manual_transfer(self):
        number = self.selected()
        ledger, review = self.catalog.ledger, self.catalog.review
        if number is None or number in review.linked or len(ledger.entries) < 2:
            return
        other, ok = QInputDialog.getInt(
            self, "Связать перевод", "Номер второй операции из журнала",
            1, 1, len(ledger.entries),
        )
        if not ok:
            return
        try:
            updated = review.link_transfer(ledger, number, other)
        except ValueError as exc:
            QMessageBox.warning(self, "Перевод", str(exc))
            return
        pair = updated.transfers[-1]
        text = f"Связать операции № {number} и № {other}? Комиссия: {amount_text(pair.fee)}."
        if QMessageBox.question(self, "Подтвердить перевод", text) == QMessageBox.StandardButton.Yes:
            self.save(updated)

    def waiting(self):
        if (number := self.selected()) is not None:
            self.save(self.catalog.review.wait_for_transfer(self.catalog.ledger, number))

    def unlink(self):
        if (number := self.selected()) is not None:
            self.save(self.catalog.review.unlink(number))

    def batch(self):
        review, ledger = self.catalog.review, self.catalog.ledger
        excluded = review.linked | set(review.waiting) | review.assignments_by_operation().keys()
        rows = []
        for number in self.numbers:
            entry = ledger.entries[number - 1]
            if number in excluded or any(word in _normalized(entry.description) for word in TRANSFER_WORDS):
                continue
            if suggestion := review.suggestion(entry):
                rows.append((number, entry, suggestion))
        if not rows:
            QMessageBox.information(self, "Статьи", "В этом периоде нет предложений для подтверждения.")
            return
        dialog = QDialog(self)
        dialog.setWindowTitle("Подтвердить статьи")
        dialog.resize(785, 510)
        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.addWidget(label("Отметьте проверенные предложения", "screenTitle"))
        grid = table(("Подтвердить", "Дата", "Операция", "Статья", "Причина"), 4)
        fill_table(grid, [("", entry.day, entry.description, hint.category, hint.reason)
                          for _, entry, hint in rows])
        for index in range(len(rows)):
            grid.item(index, 0).setCheckState(Qt.CheckState.Checked)
        layout.addWidget(grid, 1)
        close_bar(dialog, layout, dialog.accept, "Подтвердить отмеченные")
        if dialog.exec() == QDialog.DialogCode.Accepted:
            assignments = tuple(
                Assignment(number, suggestion.category) for index, (number, _, suggestion) in enumerate(rows)
                if grid.item(index, 0).checkState() == Qt.CheckState.Checked
            )
            self.save(review.assign(assignments))

    def report(self):
        try:
            start, end = self.bounds()
        except ValueError as exc:
            QMessageBox.warning(self, "Период", str(exc))
            return
        review, ledger = self.catalog.review, self.catalog.ledger
        dialog = QDialog(self)
        dialog.setWindowTitle("Доходы и расходы")
        dialog.resize(730, 530)
        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.addWidget(label("Доходы и расходы", "screenTitle"))
        layout.addWidget(label(f"{start} — {end}", "muted"))
        grid = table(("Валюта", "Вид", "Статья", "Сумма"), 2)
        totals = review.report(ledger, start, end)
        fill_table(grid, [(*key, amount_text(value)) for key, value in sorted(totals.items())])
        layout.addWidget(grid, 1)
        revenue, reserve = review.tax_reserve(ledger, start, end)
        layout.addWidget(label(
            f"Отмеченные поступления ИП: {amount_text(revenue)} RUB · Ориентир резерва 6%: {amount_text(reserve)} RUB\n"
            f"Проведённые налоги и взносы: {amount_text(totals.get(('RUB', 'Расходы', TAX_PAYMENT), 0))} RUB",
            "sectionTitle", wrap=True,
        ))
        close_bar(dialog, layout)
        dialog.exec()

    def recurring(self):
        dialog = RecurringDialog(self, self.catalog, self.end.text().strip()[:7])
        dialog.exec()
        self.catalog = dialog.catalog
        self.refresh()
