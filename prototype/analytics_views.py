"""Finance report pages for composition, trends, loans and savings."""

from collections import Counter
from html import escape

from PySide6.QtCore import Signal, Qt
from PySide6.QtGui import QColor, QIcon, QPixmap
from PySide6.QtWidgets import (
    QButtonGroup, QHBoxLayout, QStackedWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from analytics import account_history, calendar_buckets, debt_history, flow_series, fund_history
from appearance import ChoiceBox, button, frame, label
from finance_charts import COLORS, OTHER_COLOR, DonutChart, PlotLine, Slice, TrendChart, money
from ledger_views import table

GROUPING = {"day": "По дням", "week": "По неделям", "month": "По месяцам", "year": "По годам"}
CATEGORY_OR_SOURCE = {"expense": "Статьи", "income": "Источники"}


def compact_choice(name, width=14):
    choice = ChoiceBox()
    choice.setAccessibleName(name)
    choice.setSizeAdjustPolicy(ChoiceBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
    choice.setMinimumContentsLength(width)
    return choice


def set_choices(choice, entries):
    previous = choice.currentData()
    choice.blockSignals(True)
    choice.clear()
    for name, key in entries:
        choice.addItem(name, key)
        choice.setItemData(choice.count() - 1, escape(name), Qt.ItemDataRole.ToolTipRole)
    choice.setCurrentIndex(max(0, choice.findData(previous)))
    choice.blockSignals(False)


def chart_card(layout, title="", *, stretch=1):
    box = frame("analyticsCard")
    inside = QVBoxLayout(box)
    inside.setContentsMargins(12, 9, 12, 10)
    inside.setSpacing(6)
    if title:
        inside.addWidget(label(title, "sectionTitle"))
    layout.addWidget(box, stretch)
    return inside


class FlowPage(QWidget):
    operations_requested = Signal(str, object, object)

    def __init__(self, kind):
        super().__init__()
        self.kind = kind
        self.first = None
        self.currency = ""
        self.series_total = 0
        self.parts = ()
        self.title = "Расходы" if kind == "expense" else "Доходы"
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        options = QHBoxLayout()
        self.account = compact_choice("Кошелёк", 16)
        self.group = compact_choice("Разрез")
        self.group.addItem(CATEGORY_OR_SOURCE[kind], "category")
        self.group.addItem("Кошельки", "wallet")
        self.grain = compact_choice("Шаг графика", 13)
        self.grain.addItem("Автоматически", "auto")
        for key, name in GROUPING.items():
            self.grain.addItem(name, key)
        for title, item in (("Кошелёк", self.account), ("Разрез", self.group), ("Шаг", self.grain)):
            options.addWidget(label(title, "muted"))
            options.addWidget(item)
        options.addStretch(1)
        layout.addLayout(options)

        charts = QHBoxLayout()
        charts.setSpacing(10)
        left = chart_card(charts, "Структура расходов" if kind == "expense" else "Структура доходов", stretch=4)
        self.donut = DonutChart()
        self.donut.selected.connect(self.select_part)
        self.donut.setMaximumHeight(204)
        left.addWidget(self.donut, 5)
        self.legend = table(("Статья" if kind == "expense" else "Источник", "Сумма", "Доля"), 0)
        self.legend.setMinimumHeight(90)
        self.legend.verticalHeader().setDefaultSectionSize(26)
        self.legend.setAccessibleName("Состав по группам")
        self.legend.itemSelectionChanged.connect(self.select_legend)
        left.addWidget(self.legend, 4)

        right = chart_card(charts, stretch=6)
        heading = QHBoxLayout()
        heading.addWidget(label("Динамика расходов" if kind == "expense" else "Динамика доходов", "sectionTitle"), 1)
        self.detail = compact_choice("Линии на графике", 18)
        self.detail.currentIndexChanged.connect(self.plot)
        heading.addWidget(self.detail)
        right.addLayout(heading)
        self.trend = TrendChart()
        right.addWidget(self.trend, 1)
        footer = QHBoxLayout()
        self.summary = label(style="muted", wrap=True)
        footer.addWidget(self.summary, 1)
        self.open = button("Операции…", self.open_operations)
        footer.addWidget(self.open)
        right.addLayout(footer)
        layout.addLayout(charts, 1)
        for choice in (self.account, self.group, self.grain):
            choice.currentIndexChanged.connect(self.calculate)

    def refresh(self, catalog, rows, first, last, currency):
        self.rows, self.first, self.last, self.currency = rows, first, last, currency
        wallets = [asset for asset in catalog.assets if asset.category == "account" and asset.currency == currency]
        names = {asset.asset_number: asset.title for asset in wallets}
        counts = Counter(names.values())
        self.names = {key: (name if counts[name] == 1 else f"{name} · № {key}")
                      for key, name in names.items()}
        set_choices(self.account, [("Все кошельки", None), *((name, key) for key, name in self.names.items())])
        self.calculate()

    def calculate(self):
        by_wallet = self.group.currentData() == "wallet"
        self.legend.horizontalHeaderItem(0).setText(
            "Кошелёк" if by_wallet else "Статья" if self.kind == "expense" else "Источник"
        )
        self.parts = ()
        self.series_total = 0
        if self.first is not None:
            self.unit, self.buckets = calendar_buckets(self.first, self.last, self.grain.currentData())
            self.total_values, self.parts = flow_series(self.rows, self.buckets, self.kind, by_wallet=by_wallet,
                                                        account=self.account.currentData())
            self.series_total = sum(self.total_values)
        group_name = "Все кошельки" if by_wallet else "Все статьи" if self.kind == "expense" else "Все источники"
        choices = [(group_name, 0)]
        if len(self.parts) > 1:
            choices.append(("Сравнить крупнейшие", -1))
        choices.extend((self.name(item.group), item.group) for item in self.parts)
        set_choices(self.detail, choices)
        slices = [
            Slice(item.group, self.name(item.group), item.total, COLORS[index])
            for index, item in enumerate(self.parts[:len(COLORS)])
        ]
        if len(self.parts) > len(COLORS):
            rest = self.parts[len(COLORS):]
            slices.append(Slice(None, f"Остальные группы · {len(rest)}", sum(item.total for item in rest), OTHER_COLOR))
        self.donut.set_parts(slices, self.currency)
        self.fill_legend()
        self.plot()

    def name(self, key):
        return self.names[key] if isinstance(key, int) else key

    def fill_legend(self):
        self.legend.blockSignals(True)
        self.legend.clearContents()
        self.legend.setRowCount(len(self.parts))
        for index, part in enumerate(self.parts):
            name = self.name(part.group)
            icon = QPixmap(9, 9)
            icon.fill(QColor(COLORS[index] if index < len(COLORS) else OTHER_COLOR))
            title = QTableWidgetItem(QIcon(icon), name)
            title.setToolTip(escape(name))
            self.legend.setItem(index, 0, title)
            ratio = part.total / self.series_total
            percent = "<0,1%" if ratio < 0.001 else f"{ratio:.1%}".replace(".", ",")
            for column, value in ((1, money(part.total, self.currency)), (2, percent)):
                item = QTableWidgetItem(value)
                item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                self.legend.setItem(index, column, item)
        self.legend.blockSignals(False)

    def select_part(self, key):
        index = self.detail.findData(key)
        if index >= 0:
            self.detail.setCurrentIndex(index)

    def select_legend(self):
        index = self.legend.currentRow()
        if self.legend.selectedItems() and 0 <= index < len(self.parts):
            self.select_part(self.parts[index].group)

    def plot(self):
        self.open.setEnabled(bool(self.parts))
        if not self.parts:
            self.trend.set_plot((), (), self.currency)
            self.summary.setText("За выбранный период нет операций этого вида." if self.first is not None
                                 else "Укажите корректный период.")
            return
        key = self.detail.currentData()
        if key == -1:
            lines = tuple(PlotLine(self.name(part.group), part.values, COLORS[index])
                          for index, part in enumerate(self.parts[:4]))
            summary = f"Групп на графике {len(lines)} из {len(self.parts)}"
        elif key == 0:
            lines = (PlotLine(self.title, self.total_values, COLORS[0]),)
            summary = f"За период {money(self.series_total, self.currency)}"
        else:
            index, part = next((index, part) for index, part in enumerate(self.parts) if part.group == key)
            color = COLORS[index] if index < len(COLORS) else OTHER_COLOR
            lines = (PlotLine(self.name(key), part.values, color),)
            summary = f"За период {money(part.total, self.currency)}"
        self.donut.active = key if key not in (0, -1) else None
        self.donut.update()
        self.legend.blockSignals(True)
        self.legend.clearSelection()
        if self.donut.active is not None:
            self.legend.selectRow(next(index for index, item in enumerate(self.parts) if item.group == key))
        self.legend.blockSignals(False)
        labels = []
        for part in self.buckets:
            first, last = part.start, part.end
            if self.unit == "year":
                text = str(first.year) if first.year == last.year else f"{first.year}–{last.year}"
            elif self.unit == "month":
                text = first.strftime("%m.%y")
            else:
                text = first.strftime("%d.%m")
            full = first.strftime("%d.%m.%Y")
            if last != first:
                full += last.strftime(" — %d.%m.%Y")
            labels.append((text, full))
        self.trend.set_plot(labels, lines, self.currency)
        self.summary.setText(f"{summary}  ·  {GROUPING[self.unit].lower()}")

    def open_operations(self):
        key = self.detail.currentData()
        selected = key if key not in (0, -1) else None
        category, account = None, self.account.currentData()
        if self.group.currentData() == "wallet":
            account = selected if selected is not None else account
        else:
            category = selected
        self.operations_requested.emit(self.kind, category, account)


class BalancePage(QWidget):
    fund_requested = Signal(int)

    def __init__(self, kind):
        super().__init__()
        self.kind = kind
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        settings = QHBoxLayout()
        settings.addWidget(label("Долг" if kind == "debt" else "Накопления", "muted"))
        self.subject = compact_choice("Долг или счёт накоплений", 24)
        settings.addWidget(self.subject, 1)
        settings.addStretch(1)
        self.open_fund = button("Движения фонда…", self.show_fund)
        self.open_fund.hide()
        settings.addWidget(self.open_fund)
        layout.addLayout(settings)
        card = chart_card(layout)
        self.heading = label(style="sectionTitle")
        self.value = label(style="financeValue", wrap=True)
        self.change = label(style="muted", wrap=True)
        for widget in (self.heading, self.value, self.change):
            card.addWidget(widget)
        self.trend = TrendChart()
        card.addWidget(self.trend, 1)
        self.notice = label(style="muted", wrap=True)
        card.addWidget(self.notice)
        self.subject.currentIndexChanged.connect(self.plot)

    def refresh(self, catalog, first, last, currency, tagged):
        self.catalog, self.first, self.last = catalog, first, last
        self.currency, self.tagged = currency, tagged
        if self.kind == "debt":
            self.debts = {item.number: item for item in catalog.review.debts.items if item.currency == currency}
            choices = [("Я должен · все долги", "borrowed"), ("Мне должны · все долги", "lent")]
            choices.extend((item.title + (" · я должен" if item.direction == "borrowed" else " · мне должны"),
                            item.number) for item in self.debts.values())
        else:
            self.funds = {item.number: item for item in catalog.funds.funds if item.currency == currency}
            self.accounts = {item.asset_number: item for item in catalog.assets
                             if item.category == "account" and item.currency == currency}
            choices = [("Все фонды и цели", "funds"), ("Действующие счета с журналом", "accounts")]
            choices.extend((f"Фонд · {item.title}", str(item.number)) for item in self.funds.values())
            choices.extend((f"Счёт · {item.title}", item.asset_number) for item in self.accounts.values())
        set_choices(self.subject, choices)
        self.plot()

    def plot(self):
        self.value.clear()
        self.change.clear()
        self.notice.clear()
        self.fund_number = None
        self.open_fund.hide()
        target, notices = None, []
        if self.first is None:
            self.heading.clear()
            self.trend.set_plot((), (), self.currency, empty="Укажите корректный период")
            return
        selection = self.subject.currentData()
        ledger = self.catalog.ledger
        if self.kind == "debt":
            numbers = {selection} if isinstance(selection, int) else {
                item.number for item in self.debts.values() if item.direction == selection
            }
            history = debt_history(self.catalog.review.debts, ledger, numbers, self.first, self.last)
            title = "Остаток основного долга"
            notices.append("Каждая ступень отражает выдачу или возврат основного долга.")
        elif selection == "accounts" or isinstance(selection, int):
            snapshot = self.accounts.get(selection)
            numbers = {selection} if snapshot else {item.asset_number for item in self.accounts.values()
                                                    if item.status == "active"}
            history = account_history(ledger, numbers, self.first, self.last, snapshot=snapshot)
            title = "Остаток денег на счетах" if snapshot is None else "Остаток на счёте"
            if snapshot is None:
                notices.append("Сумма по действующим счетам с даты их начального остатка.")
            elif ledger.opening(snapshot.asset_number) is None and snapshot.amount is not None:
                notices.append("У счёта есть одна сохранённая оценка.")
        else:
            fund = self.funds.get(int(selection)) if selection != "funds" else None
            numbers = {fund.number} if fund is not None else set(self.funds)
            history = fund_history(self.catalog.funds, ledger, numbers, self.first, self.last)
            target = fund.goal if fund is not None and not history.undated else None
            title = "Накоплено в фондах" if fund is None else "Накоплено в фонде"
            if history.undated:
                title = "Изменение по датированным движениям"
                notices.append(f"Движений без даты · {history.undated}. Их даты можно указать в истории фонда.")
            elif any(amount < 0 for _, amount in history.points):
                notices.append("По сохранённым датам часть списаний предшествует пополнениям фонда.")
            if fund is not None:
                self.fund_number = fund.number
                self.open_fund.show()
        self.heading.setText(title)
        if self.tagged:
            notices.append("На графике все движения выбранного фонда, счёта или долга.")
        self.notice.setText(" ".join(notices))
        if not history.points:
            self.trend.set_plot((), (), self.currency, empty="За выбранный период пока нет истории")
            self.value.setText("—")
            if history.undated:
                self.change.setText(f"Всего в фонде сейчас {money(history.current, self.currency)}")
            return
        days, values = zip(*history.points)
        labels = tuple((day.strftime("%d.%m"), day.strftime("%d.%m.%Y")) for day in days)
        closing = values[-1]
        self.value.setText(money(closing, self.currency))
        details = []
        if history.undated:
            details.append(f"Всего в фондах сейчас {money(history.current, self.currency)}")
        else:
            details.append(days[-1].strftime("На %d.%m.%Y"))
            opening = history.opening if history.opening is not None else values[0] if len(values) > 1 else None
            if opening is not None:
                change = closing - opening
                amount = ("+" if change > 0 else "") + money(change, self.currency)
                caption = "Изменение" if history.opening is not None else "С первой записи"
                details.append(f"{caption} {amount}")
        if target is not None:
            details.append(f"Цель {money(target, self.currency)}")
        self.change.setText("  ·  ".join(details))
        self.trend.set_plot(labels, (PlotLine(title, values, COLORS[1] if self.kind == "debt" else COLORS[0]),),
                            self.currency, time_span=(self.first, self.last), days=days, target=target)

    def show_fund(self):
        if self.fund_number is not None:
            self.fund_requested.emit(self.fund_number)


class AnalyticsPanel(QWidget):
    operations_requested = Signal(str, object, object)
    fund_requested = Signal(int)

    def __init__(self):
        super().__init__()
        self.setObjectName("formContent")
        self.state = None
        self.pages = {"expense": FlowPage("expense"), "income": FlowPage("income"),
                      "debt": BalancePage("debt"), "saving": BalancePage("saving")}
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 11, 14, 12)
        layout.setSpacing(8)
        choices = QHBoxLayout()
        self.modes = QButtonGroup(self)
        self.mode_buttons = {}
        self.stack = QStackedWidget()
        self.mode = "expense"
        for key, name in (("expense", "Расходы"), ("income", "Доходы"), ("debt", "Долги"), ("saving", "Накопления")):
            option = button(name, lambda checked, mode=key: self.switch(mode))
            option.setObjectName("reportMode")
            option.setCheckable(True)
            self.modes.addButton(option)
            self.mode_buttons[key] = option
            choices.addWidget(option)
            self.stack.addWidget(self.pages[key])
        choices.addStretch(1)
        self.mode_buttons[self.mode].setChecked(True)
        layout.addLayout(choices)
        layout.addWidget(self.stack, 1)
        for key in ("expense", "income"):
            self.pages[key].operations_requested.connect(self.operations_requested.emit)
        self.pages["saving"].fund_requested.connect(self.fund_requested.emit)

    def refresh(self, catalog, rows, first, last, currency, *, tagged=False):
        self.state = catalog, rows, first, last, currency, tagged
        self.render()

    def render(self):
        if self.state is None:
            return
        catalog, rows, first, last, currency, tagged = self.state
        view = self.pages[self.mode]
        if self.mode in ("expense", "income"):
            view.refresh(catalog, rows, first, last, currency)
        else:
            view.refresh(catalog, first, last, currency, tagged)

    def switch(self, mode, *, subject=None):
        self.mode = mode
        self.mode_buttons[mode].setChecked(True)
        self.stack.setCurrentWidget(self.pages[mode])
        self.render()
        if subject is not None:
            selector = self.pages[mode].subject
            selector.setCurrentIndex(max(0, selector.findData(subject)))
