from pathlib import Path
import csv
from typing import Iterable, List, Tuple, Optional
from .constants import MANIFEST_NAME, MANIFEST_SIG

def manifest_path(root: Path) -> Path:
    return root / MANIFEST_NAME

def read_manifest_stream(root: Path):
    """Yield batches of existing paths from a valid manifest (strings)."""
    mpath = manifest_path(root)
    if not mpath.exists():
        yield ("status", "No manifest found."); return
    try:
        with mpath.open("r", encoding="utf-8") as f:
            first = f.readline().rstrip("\n\r")
            if first.strip() != MANIFEST_SIG:
                yield ("status", "Manifest ignored (foreign file)."); return
            reader = csv.DictReader(f)
            batch = []
            for row in reader:
                p = (row.get("path") or "").strip()
                if not p: continue
                if Path(p).exists():
                    batch.append(str(Path(p).resolve()))
                    if len(batch) >= 200:
                        yield ("paths", batch); batch = []
            if batch:
                yield ("paths", batch)
            yield ("status", "Manifest loaded.")
    except Exception as e:
        yield ("status", f"Manifest load failed: {e}")

def open_manifest_for_write(root: Path, with_sha1: bool, replace: bool=False):
    mpath = manifest_path(root)
    mode = "w" if replace or (not mpath.exists()) else "a"
    if mode == "a":
        try:
            with mpath.open("r", encoding="utf-8") as f:
                first = f.readline().rstrip("\n\r")
                if first.strip() != MANIFEST_SIG:
                    mode = "w"
        except Exception:
            mode = "w"
    f = mpath.open(mode, newline="", encoding="utf-8")
    writer = csv.writer(f)
    if mode == "w":
        f.write(MANIFEST_SIG + "\n")
        writer.writerow(["path", "sha1"] if with_sha1 else ["path"])
    return f, writer
