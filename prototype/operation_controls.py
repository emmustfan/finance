"""Inline finance article menus and the shared operation tag editor."""

from dataclasses import replace

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QApplication, QDialog, QHBoxLayout, QInputDialog, QMenu, QMessageBox, QPushButton, QStyle, QStyleOptionViewItem,
    QStyledItemDelegate, QVBoxLayout,
)

from appearance import TagEditor, button, label, tag_colors
from documents import tag_names

TAG_NAMES = Qt.ItemDataRole.UserRole
SELECTION_KEYS = Qt.KeyboardModifier.ShiftModifier | Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.MetaModifier


def selecting_rows():
    return bool(QApplication.keyboardModifiers() & SELECTION_KEYS)


class TagCells(QStyledItemDelegate):
    def paint(self, painter, option, index):
        tags = index.data(TAG_NAMES)
        if not tags:
            super().paint(painter, option, index)
            return
        cell = QStyleOptionViewItem(option)
        self.initStyleOption(cell, index)
        cell.text = ""
        cell.widget.style().drawControl(QStyle.ControlElement.CE_ItemViewItem, cell, painter, cell.widget)
        painter.save()
        painter.setClipRect(option.rect)
        x = option.rect.left() + 7
        y = option.rect.center().y() - 11
        right = option.rect.right() - 6
        metrics = option.fontMetrics
        for i, name in enumerate(tags):
            suffix = f"+{len(tags) - i - 1}" if i + 1 < len(tags) else ""
            reserved = metrics.horizontalAdvance(suffix) + 5 if suffix else 0
            width = min(metrics.horizontalAdvance(name) + 16, right - x - reserved)
            if width < 36 and i > 0:
                painter.setPen(QColor("#53657D"))
                painter.drawText(QRectF(x, y, right - x, 22), Qt.AlignmentFlag.AlignVCenter, f"+{len(tags) - i}")
                break
            if width <= 16:
                break
            fill, ink, _ = tag_colors(name)
            shape = QRectF(x, y, width, 22)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(fill))
            painter.drawRoundedRect(shape, 8, 8)
            painter.setPen(QColor(ink))
            painter.drawText(shape.adjusted(8, 0, -8, 0), Qt.AlignmentFlag.AlignVCenter,
                             metrics.elidedText(name, Qt.TextElideMode.ElideRight, width - 16))
            x += width + 5
        painter.restore()


def category_menu(parent, point, catalog, direction: int, current: str = "", *, suggestion: str = ""):
    menu = QMenu(parent)
    clear = menu.addAction("Без статьи")
    menu.addSeparator()
    directory, review, ledger = catalog.directory, catalog.review, catalog.ledger
    choices = (item.name for item in directory.effective(ledger, review)
               if item.direction == direction and not item.archived)
    actions = {}
    if suggestion:
        action = menu.addAction("Предложено: " + suggestion.replace("&", "&&"))
        actions[action] = suggestion
        menu.addSeparator()
    for name in choices:
        action = menu.addAction(name.replace("&", "&&"))
        action.setCheckable(True)
        action.setChecked(name == current)
        actions[action] = name
    menu.addSeparator()
    title = "Новый источник дохода" if direction > 0 else "Новая статья расходов"
    create = menu.addAction(f"+ {title}…")
    picked = menu.exec(point)
    if picked is None:
        return None
    if picked == clear:
        return "", directory
    if picked != create:
        return actions[picked], directory
    while True:
        name, accepted = QInputDialog.getText(parent, title, "Название")
        if not accepted:
            return None
        try:
            new_directory, _ = directory.edit(ledger, review, direction, name)
        except ValueError as exc:
            QMessageBox.warning(parent, title, str(exc))
        else:
            return " ".join(name.split()), new_directory


class CategoryField(QPushButton):
    def __init__(self, catalog, direction=-1, value="", *, suggestion=""):
        super().__init__()
        self.catalog = catalog
        self.direction = direction
        self.suggestion = suggestion
        self.setProperty("primary", False)
        self.setObjectName("categoryField")
        self.setAutoDefault(False)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setAccessibleName("Статья или источник")
        self.set_value(value)
        self.clicked.connect(self.choose)

    def set_value(self, value):
        self.value = value
        prompt = "Выбрать источник" if self.direction > 0 else "Выбрать статью"
        self.setText((value or prompt).replace("&", "&&") + "  ▾")

    def choose(self):
        result = category_menu(self, self.mapToGlobal(self.rect().bottomLeft()), self.catalog,
                               self.direction, self.value, suggestion=self.suggestion)
        if result is not None:
            value, directory = result
            self.catalog = replace(self.catalog, directory=directory)
            self.set_value(value)


def choose_tags(parent, *, known, initial=(), count=1):
    dialog = QDialog(parent)
    dialog.setWindowTitle("Теги операций")
    dialog.setMinimumWidth(435)
    layout = QVBoxLayout(dialog)
    title = "Теги операции" if count == 1 else f"Добавить теги к {count} операциям"
    layout.addWidget(label(title, "screenTitle"))
    editor = TagEditor(", ".join(initial), suggestions=known)
    layout.addWidget(editor)
    problem = label(style="financeError", wrap=True)
    layout.addWidget(problem)
    actions = QHBoxLayout()
    actions.addStretch(1)
    actions.addWidget(button("Отмена", dialog.reject))

    def save():
        if any(len(" ".join(name.split())) > 80 for name in tag_names(editor.value())):
            problem.setText("Сократите тег до 80 символов.")
            return
        dialog.accept()

    actions.addWidget(button("Применить", save, primary=True))
    layout.addLayout(actions)
    editor.input.setFocus()
    if dialog.exec() != QDialog.DialogCode.Accepted:
        return None
    return tag_names(editor.value())
