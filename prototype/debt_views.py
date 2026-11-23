"""Debt register with manual and statement-linked movements."""

from dataclasses import replace
from datetime import date

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QComboBox, QCompleter, QDialog, QHBoxLayout, QLineEdit, QMessageBox, QVBoxLayout, QWidget

from appearance import ChoiceBox, button, label
from assets import amount_text, parse_amount
from debts import DIRECTIONS, ISSUE_SIGN, Debt
from documents import document_deadline
from finance_editors import date_field, field_date, form_dialog
from ledger import Entry
from ledger_views import table
from review_views import fill_table


def due_date(text: str) -> str:
    if not text.strip():
        return ""
    result = document_deadline(text)
    if result is None:
        raise ValueError("Введите срок в формате ДД.ММ.ГГГГ или оставьте поле пустым.")
    return result.isoformat()


class DebtEditor(QDialog):
    def __init__(self, parent, catalog, currency, *, debt=None, operation_number=None):
        super().__init__(parent)
        self.catalog, self.debt = catalog, debt
        self.accounts = {item.asset_number: item for item in catalog.assets if item.category == "account"}
        self.remaining = catalog.review.debts.balances()[debt.number] if debt else 0
        title = "Новый долг" if debt is None else "Записать возврат" if self.remaining else "Записать проценты"
        form, self.error = form_dialog(self, title)
        self.form = form
        self.title = QLineEdit()
        self.title.setPlaceholderText("Имя, банк или короткое название")
        self.direction = ChoiceBox()
        for key, text in DIRECTIONS.items():
            self.direction.addItem(text, key)
        self.mode = ChoiceBox()
        self.mode.addItem("Записать движение на счёте", "manual")
        self.mode.addItem("Привязать операцию из журнала", "link")
        if debt is None:
            self.mode.addItem("Остаток по существующему долгу", "snapshot")
        self.account = ChoiceBox()
        self.link = ChoiceBox()
        self.link.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.link.setMinimumContentsLength(28)
        self.link.setEditable(True)
        self.link.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.link.completer().setFilterMode(Qt.MatchFlag.MatchContains)
        self.link.completer().setCompletionMode(QCompleter.CompletionMode.PopupCompletion)
        self.link.toggle.raise_()
        self.currency = QLineEdit(currency)
        self.currency.setMaxLength(3)
        self.day = date_field(date.today().isoformat())
        self.due = QLineEdit()
        self.due.setPlaceholderText("ДД.ММ.ГГГГ · по желанию")
        self.amount = QLineEdit()
        self.amount.setPlaceholderText("0,00")
        self.principal = QLineEdit()
        self.principal.setPlaceholderText("Пусто — до остатка основного долга")
        self.prior = QLineEdit()
        self.prior.setPlaceholderText("По желанию")
        self.hint = label(style="muted", wrap=True)
        if debt:
            form.addRow("Долг", label(debt.title, "sectionTitle", wrap=True))
        for caption, field in (
            ("Кто / название", self.title), ("Направление", self.direction), ("Как учесть", self.mode),
            ("Счёт", self.account), ("Операция", self.link), ("Валюта", self.currency), ("Дата", self.day),
            ("Сумма платежа" if debt else "Сумма займа", self.amount), ("Из неё основной долг", self.principal),
            ("Срок возврата", self.due), ("Остаток до операции", self.prior), ("", self.hint),
        ):
            form.addRow(caption, field)
        if operation_number is not None:
            entry = catalog.ledger.entries[operation_number - 1]
            if debt is None:
                self.direction.setCurrentIndex(self.direction.findData("borrowed" if entry.amount > 0 else "lent"))
                self.title.setText(entry.recipient)
            self.mode.setCurrentIndex(self.mode.findData("link"))
        elif debt is None and not any(item.status == "active" for item in self.accounts.values()):
            self.mode.setCurrentIndex(self.mode.findData("snapshot"))
        self.mode.currentIndexChanged.connect(self.refresh)
        self.direction.currentIndexChanged.connect(self.refresh)
        self.account.currentIndexChanged.connect(self.refresh_amount)
        self.link.currentIndexChanged.connect(self.refresh_amount)
        self.amount.textChanged.connect(self.refresh_amount)
        self.principal.textChanged.connect(self.refresh_amount)
        self.refresh(operation_number=operation_number)
        (self.amount if debt else self.title).setFocus()

    def direction_key(self):
        return self.debt.direction if self.debt else self.direction.currentData()

    def refresh(self, _index=None, *, operation_number=None):
        mode = self.mode.currentData()
        for field, visible in (
            (self.title, not self.debt), (self.direction, not self.debt), (self.due, not self.debt),
            (self.account, mode == "manual"), (self.link, mode == "link"), (self.currency, mode == "snapshot"),
            (self.day, mode != "link"), (self.amount, mode != "link"), (self.principal, bool(self.debt)),
        ):
            self.form.setRowVisible(field, visible)
        old = self.account.currentData()
        if mode == "manual":
            self.account.blockSignals(True)
            self.account.clear()
            for number, item in self.accounts.items():
                if item.status == "active" and (self.debt is None or item.currency == self.debt.currency):
                    self.account.addItem(f"{item.title} · {item.currency}", number)
            preferred = old
            if preferred is None:
                preferred = next((number for number, item in self.accounts.items() if item.currency == self.currency.text()
                                  and self.account.findData(number) >= 0), None)
            self.account.setCurrentIndex(max(0, self.account.findData(preferred)))
            self.account.blockSignals(False)
        if mode == "link":
            old = operation_number or self.link.currentData()
            self.link.blockSignals(True)
            self.link.clear()
            self.link.addItem("Выберите или найдите операцию", None)
            sign = ISSUE_SIGN[self.direction_key()] * (-1 if self.debt else 1)
            used = self.catalog.review.reserved_operations | {item.operation for item in self.catalog.funds.movements}
            entries = sorted(enumerate(self.catalog.ledger.entries, 1), key=lambda row: (row[1].day, row[0]), reverse=True)
            for number, entry in entries:
                asset = self.accounts[entry.account]
                if (entry.amount * sign > 0 and number not in used
                        and (self.debt is None or (entry.day >= self.debt.day and asset.currency == self.debt.currency))):
                    self.link.addItem(f"{entry.day} · {amount_text(entry.amount)} {asset.currency} · {asset.title} · "
                                      f"{entry.description[:85]} · № {number}", number)
            self.link.setCurrentIndex(max(0, self.link.findData(old)))
            self.link.blockSignals(False)
        self.refresh_amount()

    def split_payment(self, amount):
        principal = parse_amount(self.principal.text())
        return min(amount, self.remaining) if principal is None else principal

    def refresh_amount(self):
        mode = self.mode.currentData()
        account = self.account.currentData() if mode == "manual" else None
        self.form.setRowVisible(self.prior, account is not None and self.catalog.ledger.opening(account) is None)
        if mode == "manual" and account is None:
            self.hint.setText("Добавьте кошелёк в «Кошельках и целях» для записи движения.")
            return
        if not self.debt:
            text = {"link": "Выдача или получение уже отражены в журнале. Остаток счёта сохранится.",
                    "manual": "Счёт изменится на сумму займа. Она не попадёт в доходы или расходы.",
                    "snapshot": "Сохранится текущий долг без движения по счёту."}
            self.hint.setText(text[mode])
            return
        amount = None
        try:
            if mode == "link":
                number = self.link.currentData()
                if number is not None:
                    amount = abs(self.catalog.ledger.entries[number - 1].amount)
            else:
                amount = parse_amount(self.amount.text())
            if amount is not None:
                principal = self.split_payment(amount)
                if 0 <= principal <= amount and principal <= self.remaining:
                    self.hint.setText(f"В счёт долга {amount_text(principal)} · Проценты {amount_text(amount - principal)}")
                    return
        except ValueError:
            pass
        self.hint.setText(f"Осталось вернуть {amount_text(self.remaining)} {self.debt.currency}")

    def save(self):
        try:
            mode, direction = self.mode.currentData(), self.direction_key()
            issued = None
            account = None
            prior = None
            if mode == "link":
                issued = self.link.currentData()
                if issued is None or self.link.currentText() != self.link.itemText(self.link.currentIndex()):
                    raise ValueError("Выберите найденную операцию в списке.")
                entry = self.catalog.ledger.entries[issued - 1]
                amount, day = abs(entry.amount), entry.day
                currency = self.accounts[entry.account].currency
            else:
                amount = parse_amount(self.amount.text())
                if amount is None or amount <= 0:
                    raise ValueError("Введите положительную сумму.")
                day = field_date(self.day)
                if mode == "manual":
                    account = self.account.currentData()
                    if account is None:
                        raise ValueError("Выберите действующий кошелёк.")
                    currency = self.accounts[account].currency
                    if not self.prior.isHidden():
                        prior = parse_amount(self.prior.text())
                else:
                    currency = self.currency.text().strip().upper()
            if self.debt is None:
                debt = Debt(self.title.text().strip(), direction, currency, day, amount,
                            due_date(self.due.text()), issued)
                self.catalog = self.catalog.record_debt(debt, account=account, prior=prior)
            elif mode == "link":
                self.catalog = self.catalog.repay_debt(self.debt.number, self.split_payment(amount), operation_number=issued)
            else:
                title = "Возврат долга · " + self.debt.title
                entry = Entry(account, day, -ISSUE_SIGN[direction] * amount, title, recipient=self.debt.title)
                self.catalog = self.catalog.repay_debt(self.debt.number, self.split_payment(amount), entry=entry, prior=prior)
        except (OSError, ValueError) as exc:
            self.error.setText(str(exc))
            self.error.show()
            return
        self.accept()


class DebtTerms(QDialog):
    def __init__(self, parent, catalog, debt):
        super().__init__(parent)
        self.catalog, self.debt = catalog, debt
        form, self.error = form_dialog(self, "Условия долга")
        self.title = QLineEdit(debt.title)
        self.due = QLineEdit(debt.due)
        self.due.setPlaceholderText("ДД.ММ.ГГГГ · по желанию")
        form.addRow("Кто / название", self.title)
        form.addRow("Срок возврата", self.due)

    def save(self):
        try:
            review = self.catalog.review
            debts = review.debts.update_terms(self.debt.number, self.title.text().strip(), due_date(self.due.text()))
            self.catalog = self.catalog.save_review(review.with_debts(debts))
        except (OSError, ValueError) as exc:
            self.error.setText(str(exc))
            self.error.show()
            return
        self.accept()


class DebtPanel(QWidget):
    catalog_changed = Signal(object)
    report_requested = Signal(int)

    def __init__(self, parent):
        super().__init__(parent)
        self.setObjectName("formContent")
        self.items = ()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 11, 14, 14)
        top = QHBoxLayout()
        self.summary = label(style="sectionTitle", wrap=True)
        top.addWidget(self.summary, 1)
        self.create = button("+ Долг", self.new, primary=True)
        top.addWidget(self.create)
        layout.addLayout(top)
        self.grid = table(("Кто / название", "Направление", "Было", "Осталось", "Срок", "Состояние"), 0)
        self.grid.itemSelectionChanged.connect(self.refresh_actions)
        self.grid.cellDoubleClicked.connect(self.history)
        layout.addWidget(self.grid, 1)
        actions = QHBoxLayout()
        self.empty = label("Добавьте долг, чтобы увидеть остаток и сроки возврата.", "muted", wrap=True)
        actions.addWidget(self.empty)
        actions.addStretch(1)
        self.history_button = button("История…", self.history)
        self.graph_button = button("График", self.open_graph)
        self.terms_button = button("Условия…", self.terms)
        self.pay_button = button("Записать возврат…", self.pay, primary=True)
        for control in (self.history_button, self.graph_button, self.terms_button, self.pay_button):
            actions.addWidget(control)
        layout.addLayout(actions)

    def refresh(self, catalog, currency):
        selected = self.selected()
        self.catalog, self.currency = catalog, currency
        self.remaining = catalog.review.debts.balances()
        today = date.today().isoformat()
        self.items = tuple(sorted((item for item in catalog.review.debts.items if item.currency == currency),
                                  key=lambda item: (not self.remaining[item.number], item.due or "9999", -item.number)))
        self.grid.blockSignals(True)
        fill_table(self.grid, [
            (item.title, "Я должен" if item.direction == "borrowed" else "Мне должны",
             amount_text(item.amount), amount_text(self.remaining[item.number]), item.due or "—",
             "Закрыт" if not self.remaining[item.number] else
             ("Просрочен" if item.due and item.due < today else "Открыт"))
            for item in self.items
        ])
        if self.items:
            index = next((i for i, item in enumerate(self.items) if selected and item.number == selected.number), 0)
            self.grid.selectRow(index)
        self.grid.blockSignals(False)
        mine = sum(self.remaining[item.number] for item in self.items if item.direction == "borrowed")
        theirs = sum(self.remaining[item.number] for item in self.items if item.direction == "lent")
        self.summary.setText(f"Я должен {amount_text(mine)} {currency}     ·     Мне должны {amount_text(theirs)} {currency}")
        self.empty.setVisible(not self.items)
        self.create.setEnabled(catalog.editable)
        self.refresh_actions()

    def selected(self):
        index = self.grid.currentRow()
        return self.items[index] if 0 <= index < len(self.items) else None

    def refresh_actions(self):
        item = self.selected()
        self.history_button.setEnabled(item is not None)
        self.graph_button.setEnabled(item is not None)
        self.terms_button.setEnabled(item is not None and self.catalog.editable)
        self.pay_button.setEnabled(item is not None and self.catalog.editable)
        self.pay_button.setText("Записать возврат…" if item is None or self.remaining[item.number]
                                else "Записать проценты…")

    def new(self, *, operation_number=None):
        dialog = DebtEditor(self, self.catalog, self.currency, operation_number=operation_number)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.catalog_changed.emit(dialog.catalog)

    def pay(self):
        if item := self.selected():
            dialog = DebtEditor(self, self.catalog, item.currency, debt=item)
            if dialog.exec() == QDialog.DialogCode.Accepted:
                self.catalog_changed.emit(dialog.catalog)

    def terms(self):
        if item := self.selected():
            dialog = DebtTerms(self, self.catalog, item)
            if dialog.exec() == QDialog.DialogCode.Accepted:
                self.catalog_changed.emit(dialog.catalog)

    def open_graph(self):
        if item := self.selected():
            self.report_requested.emit(item.number)

    def history(self, *_):
        item = self.selected()
        if item is None:
            return
        review, ledger = self.catalog.review, self.catalog.ledger
        payments = sorted((payment for payment in review.debts.payments if payment.debt == item.number),
                          key=lambda payment: (ledger.entries[payment.operation - 1].day, payment.operation))
        dialog = QDialog(self)
        dialog.setWindowTitle("Движения по долгу")
        dialog.resize(690, 440)
        layout = QVBoxLayout(dialog)
        layout.addWidget(label(item.title, "screenTitle", wrap=True))
        layout.addWidget(label(f"{DIRECTIONS[item.direction]} · {item.day} · {amount_text(item.amount)} {item.currency}", "muted"))
        grid = table(("Дата", "Операция", "Платёж", "Основной долг", "Проценты"), 1)
        fill_table(grid, [
            (entry.day, f"№ {payment.operation} · {entry.description}", amount_text(abs(entry.amount)),
             amount_text(payment.principal), amount_text(abs(entry.amount) - payment.principal))
            for payment in payments for entry in (ledger.entries[payment.operation - 1],)
        ])
        layout.addWidget(grid, 1)
        if payments:
            grid.selectRow(0)
        else:
            layout.addWidget(label("Возвратов пока нет.", "muted"))
        actions = QHBoxLayout()
        actions.addStretch(1)

        def detach():
            index = grid.currentRow()
            if index < 0:
                return
            if QMessageBox.question(dialog, "Отвязать возврат?",
                                    "Операция останется в журнале, а остаток долга будет пересчитан."
                                    ) != QMessageBox.StandardButton.Yes:
                return
            payment = payments[index]
            debts = replace(review.debts, payments=tuple(value for value in review.debts.payments if value != payment))
            try:
                updated = self.catalog.save_review(review.with_debts(debts))
            except (OSError, ValueError) as exc:
                QMessageBox.warning(dialog, "Возврат", str(exc))
                return
            self.catalog_changed.emit(updated)
            dialog.accept()

        def remove_debt():
            if QMessageBox.question(dialog, "Убрать долг из учёта?",
                                    "Карточка долга и её связи удалятся. Операции останутся в журнале."
                                    ) != QMessageBox.StandardButton.Yes:
                return
            debts = replace(review.debts, items=tuple(value for value in review.debts.items if value != item),
                            payments=tuple(value for value in review.debts.payments if value.debt != item.number))
            try:
                updated = self.catalog.save_review(review.with_debts(debts))
            except (OSError, ValueError) as exc:
                QMessageBox.warning(dialog, "Долг", str(exc))
                return
            self.catalog_changed.emit(updated)
            dialog.accept()

        remove_button = button("Убрать долг из учёта…", remove_debt)
        remove_button.setEnabled(self.catalog.editable)
        actions.addWidget(remove_button)
        detach_button = button("Отвязать возврат…", detach)
        detach_button.setEnabled(bool(payments) and self.catalog.editable)
        actions.addWidget(detach_button)
        actions.addWidget(button("Закрыть", dialog.accept))
        layout.addLayout(actions)
        dialog.exec()
