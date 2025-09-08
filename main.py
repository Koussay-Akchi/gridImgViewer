import os
import sys
import math
import threading
import concurrent.futures
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Optional, List, Tuple, Dict

from PySide6 import QtCore, QtGui, QtWidgets
from send2trash import send2trash


CONFIG_START_DIR = ""
DEFAULT_START_DIR = str(Path.home())
THUMBNAIL_SIZE = QtCore.QSize(256, 256)
GRID_ROWS = 2
GRID_COLS = 2
MAX_WORKERS = max(4, (os.cpu_count() or 4))


class ImageSlot(QtWidgets.QFrame):
    activated = QtCore.Signal()

    def __init__(self):
        super().__init__()
        self.setFrameShape(QtWidgets.QFrame.NoFrame)
        self.setObjectName("imageSlot")
        self._path = None
        self._seen = False

        self.label = QtWidgets.QLabel()
        self.label.setAlignment(QtCore.Qt.AlignCenter)
        self.label.setObjectName("imageLabel")
        self.label.setMinimumSize(THUMBNAIL_SIZE)
        self.label.setMaximumSize(THUMBNAIL_SIZE)
        self.label.setSizePolicy(QtWidgets.QSizePolicy.Fixed, QtWidgets.QSizePolicy.Fixed)

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.label)

    def set_path(self, path: Optional[str]):
        self._path = path
        self._seen = self._seen or (path is not None)

    def path(self):
        return self._path

    def set_pixmap(self, pix: Optional[QtGui.QPixmap]):
        if pix is None:
            self.label.setText(":-)")
            self.label.setStyleSheet("color: #7a7a7a; font-size: 24px;")
            self.label.setPixmap(QtGui.QPixmap())
        else:
            self.label.setText("")
            self.label.setStyleSheet("")
            self.label.setPixmap(pix)


class StatsBar(QtWidgets.QWidget):
    def __init__(self):
        super().__init__()
        self.total_images = 0
        self.total_left = 0
        self.total_deleted = 0
        self.total_kept = 0
        self.total_seen = 0

        self.label = QtWidgets.QLabel()
        layout = QtWidgets.QHBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.addWidget(self.label)
        layout.addStretch(1)
        self.update_text()

    def update_stats(self, *, total=None, left=None, deleted=None, kept=None, seen=None):
        if total is not None:
            self.total_images = total
        if left is not None:
            self.total_left = left
        if deleted is not None:
            self.total_deleted = deleted
        if kept is not None:
            self.total_kept = kept
        if seen is not None:
            self.total_seen = seen
        self.update_text()

    def update_text(self):
        pct_seen = (self.total_seen / self.total_images * 100.0) if self.total_images else 0.0
        seen_nonzero = max(1, self.total_seen)
        pct_deleted_of_seen = (self.total_deleted / seen_nonzero * 100.0)
        kept = max(0, self.total_seen - self.total_deleted)
        self.total_kept = kept
        self.label.setText(
            f"Total: {self.total_images} | Left: {self.total_left} | Deleted: {self.total_deleted} | Kept: {kept} | Seen: {pct_seen:.1f}% | Deleted/Seen: {pct_deleted_of_seen:.1f}%"
        )


class ThumbnailCache(QtCore.QObject):
    thumbnail_ready = QtCore.Signal(str, QtGui.QPixmap)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.executor = ThreadPoolExecutor(max_workers=MAX_WORKERS)
        self._lock = threading.Lock()
        self._cache: Dict[str, QtGui.QPixmap] = {}

    def get_or_enqueue(self, path: str):
        with self._lock:
            pix = self._cache.get(path)
        if pix is not None:
            self.thumbnail_ready.emit(path, pix)
            return

        def load_thumb(p: str):
            image_reader = QtGui.QImageReader(p)
            image_reader.setAutoTransform(True)
            image_reader.setScaledSize(THUMBNAIL_SIZE)
            image = image_reader.read()
            if image.isNull():
                return None
            thumb = QtGui.QPixmap.fromImage(image)
            if thumb.size() != THUMBNAIL_SIZE:
                thumb = thumb.scaled(THUMBNAIL_SIZE, QtCore.Qt.KeepAspectRatio, QtCore.Qt.SmoothTransformation)
                canvas = QtGui.QPixmap(THUMBNAIL_SIZE)
                canvas.fill(QtCore.Qt.transparent)
                painter = QtGui.QPainter(canvas)
                x = (THUMBNAIL_SIZE.width() - thumb.width()) // 2
                y = (THUMBNAIL_SIZE.height() - thumb.height()) // 2
                painter.drawPixmap(x, y, thumb)
                painter.end()
                thumb = canvas
            return thumb

        def task_done(fut: "concurrent.futures.Future[Optional[QtGui.QPixmap]]"):
            thumb = fut.result()
            if thumb is None:
                return
            with self._lock:
                self._cache[path] = thumb
            self.thumbnail_ready.emit(path, thumb)

        fut = self.executor.submit(load_thumb, path)
        fut.add_done_callback(task_done)


class ImageGrid(QtWidgets.QWidget):
    corner_map = {
        'h': (0, 0),
        'G': (0, 1),
        'K': (1, 0),
        'L': (1, 1),
    }

    def __init__(self, stats: StatsBar, cache: ThumbnailCache):
        super().__init__()
        self.stats = stats
        self.cache = cache
        self.cache.thumbnail_ready.connect(self._on_thumb_ready)
        self.paths: List[str] = []
        self.queue: List[str] = []
        self.total_deleted = 0
        self.total_kept = 0
        self.total_seen = 0

        self.setFocusPolicy(QtCore.Qt.StrongFocus)
        grid = QtWidgets.QGridLayout(self)
        grid.setContentsMargins(24, 12, 24, 24)
        grid.setHorizontalSpacing(16)
        grid.setVerticalSpacing(16)

        self.slots: List[List[ImageSlot]] = []
        for r in range(GRID_ROWS):
            row_widgets: List[ImageSlot] = []
            for c in range(GRID_COLS):
                slot = ImageSlot()
                grid.addWidget(slot, r, c)
                row_widgets.append(slot)
            self.slots.append(row_widgets)

    def set_images(self, paths: List[str]):
        self.paths = list(paths)
        self.queue = list(paths)
        self.total_deleted = 0
        self.total_kept = 0
        self.total_seen = 0
        self._refresh_stats()
        self._fill_all()
        self.setFocus()

    def _refresh_stats(self):
        self.stats.update_stats(
            total=len(self.paths),
            left=len(self.queue),
            deleted=self.total_deleted,
            kept=self.total_kept,
            seen=self.total_seen,
        )

    def _next_image(self) -> Optional[str]:
        if not self.queue:
            return None
        return self.queue.pop(0)

    def _fill_all(self):
        for r in range(GRID_ROWS):
            for c in range(GRID_COLS):
                self._fill_slot(r, c)

    def _fill_slot(self, r: int, c: int):
        slot = self.slots[r][c]
        path = self._next_image()
        slot.set_path(path)
        if path is None:
            slot.set_pixmap(None)
        else:
            self.total_seen += 1
            self.cache.get_or_enqueue(path)
        self._refresh_stats()

    @QtCore.Slot(str, QtGui.QPixmap)
    def _on_thumb_ready(self, path: str, pix: QtGui.QPixmap):
        for r in range(GRID_ROWS):
            for c in range(GRID_COLS):
                slot = self.slots[r][c]
                if slot.path() == path:
                    slot.set_pixmap(pix)

    def keyPressEvent(self, event: QtGui.QKeyEvent):
        key_code = event.key()
        if key_code == QtCore.Qt.Key_H:
            self._delete_at(0, 0)
            return
        if key_code == QtCore.Qt.Key_G:
            self._delete_at(0, 1)
            return
        if key_code == QtCore.Qt.Key_K:
            self._delete_at(1, 0)
            return
        if key_code == QtCore.Qt.Key_L:
            self._delete_at(1, 1)
            return
        if key_code == QtCore.Qt.Key_M:
            self._delete_many([(0, 0), (0, 1), (1, 0), (1, 1)])
            return
        super().keyPressEvent(event)

    def _delete_many(self, coords: List[Tuple[int, int]]):
        for r, c in coords:
            self._delete_at(r, c, refresh_stats=False)
        self._refresh_stats()

    def _delete_at(self, r: int, c: int, refresh_stats: bool = True):
        slot = self.slots[r][c]
        path = slot.path()
        if path is None:
            QtWidgets.QApplication.beep()
            return
        try:
            send2trash(path)
            self.total_deleted += 1
        except Exception:
            pass
        self._fill_slot(r, c)
        if refresh_stats:
            self._refresh_stats()


class TopBar(QtWidgets.QWidget):
    open_requested = QtCore.Signal()

    def __init__(self):
        super().__init__()
        self.button = QtWidgets.QPushButton("Open")
        self.button.setCursor(QtCore.Qt.PointingHandCursor)
        self.button.setFixedHeight(36)
        layout = QtWidgets.QHBoxLayout(self)
        layout.setContentsMargins(24, 16, 24, 8)
        layout.addWidget(self.button, 0, QtCore.Qt.AlignLeft)
        layout.addStretch(1)
        self.button.clicked.connect(self.open_requested)


class MainWindow(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        self.setObjectName("mainWindow")
        self.setWindowTitle("Viewer")
        self.setCursor(QtCore.Qt.ArrowCursor)
        self.setWindowFlag(QtCore.Qt.FramelessWindowHint, False)
        self.setWindowState(self.windowState() | QtCore.Qt.WindowFullScreen)

        self.stats = StatsBar()
        self.cache = ThumbnailCache(self)
        self.grid = ImageGrid(self.stats, self.cache)
        self.top = TopBar()
        self.top.open_requested.connect(self._open_folder)

        container = QtWidgets.QWidget()
        v = QtWidgets.QVBoxLayout(container)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)
        v.addWidget(self.top)
        v.addWidget(self.stats)
        v.addWidget(self.grid, 1)
        self.setCentralWidget(container)

        self._apply_style()

    def showEvent(self, event: QtGui.QShowEvent):
        super().showEvent(event)
        QtCore.QTimer.singleShot(0, self.grid.setFocus)

    def _apply_style(self):
        self.setStyleSheet(
            """
            QWidget { background-color: #0f1115; color: #e6e6e6; font-family: 'Segoe UI', 'Inter', sans-serif; font-size: 14px; }
            QPushButton { background-color: #2a2f3a; border: 1px solid #3a4050; padding: 6px 14px; border-radius: 8px; }
            QPushButton:hover { background-color: #343b49; }
            QPushButton:pressed { background-color: #232833; }
            QLabel#imageLabel { background-color: #131722; border: 1px solid #1f2430; border-radius: 12px; }
            QWidget#imageSlot { border-radius: 12px; }
            """
        )

    def _open_folder(self):
        start_dir = CONFIG_START_DIR or os.environ.get("VIEWER_START_DIR", DEFAULT_START_DIR)
        dlg = QtWidgets.QFileDialog(self, "Select Folder", start_dir)
        dlg.setFileMode(QtWidgets.QFileDialog.Directory)
        dlg.setOption(QtWidgets.QFileDialog.ShowDirsOnly, True)
        if dlg.exec():
            dirs = dlg.selectedFiles()
            if dirs:
                self._load_folder(dirs[0])

    def _load_folder(self, folder: str):
        exts = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tiff", ".webp"}
        paths: List[str] = []
        p = Path(folder)
        if p.exists() and p.is_dir():
            for entry in sorted(p.iterdir()):
                if entry.is_file() and entry.suffix.lower() in exts:
                    paths.append(str(entry))
        self.grid.set_images(paths)


def main():
    if getattr(sys, 'frozen', False):
        base = getattr(sys, '_MEIPASS', Path.cwd())
        os.chdir(base)
    app = QtWidgets.QApplication(sys.argv)
    app.setApplicationName("Viewer")
    app.setOrganizationName("Viewer")
    win = MainWindow()
    win.showFullScreen()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()


