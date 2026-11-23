"""Wallets, manual operations and category management."""

from datetime import date, timedelta

from PySide6.QtCore import QDate, QSize, Qt
from PySide6.QtWidgets import (
    QCheckBox, QDateEdit, QDialog, QFormLayout, QHBoxLayout, QInputDialog, QLineEdit,
    QListWidget, QListWidgetItem, QMessageBox, QVBoxLayout,
)

from appearance import ChoiceBox, button, label
from assets import Asset, amount_text, parse_amount
from finance import Directory
from ledger import Entry
from review import is_business


def date_field(value: str) -> QDateEdit:
    field = QDateEdit(QDate.fromString(value, Qt.DateFormat.ISODate))
    field.setCalendarPopup(True)
    field.setDisplayFormat("dd.MM.yyyy")
    return field


def field_date(field: QDateEdit) -> str:
    return field.date().toString(Qt.DateFormat.ISODate)


def form_dialog(dialog, title, caption="Сохранить"):
    dialog.setWindowTitle(title)
    dialog.setMinimumWidth(510)
    layout = QVBoxLayout(dialog)
    layout.setContentsMargins(22, 18, 22, 18)
    layout.addWidget(label(title, "screenTitle"))
    form = QFormLayout()
    form.setSpacing(8)
    layout.addLayout(form)
    error = label(style="financeError", wrap=True)
    error.hide()
    layout.addWidget(error)
    actions = QHBoxLayout()
    actions.addStretch(1)
    actions.addWidget(button("Отмена", dialog.reject))
    actions.addWidget(button(caption, dialog.save, primary=True))
    layout.addLayout(actions)
    return form, error


class WalletEditor(QDialog):
    def __init__(self, parent, catalog):
        super().__init__(parent)
        self.catalog = catalog
        form, self.error = form_dialog(self, "Новый кошелёк или счёт")
        self.title = QLineEdit()
        self.title.setPlaceholderText("Например, Наличные или Карта")
        self.kind = ChoiceBox()
        self.kind.addItems(("Наличные", "Текущий счёт", "Накопительный счёт", "Вклад", "Счёт ИП"))
        self.owner = QLineEdit("Семья" if catalog.scope == "family" else "Я")
        self.currency = QLineEdit("RUB")
        self.currency.setMaxLength(3)
        self.amount = QLineEdit()
        self.amount.setPlaceholderText("По желанию")
        self.snapshot = date_field((date.today() - timedelta(days=1)).isoformat())
        for text, field in (
            ("Название", self.title), ("Тип", self.kind), ("Владелец", self.owner),
            ("Валюта", self.currency), ("Остаток на конец дня", self.amount), ("Дата остатка", self.snapshot),
        ):
            form.addRow(text, field)
        self.title.setFocus()

    def save(self):
        try:
            amount = parse_amount(self.amount.text())
            account = Asset(self.title.text().strip(), "account", self.owner.text().strip(),
                            kind=self.kind.currentText(), currency=self.currency.text().strip().upper(),
                            amount=amount, as_of=field_date(self.snapshot) if amount is not None else "")
            self.catalog = self.catalog.add_asset(account)
        except (OSError, ValueError) as exc:
            self.error.setText(str(exc))
            self.error.show()
            return
        self.accept()


class OperationEditor(QDialog):
    def __init__(self, parent, catalog, *, kind="expense", account=None, number=None):
        super().__init__(parent)
        self.catalog = catalog
        self.number = number
        self.accounts = {asset.asset_number: asset for asset in catalog.assets
                         if asset.category == "account" and asset.status == "active"}
        old = catalog.ledger.entries[number - 1] if number is not None else None
        assigned = catalog.review.assignments_by_operation().get(number)
        form, self.error = form_dialog(self, "Изменить операцию" if old else "Новая операция")
        self.form = form
        self.kind = ChoiceBox()
        for key, caption in (("expense", "Расход"), ("income", "Доход"), ("transfer", "Перевод между кошельками")):
            if old is None or key != "transfer":
                self.kind.addItem(caption, key)
        self.kind.setCurrentIndex(self.kind.findData(
            ("income" if old.amount > 0 else "expense") if old else kind
        ))
        self.day = date_field(old.day if old else date.today().isoformat())
        self.account = ChoiceBox()
        for key, asset in self.accounts.items():
            self.account.addItem(f"{asset.title} · {asset.currency}", key)
        selected = old.account if old else account
        if selected is not None:
            self.account.setCurrentIndex(self.account.findData(selected))
        self.other = ChoiceBox()
        self.amount = QLineEdit(amount_text(abs(old.amount)) if old else "")
        self.amount.setPlaceholderText("0,00")
        self.fee = QLineEdit()
        self.fee.setPlaceholderText("0,00")
        self.fund = ChoiceBox()
        self.fund.setAccessibleName("Оплата из фонда")
        self.category = ChoiceBox()
        self.category.setEditable(True)
        self.category.toggle.raise_()
        self.description = QLineEdit(old.description if old else "")
        self.recipient = QLineEdit(old.recipient if old else "")
        self.prior = QLineEdit()
        self.other_prior = QLineEdit()
        self.taxable = QCheckBox("Учесть в базе резерва 6%")
        self.taxable.setChecked(bool(assigned and assigned.taxable))
        for caption, field in (
            ("Вид операции", self.kind), ("Дата", self.day), ("Кошелёк", self.account),
            ("Куда", self.other), ("Сумма", self.amount), ("Комиссия", self.fee), ("Оплатить из", self.fund),
            ("Статья или источник", self.category), ("Описание", self.description),
            ("Получатель / отправитель", self.recipient), ("До первой операции", self.prior),
            ("На втором кошельке было", self.other_prior), ("Поступление ИП", self.taxable),
        ):
            form.addRow(caption, field)
        self.kind.currentIndexChanged.connect(self.refresh_kind)
        self.account.currentIndexChanged.connect(self.refresh_accounts)
        self.other.currentIndexChanged.connect(self.refresh_openings)
        self.refresh_kind()
        if assigned:
            self.category.setCurrentText(assigned.category)
        self.amount.setFocus()

    def refresh_kind(self):
        transfer = self.kind.currentData() == "transfer"
        self.form.setRowVisible(self.category, not transfer)
        self.form.setRowVisible(self.recipient, not transfer)
        self.form.setRowVisible(self.fee, transfer)
        self.form.setRowVisible(self.other, transfer)
        direction = 1 if self.kind.currentData() == "income" else -1
        self.category.clear()
        self.category.addItem("")
        self.category.addItems([
            item.name for item in self.catalog.directory.effective(self.catalog.ledger, self.catalog.review)
            if item.direction == direction and not item.archived
        ])
        self.category.lineEdit().setPlaceholderText("Выберите или введите своё название")
        self.refresh_accounts()

    def refresh_accounts(self):
        previous = self.other.currentData()
        self.other.blockSignals(True)
        self.other.clear()
        number = self.account.currentData()
        if number is not None:
            currency = self.accounts[number].currency
            for key, asset in self.accounts.items():
                if key != number and asset.currency == currency:
                    self.other.addItem(f"{asset.title} · {currency}", key)
            if self.other.findData(previous) >= 0:
                self.other.setCurrentIndex(self.other.findData(previous))
        self.other.blockSignals(False)
        fund_number = self.fund.currentData()
        self.fund.clear()
        self.fund.addItem("Свободные деньги", None)
        if number is not None:
            balances = self.catalog.funds.balances()
            for fund in self.catalog.funds.funds:
                if fund.currency == self.accounts[number].currency and balances[fund.number] > 0:
                    self.fund.addItem(f"{fund.title} · {amount_text(balances[fund.number])}", fund.number)
        self.fund.setCurrentIndex(max(0, self.fund.findData(fund_number)))
        self.form.setRowVisible(self.fund, self.kind.currentData() == "expense" and self.number is None
                                and self.fund.count() > 1)
        self.refresh_openings()

    def refresh_openings(self):
        number, other = self.account.currentData(), self.other.currentData()
        transfer = self.kind.currentData() == "transfer"
        self.form.setRowVisible(self.prior, number is not None and self.catalog.ledger.opening(number) is None)
        self.form.setRowVisible(self.other_prior, transfer and other is not None and self.catalog.ledger.opening(other) is None)
        asset = self.accounts.get(number)
        self.form.setRowVisible(self.taxable, self.kind.currentData() == "income" and asset is not None
                                and asset.currency == "RUB" and is_business(asset, self.catalog.scope))
        for number, field in ((number, self.prior), (other, self.other_prior)):
            asset = self.accounts.get(number)
            hint = f"Учесть {amount_text(asset.amount)} на {asset.as_of}" if asset and asset.amount is not None else "0,00, если пусто"
            field.setPlaceholderText(hint)

    def save(self):
        try:
            amount = parse_amount(self.amount.text())
            if amount is None or amount <= 0:
                raise ValueError("Введите положительную сумму операции.")
            account = self.account.currentData()
            day = field_date(self.day)
            kind = self.kind.currentData()
            prior = parse_amount(self.prior.text()) if not self.prior.isHidden() else None
            if kind == "transfer":
                other = self.other.currentData()
                if other is None:
                    raise ValueError("Добавьте второй кошелёк в той же валюте для перевода.")
                fee = parse_amount(self.fee.text()) or 0
                if fee < 0:
                    raise ValueError("Комиссия должна быть неотрицательной.")
                description = self.description.text().strip() or "Перевод между кошельками"
                other_prior = parse_amount(self.other_prior.text()) if not self.other_prior.isHidden() else None
                self.catalog = self.catalog.record_transfer(
                    Entry(account, day, -(amount + fee), description), Entry(other, day, amount, description),
                    debit_prior=prior, credit_prior=other_prior,
                )
            else:
                category = self.category.currentText().strip()
                description = self.description.text().strip() or category or ("Доход" if kind == "income" else "Расход")
                taxable = not self.taxable.isHidden() and self.taxable.isChecked()
                if taxable and not category:
                    raise ValueError("Выберите источник облагаемого дохода.")
                entry = Entry(account, day, amount if kind == "income" else -amount, description,
                              recipient=self.recipient.text().strip())
                self.catalog = self.catalog.record_manual(entry, category, prior=prior,
                                                          number=self.number, taxable=taxable,
                                                          fund=None if self.fund.isHidden() else self.fund.currentData())
        except (OSError, ValueError) as exc:
            self.error.setText(str(exc))
            self.error.show()
            return
        self.accept()


class DirectoryDialog(QDialog):
    def __init__(self, parent, catalog):
        super().__init__(parent)
        self.catalog = catalog
        self.setWindowTitle("Справочники")
        self.resize(620, 545)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(21, 18, 21, 18)
        layout.addWidget(label("Источники и статьи", "screenTitle"))
        self.direction = ChoiceBox()
        self.direction.addItem("Источники доходов", 1)
        self.direction.addItem("Статьи расходов", -1)
        layout.addWidget(self.direction)
        self.items = QListWidget()
        self.items.setAccessibleName("Справочник")
        layout.addWidget(self.items, 1)
        actions = QHBoxLayout()
        for name, callback in (("+ Добавить", self.add), ("Переименовать…", self.rename),
                               ("В архив / вернуть", self.toggle)):
            action = button(name, callback, primary=name.startswith("+"))
            action.setEnabled(catalog.editable)
            actions.addWidget(action)
        actions.addStretch(1)
        actions.addWidget(button("Готово", self.accept))
        layout.addLayout(actions)
        self.direction.currentIndexChanged.connect(self.refresh)
        self.refresh()

    def refresh(self):
        direction = self.direction.currentData()
        self.choices = [category for category in self.catalog.directory.effective(self.catalog.ledger, self.catalog.review)
                        if category.direction == direction]
        self.items.clear()
        for category in self.choices:
            item = QListWidgetItem(category.name + (" · в архиве" if category.archived else ""))
            item.setSizeHint(QSize(0, 33))
            self.items.addItem(item)
        if self.choices:
            self.items.setCurrentRow(0)

    def add(self):
        self.change()

    def rename(self):
        if self.items.currentRow() >= 0:
            self.change(self.choices[self.items.currentRow()])

    def toggle(self):
        if self.items.currentRow() >= 0:
            item = self.choices[self.items.currentRow()]
            self.save(item.name, previous=item.name, archived=not item.archived)

    def change(self, item=None):
        name, accepted = QInputDialog.getText(self, "Название статьи", "Название",
                                                text=item.name if item else "")
        if accepted:
            self.save(name, previous=item.name if item else "", archived=item.archived if item else False)

    def save(self, name, *, previous="", archived=False):
        try:
            directory, review = self.catalog.directory.edit(
                self.catalog.ledger, self.catalog.review, self.direction.currentData(),
                name, previous=previous, archived=archived,
            )
            self.catalog = self.catalog.save_directory(directory, review)
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, "Справочники", str(exc))
            return
        self.refresh()
