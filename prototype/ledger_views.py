"""Account operations, statement review and balance comparison."""

from collections import Counter
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView, QDialog, QFileDialog, QFormLayout, QHBoxLayout, QHeaderView,
    QLineEdit, QMenu, QMessageBox, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from appearance import ChoiceBox, button, label
from assets import Asset, amount_text, parse_amount
from bank_pdf import read_pdf_statements
from documents import Catalog
from finance import apply_statement
from finance_editors import OperationEditor, WalletEditor
from ledger import (
    NEEDED, Ledger, Opening, Statement, _statement_day, prepare_statement_account,
    preview_import, read_statement, reference_matches,
)
from operation_controls import TAG_NAMES, TagCells, category_menu, choose_tags, selecting_rows
from transfers import matching_account, own_transfer_hint, signed_amount, suggested_transfers, transfer_route

FIELDS = {
    "date": "Дата", "amount": "Сумма со знаком", "description": "Описание",
    "identifier": "ID операции", "currency": "Валюта", "account": "Реквизит счёта",
    "balance": "Остаток банка", "recipient": "Получатель",
    "bank_category": "Категория банка", "counterparty": "Счёт контрагента",
}
STATES = {"new": "Новая", "duplicate": "Повтор", "review": "На сверку", "error": "Ошибка"}
NEW_ACCOUNT = "new"


def statement_account_title(statement: Statement) -> str:
    title = statement.account_label or statement.bank
    return f"{title} · •{statement.opening_hint[3][-4:]}"


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
    widget.horizontalHeader().setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
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
    STATUS, DATE, DESCRIPTION, AMOUNT, KIND, ARTICLE, TAGS = range(7)

    def __init__(self, parent, statement: Statement, catalog: Catalog, *, position=None, batch=()):
        super().__init__(parent)
        self.statement = statement
        self.catalog = catalog
        self.initial_catalog = catalog
        self.result_catalog = catalog
        self.is_pdf = statement.opening_hint is not None
        self.new_pdf_account = None
        self.batch = batch
        self.preview = None
        self.articles = {}
        self.tags = {}
        self.suggestions = {}
        self.transfer_choices = {}
        self.transfer_defaults = {}
        self.maybe_transfers = {}
        self.transfer_decisions = {}
        self.transfer_intents = {}
        self.pending_openings = {}
        self.setWindowTitle("Импорт выписки")
        width, height = (1110, 700) if statement.rows else (690, 250)
        self.resize(width, height)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 16, 20, 16)
        layout.addWidget(label(statement.name, "screenTitle", wrap=True))
        if self.is_pdf:
            _, _, currency, reference = statement.opening_hint
            layout.addWidget(label(f"{statement.bank} · {statement_account_title(statement)} · {currency}"
                                   if statement.account_label else
                                   f"{statement.bank} · Счёт •{reference[-4:]} · {currency}", "muted"))
        if position is not None:
            number, total = position
            layout.addWidget(label(
                f"Раздел {number} из {total} · Счета и операции сохранятся после подтверждения всех разделов",
                "muted", wrap=True,
            ))

        account_row = QHBoxLayout()
        self.account = ChoiceBox()
        self.account.setAccessibleName("Счёт для выписки")
        account_row.addWidget(self.account, 1)
        self.opening_button = button("Указать начальный остаток…", self.set_csv_opening)
        self.opening_button.hide()
        account_row.addWidget(self.opening_button)
        layout.addLayout(account_row)

        self.columns = {}
        self.suggested = statement.suggested_columns()
        if not self.is_pdf:
            self.mapping_panel = QWidget()
            mapping = QHBoxLayout(self.mapping_panel)
            mapping.setContentsMargins(0, 0, 0, 0)
            left, right = QFormLayout(), QFormLayout()
            mapping.addLayout(left, 1)
            mapping.addLayout(right, 1)
            for index, (key, title) in enumerate(FIELDS.items()):
                form = left if index < 5 else right
                combo = ChoiceBox()
                combo.setAccessibleName(title)
                combo.addItem("Не выбрано", None)
                for column, header in enumerate(statement.columns):
                    combo.addItem(f"{header or 'Столбец'} · {column + 1}", column)
                if key in self.suggested:
                    combo.setCurrentIndex(combo.findData(self.suggested[key]))
                combo.currentIndexChanged.connect(self.build_preview)
                self.columns[key] = combo
                form.addRow(title, combo)
            change_columns = button("Столбцы CSV", lambda checked: self.mapping_panel.setVisible(checked))
            change_columns.setCheckable(True)
            show_columns = not all(key in self.suggested for key in NEEDED)
            change_columns.setChecked(show_columns)
            self.mapping_panel.setVisible(show_columns)
            account_row.addWidget(change_columns)
            layout.addWidget(self.mapping_panel)

        controls_panel = QWidget()
        controls = QHBoxLayout(controls_panel)
        controls.setContentsMargins(0, 0, 0, 0)
        self.filter = ChoiceBox()
        for key, title in (("all", "Все строки"), ("unclassified", "Без статьи"), ("expense", "Списания"),
                           ("income", "Поступления"), ("transfer", "Переводы и подсказки"),
                           ("review", "На сверку и с ошибками")):
            self.filter.addItem(title, key)
        controls.addWidget(self.filter)
        controls.addStretch(1)
        self.suggest_button = button("Принять подсказки", self.accept_suggestions)
        controls.addWidget(self.suggest_button)
        self.article_button = button("Статья…", self.selected_article)
        controls.addWidget(self.article_button)
        self.transfer_button = button("Перевод…", self.selected_transfer)
        controls.addWidget(self.transfer_button)
        self.tags_button = button("Теги…", self.selected_tags)
        controls.addWidget(self.tags_button)
        self.whole_tag_button = button("Тег на всю выписку…", self.whole_tags)
        controls.addWidget(self.whole_tag_button)
        layout.addWidget(controls_panel)
        controls_panel.setVisible(bool(statement.rows))

        self.grid = table(("Статус", "Дата", "Описание", "Сумма", "Вид  ▾", "Статья / направление", "Теги  ▾"),
                          self.DESCRIPTION)
        self.grid.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.grid.setItemDelegateForColumn(self.TAGS, TagCells(self.grid))
        for column, width in ((self.KIND, 115), (self.ARTICLE, 245), (self.TAGS, 114)):
            self.grid.horizontalHeader().setSectionResizeMode(column, QHeaderView.ResizeMode.Interactive)
            self.grid.setColumnWidth(column, width)
        self.grid.setAccessibleName("Операции выписки")
        self.grid.setToolTip("Нажмите «Вид», чтобы отметить перевод, или выберите статью и теги.")
        self.grid.itemChanged.connect(self.status_changed)
        self.grid.cellClicked.connect(self.cell_clicked)
        self.grid.itemSelectionChanged.connect(self.refresh_buttons)
        self.shortcut = QShortcut(QKeySequence("F2"), self.grid)
        self.shortcut.activated.connect(self.selected_article)
        layout.addWidget(self.grid, 1)
        self.grid.setVisible(bool(statement.rows))
        self.summary = label(style="sectionTitle", wrap=True)
        self.setup = label(style="muted", wrap=True)
        self.balance = label(style="muted", wrap=True)
        for notice in (self.summary, self.setup, self.balance):
            layout.addWidget(notice)
        actions = QHBoxLayout()
        actions.addStretch(1)
        actions.addWidget(button("Отменить импорт", self.reject))
        caption = "Далее" if position is not None and position[0] < position[1] else "Подтвердить импорт"
        self.apply_button = button(caption, self.apply, primary=True)
        actions.addWidget(self.apply_button)
        layout.addLayout(actions)

        self.filter.currentIndexChanged.connect(self.refresh_filter)
        self.account.currentIndexChanged.connect(self.account_changed)
        self.fill_accounts()
        self.build_preview()

    def fill_accounts(self, selected=None):
        currency = self.statement.opening_hint[2] if self.is_pdf else None
        self.accounts = {asset.asset_number: asset for asset in self.catalog.assets
                         if asset.category == "account" and (currency is None or asset.currency == currency)}
        if selected is None:
            reference = self.statement.reference(self.suggested)
            matches = [item.account for item in self.catalog.ledger.openings
                       if item.account in self.accounts and reference_matches(reference, item.reference)]
            if len(matches) == 1:
                selected = matches[0]
            elif self.is_pdf and not matches:
                if self.new_pdf_account is None:
                    kind = ("Накопительный счёт" if "копилка" in self.statement.account_label.casefold()
                            else "Текущий счёт")
                    owner = "Семья" if self.catalog.scope == "family" else "Я"
                    self.catalog = self.catalog.stage_asset(Asset(
                        statement_account_title(self.statement), "account", owner, kind=kind,
                        organization=self.statement.bank, currency=currency,
                    ))
                    asset = self.catalog.assets[-1]
                    self.accounts[asset.asset_number] = asset
                    self.new_pdf_account = asset.asset_number
                selected = self.new_pdf_account
            elif len(self.accounts) == 1 and not reference:
                selected = next(iter(self.accounts))
        self.account.blockSignals(True)
        self.account.clear()
        self.account.addItem("Выберите счёт" if self.accounts else "Создайте счёт для выписки", None)
        for number, asset in self.accounts.items():
            state = " · будет создан" if number == self.new_pdf_account else ""
            self.account.addItem(f"{asset.title} · {asset.owner} · {asset.currency}{state}", number)
        if not self.is_pdf:
            self.account.insertSeparator(self.account.count())
            self.account.addItem("+ Новый счёт…", NEW_ACCOUNT)
        self.account.setCurrentIndex(max(0, self.account.findData(selected)))
        self.account.blockSignals(False)
        self.selected_account = selected

    def mapping(self):
        if self.is_pdf:
            return self.suggested
        return {key: choice.currentData() for key, choice in self.columns.items() if choice.currentData() is not None}

    def csv_snapshot_day(self):
        column = self.mapping().get("date")
        if column is None:
            return ""
        days = []
        for _, values in self.statement.rows:
            try:
                days.append(date.fromisoformat(_statement_day(values[column])))
            except (ValueError, IndexError):
                continue
        earliest = min(days, default=date.min)
        return (earliest - timedelta(days=1)).isoformat() if earliest > date.min else ""

    def account_changed(self):
        if self.account.currentData() == NEW_ACCOUNT:
            dialog = WalletEditor(self, self.catalog, stage=True, opening_required=True,
                                  snapshot_day=self.csv_snapshot_day())
            if dialog.exec() != QDialog.DialogCode.Accepted:
                self.fill_accounts(self.selected_account)
                return
            self.catalog = dialog.catalog
            asset = self.catalog.assets[-1]
            self.pending_openings[asset.asset_number] = Opening(
                asset.asset_number, asset.as_of, asset.amount, asset.currency, self.statement.reference(self.mapping())
            )
            self.fill_accounts(asset.asset_number)
        self.selected_account = self.account.currentData()
        self.build_preview()

    def set_csv_opening(self):
        asset = self.accounts[self.account.currentData()]
        values = DetailsDialog(self, "Начальный остаток", "Остаток на конец дня перед первой операцией.",
            (
                ("day", "Дата", asset.as_of or self.csv_snapshot_day(), "ГГГГ-ММ-ДД"),
                ("amount", f"Остаток, {asset.currency}", amount_text(asset.amount) if asset.amount is not None else "", "0,00"),
            )
        ).values()
        if values is None:
            return
        try:
            amount = parse_amount(values["amount"])
            if amount is None:
                raise ValueError("Укажите начальный остаток, в том числе нулевой.")
            day = date.fromisoformat(values["day"]).isoformat()
            if day != values["day"]:
                raise ValueError("Введите дату в формате ГГГГ-ММ-ДД.")
        except ValueError as exc:
            QMessageBox.warning(self, "Начальный остаток", str(exc))
            return
        self.pending_openings[asset.asset_number] = Opening(
            asset.asset_number, day, amount, asset.currency, self.statement.reference(self.mapping())
        )
        self.build_preview()

    def build_preview(self):
        self.preview = None
        self.articles.clear()
        self.tags.clear()
        self.suggestions.clear()
        self.transfer_defaults.clear()
        self.maybe_transfers.clear()
        self.transfer_decisions.clear()
        self.grid.setRowCount(0)
        self.setup.clear()
        self.balance.clear()
        self.apply_button.setEnabled(False)
        self.opening_button.hide()
        self.refresh_buttons()
        account = self.account.currentData()
        if account is None:
            self.summary.setText("Выберите или создайте счёт для выписки.")
            return
        try:
            staged = self.catalog.ledger
            if account in self.pending_openings:
                staged = staged.set_opening(self.pending_openings[account])
            if not self.is_pdf and staged.opening(account) is None:
                self.opening_button.show()
                self.summary.setText("Для CSV нужен начальный остаток выбранного счёта.")
                return
            staged = prepare_statement_account(staged, self.accounts[account], self.statement, self.mapping())
            preview = preview_import(staged, account, self.statement, self.mapping())
        except ValueError as exc:
            self.summary.setText(str(exc))
            return
        self.preview, self.preview_ledger = preview, staged
        self.transfer_choices = self.other_accounts(account)
        if self.catalog.ledger.opening(account) is None:
            opening = staged.opening(account)
            self.setup.setText(f"Начальный остаток на {opening.day} · {amount_text(opening.amount)} {opening.currency}")
        self.grid.blockSignals(True)
        self.grid.setRowCount(len(preview.rows))
        for index, row in enumerate(preview.rows):
            status = QTableWidgetItem(STATES[row.state])
            status.setToolTip(f"Строка {row.line}" + (f" · {row.message}" if row.message else ""))
            if row.state == "review":
                status.setCheckState(Qt.CheckState.Unchecked)
                status.setToolTip(status.toolTip() + " · Отметьте, если это отдельная операция.")
            self.grid.setItem(index, self.STATUS, status)
            entry = row.entry
            if entry and row.state in ("new", "review"):
                matches = [
                    key for key, (_, reference) in self.transfer_choices.items()
                    if reference_matches(entry.counterparty, reference)
                ]
                if len(matches) == 1:
                    key = matches[0]
                    if "*" not in entry.counterparty and "*" not in self.transfer_choices[key][1]:
                        self.transfer_defaults[row.line] = key
                    else:
                        self.maybe_transfers[row.line] = key
                elif own_transfer_hint(entry.description):
                    self.maybe_transfers[row.line] = None
                suggestion = self.catalog.directory.suggestion(self.catalog.review, entry)
                if suggestion:
                    self.suggestions[row.line] = suggestion
            values = (entry.day if entry else "", " · ".join(filter(None, (entry.description if entry else "", row.message))),
                      signed_amount(entry.amount) if entry else "", "", "", "")
            for column, value in enumerate(values, start=1):
                item = QTableWidgetItem(value)
                item.setToolTip(value)
                if column == self.AMOUNT:
                    item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                self.grid.setItem(index, column, item)
            self.paint_row(index)
        self.grid.blockSignals(False)
        self.show_balance()
        self.refresh_filter()

    def other_accounts(self, account):
        origin = self.preview_ledger.opening(account)
        openings = self.preview_ledger.openings
        reference_by_number = {item.account: item.reference for item in openings}
        choices = {
            asset.asset_number: (asset.title, reference_by_number.get(asset.asset_number, ""))
            for asset in self.initial_catalog.assets
            if asset.category == "account" and asset.currency == origin.currency and asset.asset_number != account
        }
        for statement in self.batch:
            if statement.opening_hint is None:
                continue
            _, _, currency, reference = statement.opening_hint
            if (
                currency != origin.currency or reference_matches(reference, origin.reference)
                or any(item.currency == currency and reference_matches(reference, item.reference) for item in openings)
                or any(reference_matches(reference, key) for key in choices if isinstance(key, str))
            ):
                continue
            choices[reference] = statement_account_title(statement), reference
        return choices

    def transfer_for(self, line):
        return self.transfer_decisions.get(line, self.transfer_defaults.get(line))

    def looks_like_transfer(self, line):
        return line not in self.transfer_decisions and line in self.maybe_transfers

    def included(self, index):
        row = self.preview.rows[index]
        return row.state == "new" or (row.state == "review" and
               self.grid.item(index, self.STATUS).checkState() == Qt.CheckState.Checked)

    def confirmed_lines(self):
        return {row.line for index, row in enumerate(self.preview.rows) if row.state == "review" and self.included(index)}

    def status_changed(self, item):
        if self.preview is None or item.column() != self.STATUS:
            return
        self.paint_row(item.row())
        self.show_balance()
        self.refresh_filter()

    def paint_row(self, index):
        row = self.preview.rows[index]
        can_edit = self.included(index)
        suggestion = self.suggestions.get(row.line) if row.line not in self.articles else None
        category = self.articles.get(row.line)
        caption = category or (f"≈ {suggestion.category}" if suggestion else "Выбрать")
        kind, article, tags = (self.grid.item(index, column) for column in (self.KIND, self.ARTICLE, self.TAGS))
        target = self.transfer_for(row.line)
        hint = self.looks_like_transfer(row.line)
        if can_edit:
            movement = "Доход" if row.entry.amount > 0 else "Расход"
            if target is not None:
                movement = "Перевод"
            elif hint:
                movement = "Проверить"
            kind.setText(f"{movement}  ▾")
            kind.setToolTip("Перевод по реквизитам счета" if row.line in self.transfer_defaults
                            and row.line not in self.transfer_decisions else "Выбрать вид операции")
            if target is not None:
                caption = self.route(row, target)
            elif hint:
                caption = "Возможный перевод"
            kind.setForeground(QColor("#146951"))
        else:
            kind.setText("—")
        tag_text = ", ".join(self.tags.get(row.line, ()))
        article.setText((caption if target is not None else f"{caption}  ▾") if can_edit else "—")
        article.setToolTip((caption if target is not None or hint else suggestion.reason if suggestion
                            else category or "Выбрать статью") if can_edit else "")
        article.setForeground(QColor("#146951" if can_edit and (target is not None or not suggestion) else "#53657D"))
        tags.setText(f"{tag_text or '+ Тег'}  ▾" if can_edit else "—")
        tags.setData(TAG_NAMES, self.tags.get(row.line, ()) if can_edit else ())
        tags.setToolTip(tag_text or ("Изменить теги" if can_edit else ""))
        tags.setForeground(QColor("#146951" if can_edit else "#53657D"))

    def refresh_filter(self):
        if self.preview is None:
            return
        choice = self.filter.currentData()
        for index, row in enumerate(self.preview.rows):
            is_transfer = self.transfer_for(row.line) is not None
            visible = (
                choice == "all" or (choice == "unclassified" and self.included(index) and not is_transfer
                                    and not self.articles.get(row.line))
                or (choice == "transfer" and (is_transfer or self.looks_like_transfer(row.line)))
                or (choice == "review" and row.state in ("review", "error"))
                or (row.entry is not None and (choice == "expense" and row.entry.amount < 0
                                               or choice == "income" and row.entry.amount > 0))
            )
            self.grid.setRowHidden(index, not visible)
        self.refresh_buttons()

    def selected(self):
        if self.preview is None:
            return ()
        return tuple(index.row() for index in self.grid.selectionModel().selectedRows()
                     if not self.grid.isRowHidden(index.row()) and self.included(index.row()))

    def refresh_buttons(self):
        chosen = self.selected()
        directions = {self.preview.rows[index].entry.amount > 0 for index in chosen}
        included = tuple(row for index, row in enumerate(self.preview.rows) if self.included(index)) if self.preview else ()
        hints = sum(self.can_accept_article(row.line) for row in included)
        as_transfer = any(self.transfer_for(self.preview.rows[index].line) is not None for index in chosen)
        self.article_button.setEnabled(len(directions) == 1 and not as_transfer)
        self.transfer_button.setEnabled(bool(chosen))
        self.tags_button.setEnabled(bool(chosen))
        self.whole_tag_button.setEnabled(bool(included))
        self.suggest_button.setEnabled(hints > 0)
        self.suggest_button.setText(f"Принять подсказки · {hints}" if hints else "Принять подсказки")
        if self.preview is None:
            return
        if not self.statement.rows:
            self.summary.setText("За этот период на счёте не было операций.")
            self.apply_button.setEnabled(True)
            return
        counts = Counter(row.state for row in self.preview.rows)
        transfers = sum(self.transfer_for(row.line) is not None for row in included)
        remaining = sum(self.transfer_for(row.line) is None and not self.articles.get(row.line) for row in included)
        summary = [f"К добавлению {len(included)}", f"без статьи {remaining}"]
        if transfers:
            summary.append(f"переводы {transfers}")
        summary.extend(f"{title} {counts[key]}" for key, title in (("duplicate", "повторы"), ("review", "на сверку"),
                                                                   ("error", "ошибки")) if counts[key])
        self.summary.setText(" · ".join(summary))
        self.apply_button.setEnabled(not self.is_pdf or not counts["error"])

    def show_balance(self):
        check = self.preview.balance
        if check is not None:
            staged = self.preview.apply(self.preview_ledger, self.confirmed_lines())
            self.balance.setText(comparison(staged.balance_on(check.account, check.day), check.amount, check.day,
                                            self.accounts[check.account].currency))
        elif self.preview.balance_warning:
            self.balance.setText(self.preview.balance_warning)
        elif any(row.state == "review" for row in self.preview.rows):
            self.balance.setText("Отметьте совпадение, если это отдельная операция.")

    def article_at(self, indices, point):
        if not indices:
            return
        rows = [self.preview.rows[index] for index in indices]
        first = rows[0]
        proposal = self.suggestions.get(first.line) if first.line not in self.articles else None
        result = category_menu(self, point, self.catalog, 1 if first.entry.amount > 0 else -1,
                               self.articles.get(first.line, ""), suggestion=proposal.category if proposal else "")
        if result is None:
            return
        name, directory = result
        self.catalog = replace(self.catalog, directory=directory)
        for index, row in zip(indices, rows):
            self.articles[row.line] = name
            if row.line in self.maybe_transfers or row.line in self.transfer_defaults:
                self.transfer_decisions[row.line] = None
            self.paint_row(index)
        self.refresh_filter()

    def selected_article(self):
        indices = self.selected()
        if self.article_button.isEnabled():
            point = self.article_button.mapToGlobal(self.article_button.rect().bottomLeft())
            self.article_at(indices, point)

    def accept_suggestions(self):
        for index, row in enumerate(self.preview.rows):
            if self.included(index) and self.can_accept_article(row.line):
                self.articles[row.line] = self.suggestions[row.line].category
                self.paint_row(index)
        self.refresh_filter()

    def can_accept_article(self, line):
        return (line in self.suggestions and line not in self.articles and self.transfer_for(line) is None
                and not self.looks_like_transfer(line))

    def route(self, row, target):
        title = self.accounts[row.entry.account].title
        return transfer_route(row.entry.amount, title, self.transfer_choices[target][0])

    def selected_transfer(self):
        self.choose_transfer(self.selected())

    def choose_transfer(self, indices):
        if not indices:
            return
        first = self.preview.rows[indices[0]]
        dialog = QDialog(self)
        dialog.setWindowTitle("Перевод между своими счетами")
        dialog.setMinimumWidth(520)
        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.addWidget(label(dialog.windowTitle(), "screenTitle"))
        if len(indices) == 1:
            title = self.accounts[first.entry.account].title
            currency = self.accounts[first.entry.account].currency
            direction = "Списание со счёта" if first.entry.amount < 0 else "Зачисление на счёт"
            caption = f"{direction} «{title}» · {signed_amount(first.entry.amount)} {currency} · {first.entry.day}"
        else:
            caption = f"Выбрано операций · {len(indices)}"
        layout.addWidget(label(caption, "muted", wrap=True))
        account = ChoiceBox()
        account.addItem("Выберите второй счёт", None)
        for key, (title, _) in self.transfer_choices.items():
            account.addItem(title, key)
        target = self.transfer_for(first.line)
        if target is None:
            target = self.maybe_transfers.get(first.line)
        if target is None and len(self.transfer_choices) == 1:
            target = next(iter(self.transfer_choices))
        account.setCurrentIndex(max(0, account.findData(target)))
        account.setAccessibleName("Второй счёт")
        layout.addWidget(account)
        direction = label(style="sectionTitle", wrap=True)
        layout.addWidget(direction)
        actions = QHBoxLayout()
        actions.addStretch(1)
        actions.addWidget(button("Отмена", dialog.reject))
        apply = button("Выбрать счёт", dialog.accept, primary=True)
        actions.addWidget(apply)
        layout.addLayout(actions)

        def refresh():
            key = account.currentData()
            apply.setEnabled(key is not None)
            if key is not None:
                directions = dict.fromkeys(self.route(self.preview.rows[index], key) for index in indices)
                direction.setText("\n".join(directions))
            elif self.transfer_choices:
                direction.setText("Выберите второй счёт, чтобы увидеть направление.")
            else:
                direction.setText("Добавьте второй счёт или выберите обе выписки для импорта.")

        account.currentIndexChanged.connect(refresh)
        refresh()
        if dialog.exec() == QDialog.DialogCode.Accepted and account.currentData() is not None:
            for index in indices:
                row = self.preview.rows[index]
                self.transfer_decisions[row.line] = account.currentData()
                self.articles.pop(row.line, None)
                self.paint_row(index)
            self.refresh_filter()

    def change_kind(self, index):
        row = self.preview.rows[index]
        menu = QMenu(self)
        ordinary = menu.addAction("Доход" if row.entry.amount > 0 else "Расход")
        transfer = menu.addAction("Перевод между своими счетами…")
        rect = self.grid.visualItemRect(self.grid.item(index, self.KIND))
        choice = menu.exec(self.grid.viewport().mapToGlobal(rect.bottomLeft()))
        if choice == transfer:
            self.choose_transfer((index,))
        elif choice == ordinary:
            self.transfer_decisions[row.line] = None
            self.paint_row(index)
            self.refresh_filter()

    def edit_tags(self, indices):
        if not indices:
            return
        lines = [self.preview.rows[index].line for index in indices]
        known = tuple(dict.fromkeys((*self.catalog.review.available_tags(),
                                    *(name for values in self.tags.values() for name in values))))
        initial = self.tags.get(lines[0], ()) if len(lines) == 1 else ()
        result = choose_tags(self, known=known, initial=initial, count=len(lines))
        if result is None:
            return
        for index, line in zip(indices, lines):
            original = self.tags.get(line, ()) if len(lines) > 1 else ()
            self.tags[line] = tuple(dict.fromkeys((*original, *result)))
            self.paint_row(index)

    def selected_tags(self):
        self.edit_tags(self.selected())

    def whole_tags(self):
        self.edit_tags(tuple(index for index in range(len(self.preview.rows)) if self.included(index)))

    def cell_clicked(self, index, column):
        if self.preview is None or not self.included(index) or selecting_rows():
            return
        if column == self.KIND:
            self.change_kind(index)
        elif column == self.ARTICLE:
            row = self.preview.rows[index]
            if self.transfer_for(row.line) is not None or self.looks_like_transfer(row.line):
                self.change_kind(index)
                return
            rect = self.grid.visualItemRect(self.grid.item(index, column))
            self.article_at((index,), self.grid.viewport().mapToGlobal(rect.bottomLeft()))
        elif column == self.TAGS:
            self.edit_tags((index,))

    def apply(self):
        if self.preview is None or not self.apply_button.isEnabled():
            return
        if any(row.state == "error" for row in self.preview.rows):
            reply = QMessageBox.question(self, "В выписке есть ошибки", "Импортировать допустимые строки?")
            if reply != QMessageBox.StandardButton.Yes:
                return
        confirmed = self.confirmed_lines()
        ledger, review = apply_statement(self.preview_ledger, self.catalog.review, self.preview, confirmed,
                                         self.articles, self.tags)
        posted = self.preview.accepted_rows(confirmed)
        if len(ledger.entries) > len(self.preview_ledger.entries):
            numbers = {row.line: number for number, row in enumerate(posted, start=len(self.preview_ledger.entries) + 1)}
            self.transfer_intents = {number: target for line, number in numbers.items()
                                     if (target := self.transfer_for(line)) is not None}
            ordinary = tuple(number for line, number in numbers.items()
                             if line in self.transfer_decisions and self.transfer_decisions[line] is None)
            review = review.keep_ordinary(ordinary)
        assets = self.initial_catalog.assets
        if self.preview.account not in {asset.asset_number for asset in assets} and ledger.opening(self.preview.account):
            assets = (*assets, self.accounts[self.preview.account])
        if self.statement.bank:
            assets = tuple(replace(asset, organization=self.statement.bank)
                           if asset.asset_number == self.preview.account and not asset.organization else asset
                           for asset in assets)
        self.result_catalog = replace(self.catalog, assets=assets, ledger=ledger, review=review)
        self.accept()


def import_statements(parent, catalog: Catalog) -> Catalog:
    files, _ = QFileDialog.getOpenFileNames(parent, "Выберите выписки", "", "Выписки (*.pdf *.csv *.PDF *.CSV)")
    if not files:
        return catalog
    try:
        statements = []
        for filename in files:
            path = Path(filename)
            if path.suffix.lower() == ".pdf":
                statements.extend(read_pdf_statements(path))
            else:
                statements.append(read_statement(path))
        statements.sort(key=lambda item: item.opening_hint[0] if item.opening_hint else "9999")
        staged = catalog
        intents = {}
        for index, statement in enumerate(statements, start=1):
            position = (index, len(statements)) if len(statements) > 1 else None
            dialog = ImportDialog(parent, statement, staged, position=position, batch=statements)
            if dialog.exec() != QDialog.DialogCode.Accepted:
                return catalog
            staged = dialog.result_catalog
            intents.update(dialog.transfer_intents)
        if len(staged.ledger.entries) > len(catalog.ledger.entries):
            staged = review_import_transfers(parent, staged, len(catalog.ledger.entries), intents)
        if staged != catalog:
            return catalog.save_import(staged)
    except (OSError, ValueError) as exc:
        QMessageBox.warning(parent, "Импорт выписок", str(exc))
    return catalog


def review_import_transfers(parent, catalog, previous_count, intents):
    from review_views import confirm_transfer_matches

    ledger, review = catalog.ledger, catalog.review
    for number, target in intents.items():
        entry = ledger.entries[number - 1]
        if isinstance(target, str):
            target = matching_account(target, ledger.opening(entry.account).currency, ledger.openings)
        if target is None:
            raise ValueError("Не удалось сопоставить выбранный счёт перевода с выпиской.")
        review = review.wait_for_transfer(ledger, number, target)
    matches = suggested_transfers(
        ledger, review, catalog.assets, excluded=(move.operation for move in catalog.funds.movements),
        touched=range(previous_count + 1, len(ledger.entries) + 1),
    )
    for item in matches:
        if item.automatic:
            review = review.link_transfer(ledger, item.debit, item.credit)
    staged = replace(catalog, review=review)
    candidates = tuple(item for item in matches if not item.automatic)
    if candidates:
        staged = replace(staged, review=confirm_transfer_matches(parent, staged, candidates))
    return staged


class LedgerDialog(QDialog):
    def __init__(self, parent, catalog: Catalog):
        super().__init__(parent)
        self.catalog = catalog
        self.setWindowTitle("Выписки и операции")
        self.resize(900, 630)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.addWidget(label("Выписки и операции", "screenTitle"))
        self.account = ChoiceBox()
        self.account.setAccessibleName("Счёт")
        self.fill_accounts()
        layout.addWidget(self.account)
        actions = QHBoxLayout()
        self.opening_button = button("Начальный остаток и реквизиты…", self.set_opening)
        self.manual_button = button("+ Операция", self.add_manual)
        self.import_button = button("Импорт PDF и CSV…", self.import_files, primary=True)
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

    def fill_accounts(self):
        selected = self.account.currentData()
        self.accounts = {asset.asset_number: asset for asset in self.catalog.assets if asset.category == "account"}
        self.account.blockSignals(True)
        self.account.clear()
        for number, asset in self.accounts.items():
            self.account.addItem(f"{asset.title} · {asset.owner} · {asset.currency}", number)
        self.account.setCurrentIndex(max(0, self.account.findData(selected)))
        self.account.blockSignals(False)
        self.account.setEnabled(bool(self.accounts))

    def refresh(self) -> None:
        number = self.account.currentData()
        ledger = self.catalog.ledger
        self.opening_button.setEnabled(number is not None and self.catalog.editable)
        self.import_button.setEnabled(self.catalog.editable)
        latest = ledger.latest_balance(number)
        self.manual_button.setEnabled(number is not None and self.accounts[number].status == "active"
                                      and self.catalog.editable)
        self.check_button.setEnabled(latest is not None)
        self.bank_summary.clear()
        self.grid.setRowCount(0)
        if number is None:
            self.summary.setText("Импортируйте выписку в PDF или CSV, чтобы добавить счёт и операции.")
            return
        if latest is None:
            self.summary.setText("Добавьте первую операцию или импортируйте PDF. В PDF начальный остаток берётся из выписки.")
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
            source = {"csv": "CSV", "pdf": "PDF"}.get(entry.source, "Вручную")
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
            "Укажите остаток на конец дня перед первой операцией. Реквизит сверяется со счётом в выписке.",
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
        dialog = OperationEditor(self, self.catalog, account=number)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.catalog = dialog.catalog
            self.refresh()

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
        updated = import_statements(self, self.catalog)
        if updated is not self.catalog:
            self.catalog = updated
            self.fill_accounts()
            self.refresh()
