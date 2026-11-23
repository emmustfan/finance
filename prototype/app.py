"""Desktop entry point for the encrypted family archive."""

import subprocess
import sys
from datetime import date, timedelta
from html import escape
from io import BytesIO
from pathlib import Path

import pypdfium2 as pdfium
from cryptography.exceptions import InvalidTag
from PySide6.QtCore import QSize, Qt, QTimer
from PySide6.QtGui import QAction, QKeySequence, QPixmap, QShortcut, QTextDocument
from PySide6.QtPrintSupport import QPrintDialog, QPrinter
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QComboBox, QDialog, QFileDialog, QFrame,
    QHBoxLayout, QHeaderView, QInputDialog,
    QLineEdit, QListWidget, QListWidgetItem, QMainWindow, QMenu, QMessageBox,
    QSizePolicy, QSplitter, QTableWidget, QTableWidgetItem,
    QToolButton, QVBoxLayout, QWidget,
)
from PIL.ImageQt import ImageQt
from PIL import Image

from appearance import ChoiceBox, TagEditor, TagFlow, apply_style, area, button, clear_tags, frame, label, tag_button
from asset_views import RegistryDialog, launch_keepass
from assets import Asset
from documents import (
    CARD_FIELDS, MAX_ATTACHMENT_BYTES, SCOPES, Card, Catalog, Photo, document_deadline, read_pdf, read_photo, tag_names,
)
from finance_views import FinanceDialog
from funds import Fund, Funds, Line, Template
from ledger import Ledger, Opening
from ledger_views import import_statements
from review import Recurring, Review
from sample import sample_pdf
from vault import copy_verified

VAULT_FILTER = "Семейный архив (*.fvault)"


class AttachmentPreview(QFrame):
    def __init__(self, report_error, parent=None):
        super().__init__(parent)
        self.setObjectName("preview")
        self.report_error = report_error
        self.document = None
        self.data = None
        self.photo: Photo | None = None
        self.attachments: tuple[bytes | Photo, ...] = ()
        self.page = 0
        self.fit_timer = QTimer(self)
        self.fit_timer.setSingleShot(True)
        self.fit_timer.setInterval(90)
        self.fit_timer.timeout.connect(self.render)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        self.chooser = ChoiceBox()
        self.chooser.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.chooser.setMinimumContentsLength(12)
        self.chooser.setAccessibleName("Выбрать вложение")
        self.chooser.toggle.setAccessibleName("Открыть список вложений")
        self.chooser.currentIndexChanged.connect(self.show_attachment)
        layout.addWidget(self.chooser)
        self.image = label("Выберите документ с вложением", "muted")
        self.image.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.image.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored)
        self.image.setMinimumHeight(130)
        layout.addWidget(self.image, 1)
        controls = QHBoxLayout()
        self.previous = button("‹", lambda: self.move(-1))
        self.previous.setAccessibleName("Предыдущая страница")
        self.previous.setToolTip(self.previous.accessibleName())
        self.next = button("›", lambda: self.move(1))
        self.next.setAccessibleName("Следующая страница")
        self.next.setToolTip(self.next.accessibleName())
        self.page_label = label(style="muted")
        self.print_button = button("Печать", self.print_attachment)
        controls.addWidget(self.previous)
        controls.addWidget(self.next)
        controls.addWidget(self.page_label)
        controls.addStretch(1)
        controls.addWidget(self.print_button)
        layout.addLayout(controls)
        self.set_card(None)

    def set_card(self, card: Card | None) -> None:
        pdf = card.pdf if card is not None else None
        photos = card.photos if card is not None else ()
        self.attachments = ((pdf,) if pdf is not None else ()) + photos
        self.chooser.blockSignals(True)
        self.chooser.clear()
        for attachment in self.attachments:
            self.chooser.addItem(attachment.name if isinstance(attachment, Photo) else "PDF")
        self.chooser.blockSignals(False)
        self.chooser.setVisible(len(self.attachments) > 1)
        self.show_attachment(0)
        if card is None:
            self.image.setText("Выберите документ с вложением")

    def show_attachment(self, index: int) -> None:
        self.fit_timer.stop()
        if self.document is not None:
            self.document.close()
        self.document = None
        self.data = None
        self.photo = None
        self.page = 0
        self.image.setText("У карточки нет вложений")
        if 0 <= index < len(self.attachments):
            attachment = self.attachments[index]
            if isinstance(attachment, Photo):
                self.photo = attachment
                self.fit_timer.start()
                self.update_controls()
                return
            try:
                doc = pdfium.PdfDocument(attachment)
                if not len(doc):
                    doc.close()
                    raise ValueError("В PDF нет страниц.")
            except (ValueError, pdfium.PdfiumError) as exc:
                self.image.setText("Не удалось открыть PDF")
                self.report_error(exc)
            else:
                self.document, self.data = doc, attachment
                self.fit_timer.start()
        self.update_controls()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self.document is not None or self.photo is not None:
            self.fit_timer.start()

    def update_controls(self) -> None:
        count = len(self.document) if self.document is not None else 0
        self.previous.setEnabled(self.page > 0)
        self.next.setEnabled(self.page + 1 < count)
        self.print_button.setEnabled(count > 0 or self.photo is not None)
        if self.photo is not None:
            self.page_label.setText("Фотография")
        else:
            self.page_label.setText(f"{self.page + 1} / {count}" if count else "Предпросмотр")

    def move(self, delta: int) -> None:
        if self.document is not None and 0 <= self.page + delta < len(self.document):
            self.page += delta
            self.render()
            self.update_controls()

    def render(self) -> None:
        ratio = self.devicePixelRatioF()
        space = (
            min(1600, max(1, round((self.image.width() - 8) * ratio))),
            min(1600, max(1, round((self.image.height() - 8) * ratio))),
        )
        if self.photo is not None:
            try:
                pixmap = QPixmap.fromImage(ImageQt(self.photo.render(space)))
                pixmap.setDevicePixelRatio(ratio)
                self.image.setPixmap(pixmap)
            except (OSError, ValueError) as exc:
                self.image.setText("Не удалось отобразить фотографию")
                self.report_error(exc)
            return
        if self.document is None:
            return
        try:
            page = self.document[self.page]
            try:
                width, height = page.get_size()
                if width <= 0 or height <= 0:
                    raise ValueError("У страницы PDF некорректный размер.")
                scale = min(space[0] / width, space[1] / height)
                bitmap = page.render(scale=scale)
                try:
                    pixmap = QPixmap.fromImage(ImageQt(bitmap.to_pil()))
                    pixmap.setDevicePixelRatio(ratio)
                    self.image.setPixmap(pixmap)
                finally:
                    bitmap.close()
            finally:
                page.close()
        except (ValueError, IndexError, pdfium.PdfiumError) as exc:
            self.image.setText("Не удалось отобразить страницу")
            self.page_label.setText(str(exc))

    def print_attachment(self) -> None:
        if self.data is None and self.photo is None:
            return
        reply = QMessageBox.question(
            self, "Печать вложения", "Отправить вложение на принтер по умолчанию?",
            QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel,
        )
        if reply != QMessageBox.StandardButton.Ok:
            return
        try:
            data = self.data
            command = ["/usr/bin/lp", "-t", "Family archive document"]
            if self.photo is not None:
                photo = self.photo.render((1454, 2139))
                page = Image.new("RGB", (1654, 2339), "white")
                offset = ((page.width - photo.width) // 2, (page.height - photo.height) // 2)
                page.paste(photo, offset, mask=photo.getchannel("A"))
                stream = BytesIO()
                page.save(stream, format="PDF", resolution=200)
                data = stream.getvalue()
                command.extend(("-o", "fit-to-page"))
            result = subprocess.run(
                [*command, "-"],
                input=data, capture_output=True, timeout=30, check=False,
            )
            if result.returncode:
                raise RuntimeError(result.stderr.decode(errors="replace").strip() or "Ошибка CUPS.")
        except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as exc:
            self.report_error(exc)
            return
        QMessageBox.information(self, "Печать", "Задание передано в очередь печати.")


class DocumentTypeBox(ChoiceBox):
    def __init__(self, kinds: tuple[str, ...], value: str):
        super().__init__()
        self.setEditable(True)
        self.addItem("")
        self.addItems(kinds)
        self.setCurrentText(value)
        self.lineEdit().setPlaceholderText("Выберите или впишите свой тип")
        self.toggle.raise_()


class CardDialog(QDialog):
    def __init__(self, parent, kinds: tuple[str, ...], card: Card | None, *, tag_suggestions=()):
        super().__init__(parent)
        self.setWindowTitle("Изменить карточку" if card is not None else "Новый документ")
        self.resize(710, 600)
        self.pdf = card.pdf if card is not None else None
        self.pdf_name = "PDF"
        self.original_expiry = card.expires if card is not None else ""
        self.photos = card.photos if card is not None else ()
        self.card: Card | None = None
        self.inputs: dict[str, QLineEdit | QComboBox] = {}

        main = QVBoxLayout(self)
        main.setContentsMargins(24, 20, 24, 20)
        main.setSpacing(12)
        main.addWidget(label(self.windowTitle(), "screenTitle"))
        main.addWidget(label(
            "Укажите название и добавьте вложение или адрес оригинала.", "muted", wrap=True
        ))

        form = QWidget()
        form.setObjectName("formContent")
        form_layout = QHBoxLayout(form)
        form_layout.setContentsMargins(0, 0, 6, 0)
        form_layout.setSpacing(18)
        document = QVBoxLayout()
        document.setSpacing(9)
        original = QVBoxLayout()
        original.setSpacing(9)
        form_layout.addLayout(document, 1)
        form_layout.addLayout(original, 1)
        document.addWidget(label("О документе", "sectionTitle"))
        for key in ("title", "kind", "number"):
            document.addLayout(self.field(key, card, kinds))
        dates = QHBoxLayout()
        dates.addLayout(self.field("date", card, kinds), 1)
        dates.addLayout(self.field("expires", card, kinds), 1)
        document.addLayout(dates)
        for key in ("party", "note"):
            document.addLayout(self.field(key, card, kinds))
        document.addStretch(1)

        original.addWidget(label("Теги", "sectionTitle"))
        self.tag_editor = TagEditor(card.tags if card is not None else "", suggestions=tag_suggestions)
        original.addWidget(self.tag_editor)
        original.addSpacing(3)
        original.addWidget(label("Вложения", "sectionTitle"))
        attachment = QHBoxLayout()
        attachment.addWidget(button("+ PDF", self.choose_pdf))
        attachment.addWidget(button("+ Фото", self.choose_photos))
        attachment.addStretch(1)
        original.addLayout(attachment)
        self.attachment_list = QListWidget()
        self.attachment_list.setAccessibleName("Вложения карточки")
        self.attachment_list.setMaximumHeight(78)
        self.attachment_list.setTextElideMode(Qt.TextElideMode.ElideMiddle)
        original.addWidget(self.attachment_list)
        self.remove_attachment_button = button("Убрать выбранное", self.remove_attachment)
        original.addWidget(self.remove_attachment_button, alignment=Qt.AlignmentFlag.AlignLeft)
        self.attachment_list.currentRowChanged.connect(
            lambda row: self.remove_attachment_button.setEnabled(row >= 0)
        )
        self.refresh_attachments()
        original.addSpacing(3)
        original.addWidget(label("Бумажный оригинал", "sectionTitle"))
        for key in ("cabinet", "folder"):
            original.addLayout(self.field(key, card, kinds))
        position = QHBoxLayout()
        position.addLayout(self.field("section", card, kinds), 1)
        position.addLayout(self.field("position", card, kinds), 1)
        original.addLayout(position)
        original.addStretch(1)
        main.addWidget(area(form), 1)
        self.problem = label(style="muted", wrap=True)
        self.problem.hide()
        main.addWidget(self.problem)
        actions = QHBoxLayout()
        actions.addStretch(1)
        actions.addWidget(button("Отмена", self.reject))
        actions.addWidget(button("Сохранить", self.save, primary=True))
        main.addLayout(actions)
        self.inputs["title"].setFocus()

    def field(self, key: str, card: Card | None, kinds: tuple[str, ...]) -> QVBoxLayout:
        title = dict(CARD_FIELDS)[key]
        value = getattr(card, key) if card is not None else ""
        if key == "kind":
            field = DocumentTypeBox(kinds, value)
        else:
            field = QLineEdit(value)
            if key == "expires":
                field.setPlaceholderText("ДД.ММ.ГГГГ")
        field.setAccessibleName(title)
        self.inputs[key] = field
        layout = QVBoxLayout()
        layout.setSpacing(3)
        layout.addWidget(label(title))
        layout.addWidget(field)
        return layout

    def choose_pdf(self) -> None:
        filename, _ = QFileDialog.getOpenFileName(self, "Выберите PDF", "", "PDF (*.pdf)")
        if not filename:
            return
        try:
            path = Path(filename)
            data = read_pdf(path)
            with pdfium.PdfDocument(data) as doc:
                if not len(doc):
                    raise ValueError("В PDF нет страниц.")
        except (OSError, ValueError, pdfium.PdfiumError) as exc:
            QMessageBox.warning(self, "Не удалось приложить PDF", str(exc))
            return
        self.pdf = data
        self.pdf_name = path.name
        if not self.inputs["title"].text().strip():
            self.inputs["title"].setText(path.stem)
        self.refresh_attachments()

    def choose_photos(self) -> None:
        filenames, _ = QFileDialog.getOpenFileNames(
            self, "Выберите фотографии", "", "Фотографии (*.jpg *.jpeg *.png)"
        )
        if not filenames:
            return
        photos = []
        total = len(self.pdf or b"") + sum(len(photo.data) for photo in self.photos)
        for filename in filenames:
            path = Path(filename)
            try:
                photo = read_photo(path)
                total += len(photo.data)
                if total > MAX_ATTACHMENT_BYTES:
                    raise ValueError("Вложения превышают доступный размер архива.")
                photos.append(photo)
            except (OSError, ValueError) as exc:
                QMessageBox.warning(self, "Не удалось добавить фото", f"{path.name}\n{exc}")
                return
        self.photos = (*self.photos, *photos)
        if len(filenames) == 1 and not self.inputs["title"].text().strip():
            self.inputs["title"].setText(Path(filenames[0]).stem)
        self.refresh_attachments()

    def refresh_attachments(self) -> None:
        self.attachment_list.clear()
        if self.pdf is not None:
            self.attachment_list.addItem(self.pdf_name)
        self.attachment_list.addItems([photo.name for photo in self.photos])
        has_attachments = bool(self.attachment_list.count())
        self.attachment_list.setVisible(has_attachments)
        self.remove_attachment_button.setVisible(has_attachments)
        if has_attachments:
            self.attachment_list.setCurrentRow(0)

    def remove_attachment(self) -> None:
        row = self.attachment_list.currentRow()
        if row < 0:
            return
        if row == 0 and self.pdf is not None:
            self.pdf = None
        else:
            index = row - (1 if self.pdf is not None else 0)
            self.photos = (*self.photos[:index], *self.photos[index + 1:])
        self.refresh_attachments()

    def save(self) -> None:
        values = {
            key: (widget.currentText() if isinstance(widget, QComboBox) else widget.text()).strip()
            for key, widget in self.inputs.items()
        }
        card = Card(**values, tags=self.tag_editor.value(), pdf=self.pdf, photos=self.photos)
        try:
            if card.expires and card.expires != self.original_expiry and document_deadline(card.expires) is None:
                raise ValueError("Введите срок действия в формате ДД.ММ.ГГГГ или оставьте поле пустым.")
            card.validate()
        except ValueError as exc:
            self.problem.setText(str(exc))
            self.problem.show()
            return
        self.card = card
        self.accept()


class TagDialog(QDialog):
    def __init__(self, parent, existing: str, suggestions=()):
        super().__init__(parent)
        self.setWindowTitle("Теги документа")
        self.setMinimumWidth(360)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 20, 22, 20)
        layout.addWidget(label("Теги документа", "screenTitle"))
        self.editor = TagEditor(existing, suggestions=suggestions)
        layout.addWidget(self.editor)
        actions = QHBoxLayout()
        actions.addStretch(1)
        actions.addWidget(button("Отмена", self.reject))
        actions.addWidget(button("Сохранить", self.accept, primary=True))
        layout.addLayout(actions)
        self.editor.input.setFocus()


class DocumentRow(QWidget):
    def __init__(self, card: Card, select, filter_tag):
        super().__init__()
        self.select = select
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 7, 10, 7)
        layout.setSpacing(3)
        title = label(card.title, "rowTitle")
        title.setToolTip(card.title)
        title.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        layout.addWidget(title)
        metadata = label("  ·  ".join(filter(None, (f"№ {card.archive_label}", card.kind))), "small")
        metadata.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        layout.addWidget(metadata)
        tags = tag_names(card.tags)
        if tags:
            tags_row = QHBoxLayout()
            tags_row.setSpacing(5)
            for name in tags[:2]:
                chip = tag_button(name)
                chip.setMaximumWidth(116)
                chip.clicked.connect(lambda _, name=name: filter_tag(name))
                tags_row.addWidget(chip)
            if len(tags) > 2:
                tags_row.addWidget(label(f"+{len(tags) - 2}", "small"))
            tags_row.addStretch(1)
            layout.addLayout(tags_row)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.select()
        super().mousePressEvent(event)


INVENTORY_COLUMNS = ("№ карточки", "Документ", "Раздел", "Номер в папке")


def inventory_values(card: Card) -> tuple[str, ...]:
    document = f"{card.title}\n№ {card.number}" if card.number else card.title
    return card.archive_label, document, card.section, card.position


class FolderDialog(QDialog):
    def __init__(self, parent, catalog: Catalog):
        super().__init__(parent)
        self.setWindowTitle("Бумажные папки")
        self.resize(830, 505)
        self.folders = catalog.folder_inventory()
        self.location: tuple[str, str] | None = None
        self.archive_number: int | None = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 18, 22, 18)
        layout.addWidget(label("Бумажные папки", "screenTitle"))
        content = QHBoxLayout()
        self.folder_list = QListWidget()
        self.folder_list.setAccessibleName("Папки в архиве")
        self.folder_list.setFixedWidth(220)
        content.addWidget(self.folder_list)
        for cabinet, folder in self.folders:
            item = QListWidgetItem(f"{folder} · {cabinet}" if cabinet else folder)
            item.setToolTip(item.text())
            self.folder_list.addItem(item)
        self.folder_list.currentRowChanged.connect(self.select_folder)
        right = QVBoxLayout()
        content.addLayout(right, 1)
        self.heading = label(style="sectionTitle", wrap=True)
        right.addWidget(self.heading)
        self.inventory = QTableWidget(0, len(INVENTORY_COLUMNS))
        self.inventory.setAccessibleName("Опись папки")
        self.inventory.setHorizontalHeaderLabels(INVENTORY_COLUMNS)
        self.inventory.verticalHeader().hide()
        self.inventory.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.inventory.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.inventory.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.inventory.setAlternatingRowColors(True)
        header = self.inventory.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.inventory.cellDoubleClicked.connect(lambda row, _: self.open_card(row))
        right.addWidget(self.inventory, 1)
        layout.addLayout(content, 1)
        actions = QHBoxLayout()
        actions.addStretch(1)
        actions.addWidget(button("Закрыть", self.reject))
        self.print_button = button("Распечатать опись…", self.print_inventory, primary=True)
        actions.addWidget(self.print_button)
        layout.addLayout(actions)
        if self.folders:
            self.folder_list.setCurrentRow(0)
        else:
            self.heading.setText("Укажите папку в карточке документа, чтобы составить опись.")
            self.print_button.setEnabled(False)

    def select_folder(self, row: int) -> None:
        if row < 0:
            return
        self.location = tuple(self.folders)[row]
        cards = self.folders[self.location]
        self.heading.setText(" → ".join(filter(None, self.location)))
        self.inventory.setRowCount(len(cards))
        for row, card in enumerate(cards):
            for column, value in enumerate(inventory_values(card)):
                self.inventory.setItem(row, column, QTableWidgetItem(value))
        self.inventory.resizeRowsToContents()

    def open_card(self, row: int) -> None:
        if self.location is not None:
            self.archive_number = self.folders[self.location][row].archive_number
            self.accept()

    def printable_inventory(self) -> QTextDocument:
        document = QTextDocument()
        columns = "".join(f"<th>{escape(column)}</th>" for column in INVENTORY_COLUMNS)
        rows = []
        for card in self.folders[self.location]:
            cells = "".join(
                f"<td>{escape(value).replace(chr(10), '<br>')}</td>" for value in inventory_values(card)
            )
            rows.append(f"<tr>{cells}</tr>")
        location = escape(self.heading.text())
        document.setHtml(
            f"<h1>Опись папки</h1><p>{location}</p>"
            f'<table border="1" cellspacing="0" cellpadding="7" width="100%">'
            f"<thead><tr>{columns}</tr></thead>{''.join(rows)}</table>"
        )
        return document

    def print_inventory(self) -> None:
        if self.location is None:
            return
        printer = QPrinter(QPrinter.PrinterMode.HighResolution)
        printer.setDocName("Опись бумажной папки")
        if QPrintDialog(printer, self).exec() == QDialog.DialogCode.Accepted:
            self.printable_inventory().print_(printer)


class ArchiveWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.catalog: Catalog | None = None
        self.displayed_card: Card | None = None
        self.active_tag = ""
        self.setWindowTitle("Семейный архив")
        self.resize(1240, 810)
        self.setMinimumSize(980, 650)

        central = QWidget()
        self.setCentralWidget(central)
        root = QHBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.addWidget(self.build_sidebar())

        main = QWidget()
        main.setObjectName("workspace")
        layout = QVBoxLayout(main)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        root.addWidget(main, 1)
        layout.addWidget(self.build_top_bar())

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setContentsMargins(18, 18, 18, 18)
        splitter.setHandleWidth(12)
        splitter.setChildrenCollapsible(False)
        splitter.addWidget(self.build_document_list())
        splitter.addWidget(self.build_preview())
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([350, 620])
        layout.addWidget(splitter, 1)
        self.find_shortcut = QShortcut(QKeySequence(QKeySequence.StandardKey.Find), self)
        self.find_shortcut.activated.connect(self.search.setFocus)
        self.new_shortcut = QShortcut(QKeySequence(QKeySequence.StandardKey.New), self)
        self.new_shortcut.activated.connect(self.save_card)
        self.show_catalog(None)

    def build_sidebar(self) -> QFrame:
        sidebar = frame("sidebar")
        sidebar.setFixedWidth(224)
        layout = QVBoxLayout(sidebar)
        layout.setContentsMargins(15, 20, 15, 16)
        layout.setSpacing(8)
        brand = QHBoxLayout()
        mark = label("СА", "brandMark")
        mark.setFixedSize(34, 34)
        mark.setAlignment(Qt.AlignmentFlag.AlignCenter)
        brand.addWidget(mark)
        brand.addWidget(label("Семейный архив", "brand"), 1)
        layout.addLayout(brand)

        layout.addSpacing(16)
        layout.addWidget(label("ОТКРЫТЫЙ АРХИВ", "sideSection"))
        self.archive_name = label("Архив не открыт", "sideFile", wrap=True)
        layout.addWidget(self.archive_name)
        self.archive_hint = label(style="sideCaption", wrap=True)
        layout.addWidget(self.archive_hint)
        layout.addWidget(button("Открыть архив…", self.open_archive, sidebar=True))
        layout.addWidget(button("Новый архив…", lambda: self.create_archive(False), sidebar=True))
        self.finance_button = button("Финансы…", self.open_finance, sidebar=True)
        layout.addWidget(self.finance_button)
        self.import_button = button("Импорт выписки…", self.quick_import, sidebar=True)
        layout.addWidget(self.import_button)
        self.folders_button = button("Бумажные папки…", self.open_folders, sidebar=True)
        layout.addWidget(self.folders_button)
        self.assets_button = button("Счета и активы…", self.open_registry, sidebar=True)
        layout.addWidget(self.assets_button)
        layout.addSpacing(12)
        layout.addWidget(label("ТЕГИ", "sideSection"))
        self.all_documents = button("Все документы", self.clear_filters, sidebar=True)
        self.all_documents.setCheckable(True)
        layout.addWidget(self.all_documents)
        self.no_tags = label("Теги появятся после добавления в карточку.", "sideCaption", wrap=True)
        layout.addWidget(self.no_tags)
        tags = QWidget()
        tags.setObjectName("sidebarTags")
        self.sidebar_tags = TagFlow(tags)
        layout.addWidget(area(tags), 1)

        self.manage = QToolButton()
        self.manage.setText("Действия с архивом")
        self.manage.setProperty("sidebar", True)
        self.manage.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.manage.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        menu = QMenu(self.manage)
        menu.addAction("Открыть KeePassXC", lambda: launch_keepass(self))
        menu.addSeparator()
        menu.addAction("Создать копию…", lambda: self.copy_archive(False))
        menu.addAction("Восстановить из копии…", lambda: self.copy_archive(True))
        menu.addSeparator()
        menu.addAction("Создать тестовый архив…", lambda: self.create_archive(True))
        menu.addAction("Создать тест фондов…", lambda: self.create_archive(True, funds_demo=True))
        self.close_action = QAction("Закрыть архив", self)
        self.close_action.triggered.connect(lambda: self.show_catalog(None))
        menu.addAction(self.close_action)
        self.manage.setMenu(menu)
        layout.addWidget(self.manage)
        return sidebar

    def build_top_bar(self) -> QFrame:
        top = frame("topBar")
        layout = QHBoxLayout(top)
        layout.setContentsMargins(20, 15, 20, 15)
        layout.addWidget(label("Документы", "screenTitle"))
        layout.addStretch(1)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Поиск по названию, номеру, тегу")
        self.search.setAccessibleName("Поиск документов")
        self.search.setClearButtonEnabled(True)
        self.search.setMaximumWidth(390)
        self.search.textChanged.connect(lambda _: self.refresh_list())
        layout.addWidget(self.search, 1)
        self.add_button = button("+  Документ", self.save_card, primary=True)
        layout.addWidget(self.add_button)
        return top

    def build_document_list(self) -> QFrame:
        panel = frame("card")
        panel.setMinimumWidth(289)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(12, 14, 12, 10)
        header = QHBoxLayout()
        self.list_title = label("Все документы", "sectionTitle", wrap=True)
        header.addWidget(self.list_title, 1)
        self.count = label(style="count")
        header.addWidget(self.count)
        layout.addLayout(header)
        self.deadlines_filter = button("Истекают и просрочены", self.toggle_deadlines)
        self.deadlines_filter.setCheckable(True)
        self.deadlines_filter.setToolTip("Документы с истёкшим сроком или сроком в ближайшие 30 дней")
        layout.addWidget(self.deadlines_filter, alignment=Qt.AlignmentFlag.AlignLeft)
        self.rows = QListWidget()
        self.rows.setAccessibleName("Список документов")
        self.rows.setSpacing(3)
        self.rows.currentItemChanged.connect(lambda *_: self.select_card())
        layout.addWidget(self.rows, 1)
        self.list_empty = label(style="muted", wrap=True)
        self.list_empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.list_empty, 1)
        return panel

    def build_preview(self) -> QFrame:
        panel = frame("card")
        panel.setMinimumWidth(386)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(11)
        toolbar = QHBoxLayout()
        self.number = label(style="number")
        toolbar.addWidget(self.number)
        toolbar.addStretch(1)
        self.change_card = button("Изменить…", lambda: self.save_card(edit=True))
        toolbar.addWidget(self.change_card)
        layout.addLayout(toolbar)
        self.card_title = label("Откройте архив", "documentTitle", wrap=True)
        layout.addWidget(self.card_title)
        self.card_type = label(style="muted", wrap=True)
        layout.addWidget(self.card_type)
        self.deadline = label(style="deadlineSoon", wrap=True)
        layout.addWidget(self.deadline)
        chips = QWidget()
        self.card_tags = TagFlow(chips)
        layout.addWidget(chips)
        info = frame("keyInfo")
        info_layout = QVBoxLayout(info)
        info_layout.setContentsMargins(10, 8, 10, 8)
        self.details = label(style="muted", wrap=True)
        info_layout.addWidget(self.details)
        self.metadata = area(info)
        self.metadata.setMaximumHeight(120)
        self.metadata.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
        layout.addWidget(self.metadata)
        self.preview = AttachmentPreview(self.error)
        layout.addWidget(self.preview, 1)
        return panel

    def password(self, prompt: str) -> str | None:
        value, accepted = QInputDialog.getText(self, "Пароль архива", prompt, QLineEdit.EchoMode.Password)
        return value if accepted else None

    def save_path(self, title: str) -> Path | None:
        name, _ = QFileDialog.getSaveFileName(self, title, "", VAULT_FILTER)
        if not name:
            return None
        path = Path(name)
        return path if path.suffix else path.with_suffix(".fvault")

    def source_path(self, title: str) -> Path | None:
        name, _ = QFileDialog.getOpenFileName(self, title, "", VAULT_FILTER)
        return Path(name) if name else None

    def error(self, exc: Exception) -> None:
        if isinstance(exc, InvalidTag):
            detail = "Неверный пароль или нарушена целостность архива."
        elif isinstance(exc, FileExistsError):
            detail = "Этот файл уже существует. Выберите другое имя."
        else:
            detail = str(exc)
        QMessageBox.warning(self, "Операция не выполнена", detail)

    def create_archive(self, demo: bool, *, funds_demo: bool = False) -> None:
        scope = "family"
        if not demo:
            choices = {label: key for key, label in SCOPES.items() if key != "legacy"}
            choice, ok = QInputDialog.getItem(self, "Новый архив", "Область учёта", list(choices), editable=False)
            if not ok:
                return
            scope = choices[choice]
        path = self.save_path("Создать тестовый архив" if demo else "Новый архив")
        if path is None:
            return
        password = self.password("Новый пароль, от 12 символов")
        if password is None:
            return
        confirmation = self.password("Повторите новый пароль")
        if confirmation is None:
            return
        if password != confirmation:
            self.error(ValueError("Пароли не совпадают."))
            return
        try:
            cards = (
                Card("Страховой полис на квартиру", kind="Страховой полис", tags="Дом, 2024",
                     cabinet="Шкаф в кабинете", folder="03 · Недвижимость", pdf=sample_pdf()),
                Card("Паспорт", kind="Паспорт", party="Анна", tags="Анна, Семья", folder="02 · Личные документы"),
                Card("Договор обслуживания", kind="Договор", tags="Дом, 2025", folder="03 · Недвижимость"),
            ) if demo else ()
            assets = (
                Asset("Квартира", "property", "Семья", kind="Квартира",
                      amount=850_000_000, as_of="2026-11-01",
                      access_pointer="Семейные доступы → Страхование → Полис квартиры",
                      document_numbers=(1, 3)),
                Asset("Основной счёт", "account", "Анна", organization="Т-Банк"),
                Asset("Накопительный счёт", "account", "Анна", organization="Т-Банк"),
                Asset("Покупки", "account", "Анна", organization="Ozon Банк"),
                Asset("Расчётный счёт ИП", "account", "Анна", kind="Счёт ИП", organization="Модульбанк"),
            ) if demo else ()
            ledger = Ledger(openings=(
                Opening(2, "2026-10-31", 1_000_000, "RUB", "DEMO-T-MAIN"),
                Opening(3, "2026-10-31", 2_000_000, "RUB", "DEMO-T-SAVINGS"),
                Opening(4, "2026-10-31", 250_000, "RUB", "DEMO-OZON"),
                Opening(5, "2026-10-31", 500_000, "RUB", "DEMO-IP"),
            )) if demo else Ledger()
            review = Review(recurring=(
                Recurring("Аренда квартиры", 2, "аренда квартиры", "Жильё → Аренда", 700_000, 5, 1),
            )) if demo else Review()
            funds = Funds()
            if funds_demo:
                cards = ()
                assets = (Asset("Семейный счёт", "account", "Семья"),)
                ledger = Ledger(openings=(Opening(1, "2026-10-31", 2_000_000, "RUB", "DEMO-FUNDS"),))
                review = Review()
                funds = Funds(funds=(Fund("Семейный резерв", "Подушка", "RUB", 10_000_000, 1),))
                funds = funds.set_template(Template("RUB", (Line(1, "fixed", 6_500_000),)))
                funds = funds.apply("demo-opening", ((1, 500_000),))
            catalog = Catalog.create(path, password, cards=cards, assets=assets, ledger=ledger,
                                     review=review, funds=funds, scope=scope)
        except (OSError, ValueError) as exc:
            self.error(exc)
            return
        self.show_catalog(catalog)

    def open_archive(self) -> None:
        path = self.source_path("Открыть архив")
        if path is None:
            return
        password = self.password("Введите пароль")
        if password is None:
            return
        try:
            catalog = Catalog.open(path, password)
        except (OSError, ValueError, InvalidTag) as exc:
            self.error(exc)
            return
        self.show_catalog(catalog)

    def show_catalog(self, catalog: Catalog | None) -> None:
        self.catalog = catalog
        self.active_tag = ""
        self.deadlines_filter.setChecked(False)
        self.archive_name.setText(
            f"{SCOPES[catalog.scope]} · {catalog.path.name}" if catalog is not None else "Архив не открыт"
        )
        self.archive_hint.setText(
            "Только просмотр. Для правок восстановите личную копию."
            if catalog is not None and not catalog.editable else ""
        )
        self.archive_hint.setVisible(bool(self.archive_hint.text()))
        self.close_action.setEnabled(catalog is not None)
        self.search.setEnabled(catalog is not None)
        self.folders_button.setEnabled(catalog is not None)
        self.assets_button.setEnabled(catalog is not None)
        self.finance_button.setEnabled(catalog is not None)
        self.import_button.setEnabled(catalog is not None and catalog.editable)
        self.add_button.setEnabled(catalog is not None and catalog.editable)
        self.search.blockSignals(True)
        self.search.clear()
        self.search.blockSignals(False)
        self.refresh_sidebar_tags()
        self.refresh_list(0)

    def open_folders(self) -> None:
        if self.catalog is None:
            return
        dialog = FolderDialog(self, self.catalog)
        if dialog.exec() == QDialog.DialogCode.Accepted and dialog.archive_number is not None:
            self.show_document(dialog.archive_number)

    def open_registry(self) -> None:
        if self.catalog is None:
            return
        dialog = RegistryDialog(self, self.catalog)
        result = dialog.exec()
        self.catalog = dialog.catalog
        if result == QDialog.DialogCode.Accepted and dialog.document_number is not None:
            self.show_document(dialog.document_number)

    def open_finance(self) -> None:
        if self.catalog is not None:
            dialog = FinanceDialog(self, self.catalog)
            dialog.exec()
            self.catalog = dialog.catalog
            if dialog.document_number is not None:
                self.show_document(dialog.document_number)

    def quick_import(self) -> None:
        if self.catalog is None or not self.catalog.editable:
            return
        original = self.catalog
        self.catalog = import_statements(self, original)
        if self.catalog is not original:
            dialog = FinanceDialog(self, original, show_operations=True)
            dialog.show_posted(self.catalog)
            dialog.exec()
            self.catalog = dialog.catalog

    def show_document(self, archive_number: int) -> None:
        index = next(
            (idx for idx, card in enumerate(self.catalog.cards) if card.archive_number == archive_number),
            None,
        )
        if index is None:
            return
        self.active_tag = ""
        self.deadlines_filter.setChecked(False)
        self.search.blockSignals(True)
        self.search.clear()
        self.search.blockSignals(False)
        self.refresh_sidebar_tags()
        self.refresh_list(index)

    def refresh_sidebar_tags(self) -> None:
        tags = self.catalog.available_tags if self.catalog is not None else ()
        self.active_tag = next((tag for tag in tags if tag.casefold() == self.active_tag.casefold()), "")
        self.all_documents.setChecked(not self.active_tag and not self.deadlines_filter.isChecked())
        self.no_tags.setVisible(self.catalog is not None and not tags)
        clear_tags(self.sidebar_tags)
        for name in tags:
            chip = tag_button(name)
            chip.clicked.connect(lambda _, name=name: self.set_tag(name))
            if name == self.active_tag:
                chip.setText(f"✓  {chip.text()}")
            self.sidebar_tags.addWidget(chip)
        self.sidebar_tags.parentWidget().updateGeometry()

    def set_tag(self, name: str) -> None:
        if name.casefold() == self.active_tag.casefold():
            name = ""
        self.active_tag = name
        self.refresh_sidebar_tags()
        self.refresh_list()

    def clear_filters(self) -> None:
        self.deadlines_filter.setChecked(False)
        self.search.blockSignals(True)
        self.search.clear()
        self.search.blockSignals(False)
        self.set_tag("")

    def toggle_deadlines(self) -> None:
        self.refresh_sidebar_tags()
        self.refresh_list(-1 if self.deadlines_filter.isChecked() else None)

    def refresh_list(self, preferred: int | None = None) -> None:
        if preferred is None:
            preferred = self.selected_index()
        self.rows.blockSignals(True)
        self.rows.clear()
        cards = self.catalog.cards if self.catalog is not None else ()
        limit = date.today() + timedelta(days=30)
        deadlines = {index: deadline for index, card in enumerate(cards)
                     if card.expires and (deadline := document_deadline(card.expires)) is not None and deadline <= limit}
        self.deadlines_filter.setText(f"Истекают и просрочены · {len(deadlines)}")
        self.deadlines_filter.setVisible(bool(deadlines) or self.deadlines_filter.isChecked())
        order = sorted(deadlines, key=deadlines.get) if self.deadlines_filter.isChecked() else range(len(cards))
        for index in order:
            card = cards[index]
            if not card.matches(self.search.text(), tag=self.active_tag):
                continue
            item = QListWidgetItem()
            item.setData(Qt.ItemDataRole.UserRole, index)
            item.setData(Qt.ItemDataRole.AccessibleTextRole, card.title)
            item.setData(Qt.ItemDataRole.AccessibleDescriptionRole, f"№ {card.archive_label} · {card.kind}")
            item.setToolTip(card.title)
            item.setSizeHint(QSize(0, 72 if card.tags else 54))
            self.rows.addItem(item)
            row = DocumentRow(card, lambda item=item: self.rows.setCurrentItem(item), self.set_tag)
            self.rows.setItemWidget(item, row)
            if index == preferred:
                self.rows.setCurrentItem(item)
        if self.rows.count() and self.rows.currentItem() is None:
            self.rows.setCurrentRow(0)
        current = self.rows.currentItem()
        if current is not None:
            self.rows.scrollToItem(current)
        self.rows.blockSignals(False)
        self.list_title.setText(self.active_tag or ("По сроку действия" if self.deadlines_filter.isChecked()
                                                    else "Все документы"))
        self.count.setText(str(self.rows.count()) if self.catalog is not None else "")
        self.count.setVisible(self.catalog is not None)
        self.rows.setVisible(bool(self.rows.count()))
        if self.catalog is None:
            empty = "Откройте или создайте архив"
        elif not cards:
            empty = "Добавьте первый документ"
        elif self.deadlines_filter.isChecked():
            empty = "Нет документов с ближайшим или истёкшим сроком по выбранным фильтрам."
        else:
            empty = "Документы не найдены"
        self.list_empty.setText(empty)
        self.list_empty.setVisible(not self.rows.count())
        self.select_card()

    def selected_index(self) -> int | None:
        item = self.rows.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item is not None else None

    def select_card(self) -> None:
        index = self.selected_index()
        card = self.catalog.cards[index] if self.catalog is not None and index is not None else None
        if card is not None and card is self.displayed_card:
            return
        self.displayed_card = card
        editable = card is not None and self.catalog.editable
        self.change_card.setVisible(editable)
        self.number.setVisible(card is not None)
        self.card_type.setVisible(card is not None and bool(card.kind))
        self.deadline.hide()
        clear_tags(self.card_tags)
        if card is None:
            self.card_title.setText("Откройте архив" if self.catalog is None else "Документ")
            self.details.clear()
            self.metadata.hide()
            self.preview.set_card(None)
            return
        self.number.setText(f"№ {card.archive_label}")
        self.card_title.setText(card.title)
        self.card_type.setText(card.kind)
        if card.expires and (deadline := document_deadline(card.expires)) is not None:
            days = (deadline - date.today()).days
            if days <= 30:
                self.deadline.setText(f"Срок истёк {deadline:%d.%m.%Y}" if days < 0
                                      else "Срок истекает сегодня" if days == 0 else f"Истекает {deadline:%d.%m.%Y}")
                self.deadline.show()
        for name in tag_names(card.tags):
            chip = tag_button(name)
            chip.clicked.connect(lambda _, name=name: self.set_tag(name))
            self.card_tags.addWidget(chip)
        if editable:
            add_tags = button("Изменить теги…", self.edit_tags)
            add_tags.setObjectName("smallButton")
            self.card_tags.addWidget(add_tags)
        self.card_tags.parentWidget().updateGeometry()
        lines = [
            f"{title}: {getattr(card, key)}"
            for key, title in CARD_FIELDS[2:8] if key != "tags" and getattr(card, key)
        ]
        if card.address:
            lines.append(f"Оригинал: {card.address}")
        self.details.setText("\n".join(lines))
        self.metadata.setVisible(bool(lines))
        self.preview.set_card(card)

    def save_card(self, *, edit: bool = False) -> None:
        if self.catalog is None or not self.catalog.editable:
            return
        index = self.selected_index() if edit else None
        if edit and index is None:
            return
        card = self.catalog.cards[index] if index is not None else None
        dialog = CardDialog(self, self.catalog.available_kinds, card, tag_suggestions=self.catalog.available_tags)
        if dialog.exec() != QDialog.DialogCode.Accepted or dialog.card is None:
            return
        try:
            if index is None:
                self.catalog = self.catalog.add(dialog.card)
                index = len(self.catalog.cards) - 1
            else:
                self.catalog = self.catalog.update(index, dialog.card)
        except (OSError, ValueError) as exc:
            self.error(exc)
            return
        if not self.catalog.cards[index].matches(self.search.text(), tag=self.active_tag):
            self.active_tag = ""
            self.search.blockSignals(True)
            self.search.clear()
            self.search.blockSignals(False)
        if self.deadlines_filter.isChecked():
            deadline = document_deadline(self.catalog.cards[index].expires)
            if deadline is None or deadline > date.today() + timedelta(days=30):
                self.deadlines_filter.setChecked(False)
        self.refresh_sidebar_tags()
        self.refresh_list(index)

    def edit_tags(self) -> None:
        index = self.selected_index()
        if index is None or self.catalog is None or not self.catalog.editable:
            return
        existing = self.catalog.cards[index].tags
        dialog = TagDialog(self, existing, self.catalog.available_tags)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        value = dialog.editor.value()
        if value == existing:
            return
        try:
            self.catalog = self.catalog.set_tags(index, value)
        except (OSError, ValueError) as exc:
            self.error(exc)
            return
        self.refresh_sidebar_tags()
        self.refresh_list(index)

    def copy_archive(self, restore: bool) -> None:
        if self.catalog is not None and not restore:
            destination = self.save_path("Куда сохранить копию открытого архива")
            if destination is None:
                return
            try:
                self.catalog.backup(destination)
            except (OSError, ValueError) as exc:
                self.error(exc)
                return
            QMessageBox.information(self, "Копия создана", str(destination))
            return
        source = self.source_path("Выберите резервную копию" if restore else "Выберите архив")
        if source is None:
            return
        destination = self.save_path("Куда восстановить архив" if restore else "Куда сохранить копию")
        if destination is None:
            return
        password = self.password("Введите пароль исходного архива")
        if password is None:
            return
        try:
            copy_verified(source, destination, password, restore_shared=restore)
            if restore:
                self.show_catalog(Catalog.open(destination, password))
        except (OSError, ValueError, InvalidTag) as exc:
            self.error(exc)
            return
        if not restore:
            QMessageBox.information(self, "Копия создана", str(destination))


def main() -> None:
    app = QApplication(sys.argv)
    app.setApplicationName("Семейный архив")
    apply_style(app)
    window = ArchiveWindow()
    window.show()
    app.exec()


if __name__ == "__main__":
    main()
