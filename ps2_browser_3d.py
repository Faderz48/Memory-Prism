from __future__ import annotations

import math
import time
from dataclasses import dataclass
from pathlib import Path

from PyQt5.QtCore import QPointF, QRectF, QSize, Qt, QTimer, pyqtSignal
from PyQt5.QtGui import (
    QColor,
    QFont,
    QImage,
    QKeyEvent,
    QLinearGradient,
    QMouseEvent,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
    QRadialGradient,
)
from PyQt5.QtWidgets import QWidget

from ps2_icon_3d import OpenGLIconRenderer


@dataclass
class BrowserIcon:
    title: str
    icon_sys: Path | None
    model: Path | None
    fallback: Path | None
    image: QImage | None = None
    frames: list[QImage] | None = None
    duration: float = 1.0


class PS2BrowserScene(QWidget):
    """A PS2-inspired memory-card scene using the saves' real 3D icon models."""

    back_requested = pyqtSignal()
    selection_changed = pyqtSignal(int)

    PAGE_SIZE = 12
    COLUMNS = 4

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setMouseTracking(True)
        self.setMinimumSize(QSize(640, 460))
        self.icons: list[BrowserIcon] = []
        self.selected = 0
        self.capacity = "MEMORY CARD"
        self.started = time.monotonic()
        self.icon_rects: list[tuple[int, QRectF]] = []
        self.selection_started = time.monotonic()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._animate)
        self.timer.start(33)

    def set_entries(self, entries: list, capacity: int) -> None:
        self.clear_entries()
        self.capacity = f"MEMORY CARD ({capacity // (1024 * 1024)}MB)"
        for entry in entries:
            self.icons.append(
                BrowserIcon(
                    title=entry.title,
                    icon_sys=entry.icon_sys,
                    model=entry.models[0] if entry.models else None,
                    fallback=entry.icon,
                )
            )
        self.selected = 0
        self.started = time.monotonic()
        self._load_selected_animation()
        self.update()

    def clear_entries(self) -> None:
        self.icons.clear()
        self.icon_rects.clear()

    def _page(self) -> int:
        return self.selected // self.PAGE_SIZE

    def _page_bounds(self) -> tuple[int, int]:
        start = self._page() * self.PAGE_SIZE
        return start, min(len(self.icons), start + self.PAGE_SIZE)

    def _load_selected_animation(self) -> None:
        for icon in self.icons:
            icon.image = None
            icon.frames = None
        if not self.icons:
            return
        icon = self.icons[self.selected]
        if not icon.icon_sys or not icon.model:
            return
        renderer = None
        try:
            renderer = OpenGLIconRenderer()
            renderer.load(icon.icon_sys, icon.model)
            animation_period = renderer.icon.frame_length / max(
                0.001, 60.0 * renderer.icon.animation_speed
            )
            animation_period = max(1.0 / 30.0, animation_period)
            animation_loops = max(
                1, round((2.0 * math.pi) / (0.45 * animation_period))
            )
            duration = min(20.0, animation_period * animation_loops)
            rotation_rate = (2.0 * math.pi) / duration
            frame_count = max(24, min(180, round(duration * 15.0)))
            icon.frames = [
                renderer.render(
                    196,
                    196,
                    duration * frame / frame_count,
                    include_background=False,
                    rotation_offset=-0.12,
                    rotation_rate=rotation_rate,
                )
                for frame in range(frame_count)
            ]
            icon.duration = duration
            icon.image = icon.frames[0]
            self.selection_started = time.monotonic()
        except Exception:
            icon.image = None
            icon.frames = None
        finally:
            if renderer:
                renderer.release()

    def _animate(self) -> None:
        if not self.isVisible() or not self.icons:
            return
        icon = self.icons[self.selected]
        if icon.frames:
            elapsed = time.monotonic() - self.selection_started
            frame = int((elapsed % icon.duration) / icon.duration * len(icon.frames))
            icon.image = icon.frames[frame]
        self.update()

    def _move(self, amount: int) -> None:
        if not self.icons:
            return
        self.selected = max(0, min(len(self.icons) - 1, self.selected + amount))
        self._load_selected_animation()
        self.selection_changed.emit(self.selected)
        self.update()

    def keyPressEvent(self, event: QKeyEvent) -> None:
        key = event.key()
        if key == Qt.Key_Left:
            self._move(-1)
        elif key == Qt.Key_Right:
            self._move(1)
        elif key == Qt.Key_Up:
            self._move(-self.COLUMNS)
        elif key == Qt.Key_Down:
            self._move(self.COLUMNS)
        elif key in (Qt.Key_PageUp, Qt.Key_BracketLeft):
            self._move(-self.PAGE_SIZE)
        elif key in (Qt.Key_PageDown, Qt.Key_BracketRight):
            self._move(self.PAGE_SIZE)
        elif key in (Qt.Key_Escape, Qt.Key_Backspace):
            self.back_requested.emit()
        else:
            super().keyPressEvent(event)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        for index, rect in reversed(self.icon_rects):
            if rect.contains(event.pos()):
                self.selected = index
                self._load_selected_animation()
                self.selection_changed.emit(index)
                self.update()
                self.setFocus()
                return
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        self.mousePressEvent(event)

    @staticmethod
    def _fit_text(painter: QPainter, text: str, width: int, base_size: int) -> QFont:
        font = QFont("Noto Sans", base_size, QFont.Medium)
        painter.setFont(font)
        while base_size > 10 and painter.fontMetrics().horizontalAdvance(text) > width:
            base_size -= 1
            font.setPointSize(base_size)
            painter.setFont(font)
        return font

    def _paint_background(self, painter: QPainter) -> None:
        backdrop = QLinearGradient(0, 0, 0, self.height())
        backdrop.setColorAt(0.0, QColor(111, 115, 116))
        backdrop.setColorAt(0.52, QColor(78, 83, 84))
        backdrop.setColorAt(1.0, QColor(49, 54, 56))
        painter.fillRect(self.rect(), backdrop)

        painter.save()
        painter.setRenderHint(QPainter.Antialiasing)
        horizon = self.height() * 0.79
        painter.setPen(QPen(QColor(208, 220, 223, 30), 1))
        for line in range(11):
            y = horizon + line * line * 2.1
            painter.drawLine(0, int(y), self.width(), int(y))
        center = self.width() / 2
        for line in range(-10, 11):
            painter.drawLine(QPointF(center + line * 31, horizon), QPointF(center + line * 104, self.height()))

        elapsed = time.monotonic() - self.started
        painter.setPen(Qt.NoPen)
        for index in range(24):
            x = (index * 193 + 71) % max(1, self.width())
            y = (index * 89 + elapsed * (8 + index % 4)) % max(1, self.height())
            alpha = 18 + (index % 5) * 7
            painter.setBrush(QColor(195, 229, 236, alpha))
            radius = 1.2 + (index % 3) * 0.7
            painter.drawEllipse(QPointF(x, y), radius, radius)
        painter.restore()

    def _paint_icon(self, painter: QPainter, index: int, rect: QRectF) -> None:
        icon = self.icons[index]
        selected = index == self.selected
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing)

        if selected:
            glow = QRadialGradient(rect.center(), rect.width() * 0.52)
            glow.setColorAt(0.0, QColor(230, 251, 255, 92))
            glow.setColorAt(0.48, QColor(150, 218, 232, 36))
            glow.setColorAt(1.0, QColor(110, 190, 210, 0))
            painter.setPen(Qt.NoPen)
            painter.setBrush(glow)
            painter.drawEllipse(rect.adjusted(-16, -16, 16, 16))

            orbit = rect.adjusted(9, rect.height() * 0.73, -9, -3)
            painter.setBrush(Qt.NoBrush)
            painter.setPen(QPen(QColor(220, 248, 255, 190), 2.2))
            painter.drawEllipse(orbit)

        if icon.image and not icon.image.isNull():
            painter.drawImage(rect, icon.image)
        elif icon.fallback and icon.fallback.exists():
            pixmap = QPixmap(str(icon.fallback))
            painter.drawPixmap(rect.toRect(), pixmap)
        else:
            path = QPainterPath()
            path.addRoundedRect(rect.adjusted(25, 32, -25, -32), 5, 5)
            painter.fillPath(path, QColor(70, 76, 79, 210))
            painter.setPen(QPen(QColor(205, 217, 219, 130), 2))
            painter.drawPath(path)

        painter.restore()

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        self._paint_background(painter)

        margin = max(42, int(self.width() * 0.065))
        top = 74
        bottom = 104
        available_width = self.width() - margin * 2
        available_height = self.height() - top - bottom
        cell_width = available_width / self.COLUMNS
        rows = 3
        cell_height = available_height / rows
        icon_size = min(170.0, cell_width * 0.78, cell_height * 0.92)

        self.icon_rects = []
        start, end = self._page_bounds()
        for index in range(start, end):
            local = index - start
            column = local % self.COLUMNS
            row = local // self.COLUMNS
            size = icon_size * (1.13 if index == self.selected else 0.91)
            center_x = margin + (column + 0.5) * cell_width
            center_y = top + (row + 0.48) * cell_height
            if index == self.selected:
                center_y -= 5
            rect = QRectF(center_x - size / 2, center_y - size / 2, size, size)
            self.icon_rects.append((index, rect.adjusted(-8, -8, 8, 8)))
            self._paint_icon(painter, index, rect)

        painter.setPen(QColor(235, 240, 241, 225))
        painter.setFont(QFont("Noto Sans", 15, QFont.DemiBold))
        painter.drawText(QRectF(margin, 24, available_width, 28), Qt.AlignLeft | Qt.AlignVCenter, self.capacity)

        if self.icons:
            title = self.icons[self.selected].title
            font = self._fit_text(painter, title, available_width - 180, 19)
            font.setWeight(QFont.DemiBold)
            painter.setFont(font)
            painter.setPen(QColor(247, 249, 249))
            painter.drawText(QRectF(margin, self.height() - 75, available_width - 150, 34), Qt.AlignLeft | Qt.AlignVCenter, title)

            page_count = math.ceil(len(self.icons) / self.PAGE_SIZE)
            if page_count > 1:
                painter.setPen(Qt.NoPen)
                for page in range(page_count):
                    color = QColor(232, 247, 250, 220) if page == self._page() else QColor(225, 234, 236, 70)
                    painter.setBrush(color)
                    x = self.width() - margin - (page_count - page) * 17
                    painter.drawEllipse(QPointF(x, self.height() - 58), 4, 4)

        painter.setFont(QFont("Noto Sans", 12, QFont.Medium))
        painter.setPen(QColor(226, 234, 235, 180))
        painter.drawText(
            QRectF(self.width() - margin - 126, 24, 126, 28),
            Qt.AlignRight | Qt.AlignVCenter,
            "○  Back",
        )
        painter.end()

    def closeEvent(self, event) -> None:
        self.clear_entries()
        super().closeEvent(event)
