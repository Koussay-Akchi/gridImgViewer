import os
import sys
import threading
import queue
import hashlib
import shutil
from pathlib import Path
from typing import Optional, List, Tuple, Dict
from concurrent.futures import ThreadPoolExecutor

import tkinter as tk
from tkinter import ttk, filedialog
from PIL import Image, ImageTk, ImageOps
from send2trash import send2trash


CONFIG_START_DIR = ""
THUMBNAIL_SIZE = (320, 320)
GRID_ROWS = 2
GRID_COLS = 2
MAX_WORKERS = max(4, (os.cpu_count() or 4))


class ThumbnailCache:
    def __init__(self):
        self._cache: Dict[str, ImageTk.PhotoImage] = {}
        self._lock = threading.Lock()
        self._executor = ThreadPoolExecutor(max_workers=MAX_WORKERS)

    def get_or_submit(self, path: str, callback):
        with self._lock:
            pix = self._cache.get(path)
        if pix is not None:
            callback(path, pix)
            return

        def load_thumb(p: str) -> Optional[ImageTk.PhotoImage]:
            try:
                im = Image.open(p)
                im = ImageOps.exif_transpose(im)
                im.thumbnail(THUMBNAIL_SIZE, Image.Resampling.LANCZOS)
                canvas = Image.new("RGBA", THUMBNAIL_SIZE, (0, 0, 0, 0))
                x = (THUMBNAIL_SIZE[0] - im.width) // 2
                y = (THUMBNAIL_SIZE[1] - im.height) // 2
                canvas.paste(im, (x, y))
                return ImageTk.PhotoImage(canvas)
            except Exception:
                return None

        def done(fut):
            img = fut.result()
            if img is None:
                return
            with self._lock:
                self._cache[path] = img
            callback(path, img)

        fut = self._executor.submit(load_thumb, path)
        fut.add_done_callback(lambda f: done(f))


class StatsBar(ttk.Frame):
    def __init__(self, master):
        super().__init__(master)
        self.total_images = 0
        self.total_left = 0
        self.total_deleted = 0
        self.total_kept = 0
        self.total_seen = 0
        self.label = ttk.Label(self, text="")
        self.label.pack(side=tk.LEFT, padx=12, pady=8)
        self.update_text()

    def update_stats(self, total=None, left=None, deleted=None, kept=None, seen=None):
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
        self.label.configure(text=f"Total: {self.total_images} | Left: {self.total_left} | Deleted: {self.total_deleted} | Kept: {kept} | Seen: {pct_seen:.1f}% | Deleted/Seen: {pct_deleted_of_seen:.1f}%")


class ImageSlot(ttk.Frame):
    def __init__(self, master, on_click):
        super().__init__(master)
        self._path: Optional[str] = None
        self._photo: Optional[ImageTk.PhotoImage] = None
        self.label = ttk.Label(self, anchor=tk.CENTER)
        self.label.pack(expand=True, fill=tk.BOTH)
        self.label.bind("<Button-1>", self._handle_click)
        self._on_click = on_click
        self.configure(padding=2)

    def set_path(self, path: Optional[str]):
        self._path = path

    def path(self) -> Optional[str]:
        return self._path

    def set_pixmap(self, photo: Optional[ImageTk.PhotoImage]):
        self._photo = photo
        if photo is None:
            self.label.configure(text=":-)")
            self.label.configure(image="")
        else:
            self.label.configure(text="")
            self.label.configure(image=photo)

    def _handle_click(self, _):
        if self._path:
            try:
                os.startfile(self._path)
            except Exception:
                pass


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("GridImgViewer")
        self.state("zoomed")
        try:
            self.attributes('-fullscreen', True)
        except Exception:
            pass
        self.configure(bg="#0f1115")
        self.style = ttk.Style(self)
        self.style.theme_use("clam")
        self.style.configure("TFrame", background="#0f1115")
        self.style.configure("TLabel", background="#0f1115", foreground="#e6e6e6")
        self.style.configure("TButton", background="#2a2f3a", foreground="#e6e6e6")

        self.cache = ThumbnailCache()
        self.paths: List[str] = []
        self.queue_paths: List[str] = []
        self.total_deleted = 0
        self.total_seen = 0
        self.undo_stack: List[List[Tuple[int, int, str, str]]] = []

        top = ttk.Frame(self)
        top.pack(side=tk.TOP, fill=tk.X)
        self.open_button = ttk.Button(top, text="Open", command=self._open_folder)
        self.open_button.pack(side=tk.LEFT, padx=16, pady=10)

        self.stats = StatsBar(self)
        self.stats.pack(side=tk.TOP, fill=tk.X)

        center = ttk.Frame(self)
        center.pack(expand=True, fill=tk.BOTH)

        grid_wrapper = ttk.Frame(center)
        grid_wrapper.pack(expand=True)
        grid_wrapper.grid_columnconfigure(0, weight=0)
        grid_wrapper.grid_columnconfigure(1, weight=1)
        grid_wrapper.grid_columnconfigure(2, weight=1)
        grid_wrapper.grid_columnconfigure(3, weight=0)
        grid_wrapper.grid_rowconfigure(0, weight=1)
        grid_wrapper.grid_rowconfigure(1, weight=1)

        self.slots: List[List[ImageSlot]] = []
        for r in range(GRID_ROWS):
            row_widgets: List[ImageSlot] = []
            # Left legend aligned with row center
            lbl_left = ttk.Label(grid_wrapper, text=("H" if r == 0 else "K"))
            lbl_left.configure(font=("Segoe UI Semibold", 20))
            lbl_left.grid(row=r, column=0, padx=(16, 8), sticky="e")
            for c in range(GRID_COLS):
                slot = ImageSlot(grid_wrapper, on_click=self._on_slot_click)
                slot.grid(row=r, column=c + 1, padx=12, pady=12)
                row_widgets.append(slot)
            self.slots.append(row_widgets)
            # Right legend aligned with row center
            lbl_right = ttk.Label(grid_wrapper, text=("J" if r == 0 else "L"))
            lbl_right.configure(font=("Segoe UI Semibold", 20))
            lbl_right.grid(row=r, column=3, padx=(8, 16), sticky="w")

        side_left = ttk.Label(self, text="Z: Undo")
        side_left.configure(font=("Segoe UI Semibold", 20))
        side_left.place(relx=0.0, rely=0.5, anchor="w", x=16)

        side_right = ttk.Label(self, text="M: Delete all")
        side_right.configure(font=("Segoe UI Semibold", 20))
        side_right.place(relx=1.0, rely=0.5, anchor="e", x=-16)

        self.bind_all("<Key>", self._on_key)
        self.after(0, self._auto_open_or_load)

    def _on_slot_click(self, _slot: ImageSlot):
        pass

    def _on_key(self, event):
        key = event.keysym.lower()
        if key == "h":
            self._delete_at(0, 0)
            return
        if key == "j":
            self._delete_at(0, 1)
            return
        if key == "k":
            self._delete_at(1, 0)
            return
        if key == "l":
            self._delete_at(1, 1)
            return
        if key == "m":
            self._delete_many([(0, 0), (0, 1), (1, 0), (1, 1)])
            return
        if key == "z":
            self._undo_last()
            return

    def _refresh_stats(self):
        self.stats.update_stats(
            total=len(self.paths),
            left=len(self.queue_paths),
            deleted=self.total_deleted,
            kept=max(0, self.total_seen - self.total_deleted),
            seen=self.total_seen,
        )

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
            self.cache.get_or_submit(path, lambda p, img: self._on_thumb_ready(r, c, p, img))
        self._refresh_stats()

    def _on_thumb_ready(self, r: int, c: int, path: str, photo: ImageTk.PhotoImage):
        slot = self.slots[r][c]
        if slot.path() == path:
            slot.set_pixmap(photo)

    def _next_image(self) -> Optional[str]:
        if not self.queue_paths:
            return None
        return self.queue_paths.pop(0)

    def _delete_many(self, coords: List[Tuple[int, int]]):
        self._delete_slots(coords)

    def _delete_at(self, r: int, c: int):
        self._delete_slots([(r, c)])

    def _delete_slots(self, coords: List[Tuple[int, int]]):
        batch: List[Tuple[int, int, str, str]] = []
        for r, c in coords:
            slot = self.slots[r][c]
            path = slot.path()
            if path is None:
                self.bell()
                continue
            backup_path = self._backup_for_path(path)
            try:
                shutil.copy2(path, backup_path)
            except Exception:
                backup_path = ""
            try:
                send2trash(path)
                self.total_deleted += 1
            except Exception:
                pass
            batch.append((r, c, path, backup_path))
        if batch:
            self.undo_stack.append(batch)
        for r, c, _, _ in batch:
            self._fill_slot(r, c)
        self._refresh_stats()

    def _undo_last(self):
        if not self.undo_stack:
            self.bell()
            return
        batch = self.undo_stack.pop()
        restored_any = False
        for r, c, original_path, backup_path in batch:
            if not backup_path or not Path(backup_path).exists():
                continue
            try:
                restored_path = original_path
                op = Path(restored_path)
                if op.exists():
                    stem, suffix = op.stem, op.suffix
                    i = 1
                    while True:
                        candidate = op.with_name(f"{stem}_restored_{i}{suffix}")
                        if not candidate.exists():
                            restored_path = str(candidate)
                            break
                        i += 1
                shutil.copy2(backup_path, restored_path)
                self._insert_into_slot(r, c, restored_path)
                restored_any = True
                self.total_deleted = max(0, self.total_deleted - 1)
            except Exception:
                pass
        if restored_any:
            self._refresh_stats()

    def _insert_into_slot(self, r: int, c: int, path: str):
        slot = self.slots[r][c]
        slot.set_path(path)
        self.cache.get_or_submit(path, lambda p, img: self._on_thumb_ready(r, c, p, img))

    def _open_folder(self):
        initial_dir = self._get_initial_dir()
        folder = filedialog.askdirectory(initialdir=initial_dir, mustexist=True, title="Select Folder")
        if folder:
            self._persist_last_dir(folder)
            self._load_folder(folder)

    def _load_folder(self, folder: str):
        exts = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tiff", ".webp"}
        paths: List[str] = []
        p = Path(folder)
        if p.exists() and p.is_dir():
            for entry in sorted(p.iterdir()):
                if entry.is_file() and entry.suffix.lower() in exts:
                    paths.append(str(entry))
        self.paths = paths
        self.queue_paths = list(paths)
        self.total_deleted = 0
        self.total_seen = 0
        for r in range(GRID_ROWS):
            for c in range(GRID_COLS):
                self.slots[r][c].set_pixmap(None)
                self.slots[r][c].set_path(None)
        self._refresh_stats()
        self._fill_all()

    def _backup_dir(self) -> Path:
        base = os.getenv("APPDATA")
        if not base:
            base = str(Path.home())
        d = Path(base) / "gridImgViewer" / "session_restore"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _backup_for_path(self, original_path: str) -> str:
        h = hashlib.sha1(original_path.encode("utf-8", errors="ignore")).hexdigest()
        ext = Path(original_path).suffix
        target = self._backup_dir() / f"{h}{ext}"
        return str(target)

    def _appdata_dir(self) -> Path:
        base = os.getenv("APPDATA")
        if not base:
            base = str(Path.home())
        app_dir = Path(base) / "gridImgViewer"
        app_dir.mkdir(parents=True, exist_ok=True)
        return app_dir

    def _last_dir_file(self) -> Path:
        return self._appdata_dir() / "last_dir.txt"

    def _read_last_dir(self) -> Optional[str]:
        try:
            f = self._last_dir_file()
            if f.exists():
                txt = f.read_text(encoding="utf-8").strip()
                return txt or None
        except Exception:
            return None
        return None

    def _persist_last_dir(self, folder: str):
        try:
            self._last_dir_file().write_text(folder, encoding="utf-8")
        except Exception:
            pass

    def _get_initial_dir(self) -> str:
        if CONFIG_START_DIR:
            return CONFIG_START_DIR
        env_dir = os.environ.get("VIEWER_START_DIR")
        if env_dir:
            return env_dir
        last = self._read_last_dir()
        if last and Path(last).exists():
            return last
        return str(Path.home())

    def _auto_open_or_load(self):
        last = self._read_last_dir()
        if last and Path(last).exists():
            self._load_folder(last)
            return
        self._open_folder()


def main():
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()


