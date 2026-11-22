"""Account operations, CSV review and balance comparison."""

from collections import Counter
from datetime import date
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView, QDialog, QFileDialog, QFormLayout, QHBoxLayout, QHeaderView,
    QLineEdit, QMessageBox, QTableWidget, QTableWidgetItem, QVBoxLayout,
)

from appearance import ChoiceBox, button, label
from assets import Asset, amount_text, parse_amount
from documents import Catalog
from ledger import Entry, Ledger, Opening, Statement, _normalized, preview_import, read_statement

FIELDS = {
    "date": "Дата", "amount": "Сумма со знаком", "description": "Описание",
    "identifier": "ID операции", "currency": "Валюта", "account": "Реквизит счёта",
    "balance": "Остаток банка",
}
STATES = {"new": "Новая", "duplicate": "Повтор", "review": "На сверку", "error": "Ошибка"}


def table(headers: tuple[str, ...], stretch: int) -> QTableWidget:
    widget = QTableWidget(0, len(headers))
    widget.setObjectName("ledgerTable")
    widget.setHorizontalHeaderLabels(headers)
    widget.verticalHeader().hide()
    widget.verticalHeader().setDefaultSectionSize(34)
    widget.setShowGrid(False)
    widget.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    widget.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    widget.setAlternatingRowColors(True)
    widget.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
    widget.horizontalHeader().setSectionResizeMode(stretch, QHeaderView.ResizeMode.Stretch)
    return widget


def comparison(calculated: int, reported: int, day: str, currency: str) -> str:
    difference = calculated - reported
    result = "Остатки совпали." if not difference else f"Разница: {amount_text(difference)} {currency}."
    return (
        f"На {day} · По учёту: {amount_text(calculated)} {currency} · "
        f"По банку: {amount_text(reported)} {currency}. {result}"
    )


class DetailsDialog(QDialog):
    def __init__(self, parent, title, guidance, fields):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumWidth(460)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 18)
        notice = label(guidance, "muted", wrap=True)
        if guidance:
            layout.addWidget(notice)
        form = QFormLayout()
        self.inputs = {}
        for key, name, value, placeholder in fields:
            edit = QLineEdit(value)
            edit.setPlaceholderText(placeholder)
            self.inputs[key] = edit
            form.addRow(name, edit)
        layout.addLayout(form)
        actions = QHBoxLayout()
        actions.addStretch(1)
        actions.addWidget(button("Отмена", self.reject))
        actions.addWidget(button("Продолжить", self.accept, primary=True))
        layout.addLayout(actions)

    def values(self) -> dict[str, str] | None:
        if self.exec() != QDialog.DialogCode.Accepted:
            return None
        return {key: widget.text().strip() for key, widget in self.inputs.items()}


class ImportDialog(QDialog):
    def __init__(
        self, parent, statement: Statement, ledger: Ledger, accounts: dict[int, Asset],
        *, position: tuple[int, int] | None = None,
    ):
        super().__init__(parent)
        self.statement = statement
        self.ledger = ledger
        self.accounts = accounts
        self.preview = None
        self.result_ledger = ledger
        self.setWindowTitle("Импорт выписки")
        self.resize(890, 650)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 16, 20, 16)
        layout.addWidget(label(statement.name, "screenTitle", wrap=True))
        if position is not None:
            number, total = position
            layout.addWidget(label(
                f"Выписка {number} из {total}. Запись после подтверждения всех выбранных файлов.",
                "muted",
            ))
        self.account = ChoiceBox()
        self.account.setAccessibleName("Счёт для выписки")
        self.account.addItem("Выберите счёт", None)
        for number, asset in accounts.items():
            details = f"{asset.title} · {asset.owner} · {asset.currency} · № {number}"
            self.account.addItem(details, number)
        layout.addWidget(self.account)

        mapping = QHBoxLayout()
        left, right = QFormLayout(), QFormLayout()
        mapping.addLayout(left, 1)
        mapping.addLayout(right, 1)
        self.columns = {}
        suggested = statement.suggested_columns()
        for index, (key, title) in enumerate(FIELDS.items()):
            form = left if index < 3 else right
            combo = ChoiceBox()
            combo.setAccessibleName(title)
            combo.addItem("Не выбрано", None)
            for column, header in enumerate(statement.columns):
                combo.addItem(f"{header or 'Столбец'} · {column + 1}", column)
            if key in suggested:
                combo.setCurrentIndex(combo.findData(suggested[key]))
            self.columns[key] = combo
            form.addRow(title, combo)
        layout.addLayout(mapping)
        reference = _normalized(statement.reference(suggested))
        if reference:
            matches = [
                opening.account for opening in ledger.openings
                if opening.account in accounts and _normalized(opening.reference) == reference
            ]
            if len(matches) == 1:
                self.account.setCurrentIndex(self.account.findData(matches[0]))
        self.account.currentIndexChanged.connect(self.clear_preview)
        for combo in self.columns.values():
            combo.currentIndexChanged.connect(self.clear_preview)

        preview_actions = QHBoxLayout()
        preview_actions.addWidget(button("Предпросмотр", self.build_preview))
        self.summary = label("Выберите счёт и столбцы выписки.", "muted", wrap=True)
        preview_actions.addWidget(self.summary, 1)
        layout.addLayout(preview_actions)
        self.grid = table(("Статус", "Строка", "Дата", "Сумма", "Описание / причина"), 4)
        self.grid.itemChanged.connect(lambda _: self.refresh_summary())
        layout.addWidget(self.grid, 1)
        self.balance = label(style="muted", wrap=True)
        layout.addWidget(self.balance)
        actions = QHBoxLayout()
        actions.addStretch(1)
        actions.addWidget(button("Отменить импорт", self.reject))
        self.apply_button = button("Подтвердить выписку", self.apply, primary=True)
        self.apply_button.setEnabled(False)
        actions.addWidget(self.apply_button)
        layout.addLayout(actions)

    def clear_preview(self) -> None:
        self.preview = None
        self.grid.setRowCount(0)
        self.balance.clear()
        self.summary.setText("Настройки изменились. Постройте предпросмотр.")
        self.apply_button.setEnabled(False)

    def build_preview(self) -> None:
        account = self.account.currentData()
        if account is None:
            self.summary.setText("Выберите счёт, к которому относится выписка.")
            return
        mapping = {name: combo.currentData() for name, combo in self.columns.items() if combo.currentData() is not None}
        try:
            preview = preview_import(self.ledger, account, self.statement, mapping)
        except ValueError as exc:
            self.summary.setText(str(exc))
            return
        self.preview = preview
        self.grid.blockSignals(True)
        self.grid.setRowCount(len(preview.rows))
        for index, row in enumerate(preview.rows):
            status = QTableWidgetItem(STATES[row.state])
            if row.state == "review":
                status.setCheckState(Qt.CheckState.Unchecked)
            self.grid.setItem(index, 0, status)
            day = row.entry.day if row.entry else ""
            value = amount_text(row.entry.amount) if row.entry else ""
            description = row.entry.description if row.entry else ""
            explanation = " · ".join(filter(None, (description, row.message)))
            for column, cell in enumerate((str(row.line), day, value, explanation), start=1):
                item = QTableWidgetItem(cell)
                item.setToolTip(cell)
                if column == 3:
                    item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                self.grid.setItem(index, column, item)
        self.grid.blockSignals(False)
        self.apply_button.setEnabled(True)
        self.refresh_summary()

    def confirmed_lines(self) -> set[int]:
        return {
            row.line for index, row in enumerate(self.preview.rows)
            if row.state == "review" and self.grid.item(index, 0).checkState() == Qt.CheckState.Checked
        }

    def refresh_summary(self) -> None:
        if self.preview is None:
            return
        counts = Counter(row.state for row in self.preview.rows)
        confirmed = self.confirmed_lines()
        self.summary.setText(
            f"К добавлению: {counts['new'] + len(confirmed)} · Повторы: {counts['duplicate']} · "
            f"На сверку: {counts['review']} · Ошибки: {counts['error']}"
        )
        check = self.preview.balance
        if check is not None:
            staged = self.preview.apply(self.ledger, confirmed)
            self.balance.setText(
                comparison(staged.balance_on(check.account, check.day), check.amount, check.day,
                           self.accounts[check.account].currency)
            )
        elif self.preview.balance_warning:
            self.balance.setText(self.preview.balance_warning)
        elif counts["review"]:
            self.balance.setText("Отметьте совпадение только если это отдельная операция.")
        else:
            self.balance.clear()

    def apply(self) -> None:
        if self.preview is not None:
            if any(row.state == "error" for row in self.preview.rows):
                reply = QMessageBox.question(
                    self, "В выписке есть ошибки",
                    "Импортировать допустимые строки? Чтобы исправить ошибки в настройках, нажмите «Нет».",
                )
                if reply != QMessageBox.StandardButton.Yes:
                    return
            self.result_ledger = self.preview.apply(self.ledger, self.confirmed_lines())
            self.accept()


class LedgerDialog(QDialog):
    def __init__(self, parent, catalog: Catalog):
        super().__init__(parent)
        self.catalog = catalog
        self.accounts = {asset.asset_number: asset for asset in catalog.assets if asset.category == "account"}
        self.setWindowTitle("Выписки и операции")
        self.resize(900, 630)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.addWidget(label("Выписки и операции", "screenTitle"))
        self.account = ChoiceBox()
        self.account.setAccessibleName("Счёт")
        for number, asset in self.accounts.items():
            self.account.addItem(f"{asset.title} · {asset.owner} · {asset.currency} · № {number}", number)
        self.account.setEnabled(bool(self.accounts))
        layout.addWidget(self.account)
        actions = QHBoxLayout()
        self.opening_button = button("Начальный остаток…", self.set_opening)
        self.manual_button = button("+ Операция", self.add_manual)
        self.import_button = button("Импорт CSV…", self.import_files, primary=True)
        self.check_button = button("Сверить остаток…", self.reconcile)
        for widget in (self.opening_button, self.manual_button, self.import_button, self.check_button):
            actions.addWidget(widget)
        actions.addStretch(1)
        layout.addLayout(actions)
        self.summary = label(style="sectionTitle", wrap=True)
        layout.addWidget(self.summary)
        self.bank_summary = label(style="muted", wrap=True)
        layout.addWidget(self.bank_summary)
        self.grid = table(("Дата", "Сумма", "Описание", "Источник"), 2)
        layout.addWidget(self.grid, 1)
        actions = QHBoxLayout()
        actions.addStretch(1)
        actions.addWidget(button("Закрыть", self.accept))
        layout.addLayout(actions)
        self.account.currentIndexChanged.connect(self.refresh)
        self.refresh()

    def refresh(self) -> None:
        number = self.account.currentData()
        ledger = self.catalog.ledger
        self.opening_button.setEnabled(number is not None and self.catalog.editable)
        self.import_button.setEnabled(bool(ledger.openings) and self.catalog.editable)
        latest = ledger.latest_balance(number)
        self.manual_button.setEnabled(latest is not None and self.catalog.editable)
        self.check_button.setEnabled(latest is not None)
        self.bank_summary.clear()
        self.grid.setRowCount(0)
        if number is None:
            self.summary.setText("Добавьте банковский счёт в разделе «Счета и активы».")
            return
        if latest is None:
            self.summary.setText("Задайте начальный остаток, чтобы начать учёт по этому счёту.")
            return
        amount, day = latest
        currency = self.accounts[number].currency
        self.summary.setText(f"Остаток по учёту: {amount_text(amount)} {currency} · {day}")
        checks = [balance for balance in ledger.balances if balance.account == number]
        if checks:
            check = max(reversed(checks), key=lambda item: item.day)
            self.bank_summary.setText(comparison(
                ledger.balance_on(number, check.day), check.amount, check.day, currency,
            ))
        entries = sorted((entry for entry in ledger.entries if entry.account == number),
                         key=lambda item: item.day, reverse=True)
        self.grid.setRowCount(len(entries))
        for row, entry in enumerate(entries):
            source = "CSV" if entry.source == "csv" else "Вручную"
            for column, value in enumerate((entry.day, amount_text(entry.amount), entry.description, source)):
                item = QTableWidgetItem(value)
                if column == 1:
                    item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                self.grid.setItem(row, column, item)

    def error(self, exc: Exception) -> None:
        QMessageBox.warning(self, "Операция не выполнена", str(exc))

    def save(self, ledger: Ledger) -> None:
        try:
            self.catalog = self.catalog.save_ledger(ledger)
        except (OSError, ValueError) as exc:
            self.error(exc)
            return
        self.refresh()

    def set_opening(self) -> None:
        number = self.account.currentData()
        asset = self.accounts[number]
        initial = self.catalog.ledger.opening(number)
        known_amount = initial.amount if initial else asset.amount
        values = DetailsDialog(self, "Начальный остаток",
            "Укажите остаток на конец дня перед первой операцией. Реквизит сверяется со столбцом «Счёт» в CSV.",
            (
                ("day", "Дата", initial.day if initial else asset.as_of, "ГГГГ-ММ-ДД"),
                ("amount", f"Остаток, {asset.currency}", amount_text(known_amount) if known_amount is not None else "", "0,00"),
                ("reference", "Реквизит счёта", initial.reference if initial else "", "Как в выписке, необязательно"),
            ),
        ).values()
        if values is None:
            return
        try:
            amount = parse_amount(values["amount"])
            if amount is None:
                raise ValueError("Укажите начальный остаток, в том числе нулевой.")
            ledger = self.catalog.ledger.set_opening(
                Opening(number, values["day"], amount, asset.currency, values["reference"])
            )
        except ValueError as exc:
            self.error(exc)
            return
        self.save(ledger)

    def add_manual(self) -> None:
        number = self.account.currentData()
        values = DetailsDialog(self, "Ручная операция", "Укажите отрицательную сумму для списания.",
            (
                ("day", "Дата", date.today().isoformat(), "ГГГГ-ММ-ДД"),
                ("amount", "Сумма", "", "−300,00"),
                ("description", "Описание", "", "Например, покупка продуктов"),
            ),
        ).values()
        if values is None:
            return
        try:
            amount = parse_amount(values["amount"])
            if not amount:
                raise ValueError("Укажите ненулевую сумму.")
            self.save(self.catalog.ledger.add_manual(Entry(number, values["day"], amount, values["description"])))
        except ValueError as exc:
            self.error(exc)

    def reconcile(self) -> None:
        number = self.account.currentData()
        opening = self.catalog.ledger.opening(number)
        values = DetailsDialog(self, "Сверка остатка", "Укажите остаток из выписки на конец выбранного дня.",
            (
                ("day", "Дата", self.catalog.ledger.latest_balance(number)[1], "ГГГГ-ММ-ДД"),
                ("amount", f"Остаток банка, {opening.currency}", "", "0,00"),
            ),
        ).values()
        if values is None:
            return
        try:
            amount = parse_amount(values["amount"])
            if amount is None:
                raise ValueError("Укажите остаток банка.")
            calculated = self.catalog.ledger.balance_on(number, values["day"])
            QMessageBox.information(self, "Результат сверки", comparison(
                calculated, amount, values["day"], opening.currency,
            ))
        except ValueError as exc:
            self.error(exc)

    def import_files(self) -> None:
        files, _ = QFileDialog.getOpenFileNames(self, "Выберите выписки", "", "Выписки CSV (*.csv)")
        if not files:
            return
        staged = self.catalog.ledger
        accounts = {number: asset for number, asset in self.accounts.items() if staged.opening(number)}
        for index, filename in enumerate(files, start=1):
            try:
                statement = read_statement(Path(filename))
            except (OSError, ValueError) as exc:
                self.error(exc)
                return
            position = (index, len(files)) if len(files) > 1 else None
            dialog = ImportDialog(self, statement, staged, accounts, position=position)
            if dialog.exec() != QDialog.DialogCode.Accepted:
                return
            staged = dialog.result_ledger
        if staged != self.catalog.ledger:
            self.save(staged)
