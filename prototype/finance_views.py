"""A single workspace for cash flow, wallets, goals, investments and exports."""

import os
import tempfile
from datetime import date, timedelta
from pathlib import Path
from uuid import uuid4

from PySide6.QtCore import QDate, QRectF, Qt
from PySide6.QtGui import QColor, QKeySequence, QPainter, QPen, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QDialog, QFileDialog, QFormLayout, QHBoxLayout, QHeaderView, QLineEdit,
    QMenu, QMessageBox, QProgressBar, QTabWidget, QVBoxLayout, QWidget,
)

from appearance import ChoiceBox, area, button, frame, label
from asset_views import AssetEditor, RegistryDialog
from assets import amount_text, parse_amount
from debt_views import DebtEditor, DebtPanel
from debts import ISSUE_SIGN
from finance import FLOW_LABELS, UNCATEGORIZED, activity, export_text, flow_breakdown, flow_totals
from finance_editors import DirectoryDialog, OperationEditor, WalletEditor, date_field, field_date, form_dialog
from fund_views import FundsDialog, OverviewDialog
from funds import Fund, goal_gap
from ledger_views import LedgerDialog, import_statements, table
from operation_controls import TAG_NAMES, TagCells, category_menu, choose_tags, selecting_rows
from review import period
from review_views import CategoryDialog, ReviewDialog, fill_table


def display(value, currency):
    return f"{amount_text(value)} {currency}"


def section(layout, text, action=None, callback=None, *, enabled=True):
    row = QHBoxLayout()
    row.addWidget(label(text, "sectionTitle"), 1)
    if action:
        item = button(action, callback)
        item.setEnabled(enabled)
        row.addWidget(item)
    layout.addLayout(row)
    return row


class MonthlyChart(QWidget):
    def __init__(self):
        super().__init__()
        self.points = ()
        self.limited = False
        self.daily = False
        self.setMinimumHeight(148)
        self.setAccessibleName("График доходов и расходов")

    def set_period(self, rows, start, end):
        first = QDate.fromString(start, Qt.DateFormat.ISODate)
        last = QDate.fromString(end, Qt.DateFormat.ISODate)
        self.daily = first.daysTo(last) < 14
        totals = {}
        for row in rows:
            info = totals.setdefault(row.day if self.daily else row.day[:7], [0, 0])
            info[0] += row.income
            info[1] += row.expense
        cursor = last if self.daily else QDate(last.year(), last.month(), 1)
        length = 10 if self.daily else 7
        points = []
        while cursor.isValid() and cursor.toString(Qt.DateFormat.ISODate)[:length] >= start[:length]:
            key = cursor.toString(Qt.DateFormat.ISODate)[:length]
            points.append((key, *totals.get(key, (0, 0))))
            cursor = cursor.addDays(-1) if self.daily else cursor.addMonths(-1)
            if not self.daily and len(points) == 12:
                break
        self.points = tuple(reversed(points))
        self.limited = not self.daily and cursor.isValid() and cursor.toString(Qt.DateFormat.ISODate)[:7] >= start[:7]
        self.setToolTip("\n".join(
            f"{key}: доходы {amount_text(income)}, расходы {amount_text(expense)}"
            for key, income, expense in self.points
        ))
        self.update()

    def paintEvent(self, event):
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        try:
            painter.setPen(QColor("#263A4C"))
            title = "По дням" if self.daily else "Последние 12 месяцев периода" if self.limited else "По месяцам"
            painter.drawText(10, 19, title)
            for column, (color, title) in enumerate((("#299779", "Доходы"), ("#CF6C75", "Расходы"))):
                x = self.width() - 177 + column * 88
                painter.fillRect(x, 10, 9, 9, QColor(color))
                painter.drawText(x + 14, 19, title)
            if not self.points or not any(income or expense for _, income, expense in self.points):
                painter.setPen(QColor("#53657D"))
                painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "За этот период нет доходов и расходов")
                return
            left, top = 12, 28
            width, height = self.width() - 24, self.height() - 52
            slot = width / len(self.points)
            bar_width = min(26, slot / 5)
            scale = max(1, *(max(income, expense) for _, income, expense in self.points))
            painter.setPen(QPen(QColor("#DCE6E3"), 1))
            for fraction in (0, 0.5, 1):
                y = round(top + height * fraction)
                painter.drawLine(left, y, left + width, y)
            for index, (month, income, expense) in enumerate(self.points):
                center = left + (index + 0.5) * slot
                painter.setPen(Qt.PenStyle.NoPen)
                for offset, color, total in ((-bar_width - 2, "#299779", income), (2, "#CF6C75", expense)):
                    if total:
                        bar_height = max(2, total * height / scale)
                        painter.setBrush(QColor(color))
                        painter.drawRoundedRect(
                            QRectF(center + offset, top + height - bar_height, bar_width, bar_height), 3, 3
                        )
                painter.setPen(QColor("#53657D"))
                if len(self.points) < 9 or index % 2 == 0:
                    text_box = QRectF(center - slot / 2, top + height + 4, slot, 18)
                    text = f"{month[8:10]}.{month[5:7]}" if self.daily else f"{month[5:]}.{month[2:4]}"
                    painter.drawText(text_box, Qt.AlignmentFlag.AlignHCenter, text)
        finally:
            painter.end()


class GoalDialog(QDialog):
    def __init__(self, parent, catalog, currency, fund=None):
        super().__init__(parent)
        self.catalog, self.fund = catalog, fund
        form, self.error = form_dialog(self, "Изменить цель" if fund else "Новая цель")
        self.title = QLineEdit(fund.title if fund else "")
        self.title.setPlaceholderText("Образование, квартира, путешествие…")
        self.currency = QLineEdit(fund.currency if fund else currency)
        self.currency.setMaxLength(3)
        self.amount = QLineEdit(amount_text(fund.goal) if fund and fund.goal is not None else "")
        self.deadline = date_field(fund.deadline if fund and fund.deadline else
                                   QDate.currentDate().addYears(1).toString(Qt.DateFormat.ISODate))
        self.use_deadline = QCheckBox("Задать срок")
        self.use_deadline.setChecked(bool(fund and fund.deadline))
        form.addRow("Название", self.title)
        form.addRow("Валюта", self.currency)
        form.addRow("Сколько нужно", self.amount)
        form.addRow("", self.use_deadline)
        form.addRow("Накопить к", self.deadline)
        self.use_deadline.toggled.connect(self.deadline.setEnabled)
        self.deadline.setEnabled(self.use_deadline.isChecked())
        self.title.setFocus()

    def save(self):
        try:
            amount = parse_amount(self.amount.text())
            if amount is None or amount <= 0:
                raise ValueError("Укажите положительную сумму цели.")
            goal = Fund(
                self.title.text().strip(), self.fund.kind if self.fund else "Цель",
                self.currency.text().strip().upper(), amount, self.fund.number if self.fund else 0,
                field_date(self.deadline) if self.use_deadline.isChecked() else "",
            )
            self.catalog = self.catalog.save_funds(self.catalog.funds.save_fund(goal))
        except (OSError, ValueError) as exc:
            self.error.setText(str(exc))
            self.error.show()
            return
        self.accept()


class DistributionDialog(QDialog):
    def __init__(self, parent, catalog, currency, start, end):
        super().__init__(parent)
        self.catalog = catalog
        self.currency = currency
        self.state = catalog.funds
        self.free = catalog.liquid_totals().get(currency, 0) - self.state.reserved().get(currency, 0)
        self.command = uuid4().hex
        self.setWindowTitle("Распределить деньги")
        self.resize(660, 510)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 18, 22, 18)
        layout.addWidget(label("Распределить деньги", "screenTitle"))
        self.source = ChoiceBox()
        self.source.setAccessibleName("Источник распределения")
        self.source.addItem(f"Свободный остаток · {display(self.free, currency)}", None)
        used = {move.operation for move in self.state.movements}
        self.incomes = {
            row.number: row for row in activity(catalog.ledger, catalog.review, start, end, currency)
            if row.income and row.number not in used
        }
        for row in self.incomes.values():
            title = row.category if row.category != "Без статьи" else row.description
            self.source.addItem(f"{row.day} · {title} · {display(row.amount, currency)}", row.number)
        layout.addWidget(self.source)
        content = QWidget()
        content.setObjectName("formContent")
        form = QFormLayout(content)
        form.setSpacing(8)
        self.fields = {}
        balances = self.state.balances()
        for fund in self.state.funds:
            if fund.currency == currency:
                edit = QLineEdit()
                edit.setAccessibleName(f"Отложить в {fund.title}")
                edit.setPlaceholderText("0,00")
                form.addRow(f"{fund.title} · {amount_text(balances[fund.number])}", edit)
                edit.textChanged.connect(self.refresh)
                self.fields[fund.number] = edit
        if not self.fields:
            form.addRow(label("Создайте цель или фонд в этой валюте.", "muted"))
        layout.addWidget(area(content), 1)
        self.total = label(style="sectionTitle", wrap=True)
        layout.addWidget(self.total)
        self.error = label(style="financeError", wrap=True)
        layout.addWidget(self.error)
        actions = QHBoxLayout()
        template = button("Подставить шаблон", self.apply_template)
        template.setEnabled(bool(self.fields))
        actions.addWidget(template)
        actions.addStretch(1)
        actions.addWidget(button("Отмена", self.reject))
        self.commit = button("Подтвердить", self.save, primary=True)
        actions.addWidget(self.commit)
        layout.addLayout(actions)
        self.source.currentIndexChanged.connect(self.refresh)
        self.refresh()

    def moves(self):
        moves = []
        for fund, edit in self.fields.items():
            amount = parse_amount(edit.text()) or 0
            if amount < 0:
                raise ValueError("Сумма распределения должна быть неотрицательной.")
            if amount:
                moves.append((fund, amount))
        return tuple(moves)

    def budget(self):
        row = self.incomes.get(self.source.currentData())
        return min(self.free, row.amount) if row else self.free

    def refresh(self):
        try:
            total = sum(amount for _, amount in self.moves())
            self.total.setText(
                f"Можно распределить {display(max(0, self.budget()), self.currency)}  ·  "
                f"В фонды {display(total, self.currency)}  ·  "
                f"Свободно после {display(self.free - total, self.currency)}"
            )
            self.error.setText("Сумма превышает доступный остаток." if total > max(0, self.budget()) else "")
            self.commit.setEnabled(0 < total <= self.budget())
        except ValueError as exc:
            self.error.setText(str(exc))
            self.commit.setEnabled(False)

    def apply_template(self):
        row = self.incomes.get(self.source.currentData())
        try:
            plan = dict(self.state.plan(self.currency, row.amount if row else self.free, self.free))
            for fund, edit in self.fields.items():
                edit.setText(amount_text(plan[fund]) if fund in plan else "")
        except ValueError as exc:
            self.error.setText(str(exc))

    def save(self):
        try:
            moves = self.moves()
            if not moves or sum(amount for _, amount in moves) > self.budget():
                raise ValueError("Проверьте суммы распределения.")
            number = self.source.currentData()
            command = f"income:{number}" if number else self.command
            self.catalog = self.catalog.save_funds(self.state.apply(command, moves, number))
        except (OSError, ValueError) as exc:
            self.error.setText(str(exc))
            return
        self.accept()


class ExportDialog(QDialog):
    def __init__(self, parent, catalog, start, end, *, tag=""):
        super().__init__(parent)
        self.catalog = catalog
        self.setWindowTitle("Экспорт финансов")
        self.resize(720, 485)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 18, 22, 18)
        layout.addWidget(label("Экспорт финансов", "screenTitle"))
        form = QFormLayout()
        self.start, self.end = date_field(start), date_field(end)
        self.format = ChoiceBox()
        self.format.addItem("JSON · операции и итоги для анализа", "json")
        self.format.addItem("CSV · операции для таблиц", "csv")
        self.tag = ChoiceBox()
        self.tag.addItem("Все теги", "")
        for name in catalog.review.available_tags():
            self.tag.addItem(name, name)
        self.tag.setCurrentIndex(max(0, self.tag.findData(tag)))
        self.include_tags = QCheckBox("Включить названия тегов в файл")
        self.details = QCheckBox("Включить описания операций и контрагентов")
        form.addRow("С", self.start)
        form.addRow("По", self.end)
        form.addRow("Формат", self.format)
        form.addRow("Тег", self.tag)
        form.addRow("", self.include_tags)
        form.addRow("", self.details)
        layout.addLayout(form)
        layout.addWidget(label(
            "В файл попадут суммы и названия статей по всем валютам выбранного периода. "
            "Названия кошельков заменяются на «Счёт 1», «Счёт 2» и так далее.", "muted", wrap=True,
        ))
        self.summary = label(style="sectionTitle")
        layout.addWidget(self.summary)
        self.preview = table(("Дата", "Сумма", "Статья", "Описание", "Теги"), 2)
        layout.addWidget(self.preview, 1)
        self.error = label(style="financeError", wrap=True)
        layout.addWidget(self.error)
        actions = QHBoxLayout()
        actions.addStretch(1)
        actions.addWidget(button("Отмена", self.reject))
        self.commit = button("Сохранить файл…", self.save, primary=True)
        actions.addWidget(self.commit)
        layout.addLayout(actions)
        for signal in (self.start.dateChanged, self.end.dateChanged, self.details.toggled,
                       self.tag.currentIndexChanged, self.include_tags.toggled):
            signal.connect(self.refresh)
        self.refresh()

    def refresh(self):
        start, end = field_date(self.start), field_date(self.end)
        try:
            self.rows = activity(self.catalog.ledger, self.catalog.review, start, end, tag=self.tag.currentData())
        except ValueError as exc:
            self.rows = ()
            self.error.setText(str(exc))
        else:
            self.error.clear()
        self.summary.setText(f"Операций в файле: {len(self.rows)} · предпросмотр первых 5")
        self.preview.setColumnHidden(3, not self.details.isChecked())
        self.preview.setColumnHidden(4, not self.include_tags.isChecked())
        fill_table(self.preview, [(row.day, display(row.amount, row.currency), row.category, row.description,
                                   ", ".join(row.tags))
                                  for row in self.rows[:5]])
        self.commit.setEnabled(bool(self.rows))

    def save(self):
        if not self.rows:
            return
        kind = self.format.currentData()
        start, end = field_date(self.start), field_date(self.end)
        file, _ = QFileDialog.getSaveFileName(
            self, "Сохранить экспорт", f"finance-{start}_{end}.{kind}", f"{kind.upper()} (*.{kind})"
        )
        if not file:
            return
        path = Path(file)
        if path.suffix.lower() != f".{kind}":
            path = path.with_name(path.name + f".{kind}")
            if path.exists() and QMessageBox.question(self, "Заменить файл", path.name) != QMessageBox.StandardButton.Yes:
                return
        try:
            text = export_text(self.rows, start, end, kind=kind, details=self.details.isChecked(),
                               include_tags=self.include_tags.isChecked())
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8-sig" if kind == "csv" else "utf-8",
                                             newline="", dir=path.parent, delete=False) as stream:
                temp = Path(stream.name)
                try:
                    stream.write(text)
                    stream.close()
                    os.replace(temp, path)
                finally:
                    temp.unlink(missing_ok=True)
        except (OSError, ValueError) as exc:
            self.error.setText(str(exc))
            return
        self.accept()


class FinanceDialog(QDialog):
    ARTICLE, TAGS = 4, 6

    def __init__(self, parent, catalog, *, show_operations=False):
        super().__init__(parent)
        self.catalog = catalog
        self.document_number = None
        self.rows = ()
        self.filtered = ()
        self.article_filter = None
        self.goal_funds = ()
        self.setWindowTitle("Финансы")
        self.resize(1110, 740)
        self.setMinimumSize(900, 610)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 18, 22, 18)
        layout.setSpacing(12)
        heading = QHBoxLayout()
        heading.addWidget(label("Финансы", "screenTitle"), 1)
        self.import_button = button("Импорт выписки…", self.import_files)
        heading.addWidget(self.import_button)
        heading.addWidget(button("Справочники…", self.open_directory))
        heading.addWidget(button("Экспорт…", self.export))
        self.primary = button("+ Операция", self.new_operation, primary=True)
        heading.addWidget(self.primary)
        layout.addLayout(heading)

        filters = QHBoxLayout()
        self.range = ChoiceBox()
        for key, text in (("month", "Последний месяц"), ("week", "7 дней"), ("quarter", "3 месяца"),
                          ("all", "Весь период"), ("custom", "Свой период")):
            self.range.addItem(text, key)
        self.currency = ChoiceBox()
        self.currency.setAccessibleName("Валюта дашборда")
        self.tag_filter = ChoiceBox()
        self.tag_filter.setAccessibleName("Тег финансового отчёта")
        self.tag_filter.setMinimumWidth(135)
        self.tag_filter.setSizeAdjustPolicy(ChoiceBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        today = QDate.currentDate()
        self.start, self.end = date_field(today.addMonths(-1).addDays(1).toString(Qt.DateFormat.ISODate)), date_field(
            today.toString(Qt.DateFormat.ISODate)
        )
        filters.addWidget(self.range)
        for text, field in (("С", self.start), ("По", self.end)):
            filters.addWidget(label(text))
            filters.addWidget(field)
        filters.addStretch(1)
        filters.addWidget(label("Тег"))
        filters.addWidget(self.tag_filter)
        filters.addWidget(label("Валюта"))
        filters.addWidget(self.currency)
        layout.addLayout(filters)

        metrics = QHBoxLayout()
        self.metrics = {}
        for key, text, style in (
            ("income", "Доходы за период", "financePositive"),
            ("expense", "Расходы за период", "financeExpense"),
            ("net", "Итог за период", "financeValue"),
            ("free", "Свободно на счетах", "financeValue"),
        ):
            tile = frame("financeMetric")
            stack = QVBoxLayout(tile)
            stack.setContentsMargins(14, 12, 14, 12)
            stack.addWidget(label(text, "muted"))
            number = label(style=style, wrap=True)
            stack.addWidget(number)
            self.metrics[key] = number
            metrics.addWidget(tile, 1)
        layout.addLayout(metrics)
        self.error = label(style="financeError")
        self.error.hide()
        layout.addWidget(self.error)

        self.tabs = QTabWidget()
        self.tabs.setObjectName("financeTabs")
        self.tabs.addTab(self.build_dashboard(), "Итоги")
        self.operation_page = self.build_operations()
        self.tabs.addTab(self.operation_page, "Операции")
        self.tabs.addTab(self.build_wallets(), "Кошельки и цели")
        self.debt_panel = DebtPanel(self)
        self.debt_panel.catalog_changed.connect(self.updated_debt)
        self.tabs.addTab(self.debt_panel, "Долги")
        self.tabs.addTab(self.build_investments(), "Инвестиции и вклады")
        layout.addWidget(self.tabs, 1)
        bottom = QHBoxLayout()
        bottom.addStretch(1)
        bottom.addWidget(button("Закрыть", self.accept))
        layout.addLayout(bottom)
        self.range.currentIndexChanged.connect(self.update_dates)
        self.start.dateChanged.connect(self.custom_dates)
        self.end.dateChanged.connect(self.custom_dates)
        self.currency.currentIndexChanged.connect(self.refresh)
        self.tag_filter.currentIndexChanged.connect(self.refresh)
        self.update_currency()
        self.update_tags()
        self.update_dates()
        if show_operations:
            self.tabs.setCurrentWidget(self.operation_page)

    @staticmethod
    def page():
        widget = QWidget()
        widget.setObjectName("formContent")
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(14, 11, 14, 14)
        return widget, layout

    def build_dashboard(self):
        widget, layout = self.page()
        self.chart = MonthlyChart()
        layout.addWidget(self.chart)
        self.period_note = label(style="muted", wrap=True)
        layout.addWidget(self.period_note)
        panels = QHBoxLayout()
        left, right = QVBoxLayout(), QVBoxLayout()
        panels.addLayout(left, 1)
        panels.addLayout(right, 1)
        section(left, "Доходы по источникам")
        section(right, "Расходы по статьям")
        self.incomes = table(("Источник", "Сумма", "Доля"), 0)
        self.expenses = table(("Статья", "Сумма", "Доля"), 0)
        self.incomes.setCursor(Qt.CursorShape.PointingHandCursor)
        self.expenses.setCursor(Qt.CursorShape.PointingHandCursor)
        self.incomes.cellClicked.connect(lambda row, _: self.drill_down(self.incomes, row, "income"))
        self.expenses.cellClicked.connect(lambda row, _: self.drill_down(self.expenses, row, "expense"))
        left.addWidget(self.incomes, 1)
        right.addWidget(self.expenses, 1)
        layout.addLayout(panels, 1)
        self.balances = label(style="muted", wrap=True)
        layout.addWidget(self.balances)
        self.debt_notice = label(style="deadlineSoon", wrap=True)
        layout.addWidget(self.debt_notice)
        actions = QHBoxLayout()
        self.unclassified_button = button("Без статьи", self.show_unclassified)
        actions.addWidget(self.unclassified_button)
        self.distribute_button = button("Распределить деньги…", self.distribute, primary=True)
        actions.addWidget(self.distribute_button)
        actions.addWidget(button("Разбор и регулярные платежи…", self.open_review))
        actions.addWidget(button("Активы и ближайшие платежи…", self.overview))
        actions.addStretch(1)
        layout.addLayout(actions)
        return widget

    def build_operations(self):
        widget, layout = self.page()
        top = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("Поиск по описанию, статье, кошельку или тегу")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self.refresh_operations)
        top.addWidget(self.search, 1)
        top.addWidget(button("Выписки и сверка…", self.open_ledger))
        layout.addLayout(top)
        self.filter_notice = button("", self.clear_article_filter)
        layout.addWidget(self.filter_notice, alignment=Qt.AlignmentFlag.AlignLeft)
        self.operations = table(("Дата", "Кошелёк", "Вид", "Сумма", "Статья  ▾", "Описание", "Теги  ▾"), 5)
        self.operations.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.operations.setItemDelegateForColumn(self.TAGS, TagCells(self.operations))
        for column, width in ((1, 120), (self.ARTICLE, 163), (self.TAGS, 130)):
            self.operations.horizontalHeader().setSectionResizeMode(column, QHeaderView.ResizeMode.Interactive)
            self.operations.setColumnWidth(column, width)
        self.operations.cellClicked.connect(self.operation_clicked)
        self.operations.cellDoubleClicked.connect(self.operation_double_clicked)
        self.category_shortcut = QShortcut(QKeySequence("F2"), self.operations)
        self.category_shortcut.activated.connect(self.classify_selected)
        layout.addWidget(self.operations, 1)
        controls = QHBoxLayout()
        self.operation_count = label(style="muted")
        controls.addWidget(self.operation_count, 1)
        self.edit_button = button("Изменить…", self.edit_operation)
        controls.addWidget(self.edit_button)
        self.classify_button = button("Статья…", self.classify_selected)
        controls.addWidget(self.classify_button)
        self.tags_button = button("Теги…", self.tag_selected)
        controls.addWidget(self.tags_button)
        self.rule_button = button("Правило…", self.classify_operation)
        controls.addWidget(self.rule_button)
        self.debt_button = button("К долгу…", self.debt_for_operation)
        controls.addWidget(self.debt_button)
        self.operations.itemSelectionChanged.connect(self.update_operation_buttons)
        layout.addLayout(controls)
        return widget

    def build_wallets(self):
        widget, layout = self.page()
        actions = section(layout, "Где лежат деньги", "+ Кошелёк", self.new_wallet, enabled=self.catalog.editable)
        actions.addWidget(button("Счета и активы…", self.open_registry))
        self.wallets = table(("Кошелёк", "Тип", "Остаток", "На дату"), 0)
        layout.addWidget(self.wallets, 1)
        actions = section(layout, "Резервы и цели", "+ Цель", self.new_goal, enabled=self.catalog.editable)
        actions.addWidget(button("Движения и шаблоны…", self.open_funds))
        self.goal_table = table(("Фонд / цель", "Прогресс", "Накоплено", "Цель", "До цели", "Срок", "В месяц"), 0)
        self.goal_table.cellDoubleClicked.connect(lambda *_: self.edit_goal())
        layout.addWidget(self.goal_table, 1)
        self.goal_caption = label(
            "Взнос в месяц — остаток цели, разделённый на месяцы до срока, включая текущий.", "muted", wrap=True
        )
        layout.addWidget(self.goal_caption)
        tools = QHBoxLayout()
        self.goals_distribute = button("Распределить деньги…", self.distribute, primary=True)
        tools.addWidget(self.goals_distribute)
        self.goal_edit = button("Изменить цель…", self.edit_goal)
        tools.addWidget(self.goal_edit)
        if not self.catalog.editable:
            self.goal_edit.setEnabled(False)
        tools.addStretch(1)
        layout.addLayout(tools)
        return widget

    def build_investments(self):
        widget, layout = self.page()
        actions = section(layout, "Инвестиционные позиции", "+ Инвестиция", self.new_investment,
                          enabled=self.catalog.editable)
        actions.addWidget(button("Счета и активы…", self.open_registry))
        self.investments = table(("Позиция", "Тикер", "Количество", "Оценка", "Затраты", "Разница", "На дату"), 0)
        layout.addWidget(self.investments, 1)
        self.value_topline = label(style="sectionTitle")
        layout.addWidget(self.value_topline)
        section(layout, "Вклады")
        self.deposits = table(("Вклад", "Банк", "Остаток", "На дату"), 0)
        layout.addWidget(self.deposits, 1)
        return widget

    def bounds(self):
        start, end = field_date(self.start), field_date(self.end)
        period(start, end)
        return start, end

    def custom_dates(self):
        self.range.setCurrentIndex(self.range.findData("custom"))
        self.refresh()

    def update_dates(self):
        selected = self.range.currentData()
        if selected == "custom":
            return
        today = QDate.currentDate()
        posted = [QDate.fromString(entry.day, Qt.DateFormat.ISODate) for entry in self.catalog.ledger.entries]
        if selected == "all":
            start, end = min(posted, default=today), max([today, *posted])
        else:
            end = today
            start = today.addDays(-6) if selected == "week" else today.addMonths(
                -3 if selected == "quarter" else -1
            ).addDays(1)
        self.start.blockSignals(True)
        self.end.blockSignals(True)
        self.start.setDate(start)
        self.end.setDate(end)
        self.start.blockSignals(False)
        self.end.blockSignals(False)
        self.refresh()

    def update_currency(self):
        current = self.currency.currentData() or "RUB"
        codes = {asset.currency for asset in self.catalog.assets} | {fund.currency for fund in self.catalog.funds.funds}
        codes.update(item.currency for item in self.catalog.review.debts.items)
        self.currency.blockSignals(True)
        self.currency.clear()
        for currency in sorted(codes or {"RUB"}):
            self.currency.addItem(currency, currency)
        index = self.currency.findData(current)
        self.currency.setCurrentIndex(max(0, index))
        self.currency.blockSignals(False)

    def update_tags(self):
        current = self.tag_filter.currentData() or ""
        self.tag_filter.blockSignals(True)
        self.tag_filter.clear()
        self.tag_filter.addItem("Все теги", "")
        for name in self.catalog.review.available_tags():
            self.tag_filter.addItem(name, name)
        self.tag_filter.setCurrentIndex(max(0, self.tag_filter.findData(current)))
        self.tag_filter.blockSignals(False)

    def refresh(self):
        currency = self.currency.currentData() or "RUB"
        try:
            start, end = self.bounds()
            self.rows = activity(self.catalog.ledger, self.catalog.review, start, end, currency,
                                 tag=self.tag_filter.currentData())
        except ValueError as exc:
            self.rows = ()
            self.period_note.hide()
            self.chart.set_period((), field_date(self.end), field_date(self.end))
            self.error.setText(str(exc))
            self.error.show()
        else:
            self.error.hide()
            self.chart.set_period(self.rows, start, end)
            self.compare_period(start, end, currency)
        income, expense = flow_totals(self.rows)
        cash = self.catalog.liquid_totals().get(currency, 0)
        reserved = self.catalog.funds.reserved().get(currency, 0)
        for name, value in (("income", income), ("expense", expense), ("net", income - expense),
                            ("free", cash - reserved)):
            self.metrics[name].setText(display(value, currency))
        totals = flow_breakdown(self.rows)
        for key, widget, total in (("income", self.incomes, income), ("expense", self.expenses, expense)):
            breakdown = [(category, value) for (_, kind, category), value in totals.items()
                         if kind == key]
            fill_table(widget, [(category, display(value, currency), f"{value / total:.0%}") for category, value in
                                sorted(breakdown, key=lambda item: (-item[1], item[0]))])
        self.balances.setText(
            f"На счетах сейчас: {display(cash, currency)}  ·  Отложено: {display(reserved, currency)}"
        )
        missing = sum(self.is_unclassified(row) for row in self.rows)
        self.unclassified_button.setText(f"Без статьи · {missing}")
        self.unclassified_button.setVisible(bool(missing))
        self.show_debt_notice(currency)
        self.refresh_operations()
        self.refresh_positions(currency)
        self.refresh_goals(currency)
        self.debt_panel.refresh(self.catalog, currency)
        self.primary.setEnabled(self.catalog.editable)
        self.import_button.setEnabled(self.catalog.editable)
        for control in (self.distribute_button, self.goals_distribute):
            control.setEnabled(self.catalog.editable and self.error.isHidden())

    def compare_period(self, start, end, currency):
        first, last = date.fromisoformat(start), date.fromisoformat(end)
        width = (last - first).days + 1
        if first.toordinal() <= width or self.range.currentData() == "all":
            self.period_note.hide()
            return
        previous = activity(
            self.catalog.ledger, self.catalog.review, (first - timedelta(days=width)).isoformat(),
            (first - timedelta(days=1)).isoformat(), currency, tag=self.tag_filter.currentData(),
        )
        _, expense = flow_totals(self.rows)
        _, previous_expense = flow_totals(previous)
        if not previous_expense:
            self.period_note.hide()
            return
        difference = expense - previous_expense
        if difference:
            direction = "выше" if difference > 0 else "ниже"
            text = f"Расходы на {display(abs(difference), currency)} {direction}, чем за предыдущие {width} дн."
        else:
            text = f"Расходы такие же, как за предыдущие {width} дн."
        self.period_note.setText(text)
        self.period_note.show()

    def show_debt_notice(self, currency):
        debts = self.catalog.review.debts
        balances = debts.balances()
        today = date.today()
        soon = (today + timedelta(days=7)).isoformat()
        due = overdue = 0
        for item in debts.items:
            if item.direction != "borrowed" or item.currency != currency or not item.due:
                continue
            if item.due < today.isoformat():
                overdue += balances[item.number]
            elif item.due <= soon:
                due += balances[item.number]
        parts = []
        if overdue:
            parts.append(f"Просрочено к возврату {display(overdue, currency)}")
        if due:
            parts.append(f"Вернуть в ближайшие 7 дней {display(due, currency)}")
        self.debt_notice.setText(" · ".join(parts))
        self.debt_notice.setVisible(bool(parts))

    def refresh_operations(self):
        selected = self.selected_operation()
        query = self.search.text().casefold().split()
        titles = {asset.asset_number: asset.title for asset in self.catalog.assets}
        self.filtered = tuple(row for row in self.rows if self.matches_article(row) and all(
            word in f"{titles[row.account]} {row.category} {row.description} {row.recipient} {' '.join(row.tags)}".casefold()
            for word in query))
        self.operations.blockSignals(True)
        fill_table(self.operations, [
            (row.day, titles[row.account], FLOW_LABELS[row.kind], display(row.amount, row.currency),
             row.category, row.description + (f" · комиссия {amount_text(row.fee)}" if row.fee else ""),
             ", ".join(row.tags))
            for row in self.filtered
        ])
        for index, row in enumerate(self.filtered):
            tag = self.operations.item(index, self.TAGS)
            tag.setData(TAG_NAMES, row.tags)
            if not self.catalog.editable:
                continue
            article = self.operations.item(index, self.ARTICLE)
            if row.kind in ("income", "expense"):
                article.setText(("Выбрать" if self.is_unclassified(row) else row.category) + "  ▾")
                article.setForeground(QColor("#146951"))
            tag.setText((", ".join(row.tags) if row.tags else "+ Тег") + "  ▾")
            tag.setForeground(QColor("#146951"))
        if selected:
            index = next((i for i, row in enumerate(self.filtered) if row.number == selected.number), None)
            if index is not None:
                self.operations.selectRow(index)
        self.operations.blockSignals(False)
        self.update_operation_buttons()
        self.filter_notice.setVisible(self.article_filter is not None)
        if self.article_filter is not None:
            self.filter_notice.setText(self.article_filter[1].replace("&", "&&") + "  ×")

    @staticmethod
    def is_unclassified(row):
        return row.kind in ("income", "expense") and row.category == UNCATEGORIZED

    def matches_article(self, row):
        if self.article_filter is None:
            return True
        kind, category = self.article_filter
        if kind == "missing":
            return self.is_unclassified(row)
        amount = row.income if kind == "income" else row.expense
        return bool(amount and (("Комиссии банка" if row.fee else row.category) == category))

    def drill_down(self, widget, index, kind):
        self.open_article_filter(kind, widget.item(index, 0).text())

    def show_unclassified(self):
        self.open_article_filter("missing", UNCATEGORIZED)

    def open_article_filter(self, kind, category):
        self.article_filter = kind, category
        self.search.clear()
        self.tabs.setCurrentWidget(self.operation_page)
        self.refresh_operations()

    def clear_article_filter(self):
        self.article_filter = None
        self.refresh_operations()

    def refresh_positions(self, currency):
        assets = [self.catalog.recorded_value(asset) for asset in self.catalog.assets
                  if asset.currency == currency and asset.status == "active"]
        fill_table(self.wallets, [(asset.title, asset.kind or "Счёт", asset.valuation, asset.as_of or "—")
                                  for asset in assets if asset.category == "account"])
        fill_table(self.deposits, [(asset.title, asset.organization, asset.valuation, asset.as_of or "—")
                                   for asset in assets if asset.category == "account" and "вклад" in asset.kind.casefold()])
        investments = [asset for asset in assets if asset.category == "investment"]
        fill_table(self.investments, [
            (asset.title, asset.ticker, asset.quantity, asset.valuation,
             display(asset.purchase_cost, currency) if asset.purchase_cost is not None else "—",
             display(asset.amount - asset.purchase_cost, currency)
             if asset.purchase_cost is not None and asset.amount is not None else "—", asset.as_of or "—")
            for asset in investments
        ])
        totals = sum(asset.amount or 0 for asset in investments)
        self.value_topline.setText(f"Известная стоимость позиций: {display(totals, currency)}" if investments else
                                   "Добавьте инвестицию, чтобы увидеть её оценку.")

    def refresh_goals(self, currency):
        funds = self.catalog.funds
        saved = funds.balances()
        self.goal_funds = tuple(fund for fund in funds.funds if fund.currency == currency)
        rows = []
        for fund in self.goal_funds:
            amount = saved[fund.number]
            gap, monthly = goal_gap(fund, amount, date.today()) if fund.goal is not None else (None, None)
            rows.append((fund.title, "", display(amount, currency),
                         display(fund.goal, currency) if fund.goal is not None else "—",
                         display(gap, currency) if gap is not None else "—", fund.deadline or "—",
                         display(monthly, currency) if monthly is not None else "—"))
        self.goal_table.clearContents()
        fill_table(self.goal_table, rows)
        for index, fund in enumerate(self.goal_funds):
            if fund.goal:
                bar = QProgressBar()
                bar.setRange(0, 100)
                bar.setValue(min(100, saved[fund.number] * 100 // fund.goal))
                bar.setToolTip(f"Цель: {display(fund.goal, currency)}")
                bar.setAccessibleName(f"Прогресс цели {fund.title}")
                self.goal_table.setCellWidget(index, 1, bar)
                self.goal_table.item(index, 0).setToolTip(f"{fund.title} · цель {display(fund.goal, currency)}")
        self.goal_caption.setVisible(any(fund.deadline for fund in self.goal_funds))
        if self.goal_funds:
            self.goal_table.selectRow(0)

    def use_catalog(self, catalog, *, currency=None):
        self.catalog = catalog
        self.update_currency()
        self.update_tags()
        if currency:
            self.currency.blockSignals(True)
            self.currency.setCurrentIndex(self.currency.findData(currency))
            self.currency.blockSignals(False)
        if self.range.currentData() == "all":
            self.update_dates()
        else:
            self.refresh()

    def new_wallet(self):
        dialog = WalletEditor(self, self.catalog)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.use_catalog(dialog.catalog, currency=dialog.catalog.assets[-1].currency)
            return dialog.catalog.assets[-1].asset_number
        return None

    def new_operation(self):
        accounts = [asset for asset in self.catalog.assets if asset.category == "account" and asset.status == "active"]
        available = {asset.asset_number for asset in accounts if asset.currency == self.currency.currentData()}
        account = next((entry.account for entry in reversed(self.catalog.ledger.entries) if entry.source == "manual"
                        and entry.account in available), min(available, default=None))
        if not accounts:
            account = self.new_wallet()
            if account is None:
                return
        dialog = OperationEditor(self, self.catalog, account=account)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.show_posted(dialog.catalog)

    def show_posted(self, catalog):
        entries = catalog.ledger.entries[len(self.catalog.ledger.entries):]
        if not entries:
            self.use_catalog(catalog)
            return
        self.article_filter = None
        self.search.blockSignals(True)
        self.search.clear()
        self.search.blockSignals(False)
        self.tag_filter.blockSignals(True)
        self.tag_filter.setCurrentIndex(0)
        self.tag_filter.blockSignals(False)
        self.use_catalog(catalog, currency=catalog.ledger.opening(entries[-1].account).currency)
        start, end = min(entry.day for entry in entries), max(entry.day for entry in entries)
        if start < field_date(self.start) or end > field_date(self.end):
            self.start.blockSignals(True)
            self.end.blockSignals(True)
            self.start.setDate(min(self.start.date(), QDate.fromString(start, Qt.DateFormat.ISODate)))
            self.end.setDate(max(self.end.date(), QDate.fromString(end, Qt.DateFormat.ISODate)))
            self.start.blockSignals(False)
            self.end.blockSignals(False)
            self.custom_dates()
        self.tabs.setCurrentWidget(self.operation_page)

    def import_files(self):
        updated = import_statements(self, self.catalog)
        if updated is not self.catalog:
            self.show_posted(updated)

    def selected_operation(self):
        index = self.operations.currentRow()
        return self.filtered[index] if 0 <= index < len(self.filtered) and self.operations.item(index, 0) else None

    def selected_operations(self):
        return tuple(self.filtered[index.row()] for index in self.operations.selectionModel().selectedRows())

    def update_operation_buttons(self):
        rows = self.selected_operations()
        one = rows[0] if len(rows) == 1 else None
        editable = self.catalog.editable
        kinds = {row.kind for row in rows}
        category_possible = editable and len(kinds) == 1 and kinds <= {"expense", "income"}
        self.classify_button.setEnabled(category_possible)
        ordinary = bool(one and one.kind in ("income", "expense"))
        self.edit_button.setEnabled(editable and one is not None)
        self.rule_button.setEnabled(editable and ordinary)
        self.tags_button.setEnabled(editable and bool(rows))
        self.debt_button.setEnabled(editable and ordinary and
                                    all(move.operation != one.number for move in self.catalog.funds.movements))
        self.operation_count.setText(f"Выбрано {len(rows)} из {len(self.filtered)}" if len(rows) > 1
                                     else f"Операций: {len(self.filtered)}")

    def operation_clicked(self, index, column):
        if not self.catalog.editable or selecting_rows():
            return
        row = self.filtered[index]
        if column == self.ARTICLE and row.kind in ("income", "expense"):
            item = self.operations.item(index, column)
            point = self.operations.viewport().mapToGlobal(self.operations.visualItemRect(item).bottomLeft())
            self.set_article((row,), point)
        elif column == self.TAGS:
            self.set_tags((row,))

    def operation_double_clicked(self, _, column):
        if column not in (self.ARTICLE, self.TAGS):
            self.edit_operation()

    def set_article(self, rows, point):
        first = rows[0]
        direction = 1 if first.kind == "income" else -1
        entry = self.catalog.ledger.entries[first.number - 1]
        suggestion = self.catalog.directory.suggestion(self.catalog.review, entry)
        result = category_menu(self, point, self.catalog, direction, first.category,
                               suggestion=suggestion.category if suggestion and len(rows) == 1 else "")
        if result is not None:
            name, directory = result
            review = self.catalog.review.classify(tuple(row.number for row in rows), name)
            self.save_classification(review, directory)

    def classify_selected(self):
        if self.classify_button.isEnabled():
            point = self.classify_button.mapToGlobal(self.classify_button.rect().bottomLeft())
            self.set_article(self.selected_operations(), point)

    def set_tags(self, rows):
        initial = rows[0].tags if len(rows) == 1 else ()
        names = choose_tags(self, known=self.catalog.review.available_tags(), initial=initial, count=len(rows))
        if names is not None:
            review = self.catalog.review.tag_operations({row.number: names for row in rows}, extend=len(rows) > 1)
            self.save_classification(review, self.catalog.directory)

    def tag_selected(self):
        rows = self.selected_operations()
        if rows and self.catalog.editable:
            self.set_tags(rows)

    def save_classification(self, review, directory):
        if (review, directory) == (self.catalog.review, self.catalog.directory):
            return
        try:
            self.use_catalog(self.catalog.save_directory(directory, review))
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, "Операция", str(exc))

    def edit_operation(self):
        if not self.catalog.editable or (row := self.selected_operation()) is None:
            return
        entry = self.catalog.ledger.entries[row.number - 1]
        used = any(move.operation == row.number for move in self.catalog.funds.movements)
        if row.kind == "debt":
            self.tabs.setCurrentWidget(self.debt_panel)
            return
        if row.kind == "transfer":
            self.open_review()
            return
        if entry.source != "manual" or used:
            self.classify_operation()
            return
        dialog = OperationEditor(self, self.catalog, number=row.number)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.use_catalog(dialog.catalog)

    def classify_operation(self):
        if not self.catalog.editable or (row := self.selected_operation()) is None:
            return
        if row.kind not in ("income", "expense"):
            return
        dialog = CategoryDialog(self, self.catalog, row.number)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.save_classification(dialog.result_review, dialog.result_directory)

    def debt_for_operation(self):
        if not self.debt_button.isEnabled():
            return
        row = self.selected_operation()
        menu = QMenu(self)
        create = menu.addAction("Создать долг из операции…")
        choices = {}
        balances = self.catalog.review.debts.balances()
        for debt in self.catalog.review.debts.items:
            if debt.currency == row.currency and debt.day <= row.day and row.amount * ISSUE_SIGN[debt.direction] < 0:
                if balances[debt.number] > 0:
                    action = menu.addAction("Возврат · " + debt.title.replace("&", "&&"))
                    choices[action] = debt
        action = menu.exec(self.debt_button.mapToGlobal(self.debt_button.rect().bottomLeft()))
        if action is None:
            return
        dialog = DebtEditor(self, self.catalog, row.currency, debt=None if action == create else choices[action],
                            operation_number=row.number)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.updated_debt(dialog.catalog)
            self.tabs.setCurrentWidget(self.debt_panel)

    def updated_debt(self, catalog):
        items = catalog.review.debts.items
        currency = items[-1].currency if len(items) > len(self.catalog.review.debts.items) else None
        self.use_catalog(catalog, currency=currency)

    def new_goal(self):
        dialog = GoalDialog(self, self.catalog, self.currency.currentData())
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.use_catalog(dialog.catalog, currency=dialog.catalog.funds.funds[-1].currency)

    def edit_goal(self):
        index = self.goal_table.currentRow()
        if self.catalog.editable and 0 <= index < len(self.goal_funds):
            dialog = GoalDialog(self, self.catalog, self.currency.currentData(), self.goal_funds[index])
            if dialog.exec() == QDialog.DialogCode.Accepted:
                self.use_catalog(dialog.catalog, currency=dialog.currency.text().strip().upper())

    def distribute(self):
        try:
            start, end = self.bounds()
        except ValueError as exc:
            QMessageBox.warning(self, "Период", str(exc))
            return
        dialog = DistributionDialog(self, self.catalog, self.currency.currentData(), start, end)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.use_catalog(dialog.catalog)

    def new_investment(self):
        dialog = AssetEditor(self, self.catalog, None, initial_category="investment")
        if dialog.exec() == QDialog.DialogCode.Accepted:
            try:
                self.use_catalog(self.catalog.add_asset(dialog.result_asset), currency=dialog.result_asset.currency)
            except (OSError, ValueError) as exc:
                QMessageBox.warning(self, "Инвестиция", str(exc))

    def open_directory(self):
        dialog = DirectoryDialog(self, self.catalog)
        dialog.exec()
        self.use_catalog(dialog.catalog)

    def open_registry(self):
        dialog = RegistryDialog(self, self.catalog)
        dialog.exec()
        self.use_catalog(dialog.catalog)
        if dialog.document_number is not None:
            self.document_number = dialog.document_number
            self.accept()

    def open_ledger(self):
        dialog = LedgerDialog(self, self.catalog)
        dialog.exec()
        self.show_posted(dialog.catalog)

    def open_review(self):
        dialog = ReviewDialog(self, self.catalog)
        try:
            start, end = self.bounds()
        except ValueError:
            pass
        else:
            dialog.start.setText(start)
            dialog.end.setText(end)
            dialog.refresh()
        dialog.exec()
        self.use_catalog(dialog.catalog)

    def open_funds(self):
        dialog = FundsDialog(self, self.catalog)
        dialog.exec()
        self.use_catalog(dialog.catalog)

    def overview(self):
        OverviewDialog(self, self.catalog).exec()

    def export(self):
        try:
            start, end = self.bounds()
        except ValueError as exc:
            QMessageBox.warning(self, "Период", str(exc))
            return
        ExportDialog(self, self.catalog, start, end, tag=self.tag_filter.currentData()).exec()
