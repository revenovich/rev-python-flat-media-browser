import tkinter as tk
from tkinter import ttk, messagebox
from pathlib import Path
from typing import Callable, Dict, Tuple, Any, List
from concurrent.futures import ThreadPoolExecutor
import subprocess, sys, os, io, traceback

from PIL import Image, ImageTk, ImageFile
ImageFile.LOAD_TRUNCATED_IMAGES = True

class VirtualThumbs(ttk.Frame):
    """Virtualized thumbnail grid with selection + delete support."""
    _executor = ThreadPoolExecutor(max_workers=4)
    _PAD = 10
    _LABEL_H = 20
    _BUF_ROWS = 3

    def __init__(self, master, thumb_loader: Callable[[Path, Tuple[int,int]], bytes],
                 thumb_size: Tuple[int,int]=(140,140), columns: int = 4):
        super().__init__(master)
        self.loader = thumb_loader
        self.tw, self.th = int(thumb_size[0]), int(thumb_size[1])
        self.cols = max(1, int(columns))
        self.paths: List[Path] = []
        self.cache: Dict[Tuple[str,int,int], bytes] = {}
        self.tk_cache: Dict[str, Any] = {}
        self.cells: Dict[int, tk.Widget] = {}
        self.selected: set[Path] = set()
        self._meta: Dict[int, dict] = {}

        self._holder_bg = "#ffffff"
        self._sel_bg = "#cfe8ff"

        # Scrollable area
        self.canvas = tk.Canvas(self, highlightthickness=0, bg="#f5f5f5")
        self.vsb = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=self.vsb.set)
        self.inner = ttk.Frame(self.canvas)
        self.inner_id = self.canvas.create_window((0,0), window=self.inner, anchor="nw")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.vsb.pack(side="right", fill="y")

        self.style = ttk.Style(self)
        self.style.configure("CenterCaption.TLabel", anchor="center", justify="center", padding=(0,2))

        # Events
        self.canvas.bind("<Configure>", self._on_canvas_resize)
        self.inner.bind("<Configure>", lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<MouseWheel>", self._on_mousewheel)
        self.inner.bind("<MouseWheel>", self._on_mousewheel)
        self.canvas.bind("<Button-4>", self._on_mousewheel)
        self.canvas.bind("<Button-5>", self._on_mousewheel)
        self.inner.bind("<Button-4>", self._on_mousewheel)
        self.inner.bind("<Button-5>", self._on_mousewheel)

    # Public API
    def set_columns(self, n: int):
        n = max(1, int(n))
        if n != self.cols:
            self.cols = n
            self._reflow(full=True)

    def set_thumb_size(self, size: Tuple[int,int]):
        w, h = int(size[0]), int(size[1])
        if (w, h) != (self.tw, self.th):
            self.tw, self.th = w, h
            self._reflow(full=True)

    def populate(self, paths):
        self.paths = [p if isinstance(p, Path) else Path(p) for p in paths]
        self.selected.clear()
        self._reflow(full=True)

    def get_selected_paths(self) -> list:
        return list(self.selected)

    def remove_paths(self, paths: list):
        s = {str(p) for p in paths}
        self.paths = [p for p in self.paths if str(p) not in s]
        self.selected = {p for p in self.selected if str(p) not in s}
        self._reflow(full=True)

    # Internals
    def _cell_wh(self):
        return self.tw + self._PAD, self.th + self._PAD + self._LABEL_H

    def _on_canvas_resize(self, e):
        self.canvas.itemconfigure(self.inner_id, width=e.width)
        self._update_scroll_region_and_bar()
        self._ensure_visible()

    def _on_mousewheel(self, event):
        if not self.canvas.winfo_exists():
            return
        if getattr(self, "_virtual_h", 0) <= self.canvas.winfo_height():
            return
        if getattr(event, "num", None) == 5 or getattr(event, "delta", 0) < 0:
            self.canvas.yview_scroll(3, "units")
        elif getattr(event, "num", None) == 4 or getattr(event, "delta", 0) > 0:
            self.canvas.yview_scroll(-3, "units")
        self._ensure_visible()

    def _reflow(self, full=False):
        if full:
            for w in list(self.inner.winfo_children()):
                w.destroy()
            self.cells.clear()
            self.tk_cache.clear()
            self._meta.clear()
        self.inner.configure(height=max(1, self._content_height()))
        self._update_scroll_region_and_bar()
        self._ensure_visible()

    def _vis_range(self):
        y0 = self.canvas.canvasy(0)
        y1 = y0 + self.canvas.winfo_height()
        _, ch = self._cell_wh()
        r0 = max(0, int(y0 // ch) - self._BUF_ROWS)
        r1 = int(y1 // ch) + self._BUF_ROWS + 1
        i0 = r0 * self.cols
        i1 = min(len(self.paths), (r1 + 1) * self.cols)
        return i0, i1

    def _ensure_visible(self):
        i0, i1 = self._vis_range()
        for idx in list(self.cells.keys()):
            if idx < i0 or idx >= i1:
                w = self.cells.pop(idx); w.destroy()
        for idx in range(i0, i1):
            if idx >= len(self.paths): break
            if idx in self.cells: continue
            self._create_cell(idx)

    def _create_cell(self, idx: int):
        sep_color = "#d6d6d6"
        p = self.paths[idx]
        r = idx // self.cols
        c = idx % self.cols
        cw, ch = self._cell_wh()
        cell = tk.Frame(self.inner, bg=self._holder_bg, highlightthickness=0)
        cell.place(x=c*cw, y=r*ch, width=cw, height=ch)

        holder = tk.Frame(cell, width=self.tw, height=self.th, bg=self._holder_bg)
        holder.pack(fill="both", expand=True); holder.pack_propagate(False)
        lbl = ttk.Label(holder, text="Loading...", anchor="center")
        lbl.pack(fill="both", expand=True)

        cap = ttk.Label(cell, text=p.name, style="CenterCaption.TLabel")
        cap.pack(side="bottom", fill="x")
        cap.configure(wraplength=max(1, cw - 8))

        sep_bottom = tk.Frame(cell, height=1, bg=sep_color)
        sep_bottom.pack(side="bottom", fill="x")
        sep_right = tk.Frame(cell, width=1, bg=sep_color)
        sep_right.place(relx=1.0, rely=0.0, relheight=1.0, anchor="ne")

        self._meta[idx] = {"path": p, "holder": holder, "cap": cap}
        self.cells[idx] = cell
        self._load_async(p, lbl, holder)

        for w in (cell, holder):
            w.bind("<MouseWheel>", self._on_mousewheel)
            w.bind("<Button-4>", self._on_mousewheel)
            w.bind("<Button-5>", self._on_mousewheel)

    def _load_async(self, path: Path, lbl: ttk.Label, container: tk.Frame):
        key = (str(path), self.tw, self.th)
        if key in self.cache:
            self._place(path, self.cache[key], lbl, container); return
        def work():
            data = None
            try:
                data = self.loader(path, (self.tw, self.th))
            except Exception:
                data = None
            def done():
                if not container.winfo_exists(): return
                if not data:
                    lbl.config(text="(failed)"); return
                self.cache[key] = data
                self._place(path, data, lbl, container)
            container.after(0, done)
        VirtualThumbs._executor.submit(work)

    def _place(self, path: Path, data: bytes, lbl: ttk.Label, container: tk.Frame):
        try:
            im = Image.open(io.BytesIO(data)); im.load()
            if im.mode not in ("RGB", "RGBA"):
                im = im.convert("RGBA")
            tki = ImageTk.PhotoImage(im, master=container)
            self.tk_cache[str(path)] = tki

            for w in container.winfo_children():
                try: w.destroy()
                except: pass

            wrap = tk.Frame(container, bg=self._holder_bg)
            wrap.place(relx=0.5, rely=0.5, anchor="center")
            imgw = tk.Label(wrap, image=tki, bd=0, bg=self._holder_bg)
            imgw.image = tki
            imgw.pack()

            imgw.bind("<Button-1>", lambda e, p=path, cont=container: self._toggle_select(p, cont))
            imgw.bind("<Double-Button-1>", lambda e, p=path: self._reveal(p))
        except Exception:
            lbl.config(text="(decode error)")

    def _toggle_select(self, path: Path, container: tk.Frame):
        idx = None
        for k, m in self._meta.items():
            if m["path"] == path: idx = k; break
        cap = self._meta[idx]["cap"] if idx is not None else None

        if path in self.selected:
            self.selected.remove(path)
            container.configure(bg=self._holder_bg)
            if cap: cap.configure(background="")
        else:
            self.selected.add(path)
            container.configure(bg=self._sel_bg)
            if cap: cap.configure(background=self._sel_bg)

    def _reveal(self, path: Path):
        if os.name == "nt":
            subprocess.Popen(["explorer", "/select,", str(path)])
        elif sys.platform.startswith("darwin"):
            subprocess.Popen(["open", "-R", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path.parent)])

    def _content_height(self) -> int:
        total = len(self.paths)
        if total == 0: return 0
        rows = (total + self.cols - 1) // self.cols
        _, ch = self._cell_wh()
        return rows * ch

    def _update_scroll_region_and_bar(self):
        H = self._content_height()
        self._virtual_h = H
        self.canvas.configure(scrollregion=(0,0,self.canvas.winfo_width(),max(1,H)))
        if H <= self.canvas.winfo_height():
            try: self.vsb.state(["disabled"])
            except: pass
        else:
            try: self.vsb.state(["!disabled"])
            except: pass

class ThumbnailGalleryPopup(tk.Toplevel):
    def __init__(self, master, title: str,
                 thumb_loader: Callable[[Path, Tuple[int,int]], bytes],
                 thumb_size=(140,140), columns: int = 4):
        super().__init__(master)
        self.title(title)
        self.geometry("1000x720")
        self.minsize(800, 520)
        bar = ttk.Frame(self, padding=6); bar.pack(fill="x")

        ttk.Label(bar, text="Columns:").pack(side="left")
        self.var_cols = tk.IntVar(value=int(columns))
        def apply_cols(): self.grid.set_columns(int(self.var_cols.get() or 4))
        ttk.Spinbox(bar, from_=1, to=12, width=4, textvariable=self.var_cols, command=apply_cols).pack(side="left")

        ttk.Label(bar, text="Thumb:").pack(side="left", padx=(12,2))
        self.var_size = tk.IntVar(value=int(thumb_size[0]))
        def apply_size():
            s = int(self.var_size.get() or 140)
            self.grid.set_thumb_size((s,s))
        ttk.Spinbox(bar, from_=80, to=640, increment=20, width=4, textvariable=self.var_size, command=apply_size).pack(side="left")

        ttk.Separator(bar, orient="vertical").pack(side="left", fill="y", padx=8)
        ttk.Button(bar, text="Delete Selected", command=self._on_delete_selected).pack(side="left")

        self.grid = VirtualThumbs(self, thumb_loader=thumb_loader, thumb_size=thumb_size, columns=columns)
        self.grid.pack(fill="both", expand=True, padx=6, pady=6)

    def populate(self, paths):
        self.grid.populate(paths)

    def _on_delete_selected(self):
        sel = self.grid.get_selected_paths()
        if not sel:
            messagebox.showinfo("Delete Selected", "No items selected.")
            self._refocus()
            return

        if not messagebox.askyesno("Confirm Delete",
                                f"Delete {len(sel)} file(s)? This cannot be undone."):
            self._refocus()
            return

        deleted = []
        try:
            try:
                from send2trash import send2trash
                use_trash = True
            except Exception:
                use_trash = False

            for p in sel:
                try:
                    if use_trash: send2trash(str(p))
                    else: os.remove(p)
                    deleted.append(p)
                except Exception:
                    import traceback; traceback.print_exc()
        finally:
            if deleted:
                self.grid.remove_paths(deleted)
                messagebox.showinfo("Delete Selected", f"Deleted {len(deleted)} file(s).")
            # ensure the popup regains focus after any dialog
            self._refocus()

    def _refocus(self):
        try:
            self.lift()
            self.focus_force()
            # bump to the front, then release so it doesn't stay always-on-top
            self.attributes("-topmost", True)
            self.after(150, lambda: self.attributes("-topmost", False))
        except Exception:
            pass
