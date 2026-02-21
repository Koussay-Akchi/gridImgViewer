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
import ttkbootstrap as tb
from PIL import Image, ImageTk, ImageOps
from send2trash import send2trash
import json


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
        self.path_label_var = tk.StringVar(value="")
        self.path_label = ttk.Label(self, textvariable=self.path_label_var, font=("Segoe UI", 8))
        self.path_label.pack(side=tk.TOP, padx=16, pady=(2, 8), anchor="w")
        
        self.label = ttk.Label(self, text="")
        self.label.pack(side=tk.TOP, padx=12, pady=(8, 0), anchor="w")
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
        self.label.configure(text=f"Total: {self.total_images} | Left: {self.total_left} | Deleted: {self.total_deleted} | Kept: {self.total_kept} | Seen: {pct_seen:.1f}% | Deleted/Seen: {pct_deleted_of_seen:.1f}%")

    def update_folder_path(self, folder_path: str):
        self.path_label_var.set(folder_path or "")


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


class ToggleSwitch(ttk.Frame):
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
        self.bs = tb.Style(theme="darkly")
        
        self.style.configure("DarkRed.TCheckbutton", background="#3d1f1f", foreground="#e6e6e6", focuscolor="#3d1f1f")
        self.style.configure("DarkNormal.TCheckbutton", background="#0f1115", foreground="#e6e6e6", focuscolor="#0f1115")

        self._cleanup_session_restore()

        self.cache = ThumbnailCache()
        self.paths: List[str] = []
        self.queue_paths: List[str] = []
        self.total_deleted = 0
        self.total_kept = 0
        self.total_seen = 0
        self.undo_stack: List[List[Tuple]] = []
        self.keymap = self._read_keymap()
        self.bg_color_setting = self._read_bg_color_setting()

        top = ttk.Frame(self)
        top.pack(side=tk.TOP, fill=tk.X)
        self.open_button = ttk.Button(top, text="Open", command=self._open_folder)
        self.open_button.pack(side=tk.LEFT, padx=16, pady=10)
        self.settings_button = ttk.Button(top, text="Settings", command=self._open_settings)
        self.settings_button.pack(side=tk.LEFT, padx=8, pady=10)
        
        self.toggle_key_label = ttk.Label(top, text="", font=("Segoe UI", 8))
        self.toggle_key_label.pack(side=tk.LEFT, padx=0, pady=10)
        self.mode_delete = self._read_last_mode()
        self.mode_var = tk.BooleanVar(value=self.mode_delete)
        init_text = "Delete mode" if self.mode_var.get() else "Keep mode"
        self.mode_button = ttk.Checkbutton(
            top,
            text=init_text,
            variable=self.mode_var,
            command=self._on_mode_changed,
            style="DarkNormal.TCheckbutton",
        )
        self.mode_button.pack(side=tk.LEFT, padx=0, pady=10)

        self.open_after_keep_var = tk.BooleanVar(value=False)
        self.open_after_keep_chk = ttk.Checkbutton(top, text="open after keeping?", variable=self.open_after_keep_var)
        self.open_after_keep_chk.pack(side=tk.LEFT, padx=(0, 0), pady=10)


        self.stats = StatsBar(self)
        self.stats.pack(side=tk.TOP, fill=tk.X)

        controls_row2 = ttk.Frame(self)
        controls_row2.pack(side=tk.TOP, fill=tk.X, padx=0, pady=(0, 10))
        self.open_kept_btn = ttk.Button(controls_row2, text="Open kept folder", command=lambda: self._open_kept_folder())
        self.open_kept_btn.pack(side=tk.LEFT, padx=16, pady=0)

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
            lbl_left = ttk.Label(grid_wrapper, text=("U" if r == 0 else "J"))
            lbl_left.configure(font=("Segoe UI Semibold", 20))
            lbl_left.grid(row=r, column=0, padx=(16, 8), sticky="e")
            if r == 0:
                self.legend_H = lbl_left
            else:
                self.legend_K = lbl_left
            for c in range(GRID_COLS):
                slot = ImageSlot(grid_wrapper, on_click=self._on_slot_click)
                slot.grid(row=r, column=c + 1, padx=12, pady=12)
                row_widgets.append(slot)
            self.slots.append(row_widgets)
            lbl_right = ttk.Label(grid_wrapper, text=("I" if r == 0 else "K"))
            lbl_right.configure(font=("Segoe UI Semibold", 20))
            lbl_right.grid(row=r, column=3, padx=(8, 16), sticky="w")
            if r == 0:
                self.legend_J = lbl_right
            else:
                self.legend_L = lbl_right

        side_left = ttk.Label(self, text="Z: Undo")
        side_left.configure(font=("Segoe UI Semibold", 20))
        side_left.place(relx=0.0, rely=0.5, anchor="w", x=16)
        self.legend_Z = side_left

        side_right = ttk.Label(self, text="M: Delete all")
        side_right.configure(font=("Segoe UI Semibold", 20))
        side_right.place(relx=1.0, rely=0.5, anchor="e", x=-16)
        self.legend_M = side_right

        self.fx_key_color = "#010203"
        self.fx_win = tk.Toplevel(self)
        self.fx_win.overrideredirect(True)
        try:
            self.fx_win.attributes('-topmost', True)
            self.fx_win.attributes('-transparentcolor', self.fx_key_color)
        except Exception:
            pass
        self.fx_win.configure(bg=self.fx_key_color)
        self.fx_canvas = tk.Canvas(self.fx_win, highlightthickness=0, bd=0, bg=self.fx_key_color)
        self.fx_canvas.pack(fill=tk.BOTH, expand=True)
        self.fx_win.withdraw()
        
        self.after(0, self._on_mode_changed)
        self.after(0, self._update_legends_from_keymap)

        self.bind_all("<Key>", self._on_key)
        self.after(0, self._auto_open_or_load)

    def _cleanup_session_restore(self):
        try:
            restore_dir = self._backup_dir()
            if restore_dir.exists():
                shutil.rmtree(restore_dir)
        except Exception:
            pass

    def _on_slot_click(self, _slot: ImageSlot):
        pass

    def _on_key(self, event):
        if hasattr(self, '_settings_window') and self._settings_window and self._settings_window.winfo_exists():
            if hasattr(self, '_recording_key') and self._recording_key:
                key = event.keysym.lower()
                if (key.isalpha() and len(key) == 1) or key in ["shift_l", "shift_r", "ctrl_l", "ctrl_r", "alt_l", "alt_r", "space", "tab", "return", "escape"]:
                    self._finish_key_recording(key)
                return
        
        key = event.keysym.lower()
        if key == self.keymap.get("top_left", "u").lower():
            if self.mode_delete:
                self._delete_at(0, 0)
            else:
                self._keep_at(0, 0)
            self._pulse_over_widget(self.legend_H)
            return
        if key == self.keymap.get("top_right", "i").lower():
            if self.mode_delete:
                self._delete_at(0, 1)
            else:
                self._keep_at(0, 1)
            self._pulse_over_widget(self.legend_J)
            return
        if key == self.keymap.get("bottom_left", "j").lower():
            if self.mode_delete:
                self._delete_at(1, 0)
            else:
                self._keep_at(1, 0)
            self._pulse_over_widget(self.legend_K)
            return
        if key == self.keymap.get("bottom_right", "k").lower():
            if self.mode_delete:
                self._delete_at(1, 1)
            else:
                self._keep_at(1, 1)
            self._pulse_over_widget(self.legend_L)
            return
        if key == self.keymap.get("delete_all", "m").lower():
            if self.mode_delete:
                self._delete_many([(0, 0), (0, 1), (1, 0), (1, 1)])
            else:
                self._keep_slots([(0, 0), (0, 1), (1, 0), (1, 1)])
            self._pulse_over_widget(self.legend_M)
            return
        if key == self.keymap.get("undo", "z").lower():
            self._undo_last()
            self._pulse_over_widget(self.legend_Z)
            return
        if key == self.keymap.get("toggle_mode", "shift_l").lower():
            self.mode_var.set(not self.mode_var.get())
            self._on_mode_changed()
            return

    def _refresh_stats(self):
        self.stats.update_stats(
            total=len(self.paths),
            left=len(self.queue_paths),
            deleted=self.total_deleted,
            kept=self.total_kept,
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
        batch: List[Tuple[int, int, str, str, str]] = []
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
            batch.append((r, c, path, backup_path, "delete"))
        if batch:
            self.undo_stack.append(batch)
        for r, c, _, _, _ in batch:
            self._fill_slot(r, c)
        self._refresh_stats()

    def _undo_last(self):
        if not self.undo_stack:
            self.bell()
            return
        batch = self.undo_stack.pop()
        restored_any = False
        for item in batch:
            if len(item) == 4:
                r, c, original_path, backup_path = item
                op_type = "delete"
            else:
                r, c, original_path, backup_path, op_type = item
            try:
                if op_type == "delete":
                    if not backup_path or not Path(backup_path).exists():
                        continue
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
                elif op_type == "keep":
                    moved_target = Path(backup_path) if backup_path else None
                    if not moved_target or not moved_target.exists():
                        continue
                    original = Path(original_path)
                    if original.exists():
                        restored = self._unique_target(original.parent, original.name)
                    else:
                        restored = original
                    shutil.move(str(moved_target), str(restored))
                    self._insert_into_slot(r, c, str(restored))
                    restored_any = True
                    self.total_kept = max(0, self.total_kept - 1)
                else:
                    continue
            except Exception:
                pass
        if restored_any:
            self._refresh_stats()

    def _insert_into_slot(self, r: int, c: int, path: str):
        slot = self.slots[r][c]
        slot.set_path(path)
        self.cache.get_or_submit(path, lambda p, img: self._on_thumb_ready(r, c, p, img))

    def _widget_center(self, w: tk.Widget) -> Tuple[int, int, int]:
        self.update_idletasks()
        x = w.winfo_rootx() - self.fx_canvas.winfo_rootx()
        y = w.winfo_rooty() - self.fx_canvas.winfo_rooty()
        wdt = w.winfo_width()
        hgt = w.winfo_height()
        return x + wdt // 2, y + hgt // 2, max(wdt, hgt)

    def _pulse_over_widget(self, w: tk.Widget):
        cx, cy, _ = self._widget_center(w)
        self._pulse_at(cx, cy, max_radius=50, duration_ms=180)

    def _pulse_at(self, x: int, y: int, steps: Optional[List[int]] = None, *, max_radius: int = 50, duration_ms: int = 180):
        color = "white"
        self.update_idletasks()
        gx = self.winfo_rootx()
        gy = self.winfo_rooty()
        gw = self.winfo_width()
        gh = self.winfo_height()
        self.fx_win.geometry(f"{gw}x{gh}+{gx}+{gy}")
        self.fx_win.deiconify()
        self.fx_canvas.delete("all")
        oid = self.fx_canvas.create_oval(x-1, y-1, x+1, y+1, fill="", outline=color, width=3)

        start_time = None

        def ease_out_cubic(t: float) -> float:
            u = 1.0 - t
            return 1.0 - u * u * u

        def animate():
            nonlocal start_time
            now = self.tk.call('after', 'info')
            import time
            if start_time is None:
                start_time = time.perf_counter()
            elapsed = (time.perf_counter() - start_time) * 1000.0
            t = max(0.0, min(1.0, elapsed / float(duration_ms)))
            r = int(ease_out_cubic(t) * max_radius)
            if r < 1:
                r = 1
            self.fx_canvas.coords(oid, x - r, y - r, x + r, y + r)
            if t >= 1.0:
                self.fx_canvas.delete(oid)
                self.fx_win.withdraw()
                return
            self.after(15, animate)

        animate()

    def _open_folder(self):
        initial_dir = self._get_initial_dir()
        folder = filedialog.askdirectory(initialdir=initial_dir, mustexist=True, title="Select Folder")
        if folder:
            self._persist_last_dir(folder)
            self._load_folder(folder)

    def _on_mode_changed(self):
        self.fx_win.configure(bg=self.fx_key_color)
        self.fx_canvas.configure(bg=self.fx_key_color)
        self.mode_delete = self.mode_var.get()
        if self.mode_delete:
            self.mode_button.configure(text="Delete mode", style="DarkRed.TCheckbutton")
            self.legend_H.configure(foreground="#e6e6e6")
            self.legend_J.configure(foreground="#e6e6e6")
            self.legend_K.configure(foreground="#e6e6e6")
            self.legend_L.configure(foreground="#e6e6e6")
            if self.bg_color_setting:
                self.configure(bg="#3d1f1f")
                self.style.configure("TFrame", background="#3d1f1f")
                self.style.configure("TLabel", background="#3d1f1f", foreground="#e6e6e6")
        else:
            self.mode_button.configure(text="Keep mode", style="DarkNormal.TCheckbutton")
            self.legend_H.configure(foreground="#9be7a8")
            self.legend_J.configure(foreground="#9be7a8")
            self.legend_K.configure(foreground="#9be7a8")
            self.legend_L.configure(foreground="#9be7a8")
            self.configure(bg="#0f1115")
            self.style.configure("TFrame", background="#0f1115")
            self.style.configure("TLabel", background="#0f1115", foreground="#e6e6e6")
        self._persist_last_mode(self.mode_delete)
        self._update_legends_from_keymap()

    def _keep_at(self, r: int, c: int):
        self._keep_slots([(r, c)])

    def _kept_dir(self) -> Optional[Path]:
        try:
            if hasattr(self, "current_folder") and self.current_folder:
                d = Path(self.current_folder) / "kept"
                d.mkdir(parents=True, exist_ok=True)
                return d
        except Exception:
            return None
        return None

    def _open_kept_folder(self):
        try:
            d = self._kept_dir()
            if d is not None:
                os.startfile(str(d))
        except Exception:
            pass

    def _unique_target(self, directory: Path, original_name: str) -> Path:
        base = Path(original_name).stem
        suffix = Path(original_name).suffix
        candidate = directory / f"{base}{suffix}"
        if not candidate.exists():
            return candidate
        i = 1
        while True:
            c = directory / f"{base}_{i}{suffix}"
            if not c.exists():
                return c
            i += 1

    def _keep_slots(self, coords: List[Tuple[int, int]]):
        kept_dir = self._kept_dir()
        if kept_dir is None:
            self.bell()
            return
        batch: List[Tuple[int, int, str, str, str]] = []
        for r, c in coords:
            slot = self.slots[r][c]
            path = slot.path()
            if path is None:
                self.bell()
                continue
            try:
                target = self._unique_target(kept_dir, Path(path).name)
                shutil.move(path, str(target))
                self.total_kept += 1
                if self.open_after_keep_var.get():
                    try:
                        os.startfile(str(target))
                    except Exception:
                        pass
                batch.append((r, c, path, str(target), "keep"))
            except Exception:
                pass
        if batch:
            self.undo_stack.append(batch)
        for r, c, _, _, _ in batch:
            self._fill_slot(r, c)
        self._refresh_stats()

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
        self.current_folder = folder
        for r in range(GRID_ROWS):
            for c in range(GRID_COLS):
                self.slots[r][c].set_pixmap(None)
                self.slots[r][c].set_path(None)
        self._refresh_stats()
        try:
            self.stats.update_folder_path(folder)
        except Exception:
            pass
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

    def _mode_file(self) -> Path:
        return self._appdata_dir() / "mode.txt"

    def _bg_color_file(self) -> Path:
        return self._appdata_dir() / "bg_color.txt"

    def _read_bg_color_setting(self) -> bool:
        try:
            f = self._bg_color_file()
            if f.exists():
                content = f.read_text(encoding="utf-8").strip().lower()
                return content == "true"
        except Exception:
            pass
        return False

    def _persist_bg_color_setting(self, enabled: bool):
        try:
            self._bg_color_file().write_text("true" if enabled else "false", encoding="utf-8")
        except Exception:
            pass

    def _read_last_mode(self) -> bool:
        try:
            f = self._mode_file()
            if f.exists():
                txt = f.read_text(encoding="utf-8").strip().lower()
                if txt == "delete":
                    return True
                if txt in ("open", "keep"):
                    return False
                return True
            return True
        except Exception:
            return True

    def _persist_last_mode(self, delete_mode: bool):
        try:
            self._mode_file().write_text("delete" if delete_mode else "keep", encoding="utf-8")
        except Exception:
            pass

    def _keymap_file(self) -> Path:
        return self._appdata_dir() / "keys.json"

    def _read_keymap(self) -> Dict[str, str]:
        defaults = {
            "top_left": "u",
            "top_right": "i",
            "bottom_left": "j",
            "bottom_right": "k",
            "undo": "z",
            "delete_all": "m",
            "toggle_mode": "shift_l",
        }
        try:
            f = self._keymap_file()
            if f.exists():
                obj = json.loads(f.read_text(encoding="utf-8"))
                if isinstance(obj, dict):
                    merged = defaults.copy()
                    for k, v in obj.items():
                        if isinstance(v, str) and k in merged and v:
                            merged[k] = v.lower()
                    return merged
        except Exception:
            pass
        return defaults

    def _persist_keymap(self):
        try:
            self._keymap_file().write_text(json.dumps(self.keymap, indent=2), encoding="utf-8")
        except Exception:
            pass

    def _update_legends_from_keymap(self):
        try:
            self.legend_H.configure(text=self.keymap.get("top_left", "u").upper())
            self.legend_J.configure(text=self.keymap.get("top_right", "i").upper())
            self.legend_K.configure(text=self.keymap.get("bottom_left", "j").upper())
            self.legend_L.configure(text=self.keymap.get("bottom_right", "k").upper())
            self.legend_Z.configure(text=f"{self.keymap.get('undo', 'z').upper()}: Undo")
            action_all = "Delete all" if getattr(self, 'mode_delete', True) else "Keep all"
            self.legend_M.configure(text=f"{self.keymap.get('delete_all', 'm').upper()}: {action_all}")
            
            # Update toggle key label
            toggle_key = self.keymap.get("toggle_mode", "shift_l")
            toggle_display = self._format_key_display(toggle_key)
            self.toggle_key_label.configure(text=f"Toggle: {toggle_display}")
        except Exception:
            pass

    def _open_settings(self):
        self._settings_window = tk.Toplevel(self)
        win = self._settings_window
        win.title("Settings")
        win.transient(self)
        try:
            win.grab_set()
        except Exception:
            pass
        frm = ttk.Frame(win)
        frm.pack(padx=16, pady=16, fill=tk.BOTH, expand=True)

        self._recording_key = None
        self._key_buttons = {}
        self._key_vars = {}
        
        bg_color_var = tk.BooleanVar(value=self.bg_color_setting)
        ttk.Checkbutton(frm, text="Change background color in delete mode", variable=bg_color_var).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 12))
        
        fields = [
            ("top_left", "Top-Left"),
            ("top_right", "Top-Right"),
            ("bottom_left", "Bottom-Left"),
            ("bottom_right", "Bottom-Right"),
            ("undo", "Undo"),
            ("delete_all", "Delete All"),
            ("toggle_mode", "Toggle Mode"),
        ]
        
        for idx, (key, label) in enumerate(fields):
            ttk.Label(frm, text=label).grid(row=idx+1, column=0, sticky="e", padx=(0,8), pady=6)
            
            key_value = self.keymap.get(key, "")
            display_value = self._format_key_display(key_value)
            var = tk.StringVar(value=display_value)
            self._key_vars[key] = var
            
            btn = ttk.Button(frm, textvariable=var, width=12, command=lambda k=key: self._start_key_recording(k))
            btn.grid(row=idx+1, column=1, sticky="w", pady=6)
            self._key_buttons[key] = btn

        btns = ttk.Frame(frm)
        btns.grid(row=len(fields)+1, column=0, columnspan=2, pady=(12,0))
        status_var = tk.StringVar(value="")
        status_lbl = ttk.Label(frm, textvariable=status_var, foreground="#ff8080")
        status_lbl.grid(row=len(fields)+2, column=0, columnspan=2, pady=(6,0))

        def on_save():
            values = {}
            for k, var in self._key_vars.items():
                display_txt = (var.get() or "").strip()
                key_value = self._display_to_key_value(display_txt)
                if not key_value:
                    status_var.set("Each key must be a single alphabet letter or special key.")
                    return
                values[k] = key_value
            # uniqueness
            used = set(values.values())
            if len(used) != len(values):
                status_var.set("Keys must be unique.")
                return
            self.keymap = values
            self._persist_keymap()
            self.bg_color_setting = bg_color_var.get()
            self._persist_bg_color_setting(self.bg_color_setting)
            self._on_mode_changed()
            status_var.set("")
            self._settings_window = None
            win.destroy()

        def on_cancel():
            self._settings_window = None
            win.destroy()

        ttk.Button(btns, text="Save", command=on_save).pack(side=tk.LEFT, padx=6)
        ttk.Button(btns, text="Cancel", command=on_cancel).pack(side=tk.LEFT, padx=6)

    def _format_key_display(self, key_value):
        if not key_value:
            return ""
        if key_value in ["shift_l", "shift_r"]:
            return "Shift"
        elif key_value in ["ctrl_l", "ctrl_r"]:
            return "Ctrl"
        elif key_value in ["alt_l", "alt_r"]:
            return "Alt"
        elif key_value == "space":
            return "Space"
        elif key_value == "tab":
            return "Tab"
        elif key_value == "return":
            return "Enter"
        elif key_value == "escape":
            return "Esc"
        else:
            return key_value.upper()

    def _display_to_key_value(self, display_text):
        if not display_text:
            return ""
        display_text = display_text.lower()
        if display_text == "shift":
            return "shift_l"
        elif display_text == "ctrl":
            return "ctrl_l"
        elif display_text == "alt":
            return "alt_l"
        elif display_text == "space":
            return "space"
        elif display_text == "tab":
            return "tab"
        elif display_text == "enter":
            return "return"
        elif display_text == "esc":
            return "escape"
        elif display_text.isalpha() and len(display_text) == 1:
            return display_text
        else:
            return ""

    def _start_key_recording(self, key_name):
        self._recording_key = key_name
        if key_name in self._key_buttons:
            self._key_buttons[key_name].configure(text="Press key...", state="disabled")
        for k, btn in self._key_buttons.items():
            if k != key_name:
                btn.configure(state="disabled")

    def _finish_key_recording(self, key):
        if not self._recording_key:
            return
        
        current_values = {}
        for k, var in self._key_vars.items():
            if k != self._recording_key:
                display_txt = var.get()
                key_val = self._display_to_key_value(display_txt)
                if key_val:
                    current_values[k] = key_val
        if key in current_values.values():
            self._cancel_key_recording()
            return
        
        if self._recording_key in self._key_vars:
            display_value = self._format_key_display(key)
            self._key_vars[self._recording_key].set(display_value)
        
        self._cancel_key_recording()

    def _cancel_key_recording(self):
        self._recording_key = None
        for k, btn in self._key_buttons.items():
            btn.configure(state="normal")
            if k in self._key_vars:
                btn.configure(text=self._key_vars[k].get())


def main():
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()