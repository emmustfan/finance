"""A single workspace for cash flow, wallets, goals, investments and exports."""

import os
import tempfile
from datetime import date
from pathlib import Path
from uuid import uuid4

from PySide6.QtCore import QDate, QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import (
    QCheckBox, QDialog, QFileDialog, QFormLayout, QHBoxLayout, QLineEdit,
    QMessageBox, QProgressBar, QTabWidget, QVBoxLayout, QWidget,
)

from appearance import ChoiceBox, area, button, frame, label
from asset_views import AssetEditor, RegistryDialog
from assets import amount_text, parse_amount
from finance import FLOW_LABELS, activity, export_text, flow_breakdown, flow_totals
from finance_editors import DirectoryDialog, OperationEditor, WalletEditor, date_field, field_date, form_dialog
from fund_views import FundsDialog, OverviewDialog
from funds import Fund, goal_gap
from ledger_views import LedgerDialog, table
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
        self.setMinimumHeight(148)
        self.setAccessibleName("Доходы и расходы по месяцам")

    def set_period(self, rows, start, end):
        totals = {}
        for row in rows:
            info = totals.setdefault(row.day[:7], [0, 0])
            info[0] += row.income
            info[1] += row.expense
        cursor = QDate.fromString(end[:7] + "-01", Qt.DateFormat.ISODate)
        months = []
        while cursor.toString(Qt.DateFormat.ISODate)[:7] >= start[:7] and len(months) < 12:
            key = cursor.toString(Qt.DateFormat.ISODate)[:7]
            months.append((key, *totals.get(key, (0, 0))))
            cursor = cursor.addMonths(-1)
        self.points = tuple(reversed(months))
        self.limited = cursor.toString(Qt.DateFormat.ISODate)[:7] >= start[:7]
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
            painter.drawText(10, 19, "Последние 12 месяцев периода" if self.limited else "По месяцам")
            for column, (color, title) in enumerate((("#299779", "Доходы"), ("#CF6C75", "Расходы"))):
                x = self.width() - 177 + column * 88
                painter.fillRect(x, 10, 9, 9, QColor(color))
                painter.drawText(x + 14, 19, title)
            if not self.points or not any(income or expense for _, income, expense in self.points):
                painter.setPen(QColor("#53657D"))
                painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "За этот период операций пока нет")
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
                    painter.drawText(text_box, Qt.AlignmentFlag.AlignHCenter, f"{month[5:]}.{month[2:4]}")
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
    def __init__(self, parent, catalog, start, end):
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
        self.details = QCheckBox("Включить описания операций и контрагентов")
        form.addRow("С", self.start)
        form.addRow("По", self.end)
        form.addRow("Формат", self.format)
        form.addRow("", self.details)
        layout.addLayout(form)
        layout.addWidget(label(
            "В файл попадут суммы и названия статей по всем валютам выбранного периода. "
            "Названия кошельков заменяются на «Счёт 1», «Счёт 2» и так далее.", "muted", wrap=True,
        ))
        self.summary = label(style="sectionTitle")
        layout.addWidget(self.summary)
        self.preview = table(("Дата", "Сумма", "Статья", "Описание"), 2)
        layout.addWidget(self.preview, 1)
        self.error = label(style="financeError", wrap=True)
        layout.addWidget(self.error)
        actions = QHBoxLayout()
        actions.addStretch(1)
        actions.addWidget(button("Отмена", self.reject))
        self.commit = button("Сохранить файл…", self.save, primary=True)
        actions.addWidget(self.commit)
        layout.addLayout(actions)
        for signal in (self.start.dateChanged, self.end.dateChanged, self.details.toggled):
            signal.connect(self.refresh)
        self.refresh()

    def refresh(self):
        start, end = field_date(self.start), field_date(self.end)
        try:
            self.rows = activity(self.catalog.ledger, self.catalog.review, start, end)
        except ValueError as exc:
            self.rows = ()
            self.error.setText(str(exc))
        else:
            self.error.clear()
        self.summary.setText(f"Операций в файле: {len(self.rows)} · предпросмотр первых 5")
        self.preview.setColumnHidden(3, not self.details.isChecked())
        fill_table(self.preview, [(row.day, display(row.amount, row.currency), row.category, row.description)
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
            text = export_text(self.rows, start, end, kind=kind, details=self.details.isChecked())
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
    def __init__(self, parent, catalog):
        super().__init__(parent)
        self.catalog = catalog
        self.document_number = None
        self.rows = ()
        self.goal_funds = ()
        self.setWindowTitle("Финансы")
        self.resize(1110, 740)
        self.setMinimumSize(930, 600)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 18, 22, 18)
        layout.setSpacing(12)
        heading = QHBoxLayout()
        heading.addWidget(label("Финансы", "screenTitle"), 1)
        heading.addWidget(button("Справочники…", self.open_directory))
        heading.addWidget(button("Экспорт…", self.export))
        self.primary = button("+ Операция", self.new_operation, primary=True)
        heading.addWidget(self.primary)
        layout.addLayout(heading)

        filters = QHBoxLayout()
        self.range = ChoiceBox()
        for key, text in (("month", "Последний месяц"), ("quarter", "3 месяца"),
                          ("all", "Весь период"), ("custom", "Свой период")):
            self.range.addItem(text, key)
        self.currency = ChoiceBox()
        self.currency.setAccessibleName("Валюта дашборда")
        today = QDate.currentDate()
        self.start, self.end = date_field(today.addMonths(-1).addDays(1).toString(Qt.DateFormat.ISODate)), date_field(
            today.toString(Qt.DateFormat.ISODate)
        )
        filters.addWidget(self.range)
        for text, field in (("С", self.start), ("По", self.end)):
            filters.addWidget(label(text))
            filters.addWidget(field)
        filters.addStretch(1)
        filters.addWidget(label("Валюта"))
        filters.addWidget(self.currency)
        layout.addLayout(filters)

        metrics = QHBoxLayout()
        self.metrics = {}
        for key, text, style in (
            ("income", "Доходы за период", "financePositive"),
            ("expense", "Расходы за период", "financeExpense"),
            ("net", "Итог за период", "financeValue"),
            ("free", "Свободно сейчас", "financeValue"),
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

        tabs = QTabWidget()
        tabs.setObjectName("financeTabs")
        tabs.addTab(self.build_dashboard(), "Итоги")
        tabs.addTab(self.build_operations(), "Операции")
        tabs.addTab(self.build_wallets(), "Кошельки и цели")
        tabs.addTab(self.build_investments(), "Инвестиции и вклады")
        layout.addWidget(tabs, 1)
        bottom = QHBoxLayout()
        bottom.addStretch(1)
        bottom.addWidget(button("Закрыть", self.accept))
        layout.addLayout(bottom)
        self.range.currentIndexChanged.connect(self.update_dates)
        self.start.dateChanged.connect(self.custom_dates)
        self.end.dateChanged.connect(self.custom_dates)
        self.currency.currentIndexChanged.connect(self.refresh)
        self.update_currency()
        self.update_dates()

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
        panels = QHBoxLayout()
        left, right = QVBoxLayout(), QVBoxLayout()
        panels.addLayout(left, 1)
        panels.addLayout(right, 1)
        section(left, "Доходы по источникам")
        section(right, "Расходы по статьям")
        self.incomes = table(("Источник", "Сумма"), 0)
        self.expenses = table(("Статья", "Сумма"), 0)
        left.addWidget(self.incomes, 1)
        right.addWidget(self.expenses, 1)
        layout.addLayout(panels, 1)
        self.balances = label(style="muted", wrap=True)
        layout.addWidget(self.balances)
        actions = QHBoxLayout()
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
        self.search.setPlaceholderText("Поиск по кошельку, статье или описанию")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self.refresh_operations)
        top.addWidget(self.search, 1)
        top.addWidget(button("Выписки и сверка…", self.open_ledger))
        layout.addLayout(top)
        self.operations = table(("Дата", "Кошелёк", "Вид", "Статья / источник", "Сумма", "Описание"), 5)
        self.operations.cellDoubleClicked.connect(lambda *_: self.edit_operation())
        layout.addWidget(self.operations, 1)
        controls = QHBoxLayout()
        self.operation_count = label(style="muted")
        controls.addWidget(self.operation_count, 1)
        self.edit_button = button("Изменить…", self.edit_operation)
        controls.addWidget(self.edit_button)
        self.classify_button = button("Статья…", self.classify_operation)
        controls.addWidget(self.classify_button)
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
            start = today.addMonths(-3 if selected == "quarter" else -1).addDays(1)
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
        self.currency.blockSignals(True)
        self.currency.clear()
        for currency in sorted(codes or {"RUB"}):
            self.currency.addItem(currency, currency)
        index = self.currency.findData(current)
        self.currency.setCurrentIndex(max(0, index))
        self.currency.blockSignals(False)

    def refresh(self):
        currency = self.currency.currentData() or "RUB"
        try:
            start, end = self.bounds()
            self.rows = activity(self.catalog.ledger, self.catalog.review, start, end, currency)
        except ValueError as exc:
            self.rows = ()
            self.chart.set_period((), field_date(self.end), field_date(self.end))
            self.error.setText(str(exc))
            self.error.show()
        else:
            self.error.hide()
            self.chart.set_period(self.rows, start, end)
        income, expense = flow_totals(self.rows)
        cash = self.catalog.liquid_totals().get(currency, 0)
        reserved = self.catalog.funds.reserved().get(currency, 0)
        for name, value in (("income", income), ("expense", expense), ("net", income - expense),
                            ("free", cash - reserved)):
            self.metrics[name].setText(display(value, currency))
        totals = flow_breakdown(self.rows)
        for key, widget in (("income", self.incomes), ("expense", self.expenses)):
            breakdown = [(category, value) for (_, kind, category), value in totals.items()
                         if kind == key]
            fill_table(widget, [(category, display(value, currency)) for category, value in
                                sorted(breakdown, key=lambda item: (-item[1], item[0]))])
        self.balances.setText(
            f"На счетах сейчас: {display(cash, currency)}  ·  Отложено: {display(reserved, currency)}"
        )
        self.refresh_operations()
        self.refresh_positions(currency)
        self.refresh_goals(currency)
        self.primary.setEnabled(self.catalog.editable)
        for control in (self.distribute_button, self.goals_distribute):
            control.setEnabled(self.catalog.editable and self.error.isHidden())
        self.edit_button.setEnabled(self.catalog.editable)
        self.classify_button.setEnabled(self.catalog.editable)

    def refresh_operations(self):
        query = self.search.text().casefold().split()
        titles = {asset.asset_number: asset.title for asset in self.catalog.assets}
        self.filtered = tuple(row for row in self.rows if all(
            word in f"{titles[row.account]} {row.category} {row.description} {row.recipient}".casefold()
            for word in query
        ))
        fill_table(self.operations, [
            (row.day, titles[row.account], FLOW_LABELS[row.kind], row.category, display(row.amount, row.currency),
             row.description + (f" · комиссия {amount_text(row.fee)}" if row.fee else ""))
            for row in self.filtered
        ])
        self.operation_count.setText(f"Операций: {len(self.filtered)}")
        if self.filtered:
            self.operations.selectRow(0)

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
        if currency:
            self.currency.setCurrentIndex(self.currency.findData(currency))
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
        account = next((asset.asset_number for asset in accounts if asset.currency == self.currency.currentData()), None)
        if not accounts:
            account = self.new_wallet()
            if account is None:
                return
        dialog = OperationEditor(self, self.catalog, account=account)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            entry = dialog.catalog.ledger.entries[-1]
            currency = dialog.catalog.ledger.opening(entry.account).currency
            self.use_catalog(dialog.catalog, currency=currency)
            if not field_date(self.start) <= entry.day <= field_date(self.end):
                self.start.setDate(min(self.start.date(), QDate.fromString(entry.day, Qt.DateFormat.ISODate)))
                self.end.setDate(max(self.end.date(), QDate.fromString(entry.day, Qt.DateFormat.ISODate)))

    def selected_operation(self):
        index = self.operations.currentRow()
        return self.filtered[index] if 0 <= index < len(self.filtered) else None

    def edit_operation(self):
        if not self.catalog.editable or (row := self.selected_operation()) is None:
            return
        entry = self.catalog.ledger.entries[row.number - 1]
        used = any(move.operation == row.number for move in self.catalog.funds.movements)
        if entry.source != "manual" or used or row.kind == "transfer":
            self.classify_operation()
            return
        dialog = OperationEditor(self, self.catalog, number=row.number)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.use_catalog(dialog.catalog)

    def classify_operation(self):
        if not self.catalog.editable or (row := self.selected_operation()) is None:
            return
        if row.kind == "transfer":
            QMessageBox.information(self, "Связанный перевод", "Для изменения связи откройте «Разбор операций».")
            return
        dialog = CategoryDialog(self, self.catalog, row.number)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            try:
                self.use_catalog(self.catalog.save_review(dialog.result_review))
            except (OSError, ValueError) as exc:
                QMessageBox.warning(self, "Статья", str(exc))

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
        self.use_catalog(dialog.catalog)

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
        ExportDialog(self, self.catalog, start, end).exec()
