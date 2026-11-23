"""Visual styles and wrapping tag controls for the archive."""

from zlib import crc32

from PySide6.QtCore import QPoint, QRect, QSize, Qt, Signal
from PySide6.QtGui import QFont, QFontDatabase
from PySide6.QtWidgets import (
    QApplication, QComboBox, QCompleter, QFrame, QLabel, QLayout, QLineEdit, QPushButton, QScrollArea, QToolButton,
    QVBoxLayout, QWidget,
)

from documents import tag_names

PALETTES = (
    ("#E0F3EC", "#195848", "#BFDED3"),
    ("#EFE8FC", "#544184", "#DCD0F2"),
    ("#FCEED9", "#744616", "#EAD6B4"),
    ("#E6EFFC", "#305383", "#CBDDF4"),
    ("#F8E7EE", "#853D5A", "#EACCD9"),
)

STYLE = """
QWidget { color: #1A2737; font-size: 13px; }
QMainWindow, QWidget#workspace { background: #F3F5F8; }
QFrame#sidebar, QWidget#sidebarTags { background: #172536; }
QWidget#formContent { background: white; }
QFrame#card, QFrame#topBar { background: white; }
QFrame#card { border: 1px solid #E0E6ED; border-radius: 16px; }
QFrame#topBar { border-bottom: 1px solid #E1E7EF; }
QFrame#preview { background: #ECF0F5; border: 1px solid #E1E7EE; border-radius: 12px; }
QFrame#keyInfo { background: #F3F6FA; border-radius: 10px; }
QLabel#brandMark { color: #14372F; background: #A8EFDA; border-radius: 10px; font-weight: 700; }
QLabel#brand { color: #FFFFFF; font-size: 15px; font-weight: 700; }
QLabel#sideCaption { color: #A8B8CD; font-size: 12px; }
QLabel#sideSection { color: #A8B8CD; font-size: 11px; font-weight: 700; }
QLabel#sideFile { color: #E2EBF5; background: #223449; border-radius: 9px; padding: 9px; }
QLabel#number { color: #17634D; background: #E2F5EE; border-radius: 8px; padding: 4px 8px; font-weight: 700; }
QLabel#screenTitle { color: #172536; font-size: 25px; font-weight: 700; }
QLabel#documentTitle { color: #172536; font-size: 25px; font-weight: 700; }
QLabel#sectionTitle { color: #25374B; font-size: 13px; font-weight: 700; }
QLabel#muted { color: #53657D; }
QLabel#small { color: #53657D; font-size: 11px; }
QLabel#count { color: #52657A; background: #F0F3F7; border-radius: 8px; padding: 3px 7px; font-size: 11px; }
QFrame#financeMetric { background: #F3F7F6; border: 1px solid #DEEAE5; border-radius: 11px; }
QLabel#financeValue, QLabel#financePositive, QLabel#financeExpense { font-size: 21px; font-weight: 700; }
QLabel#financePositive { color: #146951; }
QLabel#financeExpense, QLabel#financeError { color: #9E3940; }
QLabel#deadlineSoon { color: #7C5018; }
QTabWidget#financeTabs::pane { border: 1px solid #DEE6EC; border-radius: 10px; }
QTabWidget#financeTabs QTabBar::tab {
    padding: 9px 14px; margin-right: 4px; color: #465970; background: #F4F7FA;
    border: 1px solid #E3EAF1; border-bottom-color: transparent;
    border-top-left-radius: 8px; border-top-right-radius: 8px;
}
QTabWidget#financeTabs QTabBar::tab:hover { background: #EFF6F4; color: #146951; }
QTabWidget#financeTabs QTabBar::tab:selected {
    color: #146951; background: #E6F3EE; border-bottom: 2px solid #146951; font-weight: 700;
}
QProgressBar { background: #EAF2EF; border: 0; border-radius: 7px; text-align: center; color: #162D27; }
QProgressBar::chunk { background: #A6DCC8; border-radius: 7px; }
QLabel#rowTitle { color: #203146; font-weight: 700; }
QPushButton, QToolButton {
    color: #25374B; background: #FFFFFF; border: 1px solid #DCE3EC;
    border-radius: 9px; padding: 7px 11px; font-weight: 600;
}
QPushButton:hover, QToolButton:hover { background: #F1F5FA; border-color: #BFCFE0; }
QPushButton:checked { background: #E7F4EF; border-color: #A6DCC8; }
QPushButton:focus, QToolButton:focus { border: 1px solid #14755E; }
QPushButton:disabled, QToolButton:disabled { color: #65768A; background: #F1F3F6; border-color: #E1E6ED; }
QPushButton[primary="true"] { color: white; background: #146951; border-color: #146951; }
QPushButton[primary="true"]:hover { background: #0D5541; }
QPushButton[primary="true"]:focus { border-color: #052D22; }
QPushButton[primary="true"]:disabled { color: #526C62; background: #D9E9E1; border-color: #D9E9E1; }
QPushButton[sidebar="true"], QToolButton[sidebar="true"] {
    color: #E5EDF7; background: transparent; text-align: left;
    border: 1px solid transparent; border-radius: 9px; padding: 8px 9px;
}
QPushButton[sidebar="true"]:hover, QToolButton[sidebar="true"]:hover { background: #26394E; }
QPushButton[sidebar="true"]:focus, QToolButton[sidebar="true"]:focus { border-color: #A8EFDA; }
QPushButton[sidebar="true"]:checked { color: #B6F2DD; background: #274A49; }
QPushButton[sidebar="true"]:disabled, QToolButton[sidebar="true"]:disabled { color: #9AA9BD; }
QPushButton#smallButton { padding: 5px 9px; }
QPushButton#categoryField { text-align: left; }
QToolButton#typeToggle { border: 1px solid transparent; padding: 0; background: transparent; }
QToolButton#typeToggle:hover, QToolButton#typeToggle:focus { border-color: #BFCFE0; background: #E7F4EF; }
QLineEdit, QComboBox {
    color: #1A2737; background: #F8FAFC; border: 1px solid #D7E0EA;
    border-radius: 9px; padding: 8px 10px; selection-background-color: #C4EBDF;
    selection-color: #163D31;
}
QLineEdit:focus, QComboBox:focus { border-color: #388E76; background: white; }
QLineEdit:disabled { color: #65768A; background: #F1F3F6; }
QComboBox { padding-right: 24px; }
QComboBox::drop-down { width: 23px; border: 0; padding-right: 7px; }
QComboBox::down-arrow { width: 9px; height: 9px; }
QDateEdit {
    color: #1A2737; background: #F8FAFC; border: 1px solid #D7E0EA;
    border-radius: 8px; padding: 7px 7px;
}
QDateEdit:focus { border-color: #388E76; background: #FFFFFF; }
QListWidget { background: transparent; border: 0; outline: 0; padding: 1px; }
QListWidget::item { border: 1px solid transparent; border-radius: 11px; }
QListWidget::item:hover { background: #F1F5FA; }
QListWidget::item:selected { color: #1A2737; background: #E7F4EF; border: 1px solid #C6E4D9; }
QTableWidget#ledgerTable {
    background: white; alternate-background-color: #F4F7FA; border: 1px solid #DCE3EC;
    border-radius: 8px; selection-background-color: #E7F4EF; selection-color: #1A2737;
}
QTableWidget#ledgerTable::item { padding: 0 6px; }
QTableWidget#ledgerTable QHeaderView::section {
    background: #EAF0F6; color: #35485E; padding: 7px 6px; border: 0;
}
QDialog { background: white; }
QMenu { color: #1A2737; background: white; border: 1px solid #DBE3EC; padding: 4px; }
QMenu::item { padding: 8px 16px; }
QMenu::item:selected { background: #E7F4EF; }
QMenu::item:disabled { color: #6E7C8C; }
QScrollArea { border: 0; background: transparent; }
QScrollBar:vertical { background: transparent; width: 9px; margin: 2px; }
QScrollBar::handle:vertical { background: #B9C8D6; min-height: 22px; border-radius: 3px; }
QScrollBar::add-line, QScrollBar::sub-line, QScrollBar::add-page, QScrollBar::sub-page {
    border: 0; background: transparent; height: 0;
}
QSplitter::handle { background: transparent; }
"""


def apply_style(app: QApplication) -> None:
    app.setStyle("Fusion")
    families = QFontDatabase.families()
    for name in ("Inter", "Helvetica Neue", "Open Sans", "DejaVu Sans"):
        if name in families:
            app.setFont(QFont(name))
            break
    app.setStyleSheet(STYLE)


def label(text="", style="", *, wrap=False) -> QLabel:
    widget = QLabel(text)
    widget.setObjectName(style)
    widget.setTextFormat(Qt.TextFormat.PlainText)
    widget.setWordWrap(wrap)
    return widget


def button(text, action, *, primary=False, sidebar=False) -> QPushButton:
    widget = QPushButton(text)
    widget.setProperty("primary", primary)
    widget.setProperty("sidebar", sidebar)
    widget.setAutoDefault(False)
    widget.setCursor(Qt.CursorShape.PointingHandCursor)
    widget.clicked.connect(action)
    return widget


def frame(style, parent=None) -> QFrame:
    widget = QFrame(parent)
    widget.setObjectName(style)
    return widget


def area(widget) -> QScrollArea:
    view = QScrollArea()
    view.setWidget(widget)
    view.setWidgetResizable(True)
    view.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    return view


def tag_colors(name: str) -> tuple[str, str, str]:
    return PALETTES[crc32(name.casefold().encode()) % len(PALETTES)]


def tag_button(name: str, *, remove: bool = False) -> QPushButton:
    background, ink, border = tag_colors(name)
    caption = name.replace("&", "&&")
    button = QPushButton(f"{caption}  ×" if remove else caption)
    button.setAutoDefault(False)
    button.setCursor(Qt.CursorShape.PointingHandCursor)
    button.setAccessibleName(f"Удалить тег {name}" if remove else f"Фильтр по тегу {name}")
    button.setToolTip(button.accessibleName())
    button.setStyleSheet(f"""
        QPushButton {{
            color: {ink}; background: {background}; border: 1px solid {border};
            border-radius: 9px; padding: 4px 9px; font-size: 11px;
        }}
        QPushButton:hover, QPushButton:focus {{ border: 1px solid {ink}; }}
        QPushButton:pressed {{ background: {border}; }}
    """)
    return button


class ChoiceBox(QComboBox):
    def __init__(self):
        super().__init__()
        self.toggle = QToolButton(self)
        self.toggle.setObjectName("typeToggle")
        self.toggle.setText("▾")
        self.toggle.setAccessibleName("Открыть список")
        self.toggle.setCursor(Qt.CursorShape.PointingHandCursor)
        self.toggle.clicked.connect(self.showPopup)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.toggle.setGeometry(self.width() - 29, (self.height() - 26) // 2, 25, 26)


class TagFlow(QLayout):
    def __init__(self, parent: QWidget):
        super().__init__(parent)
        self.items = []
        self.setContentsMargins(0, 0, 0, 0)
        self.setSpacing(6)

    def addItem(self, item):
        self.items.append(item)
        self.invalidate()

    def count(self):
        return len(self.items)

    def itemAt(self, index):
        return self.items[index] if 0 <= index < len(self.items) else None

    def takeAt(self, index):
        if 0 <= index < len(self.items):
            item = self.items.pop(index)
            self.invalidate()
            return item
        return None

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, width):
        return self.arrange(QRect(0, 0, width, 0), move=False)

    def setGeometry(self, rect):
        super().setGeometry(rect)
        self.arrange(rect, move=True)

    def minimumSize(self):
        return QSize(0, max((item.minimumSize().height() for item in self.items), default=0))

    def sizeHint(self):
        return self.minimumSize()

    def arrange(self, rect, *, move):
        x, y, row_height = rect.x(), rect.y(), 0
        for item in self.items:
            size = item.sizeHint()
            size.setWidth(min(size.width(), max(1, rect.width())))
            if x > rect.x() and x + size.width() > rect.x() + rect.width():
                x, y, row_height = rect.x(), y + row_height + self.spacing(), 0
            if move:
                item.setGeometry(QRect(QPoint(x, y), size))
            x += size.width() + self.spacing()
            row_height = max(row_height, size.height())
        return y + row_height - rect.y()


class TagEditor(QWidget):
    def __init__(self, existing: str = "", parent=None, *, suggestions: tuple[str, ...] = ()):
        super().__init__(parent)
        self.names = list(tag_names(existing))
        stack = QVBoxLayout(self)
        stack.setContentsMargins(0, 0, 0, 0)
        self.chips = QWidget()
        self.tags = TagFlow(self.chips)
        stack.addWidget(self.chips)
        self.input = QLineEdit()
        self.input.setPlaceholderText("Введите тег и нажмите Enter")
        self.input.setAccessibleName("Добавить тег")
        if suggestions:
            completion = QCompleter(suggestions, self.input)
            completion.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
            completion.setFilterMode(Qt.MatchFlag.MatchContains)
            self.input.setCompleter(completion)
        self.input.returnPressed.connect(self.commit_input)
        self.input.textEdited.connect(self.split_input)
        stack.addWidget(self.input)
        self.refresh()

    def split_input(self, text: str) -> None:
        if "," in text:
            self.commit_input()

    def commit_input(self) -> None:
        self.names = list(tag_names(", ".join((*self.names, self.input.text()))))
        self.input.clear()
        self.refresh()

    def value(self) -> str:
        return ", ".join(tag_names(", ".join((*self.names, self.input.text()))))

    def remove(self, name: str) -> None:
        self.names.remove(name)
        self.refresh()

    def refresh(self) -> None:
        clear_tags(self.tags)
        for name in self.names:
            chip = tag_button(name, remove=True)
            chip.clicked.connect(lambda _, name=name: self.remove(name))
            self.tags.addWidget(chip)
        self.chips.setVisible(bool(self.names))
        self.chips.updateGeometry()


def clear_tags(layout: QLayout) -> None:
    while layout.count():
        item = layout.takeAt(0)
        widget = item.widget()
        widget.hide()
        widget.deleteLater()
