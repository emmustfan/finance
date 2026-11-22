"""Registry and editor for accounts, investments and property."""

import subprocess
import sys

from PySide6.QtCore import QSize, Qt
from PySide6.QtWidgets import (
    QDialog, QFormLayout, QHBoxLayout, QLineEdit, QListWidget, QListWidgetItem,
    QMessageBox, QSizePolicy, QVBoxLayout, QWidget,
)

from appearance import ChoiceBox, area, button, frame, label
from assets import Asset, CATEGORIES, KINDS, STATUSES, amount_text, parse_amount, totals_by_category
from documents import Catalog


class AssetEditor(QDialog):
    def __init__(self, parent, catalog: Catalog, asset: Asset | None):
        super().__init__(parent)
        self.setWindowTitle("Изменить карточку" if asset else "Новый счёт или актив")
        self.resize(780, 595)
        self.result_asset: Asset | None = None
        self.documents = {card.archive_number for card in catalog.cards}

        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 18, 22, 18)
        layout.addWidget(label(self.windowTitle(), "screenTitle"))
        content = QWidget()
        content.setObjectName("formContent")
        panels = QHBoxLayout(content)
        panels.setContentsMargins(0, 4, 6, 4)
        panels.setSpacing(18)
        form = QFormLayout()
        form.setSpacing(7)
        panels.addLayout(form, 3)

        self.title = QLineEdit(asset.title if asset else "")
        self.owner = QLineEdit(asset.owner if asset else "")
        self.organization = QLineEdit(asset.organization if asset else "")
        self.kind = QLineEdit(asset.kind if asset else "")
        self.category = ChoiceBox()
        for key, title in CATEGORIES.items():
            self.category.addItem(title, key)
        self.category.setCurrentIndex(self.category.findData(asset.category if asset else "account"))
        self.category.setAccessibleName("Категория")
        self.category.toggle.setAccessibleName("Открыть категории")
        self.category.currentIndexChanged.connect(self.kind_hint)
        self.kind_hint()
        self.status = ChoiceBox()
        for key, title in STATUSES.items():
            self.status.addItem(title, key)
        self.status.setAccessibleName("Статус")
        self.status.toggle.setAccessibleName("Открыть статусы")
        if asset:
            self.status.setCurrentIndex(self.status.findData(asset.status))

        self.amount = QLineEdit(amount_text(asset.amount) if asset and asset.amount is not None else "")
        self.amount.setPlaceholderText("125 500,50")
        self.currency = QLineEdit(asset.currency if asset else "RUB")
        self.currency.setMaxLength(3)
        self.currency.setMaximumWidth(62)
        money = QHBoxLayout()
        money.addWidget(self.amount, 1)
        money.addWidget(self.currency)
        self.as_of = QLineEdit(asset.as_of if asset else "")
        self.as_of.setPlaceholderText("ГГГГ-ММ-ДД")
        self.pointer = QLineEdit(asset.access_pointer if asset else "")
        self.pointer.setPlaceholderText("База → группа → запись")
        form.addRow("Название", self.title)
        form.addRow("Владелец", self.owner)
        form.addRow("Категория", self.category)
        form.addRow("Тип", self.kind)
        form.addRow("Банк / организация", self.organization)
        form.addRow("Статус", self.status)
        form.addRow("Остаток / оценка", money)
        form.addRow("На дату", self.as_of)
        form.addRow("Путь в KeePassXC", self.pointer)
        form.addRow("", label("Укажите, где искать запись в KeePassXC.", "small", wrap=True))

        links = QVBoxLayout()
        panels.addLayout(links, 2)
        links.addWidget(label("Связанные документы", "sectionTitle"))
        search = QLineEdit()
        search.setPlaceholderText("Поиск по названию или номеру")
        links.addWidget(search)
        self.cards = QListWidget()
        self.cards.setAccessibleName("Документы для связи")
        self.cards.setMinimumHeight(170)
        links.addWidget(self.cards, 1)
        selected = set(asset.document_numbers) if asset else set()
        for card in catalog.cards:
            item = QListWidgetItem(f"№ {card.archive_label} · {card.title}")
            item.setData(Qt.ItemDataRole.UserRole, card.archive_number)
            item.setSizeHint(QSize(0, 30))
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(
                Qt.CheckState.Checked if card.archive_number in selected else Qt.CheckState.Unchecked
            )
            self.cards.addItem(item)
        search.textChanged.connect(self.filter_cards)
        if not catalog.cards:
            links.addWidget(label("В архиве пока нет документов.", "muted", wrap=True))
        layout.addWidget(area(content), 1)

        self.problem = label(style="muted", wrap=True)
        self.problem.hide()
        layout.addWidget(self.problem)
        actions = QHBoxLayout()
        actions.addStretch(1)
        actions.addWidget(button("Отмена", self.reject))
        actions.addWidget(button("Сохранить", self.save, primary=True))
        layout.addLayout(actions)
        self.title.setFocus()

    def kind_hint(self) -> None:
        self.kind.setPlaceholderText(", ".join(KINDS[self.category.currentData()][:2]))

    def filter_cards(self, query: str) -> None:
        for row in range(self.cards.count()):
            item = self.cards.item(row)
            item.setHidden(not all(term in item.text().casefold() for term in query.casefold().split()))

    def save(self) -> None:
        numbers = tuple(
            self.cards.item(row).data(Qt.ItemDataRole.UserRole) for row in range(self.cards.count())
            if self.cards.item(row).checkState() == Qt.CheckState.Checked
        )
        try:
            asset = Asset(
                title=self.title.text().strip(), category=self.category.currentData(), owner=self.owner.text().strip(),
                kind=self.kind.text().strip(), organization=self.organization.text().strip(),
                status=self.status.currentData(), amount=parse_amount(self.amount.text()),
                currency=self.currency.text().strip().upper(), as_of=self.as_of.text().strip(),
                access_pointer=self.pointer.text().strip(), document_numbers=numbers,
            )
            asset.validate(self.documents)
        except ValueError as exc:
            self.problem.setText(str(exc))
            self.problem.show()
            return
        self.result_asset = asset
        self.accept()


class RegistryDialog(QDialog):
    def __init__(self, parent, catalog: Catalog):
        super().__init__(parent)
        self.catalog = catalog
        self.document_number: int | None = None
        self.setWindowTitle("Счета и активы")
        self.resize(895, 610)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 18)
        heading = QHBoxLayout()
        heading.addWidget(label("Счета и активы", "screenTitle"), 1)
        self.add_button = button("+ Счёт или актив", self.edit_asset, primary=True)
        self.add_button.setEnabled(catalog.editable)
        heading.addWidget(self.add_button)
        layout.addLayout(heading)
        layout.addWidget(label("Известные суммы действующих карточек по валютам", "muted"))
        summary = QHBoxLayout()
        self.totals = {}
        for key, name in CATEGORIES.items():
            panel = frame("keyInfo")
            contents = QVBoxLayout(panel)
            contents.setContentsMargins(11, 8, 11, 8)
            contents.addWidget(label(name, "sectionTitle"))
            self.totals[key] = label(style="muted", wrap=True)
            contents.addWidget(self.totals[key])
            summary.addWidget(panel, 1)
        layout.addLayout(summary)

        body = QHBoxLayout()
        left = QVBoxLayout()
        body.addLayout(left, 2)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Поиск по названию или владельцу")
        left.addWidget(self.search)
        self.rows = QListWidget()
        self.rows.setAccessibleName("Счета и активы в архиве")
        self.rows.setSpacing(3)
        left.addWidget(self.rows, 1)

        detail = frame("card")
        body.addWidget(detail, 3)
        display = QVBoxLayout(detail)
        display.setContentsMargins(13, 11, 13, 11)
        self.title = label(style="documentTitle", wrap=True)
        display.addWidget(self.title)
        self.values = label(style="muted", wrap=True)
        self.values.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)
        display.addWidget(self.values)
        self.access_heading = label("Запись KeePassXC", "sectionTitle")
        display.addWidget(self.access_heading)
        self.pointer = label(style="muted", wrap=True)
        self.pointer.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        display.addWidget(self.pointer)
        self.keepass_button = button("Открыть KeePassXC", self.open_keepass)
        display.addWidget(self.keepass_button, alignment=Qt.AlignmentFlag.AlignLeft)
        self.links_heading = label("Связанные документы", "sectionTitle")
        display.addWidget(self.links_heading)
        self.links = QListWidget()
        self.links.setAccessibleName("Связанные документы")
        self.links.itemDoubleClicked.connect(self.open_document)
        display.addWidget(self.links, 1)
        self.open_button = button("Открыть документ", lambda: self.open_document(self.links.currentItem()))
        self.links.currentItemChanged.connect(lambda item, _: self.open_button.setEnabled(item is not None))
        display.addWidget(self.open_button, alignment=Qt.AlignmentFlag.AlignLeft)
        layout.addLayout(body, 1)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self.edit_button = button("Изменить…", lambda: self.edit_asset(edit=True))
        buttons.addWidget(self.edit_button)
        buttons.addWidget(button("Закрыть", self.reject))
        layout.addLayout(buttons)

        self.search.textChanged.connect(lambda _: self.refresh())
        self.rows.currentItemChanged.connect(lambda *_: self.show_asset())
        self.refresh()

    def selected_index(self) -> int | None:
        item = self.rows.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item is not None else None

    def refresh(self, preferred: int | None = None) -> None:
        if preferred is None:
            preferred = self.selected_index()
        for key, totals in totals_by_category(self.catalog.assets).items():
            values = [f"{amount_text(value)} {currency}" for currency, value in sorted(totals.items())]
            self.totals[key].setText("\n".join(values) if values else "—")
        self.rows.blockSignals(True)
        self.rows.clear()
        for index, asset in enumerate(self.catalog.assets):
            if asset.matches(self.search.text()):
                item = QListWidgetItem(f"{asset.title} · {asset.owner}")
                item.setToolTip(item.text())
                item.setData(Qt.ItemDataRole.UserRole, index)
                item.setSizeHint(QSize(0, 36))
                self.rows.addItem(item)
                if index == preferred:
                    self.rows.setCurrentItem(item)
        if self.rows.count() and self.rows.currentRow() < 0:
            self.rows.setCurrentRow(0)
        self.rows.blockSignals(False)
        self.show_asset()

    def show_asset(self) -> None:
        index = self.selected_index()
        self.links.clear()
        self.open_button.setEnabled(False)
        if index is None:
            self.title.setText("Счета и активы не найдены" if self.catalog.assets else "Добавьте первый счёт или актив")
            self.values.clear()
            self.pointer.clear()
        else:
            asset = self.catalog.assets[index]
            self.title.setText(asset.title)
            details = [
                " · ".join(filter(None, (CATEGORIES[asset.category], asset.kind, STATUSES[asset.status]))),
                f"Владелец: {asset.owner}",
            ]
            if asset.organization:
                details.append(f"Банк / организация: {asset.organization}")
            valuation = asset.valuation
            if asset.as_of:
                valuation = f"{valuation} на {asset.as_of}"
            details.append(valuation)
            self.values.setText("\n".join(details))
            self.pointer.setText(asset.access_pointer)
            linked = set(asset.document_numbers)
            for card in self.catalog.cards:
                if card.archive_number in linked:
                    item = QListWidgetItem(f"№ {card.archive_label} · {card.title}")
                    item.setData(Qt.ItemDataRole.UserRole, card.archive_number)
                    item.setSizeHint(QSize(0, 32))
                    self.links.addItem(item)
            if self.links.count():
                self.links.setCurrentRow(0)
        self.access_heading.setVisible(bool(self.pointer.text()))
        self.pointer.setVisible(bool(self.pointer.text()))
        self.keepass_button.setVisible(bool(self.pointer.text()))
        self.links_heading.setVisible(bool(self.links.count()))
        self.links.setVisible(bool(self.links.count()))
        self.open_button.setVisible(bool(self.links.count()))
        self.edit_button.setEnabled(index is not None and self.catalog.editable)

    def edit_asset(self, *, edit: bool = False) -> None:
        if not self.catalog.editable:
            return
        index = self.selected_index() if edit else None
        if edit and index is None:
            return
        editor = AssetEditor(self, self.catalog, self.catalog.assets[index] if index is not None else None)
        if editor.exec() != QDialog.DialogCode.Accepted or editor.result_asset is None:
            return
        try:
            if index is None:
                self.catalog = self.catalog.add_asset(editor.result_asset)
                index = len(self.catalog.assets) - 1
            else:
                self.catalog = self.catalog.update_asset(index, editor.result_asset)
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, "Операция не выполнена", str(exc))
            return
        if not self.catalog.assets[index].matches(self.search.text()):
            self.search.blockSignals(True)
            self.search.clear()
            self.search.blockSignals(False)
        self.refresh(index)

    def open_document(self, item) -> None:
        if item is not None:
            self.document_number = item.data(Qt.ItemDataRole.UserRole)
            self.accept()

    def open_keepass(self) -> None:
        try:
            if sys.platform != "darwin":
                raise OSError("Кнопка запуска KeePassXC предназначена для macOS.")
            subprocess.run(
                ["/usr/bin/open", "-a", "KeePassXC"], capture_output=True, check=True, timeout=10,
            )
        except subprocess.CalledProcessError:
            QMessageBox.warning(
                self, "Не удалось открыть KeePassXC",
                "Проверьте, установлен ли KeePassXC. Указатель на запись можно скопировать из карточки.",
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            QMessageBox.warning(self, "Не удалось открыть KeePassXC", str(exc))
