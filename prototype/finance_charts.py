"""Interactive Qt charts for amounts grouped by period and for posted balances."""

from bisect import bisect_left, bisect_right
from dataclasses import dataclass
from datetime import date, timedelta
from html import escape
from math import atan2, degrees, floor, hypot, log10

from PySide6.QtCore import QPointF, QRectF, Signal, Qt
from PySide6.QtGui import QColor, QFont, QLinearGradient, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QToolTip, QWidget

from assets import amount_text

COLORS = ("#168878", "#6867C8", "#B66A2D", "#4679B9", "#B44F78", "#618237", "#855FB0")
OTHER_COLOR = "#8191A8"
TEXT = "#192B3D"
MUTED = "#51647B"
GRID = "#E3EBEF"


def money(value: int, currency: str) -> str:
    return f"{amount_text(value)} {'₽' if currency == 'RUB' else currency}"


def axis_value(value: int) -> str:
    for unit, suffix in ((10**14, "трлн"), (10**11, "млрд"), (10**8, "млн"), (10**5, "тыс.")):
        if abs(value) >= unit:
            compact = f"{value / unit:.2f}".rstrip("0").rstrip(".").replace(".", ",").replace("-", "−")
            return f"{compact} {suffix}"
    return amount_text(value).removesuffix(",00")


def date_label(day: date, span: int) -> str:
    return day.strftime("%d.%m" if span <= 180 else "%d.%m.%y" if span <= 730 else "%m.%y")


@dataclass(frozen=True)
class PlotLine:
    name: str
    values: tuple[int, ...]
    color: str


class TrendChart(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(174)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAccessibleName("График динамики")
        self.lines = ()
        self.labels = ()
        self.positions = ()
        self.time_span = None
        self.days = ()
        self.index = None
        self.cursor_offset = None
        self.target = None
        self.currency = ""
        self.empty = "Пока нет данных для графика"
        self.rect_plot = QRectF()

    def set_plot(self, labels, lines, currency, *, time_span=None, days=(), target=None, empty=None):
        self.labels = tuple(labels)
        self.lines = tuple(lines)
        self.currency = currency
        self.time_span = time_span
        self.days = tuple(days)
        self.target = target
        self.empty = empty or "Пока нет данных для графика"
        self.index = None
        self.cursor_offset = None
        self.rect_plot = QRectF()
        size = len(self.labels)
        if time_span is not None:
            first, last = time_span
            span = (last - first).days
            self.positions = tuple((day - first).days / span if span else 0.5 for day in self.days)
        else:
            self.positions = tuple(index / (size - 1) if size > 1 else 0.5 for index in range(size))
        self.setAccessibleDescription(self.empty if not self.lines else f"{size} точек · {currency}")
        self.update()

    def value_scale(self):
        low, high = 0, 0
        for line in self.lines:
            low = min(low, min(line.values))
            high = max(high, max(line.values))
        if self.target is not None:
            high = max(high, self.target)
        raw = max(1, (high - low) / 4)
        base = 10 ** floor(log10(raw))
        step = next(int(base * factor) for factor in (1, 2, 2.5, 5, 10) if base * factor >= raw)
        lower = (low // step) * step
        upper = max(lower + step, -(-high // step) * step)
        return range(lower, upper + 1, step)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        if not self.lines or not self.positions:
            painter.setPen(QColor(MUTED))
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, self.empty)
            painter.end()
            return
        ticks = self.value_scale()
        painter.setFont(QFont(self.font()))
        metrics = painter.fontMetrics()
        width = max(metrics.horizontalAdvance(axis_value(value)) for value in ticks)
        left = width + 16
        top = 27 if len(self.lines) > 1 else 17
        self.rect_plot = plot = QRectF(left, top, max(1, self.width() - left - 23), max(1, self.height() - top - 30))
        lower, upper = ticks[0], ticks[-1]

        def y(value):
            return plot.bottom() - (value - lower) * plot.height() / (upper - lower)

        for value in ticks:
            vertical = y(value)
            painter.setPen(QPen(QColor("#A9BAC5" if value == 0 else GRID), 1))
            painter.drawLine(QPointF(plot.left(), vertical), QPointF(plot.right(), vertical))
            painter.setPen(QColor(MUTED))
            painter.drawText(QRectF(0, vertical - 9, left - 8, 18),
                             Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, axis_value(value))
        self.paint_labels(painter, plot)
        if len(self.lines) > 1:
            space = (self.width() - 6) / len(self.lines)
            for index, line in enumerate(self.lines):
                left = index * space + 4
                painter.setPen(QPen(QColor(line.color), 3))
                painter.drawLine(QPointF(left, 9), QPointF(left + 12, 9))
                painter.setPen(QColor(TEXT))
                painter.drawText(QRectF(left + 17, 0, space - 21, 18), Qt.AlignmentFlag.AlignVCenter,
                                 metrics.elidedText(line.name, Qt.TextElideMode.ElideRight, int(space - 22)))
        if self.target is not None:
            line = QPen(QColor("#8191A8"), 1, Qt.PenStyle.DashLine)
            painter.setPen(line)
            level = y(self.target)
            painter.drawLine(QPointF(plot.left(), level), QPointF(plot.right(), level))
        for line in self.lines:
            points = tuple(QPointF(plot.left() + offset * plot.width(), y(value))
                           for offset, value in zip(self.positions, line.values))
            path = QPainterPath(points[0])
            for index, point in enumerate(points[1:], 1):
                if self.time_span is not None:
                    path.lineTo(point.x(), points[index - 1].y())
                path.lineTo(point)
            if len(points) > 1 and len(self.lines) == 1:
                fill = QPainterPath(path)
                fill.lineTo(points[-1].x(), y(0))
                fill.lineTo(points[0].x(), y(0))
                fill.closeSubpath()
                gradient = QLinearGradient(plot.topLeft(), plot.bottomLeft())
                color = QColor(line.color)
                color.setAlpha(44)
                gradient.setColorAt(0, color)
                color.setAlpha(8)
                gradient.setColorAt(1, color)
                painter.fillPath(fill, gradient)
            pen = QPen(QColor(line.color), 2.6, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap,
                       Qt.PenJoinStyle.RoundJoin)
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawPath(path)
            painter.setBrush(QColor(line.color))
            painter.setPen(Qt.PenStyle.NoPen)
            for point in points if len(points) <= 24 else (points[-1],):
                painter.drawEllipse(point, 3.1, 3.1)
        if self.index is not None:
            x = plot.left() + self.cursor_offset * plot.width()
            painter.setPen(QPen(QColor("#8CA5B4"), 1, Qt.PenStyle.DashLine))
            painter.drawLine(QPointF(x, plot.top()), QPointF(x, plot.bottom()))
            for line in self.lines:
                painter.setPen(QPen(QColor("white"), 1.7))
                painter.setBrush(QColor(line.color))
                painter.drawEllipse(QPointF(x, y(line.values[self.index])), 5, 5)
        painter.end()

    def paint_labels(self, painter, plot):
        slots = max(2, min(7, int(plot.width() // 76)))
        if self.time_span is not None:
            first, last = self.time_span
            span = (last - first).days
            distances = sorted({round(span * index / (slots - 1)) for index in range(slots)})
            marks = [(offset / span if span else 0.5, date_label(first + timedelta(days=offset), span))
                     for offset in distances]
        else:
            count = len(self.labels)
            indexes = sorted({round((count - 1) * index / (slots - 1)) for index in range(slots)})
            marks = [(self.positions[index], self.labels[index][0]) for index in indexes]
        painter.setPen(QColor(MUTED))
        for pos, text in marks:
            x = min(self.width() - 33, max(34, plot.left() + pos * plot.width()))
            painter.drawText(QRectF(x - 36, plot.bottom() + 8, 72, 18), Qt.AlignmentFlag.AlignHCenter, text)

    def highlight(self, index, position, *, day=None, offset=None):
        self.index = index
        self.cursor_offset = self.positions[index] if offset is None else offset
        caption = self.labels[index][1] if day is None else day.strftime("%d.%m.%Y")
        details = [f"{line.name} · {money(line.values[index], self.currency)}" for line in self.lines]
        if self.target is not None:
            details.append(f"Цель · {money(self.target, self.currency)}")
        self.setAccessibleDescription(" · ".join((caption, *details)))
        tooltip = "<br>".join((f"<b>{escape(caption)}</b>", *(escape(detail) for detail in details)))
        QToolTip.showText(position, tooltip, self)
        self.update()

    def mouseMoveEvent(self, event):
        if not self.lines or not self.rect_plot.contains(event.position()):
            if self.index is not None:
                self.leaveEvent(None)
            return
        offset = (event.position().x() - self.rect_plot.left()) / self.rect_plot.width()
        if self.time_span is not None and len(self.days) > 1:
            first, last = self.time_span
            span = (last - first).days
            day = first + timedelta(days=round(offset * span))
            index = bisect_right(self.days, day) - 1
            aligned = (day - first).days / span if span else 0.5
            if index >= 0 and (index != self.index or self.cursor_offset != aligned):
                self.highlight(index, event.globalPosition().toPoint(), day=day, offset=aligned)
            elif index < 0 and self.index is not None:
                self.leaveEvent(None)
            return
        index = bisect_left(self.positions, offset)
        index = min(index, len(self.positions) - 1)
        if index and offset - self.positions[index - 1] < self.positions[index] - offset:
            index -= 1
        if index != self.index:
            self.highlight(index, event.globalPosition().toPoint())

    def keyPressEvent(self, event):
        if self.lines and event.key() in (Qt.Key.Key_Left, Qt.Key.Key_Right):
            step = -1 if event.key() == Qt.Key.Key_Left else 1
            index = 0 if self.index is None else max(0, min(len(self.positions) - 1, self.index + step))
            self.highlight(index, self.mapToGlobal(self.rect().center()))
        else:
            super().keyPressEvent(event)

    def leaveEvent(self, event):
        self.index = None
        self.cursor_offset = None
        QToolTip.hideText()
        self.update()


@dataclass(frozen=True)
class Slice:
    key: str | int | None
    name: str
    amount: int
    color: str


class DonutChart(QWidget):
    selected = Signal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(135)
        self.setMouseTracking(True)
        self.setAccessibleName("Кольцевая диаграмма")
        self.parts = ()
        self.total = 0
        self.currency = ""
        self.active = None
        self.hover = None

    def set_parts(self, parts, currency):
        self.parts = tuple(parts)
        self.total = sum(item.amount for item in self.parts)
        self.currency = currency
        self.hover = None
        self.setAccessibleDescription(f"Всего {money(self.total, currency)}")
        self.update()

    def geometry_values(self):
        diameter = max(10, min(self.width() - 28, self.height() - 31, 200))
        thickness = min(23, diameter * 0.14)
        return QRectF((self.width() - diameter) / 2, (self.height() - diameter) / 2, diameter, diameter), thickness

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        ring, thickness = self.geometry_values()
        painter.setPen(QPen(QColor("#EAF0F4"), thickness))
        painter.drawEllipse(ring)
        angle = 0
        selected_in_ring = any(section.key == self.active for section in self.parts)
        for part in self.parts:
            span = part.amount * 360 / self.total
            gap = min(1.45, span / 4) if len(self.parts) > 1 else 0
            color = QColor(part.color)
            if self.active is not None and part.key not in (self.active, self.hover) and not (
                part.key is None and not selected_in_ring
            ):
                color.setAlpha(105)
            painter.setPen(QPen(color, thickness + (3 if part.key == self.hover and self.hover is not None else 0)))
            painter.drawArc(ring, round((90 - angle - gap / 2) * 16), -round((span - gap) * 16))
            angle += span
        active = self.hover if self.hover is not None else self.active
        focus = next((item for item in self.parts if active is not None and item.key == active), None)
        value = focus.amount if focus else self.total
        symbol = "₽" if self.currency == "RUB" else self.currency
        text = f"{amount_text(value).removesuffix(',00')} {symbol}"
        font = painter.font()
        font.setBold(True)
        font.setPixelSize(21)
        painter.setFont(font)
        inner = max(72, ring.width() - thickness - 17)
        if painter.fontMetrics().horizontalAdvance(text) > inner:
            font.setPixelSize(15)
            painter.setFont(font)
            if painter.fontMetrics().horizontalAdvance(text) > inner:
                text = f"{axis_value(value)} {symbol}"
        metrics = painter.fontMetrics()
        painter.setPen(QColor(TEXT))
        center = ring.center()
        painter.drawText(QRectF(center.x() - inner / 2, center.y() - 17, inner, 29),
                         Qt.AlignmentFlag.AlignCenter, metrics.elidedText(text, Qt.TextElideMode.ElideRight, int(inner)))
        font.setBold(False)
        font.setPixelSize(11)
        painter.setFont(font)
        painter.setPen(QColor(MUTED))
        note = f"{focus.amount / self.total:.1%}".replace(".", ",") if focus else "за период"
        painter.drawText(QRectF(center.x() - inner / 2, center.y() + 11, inner, 20), Qt.AlignmentFlag.AlignCenter, note)
        painter.end()

    def segment(self, position):
        ring, thickness = self.geometry_values()
        shift = position - ring.center()
        radius = ring.width() / 2
        if not self.parts or abs(hypot(shift.x(), shift.y()) - radius) > thickness / 2 + 3:
            return None
        angle = (degrees(atan2(shift.x(), -shift.y())) + 360) % 360
        cumulative = 0
        for item in self.parts:
            cumulative += item.amount * 360 / self.total
            if angle < cumulative:
                return item
        return self.parts[-1]

    def mouseMoveEvent(self, event):
        part = self.segment(event.position())
        self.setCursor(Qt.CursorShape.PointingHandCursor if part and part.key is not None else Qt.CursorShape.ArrowCursor)
        self.hover = part.key if part else None
        if part:
            percent = f"{part.amount / self.total:.1%}".replace(".", ",")
            QToolTip.showText(event.globalPosition().toPoint(),
                              escape(f"{part.name} · {money(part.amount, self.currency)} · {percent}"), self)
        else:
            QToolTip.hideText()
        self.update()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and (part := self.segment(event.position())):
            if part.key is not None:
                self.selected.emit(part.key)

    def leaveEvent(self, event):
        self.hover = None
        QToolTip.hideText()
        self.update()
