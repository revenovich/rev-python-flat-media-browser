from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from typing import Iterable, Set, Optional
from .utils import sha1_of_file, is_image
from .constants import IMG_EXTS
from .manifest import open_manifest_for_write

def discover_images(root: Path, already: Set[str], cancel_event=None):
    for p in root.rglob("*"):
        if cancel_event is not None and cancel_event.is_set():
            break
        if p.is_file() and is_image(p, IMG_EXTS):
            rp = str(p.resolve())
            if rp not in already:
                yield p

def process_images(root: Path, paths, with_manifest: bool, with_sha1: bool,
                   threads: int, q, already: Set[str], cancel_event=None):
    f = None; writer = None
    try:
        if with_manifest:
            f, writer = open_manifest_for_write(root, with_sha1, replace=False)
        def worker(p: Path):
            if cancel_event is not None and cancel_event.is_set():
                return
            sha = ""
            if with_sha1:
                try: sha = sha1_of_file(p, cancel_event=cancel_event)
                except Exception: sha = ""
            if writer and (cancel_event is None or not cancel_event.is_set()):
                row = [str(p.resolve())]
                if with_sha1: row.append(sha)
                writer.writerow(row); f.flush()
            if cancel_event is None or not cancel_event.is_set():
                q.put({"type":"item","path":str(p.resolve())})
        with ThreadPoolExecutor(max_workers=max(1,int(threads))) as ex:
            for p in paths:
                ex.submit(worker, p)
    finally:
        if f: f.close()
