#!/usr/bin/env python3
from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from PyQt5.QtCore import QObject, QSize, Qt, QThread, QTimer, pyqtSignal
from PyQt5.QtGui import QColor, QFont, QIcon, QPainter, QPen, QPixmap
from PyQt5.QtWidgets import (
    QApplication,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSplitter,
    QStackedWidget,
    QStyle,
    QVBoxLayout,
    QWidget,
)

from reader import AdapterError, PowerWaveReader
from live_card import LiveCardCache
from ps2_icon_3d import IconSysData, OpenGLIconRenderer, PS2IconView
from ps2_browser_3d import PS2BrowserScene
from version import APP_VERSION


APP_DIR = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
TOOL = APP_DIR / "bin" / "ps2vmc-tool"
DATA_DIR = Path.home() / ".local" / "share" / "memory-prism"
LIVE_CACHE = DATA_DIR / "live-card-cache.ps2"


@dataclass
class FileEntry:
    name: str
    kind: str
    size: int
    modified: str


@dataclass
class SaveEntry:
    folder: str
    title: str
    modified: str
    size: int
    files: list[FileEntry]
    icon: Path | None
    icon_sys: Path | None
    models: list[Path]


@dataclass
class Snapshot:
    image: Path
    capacity: int
    free: int
    saves: list[SaveEntry]
    complete: bool = True


def run_tool(image: Path, *arguments: str, cwd: Path | None = None) -> str:
    result = subprocess.run(
        [str(TOOL), str(image.resolve()), *arguments],
        cwd=cwd,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "Unknown card error"
        raise RuntimeError(detail)
    return result.stdout


def parse_listing(output: str) -> list[FileEntry]:
    entries: list[FileEntry] = []
    for line in output.splitlines():
        if "|" not in line or line.startswith("-"):
            continue
        columns = [part.strip() for part in line.split("|")]
        if len(columns) < 5 or columns[0] in (".", "..", "Filename"):
            continue
        try:
            size = int(columns[2])
        except ValueError:
            continue
        entries.append(
            FileEntry(
                name=columns[0],
                kind="folder" if "dir" in columns[1] else "file",
                size=size,
                modified=columns[4],
            )
        )
    return entries


def clean_title(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).split())


def read_icon_title(icon_sys: Path, fallback: str) -> str:
    data = icon_sys.read_bytes()
    if len(data) < 260:
        return fallback
    second_line = int.from_bytes(data[6:8], "little")
    raw = data[192:260].split(b"\0", 1)[0]
    lines: list[str] = []
    for part in (raw[:second_line], raw[second_line:]):
        text = part.decode("shift_jis", "replace").strip(" \0")
        if text:
            lines.append(clean_title(text))
    return " ".join(lines) or fallback


def load_snapshot(image: Path, complete: bool = True) -> Snapshot:
    if not image.exists() or image.stat().st_size < 8 * 1024 * 1024:
        raise RuntimeError("This is not a PS2 memory card image")
    with image.open("rb") as source:
        superblock = source.read(64)
    if superblock[:28] != b"Sony PS2 Memory Card Format ":
        raise RuntimeError("This is not a formatted PS2 memory card image")
    page_size = int.from_bytes(superblock[40:42], "little")
    pages_per_cluster = int.from_bytes(superblock[42:44], "little")
    clusters_per_card = int.from_bytes(superblock[48:52], "little")
    capacity = page_size * pages_per_cluster * clusters_per_card
    if image.stat().st_size != capacity:
        raise RuntimeError("The PS2 memory card image has an unexpected size")

    cache = DATA_DIR / "cache"
    if cache.exists():
        shutil.rmtree(cache)
    cache.mkdir(parents=True)

    root = parse_listing(run_tool(image, "--list", "/"))
    free_output = run_tool(image, "--mc-free")
    match = re.search(r"Available space:\s*(\d+)\s*KB", free_output)
    free = int(match.group(1)) * 1024 if match else 0
    saves: list[SaveEntry] = []
    renderer = None

    try:
        for directory in (item for item in root if item.kind == "folder"):
            card_path = f"/{directory.name}"
            files = parse_listing(run_tool(image, "--list", card_path))
            size = sum(item.size for item in files if item.kind == "file")
            title = clean_title(directory.name.replace("-", " ").replace("_", " "))
            icon_path: Path | None = None
            icon_sys_path: Path | None = None
            model_paths: list[Path] = []

            if any(item.name.lower() == "icon.sys" for item in files):
                item_cache = cache / directory.name
                item_cache.mkdir()
                icon_sys_path = item_cache / "icon.sys"
                run_tool(
                    image,
                    "--extract-file",
                    f"{card_path}/icon.sys",
                    str(icon_sys_path),
                )
                title = read_icon_title(icon_sys_path, title)
                try:
                    icon_sys_data = IconSysData.parse(icon_sys_path.read_bytes())
                    available = {item.name.lower(): item.name for item in files}
                    for model_name in dict.fromkeys(
                        (
                            icon_sys_data.normal_name,
                            icon_sys_data.copy_name,
                            icon_sys_data.delete_name,
                        )
                    ):
                        actual_name = available.get(model_name.lower())
                        if not actual_name:
                            continue
                        model_path = item_cache / Path(actual_name).name
                        run_tool(
                            image,
                            "--extract-file",
                            f"{card_path}/{actual_name}",
                            str(model_path),
                        )
                        model_paths.append(model_path)

                    if model_paths:
                        if renderer is None:
                            renderer = OpenGLIconRenderer()
                        renderer.load(icon_sys_path, model_paths[0])
                        icon_path = item_cache / "icon-3d.png"
                        thumbnail = renderer.render(
                            180,
                            180,
                            elapsed=0.8,
                            include_background=False,
                            rotation_offset=-0.22,
                        )
                        if thumbnail.isNull() or not thumbnail.save(str(icon_path)):
                            raise RuntimeError("Could not render 3D icon")
                except Exception as error:
                    print(
                        f"3D icon fallback for {directory.name}: {error}",
                        file=sys.stderr,
                    )
                    model_paths = []
                    run_tool(image, "--icons-png", card_path, cwd=item_cache)
                    icon_path = next(item_cache.glob("*.png"), None)

            saves.append(
                SaveEntry(
                    folder=directory.name,
                    title=title,
                    modified=directory.modified,
                    size=size,
                    files=files,
                    icon=icon_path,
                    icon_sys=icon_sys_path,
                    models=model_paths,
                )
            )
    finally:
        if renderer is not None:
            renderer.release()

    return Snapshot(
        image=image,
        capacity=capacity,
        free=free,
        saves=saves,
        complete=complete,
    )


class CardWorker(QObject):
    progress = pyqtSignal(int, str)
    completed = pyqtSignal(object)
    failed = pyqtSignal(str)

    def __init__(self, output: Path) -> None:
        super().__init__()
        self.output = output

    def run(self) -> None:
        try:
            self.output.parent.mkdir(parents=True, exist_ok=True)
            self.progress.emit(1, "Connecting to PowerWave adapter")
            last_error = None
            attempts = 5
            for attempt in range(attempts):
                try:
                    if attempt:
                        self.progress.emit(
                            1,
                            f"Waiting for inserted card  {attempt + 1}/{attempts}",
                        )
                    # Check the slot before resetting USB. On PowerWave
                    # adapters this acknowledges the no-card transition that
                    # otherwise leaves the previous card session cached.
                    try:
                        with PowerWaveReader(reset_usb=False) as probe:
                            detected_type = probe.card_type()
                            authenticated = probe.is_authenticated()
                    except AdapterError:
                        # A timed-out probe is the PowerWave's other common
                        # post-swap state. Continue into a reset instead of
                        # repeating the same unresponsive probe.
                        detected_type = 0
                        authenticated = False
                    if detected_type == 0:
                        self.progress.emit(1, "Detected card change; refreshing adapter")
                        time.sleep(0.35)
                    elif detected_type != 2:
                        raise AdapterError("No PS2 memory card is inserted")
                    elif not authenticated:
                        self.progress.emit(1, "Authenticating newly inserted card")

                    with PowerWaveReader() as reader:
                        # The PowerWave can briefly retain the previous card's
                        # authentication state after a hot swap.
                        time.sleep(0.35 + attempt * 0.4)
                        info = reader.info()
                        self.progress.emit(
                            2,
                            f"Browsing {info.capacity // (1024 * 1024)}MB memory card live",
                        )
                        with LiveCardCache(
                            reader, self.output, self.progress.emit
                        ) as cache:
                            cache.build()
                    break
                except AdapterError as error:
                    last_error = error
                    if attempt + 1 == attempts:
                        raise
                    time.sleep(0.85)
            else:
                raise last_error or AdapterError("Could not refresh the memory card")
            snapshot = load_snapshot(self.output, complete=False)
            self.progress.emit(100, "Ready")
            self.completed.emit(snapshot)
        except Exception as error:
            self.failed.emit(str(error))


class BackupWorker(QObject):
    progress = pyqtSignal(int, str)
    completed = pyqtSignal(object)
    failed = pyqtSignal(str)

    def __init__(self, output: Path) -> None:
        super().__init__()
        self.output = output

    def run(self) -> None:
        try:
            self.output.parent.mkdir(parents=True, exist_ok=True)
            self.progress.emit(1, "Connecting to PowerWave adapter")
            with PowerWaveReader() as reader:
                info = reader.info()

                def update(done: int, total: int) -> None:
                    percent = 2 + int((done / total) * 98)
                    self.progress.emit(percent, f"Creating full backup  {done * 100 // total}%")

                reader.backup(self.output, update)
            self.completed.emit(self.output)
        except Exception as error:
            self.failed.emit(str(error))


class SnapshotWorker(QObject):
    completed = pyqtSignal(object)
    failed = pyqtSignal(str)

    def __init__(self, image: Path) -> None:
        super().__init__()
        self.image = image

    def run(self) -> None:
        try:
            self.completed.emit(load_snapshot(self.image))
        except Exception as error:
            self.failed.emit(str(error))


def format_size(value: int) -> str:
    if value >= 1024 * 1024:
        return f"{value / (1024 * 1024):.1f} MB"
    if value >= 1024:
        return f"{value / 1024:.0f} KB"
    return f"{value} bytes"


def fallback_icon(text: str, size: int = 128) -> QPixmap:
    pixmap = QPixmap(size, size)
    pixmap.fill(QColor("#202329"))
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing)
    painter.setPen(QPen(QColor("#61d4e8"), 3))
    painter.drawRoundedRect(12, 18, size - 24, size - 36, 7, 7)
    painter.setPen(QColor("#f1f3f5"))
    font = QFont("Sans Serif", 20, QFont.Bold)
    painter.setFont(font)
    initials = "".join(part[0] for part in text.split()[:3]).upper() or "MC"
    painter.drawText(pixmap.rect(), Qt.AlignCenter, initials)
    painter.end()
    return pixmap


class MainWindow(QMainWindow):
    def __init__(
        self,
        image: Path | None = None,
        screenshot: Path | None = None,
        ps2_view: bool = False,
    ) -> None:
        super().__init__()
        self.snapshot: Snapshot | None = None
        self.thread: QThread | None = None
        self.worker: QObject | None = None
        self.screenshot = screenshot
        self.start_in_ps2_view = ps2_view

        self.setWindowTitle(f"Memory Prism v{APP_VERSION}")
        self.resize(1180, 760)
        self.setMinimumSize(900, 620)
        self.build_ui()

        if image:
            QTimer.singleShot(0, lambda: self.open_image(image))

    def build_ui(self) -> None:
        root = QWidget()
        page = QVBoxLayout(root)
        page.setContentsMargins(0, 0, 0, 0)
        page.setSpacing(0)

        header = QFrame()
        header.setObjectName("header")
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(24, 16, 20, 16)
        header_layout.setSpacing(12)

        mark = QLabel("MC")
        mark.setObjectName("mark")
        mark.setAlignment(Qt.AlignCenter)
        mark.setFixedSize(44, 44)
        header_layout.addWidget(mark)

        heading = QVBoxLayout()
        title = QLabel("Memory Prism")
        title.setObjectName("appTitle")
        subtitle = QLabel(f"PS2 USB adapter · read-only mode · v{APP_VERSION}")
        subtitle.setObjectName("muted")
        heading.addWidget(title)
        heading.addWidget(subtitle)
        header_layout.addLayout(heading)
        header_layout.addStretch()

        self.open_button = QPushButton("Open backup")
        self.open_button.setIcon(self.style().standardIcon(QStyle.SP_DialogOpenButton))
        self.open_button.clicked.connect(self.choose_image)
        header_layout.addWidget(self.open_button)

        self.ps2_view_button = QPushButton("PS2 Browser")
        self.ps2_view_button.setEnabled(False)
        self.ps2_view_button.setIcon(self.style().standardIcon(QStyle.SP_ComputerIcon))
        self.ps2_view_button.clicked.connect(self.show_ps2_browser)
        header_layout.addWidget(self.ps2_view_button)

        self.scan_button = QPushButton("Read card")
        self.scan_button.setObjectName("primary")
        self.scan_button.setIcon(self.style().standardIcon(QStyle.SP_BrowserReload))
        self.scan_button.clicked.connect(self.read_card)
        header_layout.addWidget(self.scan_button)
        page.addWidget(header)

        body = QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)

        self.sidebar = QFrame()
        self.sidebar.setObjectName("sidebar")
        self.sidebar.setFixedWidth(235)
        side = QVBoxLayout(self.sidebar)
        side.setContentsMargins(22, 28, 22, 22)
        side.setSpacing(10)
        side.addWidget(QLabel("MEMORY CARD"), alignment=Qt.AlignLeft)

        self.card_visual = QFrame()
        self.card_visual.setObjectName("cardVisual")
        self.card_visual.setFixedHeight(132)
        visual_layout = QVBoxLayout(self.card_visual)
        visual_layout.setContentsMargins(18, 18, 18, 16)
        visual_layout.addWidget(QLabel("PlayStation 2"))
        visual_layout.addStretch()
        self.card_capacity = QLabel("No card loaded")
        self.card_capacity.setObjectName("cardCapacity")
        visual_layout.addWidget(self.card_capacity)
        side.addWidget(self.card_visual)

        self.usage_label = QLabel("Storage")
        self.usage_label.setObjectName("muted")
        side.addWidget(self.usage_label)
        self.usage = QProgressBar()
        self.usage.setTextVisible(False)
        self.usage.setFixedHeight(7)
        side.addWidget(self.usage)
        self.space_label = QLabel("Connect or open a backup")
        self.space_label.setObjectName("muted")
        self.space_label.setWordWrap(True)
        side.addWidget(self.space_label)
        side.addSpacing(12)

        safe = QLabel("READ ONLY")
        safe.setObjectName("safeBadge")
        safe.setAlignment(Qt.AlignCenter)
        safe.setFixedWidth(92)
        side.addWidget(safe)
        note = QLabel("Browsing and exports never modify the inserted card.")
        note.setObjectName("muted")
        note.setWordWrap(True)
        side.addWidget(note)
        side.addStretch()

        self.backup_button = QPushButton("Save backup as…")
        self.backup_button.setEnabled(False)
        self.backup_button.setIcon(self.style().standardIcon(QStyle.SP_DialogSaveButton))
        self.backup_button.clicked.connect(self.save_backup_as)
        side.addWidget(self.backup_button)
        body.addWidget(self.sidebar)

        self.pages = QStackedWidget()
        self.pages.addWidget(self.build_welcome())
        self.pages.addWidget(self.build_progress())
        self.pages.addWidget(self.build_browser())
        self.pages.addWidget(self.build_ps2_browser())
        body.addWidget(self.pages, 1)
        page.addLayout(body, 1)

        self.setCentralWidget(root)
        self.setStyleSheet(STYLESHEET)

    def build_welcome(self) -> QWidget:
        widget = QWidget()
        layout = QVBoxLayout(widget)
        layout.setAlignment(Qt.AlignCenter)
        icon = QLabel("▣")
        icon.setObjectName("welcomeIcon")
        icon.setAlignment(Qt.AlignCenter)
        layout.addWidget(icon)
        title = QLabel("Ready for a memory card")
        title.setObjectName("emptyTitle")
        title.setAlignment(Qt.AlignCenter)
        layout.addWidget(title)
        text = QLabel("Connect the PowerWave adapter and choose Read card.")
        text.setObjectName("muted")
        text.setAlignment(Qt.AlignCenter)
        layout.addWidget(text)
        button = QPushButton("Read card")
        button.setObjectName("primary")
        button.clicked.connect(self.read_card)
        row = QHBoxLayout()
        row.addStretch()
        row.addWidget(button)
        row.addStretch()
        layout.addLayout(row)
        return widget

    def build_progress(self) -> QWidget:
        widget = QWidget()
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(120, 0, 120, 0)
        layout.setAlignment(Qt.AlignCenter)
        title = QLabel("Reading memory card")
        title.setObjectName("emptyTitle")
        title.setAlignment(Qt.AlignCenter)
        layout.addWidget(title)
        self.progress_text = QLabel("Connecting…")
        self.progress_text.setObjectName("muted")
        self.progress_text.setAlignment(Qt.AlignCenter)
        layout.addWidget(self.progress_text)
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setFixedHeight(10)
        self.progress_bar.setTextVisible(False)
        layout.addWidget(self.progress_bar)
        return widget

    def build_browser(self) -> QWidget:
        splitter = QSplitter(Qt.Horizontal)
        browser = QWidget()
        browser_layout = QVBoxLayout(browser)
        browser_layout.setContentsMargins(26, 24, 18, 22)
        browser_layout.setSpacing(12)

        top = QHBoxLayout()
        self.browser_title = QLabel("Card contents")
        self.browser_title.setObjectName("sectionTitle")
        self.entry_count = QLabel()
        self.entry_count.setObjectName("muted")
        top.addWidget(self.browser_title)
        top.addStretch()
        top.addWidget(self.entry_count)
        browser_layout.addLayout(top)

        self.grid = QListWidget()
        self.grid.setViewMode(QListWidget.IconMode)
        self.grid.setResizeMode(QListWidget.Adjust)
        self.grid.setMovement(QListWidget.Static)
        self.grid.setIconSize(QSize(128, 128))
        self.grid.setGridSize(QSize(184, 190))
        self.grid.setSpacing(6)
        self.grid.setWordWrap(True)
        self.grid.currentRowChanged.connect(self.show_details)
        browser_layout.addWidget(self.grid, 1)
        splitter.addWidget(browser)

        details_scroll = QScrollArea()
        details_scroll.setObjectName("details")
        details_scroll.setWidgetResizable(True)
        details_scroll.setFixedWidth(340)
        details = QWidget()
        detail_layout = QVBoxLayout(details)
        detail_layout.setContentsMargins(28, 26, 24, 24)
        detail_layout.setSpacing(9)

        self.detail_visual = QStackedWidget()
        self.detail_visual.setFixedSize(260, 220)
        self.detail_icon = QLabel()
        self.detail_icon.setAlignment(Qt.AlignCenter)
        self.detail_visual.addWidget(self.detail_icon)
        self.detail_model = PS2IconView()
        self.detail_visual.addWidget(self.detail_model)
        detail_layout.addWidget(self.detail_visual, alignment=Qt.AlignHCenter)
        self.detail_title = QLabel("Select an entry")
        self.detail_title.setObjectName("detailTitle")
        self.detail_title.setWordWrap(True)
        detail_layout.addWidget(self.detail_title)
        self.detail_folder = QLabel()
        self.detail_folder.setObjectName("folder")
        self.detail_folder.setWordWrap(True)
        detail_layout.addWidget(self.detail_folder)
        self.detail_meta = QLabel()
        self.detail_meta.setObjectName("muted")
        self.detail_meta.setWordWrap(True)
        detail_layout.addWidget(self.detail_meta)
        detail_layout.addSpacing(12)
        files_heading = QLabel("FILES")
        files_heading.setObjectName("eyebrow")
        detail_layout.addWidget(files_heading)
        self.files = QLabel()
        self.files.setObjectName("fileList")
        self.files.setWordWrap(True)
        self.files.setTextInteractionFlags(Qt.TextSelectableByMouse)
        detail_layout.addWidget(self.files)
        detail_layout.addStretch()
        self.export_button = QPushButton("Export save (.psu)")
        self.export_button.setEnabled(False)
        self.export_button.setIcon(self.style().standardIcon(QStyle.SP_DialogSaveButton))
        self.export_button.clicked.connect(self.export_save)
        detail_layout.addWidget(self.export_button)
        details_scroll.setWidget(details)
        splitter.addWidget(details_scroll)
        splitter.setStretchFactor(0, 1)
        splitter.setCollapsible(0, False)
        splitter.setCollapsible(1, False)
        return splitter

    def build_ps2_browser(self) -> QWidget:
        self.ps2_scene = PS2BrowserScene()
        self.ps2_scene.back_requested.connect(self.hide_ps2_browser)
        self.ps2_scene.selection_changed.connect(self.sync_ps2_selection)
        return self.ps2_scene

    def set_busy(self, busy: bool) -> None:
        self.scan_button.setEnabled(not busy)
        self.open_button.setEnabled(not busy)
        self.backup_button.setEnabled(not busy and self.snapshot is not None)
        self.ps2_view_button.setEnabled(not busy and self.snapshot is not None)

    def read_card(self) -> None:
        self.pages.setCurrentIndex(1)
        self.progress_bar.setValue(0)
        self.progress_text.setText("Connecting to PowerWave adapter")
        self.set_busy(True)
        self.thread = QThread()
        worker = CardWorker(LIVE_CACHE)
        self.worker = worker
        worker.moveToThread(self.thread)
        self.thread.started.connect(worker.run)
        worker.progress.connect(self.update_progress)
        worker.completed.connect(self.load_complete)
        worker.failed.connect(self.load_failed)
        worker.completed.connect(self.thread.quit)
        worker.failed.connect(self.thread.quit)
        self.thread.finished.connect(worker.deleteLater)
        self.thread.finished.connect(self.thread.deleteLater)
        self.thread.start()

    def update_progress(self, value: int, text: str) -> None:
        self.progress_bar.setValue(value)
        self.progress_text.setText(text)

    def choose_image(self) -> None:
        filename, _ = QFileDialog.getOpenFileName(
            self,
            "Open PS2 memory card backup",
            str(Path.home()),
            "PS2 memory cards (*.ps2 *.bin *.vmc);;All files (*)",
        )
        if filename:
            self.open_image(Path(filename))

    def open_image(self, image: Path) -> None:
        self.pages.setCurrentIndex(1)
        self.progress_bar.setRange(0, 0)
        self.progress_text.setText("Reading saves and icons")
        self.set_busy(True)
        self.thread = QThread()
        worker = SnapshotWorker(image)
        self.worker = worker
        worker.moveToThread(self.thread)
        self.thread.started.connect(worker.run)
        worker.completed.connect(self.load_complete)
        worker.failed.connect(self.load_failed)
        worker.completed.connect(self.thread.quit)
        worker.failed.connect(self.thread.quit)
        self.thread.finished.connect(worker.deleteLater)
        self.thread.finished.connect(self.thread.deleteLater)
        self.thread.start()

    def load_complete(self, snapshot: Snapshot) -> None:
        self.snapshot = snapshot
        self.progress_bar.setRange(0, 100)
        self.populate(snapshot)
        self.set_busy(False)
        self.pages.setCurrentIndex(2)
        if self.start_in_ps2_view:
            self.start_in_ps2_view = False
            self.show_ps2_browser()
        if self.screenshot:
            QTimer.singleShot(700, self.capture_screenshot)

    def load_failed(self, message: str) -> None:
        self.set_busy(False)
        self.pages.setCurrentIndex(0)
        QMessageBox.critical(self, "Could not read memory card", message)

    def populate(self, snapshot: Snapshot) -> None:
        used = snapshot.capacity - snapshot.free
        self.card_capacity.setText(f"{snapshot.capacity // (1024 * 1024)}MB MEMORY CARD")
        self.usage.setValue(round((used / snapshot.capacity) * 100))
        self.space_label.setText(f"{format_size(used)} used · {format_size(snapshot.free)} free")
        self.entry_count.setText(f"{len(snapshot.saves)} entries")
        self.backup_button.setEnabled(True)
        self.ps2_view_button.setEnabled(True)
        self.backup_button.setText(
            "Save backup as…" if snapshot.complete else "Create full backup…"
        )
        self.grid.clear()
        for save in snapshot.saves:
            pixmap = QPixmap(str(save.icon)) if save.icon else fallback_icon(save.title)
            item = QListWidgetItem(QIcon(pixmap), save.title)
            item.setTextAlignment(int(Qt.AlignHCenter | Qt.AlignTop))
            item.setSizeHint(QSize(174, 182))
            self.grid.addItem(item)
        if snapshot.saves:
            self.grid.setCurrentRow(0)

    def show_ps2_browser(self) -> None:
        if not self.snapshot:
            return
        self.detail_model.release_renderer()
        self.ps2_scene.set_entries(self.snapshot.saves, self.snapshot.capacity)
        self.sidebar.hide()
        self.pages.setCurrentIndex(3)
        self.ps2_view_button.setText("Card manager")
        self.ps2_view_button.setIcon(self.style().standardIcon(QStyle.SP_ArrowBack))
        try:
            self.ps2_view_button.clicked.disconnect()
        except TypeError:
            pass
        self.ps2_view_button.clicked.connect(self.hide_ps2_browser)
        self.ps2_scene.setFocus()

    def hide_ps2_browser(self) -> None:
        self.ps2_scene.clear_entries()
        self.sidebar.show()
        self.pages.setCurrentIndex(2)
        self.ps2_view_button.setText("PS2 Browser")
        self.ps2_view_button.setIcon(self.style().standardIcon(QStyle.SP_ComputerIcon))
        try:
            self.ps2_view_button.clicked.disconnect()
        except TypeError:
            pass
        self.ps2_view_button.clicked.connect(self.show_ps2_browser)
        self.show_details(self.grid.currentRow())
        self.grid.setFocus()

    def sync_ps2_selection(self, row: int) -> None:
        if 0 <= row < self.grid.count():
            self.grid.setCurrentRow(row)

    def show_details(self, row: int) -> None:
        if not self.snapshot or row < 0 or row >= len(self.snapshot.saves):
            return
        save = self.snapshot.saves[row]
        if save.icon_sys and save.models:
            self.detail_model.set_icon(save.icon_sys, save.models[0])
            self.detail_visual.setCurrentWidget(self.detail_model)
        else:
            pixmap = QPixmap(str(save.icon)) if save.icon else fallback_icon(save.title)
            self.detail_icon.setPixmap(
                pixmap.scaled(
                    180,
                    180,
                    Qt.KeepAspectRatio,
                    Qt.SmoothTransformation,
                )
            )
            self.detail_visual.setCurrentWidget(self.detail_icon)
        self.detail_title.setText(save.title)
        self.detail_folder.setText(save.folder)
        self.detail_meta.setText(f"{format_size(save.size)} · Modified {save.modified}")
        rows = [f"{item.name}    {format_size(item.size)}" for item in save.files]
        self.files.setText("\n".join(rows) or "No visible files")
        self.export_button.setEnabled(self.snapshot.complete)
        self.export_button.setToolTip(
            "" if self.snapshot.complete else "Create a full backup before exporting saves"
        )

    def export_save(self) -> None:
        if not self.snapshot or self.grid.currentRow() < 0:
            return
        save = self.snapshot.saves[self.grid.currentRow()]
        filename, _ = QFileDialog.getSaveFileName(
            self,
            "Export PS2 save",
            str(Path.home() / f"{save.folder}.psu"),
            "PS2 save archive (*.psu)",
        )
        if not filename:
            return
        if not self.snapshot.complete:
            return
        try:
            run_tool(
                self.snapshot.image,
                "--psu-export",
                f"/{save.folder}",
                filename,
            )
        except Exception as error:
            QMessageBox.critical(self, "Export failed", str(error))
            return
        QMessageBox.information(self, "Save exported", f"Saved to {filename}")

    def save_backup_as(self) -> None:
        if not self.snapshot:
            return
        filename, _ = QFileDialog.getSaveFileName(
            self,
            "Save memory card backup",
            str(Path.home() / "ps2-memory-card.ps2"),
            "PS2 memory card (*.ps2)",
        )
        if not filename:
            return
        if self.snapshot.complete:
            shutil.copy2(self.snapshot.image, filename)
            return

        self.pages.setCurrentIndex(1)
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_text.setText("Connecting to PowerWave adapter")
        self.set_busy(True)
        self.thread = QThread()
        worker = BackupWorker(Path(filename))
        self.worker = worker
        worker.moveToThread(self.thread)
        self.thread.started.connect(worker.run)
        worker.progress.connect(self.update_progress)
        worker.completed.connect(self.backup_complete)
        worker.failed.connect(self.load_failed)
        worker.completed.connect(self.thread.quit)
        worker.failed.connect(self.thread.quit)
        self.thread.finished.connect(worker.deleteLater)
        self.thread.finished.connect(self.thread.deleteLater)
        self.thread.start()

    def backup_complete(self, output: Path) -> None:
        self.set_busy(False)
        self.pages.setCurrentIndex(2)
        QMessageBox.information(self, "Backup complete", f"Saved to {output}")

    def capture_screenshot(self) -> None:
        if not self.screenshot:
            return
        self.screenshot.parent.mkdir(parents=True, exist_ok=True)
        self.grab().save(str(self.screenshot))
        QApplication.instance().quit()


STYLESHEET = """
* { font-family: Inter, "Noto Sans", sans-serif; font-size: 14px; color: #e9edf0; }
QMainWindow, QWidget { background: #17191d; }
#header { background: #22252a; border-bottom: 1px solid #34383f; }
#mark { background: #4da3ff; color: #0e151d; font-size: 15px; font-weight: 800; border-radius: 6px; }
#appTitle { font-size: 20px; font-weight: 700; }
#muted { color: #9da5ad; font-size: 13px; }
#sidebar { background: #1d2024; border-right: 1px solid #34383f; }
#cardVisual { background: #2b2f35; border: 1px solid #444951; border-radius: 7px; }
#cardVisual QLabel { background: transparent; color: #b9c0c7; }
#cardCapacity { color: white; font-size: 12px; font-weight: 700; }
#safeBadge { color: #72d6b3; background: #18352e; border: 1px solid #28604f; border-radius: 4px; padding: 5px 8px; font-size: 11px; font-weight: 800; }
QPushButton { background: #30343a; border: 1px solid #474c54; border-radius: 6px; padding: 9px 13px; font-weight: 600; }
QPushButton:hover { background: #393e45; border-color: #656c75; }
QPushButton:disabled { color: #6f767e; background: #25282d; border-color: #34383f; }
QPushButton#primary { background: #367fc7; border-color: #4da3ff; color: white; }
QPushButton#primary:hover { background: #428fd9; }
QProgressBar { background: #2b2f35; border: none; border-radius: 3px; }
QProgressBar::chunk { background: #4da3ff; border-radius: 3px; }
#welcomeIcon { color: #61d4e8; font-size: 76px; }
#emptyTitle { font-size: 25px; font-weight: 700; margin-bottom: 5px; }
#sectionTitle { font-size: 20px; font-weight: 700; }
QListWidget { background: #17191d; border: none; outline: none; }
QListWidget::item { background: #22252a; border: 1px solid #34383f; border-radius: 7px; padding: 10px 8px; }
QListWidget::item:hover { background: #292d32; border-color: #535961; }
QListWidget::item:selected { background: #273646; border: 2px solid #4da3ff; }
#details { background: #1d2024; border: none; border-left: 1px solid #34383f; }
#details QWidget { background: #1d2024; }
#detailTitle { font-size: 22px; font-weight: 700; }
#folder { color: #61d4e8; font-family: monospace; font-size: 13px; }
#eyebrow { color: #aeb5bc; font-size: 11px; font-weight: 800; }
#fileList { color: #c9ced3; font-family: monospace; font-size: 12px; line-height: 1.4; }
QSplitter::handle { background: #34383f; width: 1px; }
QScrollBar:vertical { background: #1d2024; width: 10px; }
QScrollBar::handle:vertical { background: #4a5058; min-height: 30px; border-radius: 4px; }
"""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--version", action="version", version=f"%(prog)s {APP_VERSION}")
    parser.add_argument("--image", type=Path)
    parser.add_argument("--screenshot", type=Path)
    parser.add_argument("--ps2-view", action="store_true")
    options = parser.parse_args()
    app = QApplication(sys.argv[:1])
    app.setApplicationName("Memory Prism")
    window = MainWindow(options.image, options.screenshot, options.ps2_view)
    window.show()
    return app.exec_()


if __name__ == "__main__":
    raise SystemExit(main())
