import os
import csv
import shutil
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from pathlib import Path
import threading, queue, time, subprocess, sys
from typing import Dict, Tuple, Set, Optional, List

from ..core.constants import IMG_EXTS, MANIFEST_NAME, MANIFEST_SIG
from ..core.utils import cpu_threads, fmt_secs
from ..core.manifest import read_manifest_stream, manifest_path
from ..core.preview import make_thumbnail_png_bytes
from ..core.scan import discover_images, process_images
from ..core.phash import HAS_DCT  # to adjust UI options
from .widgets import open_gallery_popup

try:
    from PIL import Image, ImageTk, ImageFile
    ImageFile.LOAD_TRUNCATED_IMAGES = True
except Exception:
    Image = None
    ImageTk = None

class MainWindow(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Image Browser (Modular)")
        self.geometry("1120x760")
        self.minsize(900,560)

        # State
        self.source_dir: Optional[Path] = None
        self.idx_to_path: Dict[int,str] = {}
        self.loaded_paths_set: Set[str] = set()
        self.items_count = 0
        self.total_images = None
        self.start_time: Optional[float] = None
        self.manifest_loading = False
        self.cancel_event = threading.Event()
        self.q = queue.Queue()

        # Preview
        self.current_preview_path: Optional[Path] = None
        self.preview_request_id = 0
        self.preview_cache: Dict[Tuple[str,int,int], bytes] = {}
        self.preview_image_tk = None

        self._build_ui()
        self.after(100, self._poll_queue)

        if Image is None or ImageTk is None:
            messagebox.showwarning("Missing dependency","This app needs Pillow:\n\npip install pillow")

    def _build_ui(self):
        top = ttk.Frame(self, padding=8)
        top.pack(side=tk.TOP, fill=tk.X)
        self.btn_pick = ttk.Button(top, text="Pick Folder", command=self.on_pick_folder)
        self.btn_pick.pack(side=tk.LEFT)
        self.lbl_source = ttk.Label(top, text="No folder selected")
        self.lbl_source.pack(side=tk.LEFT, padx=10)
        self.btn_scan = ttk.Button(top, text="Scan (new only)", command=self.on_scan, state=tk.DISABLED)
        self.btn_scan.pack(side=tk.RIGHT)

        opts = ttk.Frame(self, padding=(8,0,8,0))
        opts.pack(fill=tk.X)
        self.var_manifest = tk.BooleanVar(value=True)
        self.var_hash = tk.BooleanVar(value=False)
        self.var_threads = tk.IntVar(value=max(2, min(32, cpu_threads())))
        ttk.Checkbutton(opts, text="Write manifest (hidden file)", variable=self.var_manifest).pack(side=tk.LEFT)
        ttk.Checkbutton(opts, text="Compute SHA1 (slower)", variable=self.var_hash).pack(side=tk.LEFT, padx=(12,0))
        ttk.Label(opts, text="Threads:").pack(side=tk.LEFT, padx=(16,4))
        self.spin_threads = ttk.Spinbox(opts, from_=1, to=64, width=4, textvariable=self.var_threads)
        self.spin_threads.pack(side=tk.LEFT)

        # Tabs: Browser & Manage
        tabs = ttk.Notebook(self)
        self.tabs = tabs
        tabs.pack(fill=tk.BOTH, expand=True, pady=6, padx=6)
        browser_tab = ttk.Frame(tabs)
        manage_tab = ttk.Frame(tabs)
        tabs.add(browser_tab, text='Image Browser')
        tabs.add(manage_tab, text='Manage')
        # ---- Browser layout ----
        main = ttk.Panedwindow(browser_tab, orient=tk.HORIZONTAL)
        main.pack(fill=tk.BOTH, expand=True)

        left = ttk.Frame(main)
        main.add(left, weight=3)

        self.preview_canvas = tk.Canvas(left, background="#111111", highlightthickness=0)
        self.preview_canvas.pack(fill=tk.BOTH, expand=True)
        self.preview_canvas.bind("<Configure>", self._on_canvas_resize)
        self.preview_canvas.bind("<Double-Button-1>", self.on_open_from_preview)

        status = ttk.Frame(left)
        status.pack(fill=tk.X, pady=(6,0))
        self.progress = ttk.Progressbar(status, orient=tk.HORIZONTAL, mode="indeterminate")
        self.progress.pack(fill=tk.X, expand=True)
        self.lbl_status = ttk.Label(status, text="Idle")
        self.lbl_status.pack(side=tk.LEFT, pady=3)

        right = ttk.Frame(main)
        main.add(right, weight=2)
        ttk.Label(right, text="Images (Enter/Double-click to open location):").grid(row=0, column=0, columnspan=2, sticky="w")
        self.listbox = tk.Listbox(right, activestyle="dotbox")
        self.listbox.grid(row=1, column=0, sticky="nsew", padx=(0,4), pady=(2,6))
        self.scrollbar = ttk.Scrollbar(right, orient=tk.VERTICAL, command=self.listbox.yview)
        self.scrollbar.grid(row=1, column=1, sticky="ns", pady=(2,6))
        self.listbox.config(yscrollcommand=self.scrollbar.set)
        self.listbox.bind("<<ListboxSelect>>", self.on_list_selection_changed)
        self.listbox.bind("<Double-Button-1>", self.on_open_from_list)
        self.listbox.bind("<Return>", self.on_open_from_list)
        right.rowconfigure(1, weight=1)
        right.columnconfigure(0, weight=1)

        # Actions under browser
        actions = ttk.Frame(browser_tab, padding=(8,6,8,0))
        actions.pack(fill=tk.X)
        ttk.Button(actions, text="Rebuild Manifest (full rescan)", command=self.on_rebuild).pack(side=tk.LEFT)
        ttk.Button(actions, text="Clean Missing from Manifest", command=self.on_clean).pack(side=tk.LEFT, padx=8)
        self.btn_cancel = ttk.Button(actions, text="Cancel Current Task", command=self.on_cancel, state=tk.DISABLED)
        self.btn_cancel.pack(side=tk.LEFT, padx=8)

        # ---- Manage tab ----
        self._build_manage_tab(manage_tab)

    # ---------- Manage tab (duplicates + batch ops) ----------
    def _build_manage_tab(self, container):
        top = ttk.Frame(container, padding=(8,6,8,0))
        top.pack(fill=tk.X)
        self.var_dupe_phash = tk.BooleanVar(value=False)
        self.var_dupe_method = tk.StringVar(value="ahash")
        self.var_dupe_threshold = tk.IntVar(value=5)
        ttk.Button(top, text="Find Exact Duplicates (SHA1)", command=self.on_find_exact_dupes).pack(side=tk.LEFT)
        ttk.Checkbutton(top, text="Use Perceptual", variable=self.var_dupe_phash).pack(side=tk.LEFT, padx=(10,0))
        ttk.Label(top, text="Method:").pack(side=tk.LEFT, padx=(10,2))
        methods = ["ahash","dhash"] + (["phash"] if HAS_DCT else [])
        self.dupe_method_combo = ttk.Combobox(top, textvariable=self.var_dupe_method, values=methods, width=6, state="readonly")
        self.dupe_method_combo.pack(side=tk.LEFT)
        ttk.Label(top, text="Threshold:").pack(side=tk.LEFT, padx=(10,2))
        ttk.Spinbox(top, from_=0, to=64, textvariable=self.var_dupe_threshold, width=4).pack(side=tk.LEFT)
        ttk.Button(top, text="Find Near Duplicates", command=self.on_find_near_dupes).pack(side=tk.LEFT, padx=(10,0))
        self.btn_cancel_manage = ttk.Button(top, text="Cancel", command=self.on_cancel, state=tk.DISABLED)
        self.btn_cancel_manage.pack(side=tk.LEFT, padx=8)
        ttk.Button(top, text="Preview Selected Group", command=self.on_preview_group).pack(side=tk.LEFT, padx=(10,0))

        # Manage progress UI
        self.manage_pbar = ttk.Progressbar(container, orient=tk.HORIZONTAL, mode="determinate")
        self.manage_pbar.pack(fill=tk.X, padx=8, pady=(4,0))
        self.manage_status = ttk.Label(container, text="Idle")
        self.manage_status.pack(fill=tk.X, padx=8, pady=(0,6))

        # Result tree
        mid = ttk.Frame(container, padding=6)
        mid.pack(fill=tk.BOTH, expand=True)
        columns = ("name","path","info")
        self.tree = ttk.Treeview(mid, columns=columns, show="tree headings", selectmode="extended")
        self.tree.heading("#0", text="Group")
        self.tree.heading("name", text="Name")
        self.tree.heading("path", text="Path")
        self.tree.heading("info", text="Info")
        self.tree.column("#0", width=160, stretch=False)
        self.tree.column("name", width=220)
        self.tree.column("path", width=500)
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        sb = ttk.Scrollbar(mid, orient="vertical", command=self.tree.yview)
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        self.tree.configure(yscrollcommand=sb.set)

        bottom = ttk.Frame(container, padding=(8,0,8,8))
        bottom.pack(fill=tk.X)
        ttk.Button(bottom, text="Open Selected", command=self.on_manage_open).pack(side=tk.LEFT)
        ttk.Button(bottom, text="Export CSV", command=self.on_manage_export).pack(side=tk.LEFT, padx=8)
        ttk.Button(bottom, text="Copy to...", command=lambda: self.on_manage_copymove(move=False)).pack(side=tk.LEFT, padx=8)
        ttk.Button(bottom, text="Move to...", command=lambda: self.on_manage_copymove(move=True)).pack(side=tk.LEFT)

    def _manage_clear(self):
        for i in self.tree.get_children():
            self.tree.delete(i)

    def on_find_exact_dupes(self):
        if self.source_dir is None:
            return
        self._manage_clear()
        self.manage_status.config(text="Finding exact duplicates...")
        self.cancel_event.clear()
        self.btn_cancel_manage.config(state=tk.NORMAL)
        paths = [Path(p) for p in self.loaded_paths_set]

        self.manage_pbar.config(mode='determinate', maximum=max(1,len(paths)), value=0)
        def run():
            from ..core.duplicates import compute_sha1_map
            groups = compute_sha1_map(paths, threads=self.var_threads.get(), cancel_event=self.cancel_event, q=self.q)
            self.q.put({"type":"dupe_exact","groups":groups})
        threading.Thread(target=run, daemon=True).start()

    def on_find_near_dupes(self):
        if self.source_dir is None:
            return
        self._manage_clear()
        method = self.var_dupe_method.get()
        if method == "phash" and not HAS_DCT:
            messagebox.showwarning("pHash unavailable", "Install scipy to enable pHash, or use aHash/dHash.")
            return
        self.manage_status.config(text=f"Finding near duplicates ({method})...")
        self.cancel_event.clear()
        self.btn_cancel_manage.config(state=tk.NORMAL)
        paths = [Path(p) for p in self.loaded_paths_set]
        thr = int(self.var_dupe_threshold.get())

        self.manage_pbar.config(mode='determinate', maximum=max(1,len(paths)), value=0)
        def run():
            from ..core.duplicates import compute_perceptual_groups
            groups = compute_perceptual_groups(paths, method=method, threshold=thr, threads=self.var_threads.get(), cancel_event=self.cancel_event, q=self.q)
            self.q.put({"type":"dupe_near","groups":groups, "method":method, "thr":thr})
        threading.Thread(target=run, daemon=True).start()

    def _populate_dupe_tree_exact(self, groups):
        self._manage_clear()
        gi = 1
        total_files = 0
        for h, items in groups.items():
            parent = self.tree.insert("", "end", text=f"Group {gi} ({len(items)})", values=("", "", f"SHA1={h[:10]}..."))
            for p in items:
                name = Path(p).name
                self.tree.insert(parent, "end", text="", values=(name, p, ""))
                total_files += 1
            gi += 1
        self.btn_cancel_manage.config(state=tk.DISABLED)
        self.manage_status.config(text=f"Found {total_files} files in {gi-1} groups")
        self.lbl_status.config(text=f"Exact duplicates grouped: {gi-1} groups.")

    def _populate_dupe_tree_near(self, groups, method, thr):
        self._manage_clear()
        gi = 1
        total_files = 0
        for items in groups:
            parent = self.tree.insert("", "end", text=f"Group {gi} ({len(items)})", values=("", "", f"{method} ≤ {thr}"))
            for p in items:
                name = Path(p).name
                self.tree.insert(parent, "end", text="", values=(name, p, ""))
                total_files += 1
            gi += 1
        self.btn_cancel_manage.config(state=tk.DISABLED)
        self.manage_status.config(text=f"Found {total_files} files in {gi-1} groups")
        self.lbl_status.config(text=f"Near-duplicate groups: {gi-1}.")

    def _tree_selected_paths(self):
        paths = []
        for item in self.tree.selection():
            vals = self.tree.item(item, "values")
            if len(vals) >= 2 and vals[1]:
                paths.append(vals[1])
            else:
                # if group node selected, include all children
                for child in self.tree.get_children(item):
                    vals = self.tree.item(child, "values")
                    if len(vals) >= 2 and vals[1]:
                        paths.append(vals[1])
        return paths

    def on_manage_open(self):
        paths = self._tree_selected_paths()
        for p in paths:
            pp = Path(p)
            if pp.exists():
                if os.name == "nt":
                    subprocess.Popen(['explorer', '/select,', str(pp)])
                elif sys.platform.startswith("darwin"):
                    subprocess.Popen(["open", pp.parent])
                else:
                    subprocess.Popen(["xdg-open", pp.parent])

    def on_manage_export(self):
        paths = self._tree_selected_paths()
        if not paths:
            self.manage_status.config(text="Nothing selected to export.")
            return
        out = Path(self.source_dir) / "export_selection.csv"
        with out.open("w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["path"])
            for p in paths:
                w.writerow([p])
        self.manage_status.config(text=f"Exported {len(paths)} paths → {out.name}")

    def on_manage_copymove(self, move: bool):
        target = filedialog.askdirectory(title=("Move to folder" if move else "Copy to folder"))
        if not target:
            return
        target = Path(target)
        paths = self._tree_selected_paths()
        if not paths:
            self.manage_status.config(text="Nothing selected.")
            return

        self.manage_status.config(text=("Moving files..." if move else "Copying files..."))

        def worker():
            copied = 0
            moved = 0
            for p in paths:
                if self.cancel_event.is_set():
                    break
                src = Path(p)
                if not src.exists():
                    continue
                dst = target / src.name
                try:
                    if move:
                        shutil.move(str(src), str(dst))
                        moved += 1
                    else:
                        shutil.copy2(str(src), str(dst))
                        copied += 1
                except Exception:
                    pass

            def done():
                if move:
                    self.manage_status.config(text=f"Moved {moved} files → {target}")
                else:
                    self.manage_status.config(text=f"Copied {copied} files → {target}")

            self.after(0, done)

        threading.Thread(target=worker, daemon=True).start()

    # --- Events ---
    def on_pick_folder(self):
        sel = filedialog.askdirectory(title="Select source folder")
        if not sel:
            return
        self.source_dir = Path(sel)
        self.lbl_source.config(text=str(self.source_dir))
        self.btn_scan.config(state=tk.DISABLED)
        self.btn_cancel.config(state=tk.NORMAL)
        self.listbox.delete(0, tk.END)
        self.idx_to_path.clear()
        self.loaded_paths_set.clear()
        self.items_count = 0
        self.total_images = None
        self.start_time = None
        self.progress.start(10)
        self.lbl_status.config(text="Loading manifest...")
        self._load_manifest_async()

    def on_scan(self):
        if self.source_dir is None:
            return
        if self.manifest_loading:
            messagebox.showinfo("Please wait", "Manifest is still loading.")
            return
        self.cancel_event.clear()
        self.btn_scan.config(state=tk.DISABLED)
        self.btn_cancel.config(state=tk.NORMAL)
        self.items_count = len(self.loaded_paths_set)
        self.total_images = None
        self.start_time = time.time()
        self.progress.start(10)
        self.lbl_status.config(text="Scanning...")
        threading.Thread(target=self._scan_worker, daemon=True).start()

    def on_rebuild(self):
        if self.source_dir is None:
            return
        if not messagebox.askyesno("Rebuild Manifest","Full rescan & overwrite manifest. Continue?"):
            return
        self.cancel_event.clear()
        self.btn_cancel.config(state=tk.NORMAL)
        self.progress.start(10)
        self.lbl_status.config(text="Rebuilding manifest...")
        threading.Thread(target=self._rebuild_worker, daemon=True).start()

    def on_clean(self):
        if self.source_dir is None:
            return
        self.cancel_event.clear()
        self.btn_cancel.config(state=tk.NORMAL)
        self.progress.start(10)
        self.lbl_status.config(text="Cleaning manifest...")
        threading.Thread(target=self._clean_worker, daemon=True).start()

    def on_cancel(self):
        self.cancel_event.set()
        self.lbl_status.config(text="Cancelling...")
        self.manage_status.config(text="Cancelling...")

    # --- Workers ---
    def _load_manifest_async(self):
        if self.manifest_loading:
            return
        self.manifest_loading = True
        def run():
            for typ, payload in read_manifest_stream(self.source_dir):
                if self.cancel_event.is_set():
                    break
                if typ == "paths":
                    self.q.put({"type":"pre_items","paths":payload})
                elif typ == "status":
                    self.q.put({"type":"status","msg":payload})
            self.q.put({"type":"manifest_done"})
        threading.Thread(target=run, daemon=True).start()

    def _scan_worker(self):
        root = self.source_dir
        # start with already indexed items
        self.total_images = len(self.loaded_paths_set)
        self.q.put({"type": "total", "n": self.total_images})

        def path_iter():
            for p in discover_images(root, self.loaded_paths_set, cancel_event=self.cancel_event):
                if self.cancel_event.is_set():
                    break
                self.total_images += 1
                # update total as we discover more files
                self.q.put({"type": "total", "n": self.total_images})
                yield p

        process_images(
            root,
            path_iter(),
            self.var_manifest.get(),
            self.var_hash.get(),
            self.var_threads.get(),
            self.q,
            self.loaded_paths_set,
            cancel_event=self.cancel_event,
        )

        self.q.put({"type": "cancelled" if self.cancel_event.is_set() else "done"})

    def _rebuild_worker(self):
        # full rescan and rewrite manifest; then reload manifest into UI
        from ..core.manifest import open_manifest_for_write
        from ..core.utils import sha1_of_file, is_image
        from ..core.constants import IMG_EXTS

        root = self.source_dir
        f, writer = open_manifest_for_write(root, self.var_hash.get(), replace=True)
        count = 0
        try:
            for p in root.rglob("*"):
                if self.cancel_event.is_set():
                    break
                if p.is_file() and is_image(p, IMG_EXTS):
                    row = [str(p.resolve())]
                    if self.var_hash.get():
                        try:
                            row.append(sha1_of_file(p, cancel_event=self.cancel_event))
                        except Exception:
                            row.append("")
                    writer.writerow(row)
                    count += 1
                    if count % 200 == 0:
                        f.flush()
            f.flush()
        finally:
            f.close()
        if self.cancel_event.is_set():
            self.q.put({"type":"status","msg":"Rebuild cancelled."})
            self.q.put({"type":"manifest_done"})
            return

        self.q.put({"type":"status","msg":f"Manifest rebuilt: {count} entries."})
        # reload manifest
        self.listbox.delete(0, tk.END)
        self.idx_to_path.clear()
        self.loaded_paths_set.clear()
        self.items_count = 0
        self.total_images = None
        self.progress.start(10)
        self.lbl_status.config(text="Loading manifest...")
        self._load_manifest_async()

    def _clean_worker(self):
        from ..core.manifest import MANIFEST_SIG  # correct import path
        mpath = manifest_path(self.source_dir)
        if not mpath.exists():
            self.q.put({"type":"status","msg":"No manifest to clean."})
            self.q.put({"type":"manifest_done"})
            return
        rows = []
        valid = False
        try:
            with mpath.open("r", encoding="utf-8") as f:
                first = f.readline().rstrip("\n\r")
                if first.strip() == MANIFEST_SIG:
                    valid = True
                    reader = csv.DictReader(f)
                    for row in reader:
                        if self.cancel_event.is_set():
                            break
                        p = (row.get("path") or "").strip()
                        if p and Path(p).exists():
                            rows.append(row)
        except Exception as e:
            self.q.put({"type":"status","msg":f"Failed to read manifest: {e}"})
            self.q.put({"type":"manifest_done"})
            return
        if not valid:
            self.q.put({"type":"status","msg":"Manifest ignored (foreign file)."})
            self.q.put({"type":"manifest_done"})
            return
        try:
            with mpath.open("w", newline="", encoding="utf-8") as f:
                f.write(MANIFEST_SIG + "\n")
                fields = ["path","sha1"] if (rows and "sha1" in rows[0]) else ["path"]
                w = csv.DictWriter(f, fieldnames=fields)
                w.writeheader()
                for r in rows:
                    w.writerow(r)
        except Exception as e:
            self.q.put({"type":"status","msg":f"Failed to write manifest: {e}"})
            self.q.put({"type":"manifest_done"})
            return
        self.q.put({"type":"status","msg":f"Manifest cleaned: {len(rows)} kept."})
        # reload
        self.listbox.delete(0, tk.END)
        self.idx_to_path.clear()
        self.loaded_paths_set.clear()
        self.items_count = 0
        self.total_images = None
        self.progress.start(10)
        self.lbl_status.config(text="Loading manifest...")
        self._load_manifest_async()

    # --- Queue/UI ---
    def _poll_queue(self):
        N = 300
        processed = 0
        updated = False
        while processed < N:
            try:
                msg = self.q.get_nowait()
            except queue.Empty:
                break
            processed += 1
            typ = msg.get("type")
            if typ == "total":
                self.progress.config(mode="determinate", maximum=msg["n"], value=self.items_count)
                self._update_status()
            elif typ == "item":
                self._on_new_item(msg["path"])
                updated = True
            elif typ == "pre_items":
                for p in msg["paths"]:
                    if p in self.loaded_paths_set:
                        continue
                    pp = Path(p)
                    idx = self.listbox.size()
                    self.listbox.insert(tk.END, pp.name)
                    self.idx_to_path[idx] = p
                    self.loaded_paths_set.add(p)
                    self.items_count += 1
                updated = True
            elif typ == "status":
                self.lbl_status.config(text=msg.get("msg",""))
            elif typ == "manifest_done":
                self.manifest_loading = False
                self.progress.stop()
                self.progress.config(mode="determinate", maximum=self.items_count or 1, value=self.items_count)
                self.btn_scan.config(state=tk.NORMAL)
                self.btn_cancel.config(state=tk.DISABLED)
                self._update_status()
            elif typ == "manage_progress":
                total = max(1, msg.get('total', 1))
                done = min(total, msg.get('done', 0))
                self.manage_pbar.config(maximum=total, value=done)
                phase = msg.get('phase','')
                self.manage_status.config(text=f"{phase}: {done}/{total}")
            elif typ == "manage_done":
                self.manage_pbar.config(value=self.manage_pbar['maximum'])
                self.btn_cancel_manage.config(state=tk.DISABLED)
            elif typ == "dupe_exact":
                self._populate_dupe_tree_exact(msg["groups"])
            elif typ == "dupe_near":
                self._populate_dupe_tree_near(msg["groups"], msg["method"], msg["thr"])
            elif typ == "done":
                if self.total_images is not None:
                    self.progress.config(value=self.total_images)
                self.btn_scan.config(state=tk.NORMAL)
                self.btn_cancel.config(state=tk.DISABLED)
                self._update_status(done=True)
            elif typ == "cancelled":
                self.progress.stop()
                self.btn_scan.config(state=tk.NORMAL)
                self.btn_cancel.config(state=tk.DISABLED)
                self.manage_status.config(text="Cancelled.")
                self.lbl_status.config(text="Cancelled.")
                self.cancel_event.clear()
        self.after(100, self._poll_queue)
        if updated:
            self._update_status()

    def _on_new_item(self, path: str):
        if path in self.loaded_paths_set:
            return
        pp = Path(path)
        idx = self.listbox.size()
        self.listbox.insert(tk.END, pp.name)
        self.idx_to_path[idx] = path
        self.loaded_paths_set.add(path)
        self.items_count += 1
        if self.total_images:
            self.progress.config(value=min(self.items_count, self.total_images))

    def _update_status(self, done: bool=False):
        total_known = self.total_images if self.total_images is not None else self.items_count
        if total_known in (None, 0):
            self.lbl_status.config(text=f"Indexed {self.items_count} (estimating total)...")
            return
        elapsed = max(0.001, time.time()-self.start_time) if self.start_time else 0.001
        rate = self.items_count/elapsed if elapsed>0 else 0
        remaining = max(0, total_known-self.items_count)
        eta = remaining/rate if rate>0 else 0
        eta_str = fmt_secs(eta)
        if done:
            self.progress.stop()
            self.lbl_status.config(text=f"Done. {self.items_count}/{total_known} in {fmt_secs(elapsed)}.")
        else:
            self.lbl_status.config(text=f"{self.items_count}/{total_known} • {rate:.1f}/s • ETA ~ {eta_str}")

    # --- Preview ---
    def on_list_selection_changed(self, event=None):
        sel = self.listbox.curselection()
        if not sel:
            return
        idx = sel[0]
        path = Path(self.idx_to_path.get(idx,""))
        if not path.exists():
            self._show_preview_text("(file missing)")
            return
        self.current_preview_path = path
        self.preview_request_id += 1
        cw = max(50,self.preview_canvas.winfo_width())
        ch = max(50,self.preview_canvas.winfo_height())
        key = (str(path),cw,ch)
        if key in self.preview_cache:
            self._draw_bytes(self.preview_cache[key])
            return
        rid = self.preview_request_id
        def work():
            try:
                data = make_thumbnail_png_bytes(path,(cw,ch))
                if rid == self.preview_request_id and self.current_preview_path == path:
                    self.preview_cache[key] = data
                    self.after(0, lambda d=data: self._draw_bytes(d))
            except Exception as e:
                self.after(0, lambda: self._show_preview_text(str(e)))
        threading.Thread(target=work, daemon=True).start()

    def _on_canvas_resize(self, event):
        if self.current_preview_path:
            self.on_list_selection_changed()

    def _draw_bytes(self, data: bytes):
        if Image is None or ImageTk is None:
            self._show_preview_text("Pillow not installed")
            return
        from io import BytesIO
        im = Image.open(BytesIO(data))
        tk_img = ImageTk.PhotoImage(im)
        self.preview_image_tk = tk_img
        self.preview_canvas.delete("all")
        cw = self.preview_canvas.winfo_width()
        ch = self.preview_canvas.winfo_height()
        self.preview_canvas.create_image(cw//2, ch//2, image=tk_img, anchor="center")

    def _show_preview_text(self, msg: str):
        self.preview_canvas.delete("all")
        self.preview_canvas.create_text(self.preview_canvas.winfo_width()//2, self.preview_canvas.winfo_height()//2, text=msg, fill="#DDDDDD")

    # --- Open in Explorer ---
    def on_open_from_preview(self, event=None):
        sel = self.listbox.curselection()
        if not sel:
            return
        idx = sel[0]
        path = Path(self.idx_to_path.get(idx,""))
        if path.exists():
            self._reveal_in_explorer(path)

    def on_open_from_list(self, event=None):
        sel = self.listbox.curselection()
        if not sel:
            return
        idx = sel[0]
        path = Path(self.idx_to_path.get(idx,""))
        if path.exists():
            self._reveal_in_explorer(path)

    def _reveal_in_explorer(self, path: Path):
        try:
            if os.name == "nt":
                subprocess.Popen(['explorer', '/select,', str(path)])
            elif sys.platform.startswith("darwin"):
                subprocess.Popen(["open", path.parent])
            else:
                subprocess.Popen(["xdg-open", path.parent])
        except Exception as e:
            messagebox.showerror("Open failed", f"Could not open location:\n{e}")

    def on_preview_group(self):
        paths = self._tree_selected_paths()
        if not paths:
            focus = self.tree.focus()
            if focus:
                tmp = []
                for ch in self.tree.get_children(focus):
                    vals = self.tree.item(ch, 'values')
                    if len(vals) >= 2 and vals[1]:
                        tmp.append(vals[1])
                paths = tmp
        if not paths:
            self.manage_status.config(text="No items selected for preview.")
            return
        open_gallery_popup(self, f"Preview ({len(paths)} files)", paths,
                        self._load_thumb_bytes, thumb_size=(140, 140), columns=4)

    def _load_thumb_bytes(self, path: Path, size: Tuple[int, int]) -> bytes:
        from ..core.preview import make_thumbnail_png_bytes
        return make_thumbnail_png_bytes(path, size)