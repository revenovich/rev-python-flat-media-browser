import os, hashlib
from pathlib import Path

def is_image(p: Path, IMG_EXTS) -> bool:
    return p.suffix.lower() in IMG_EXTS

def sha1_of_file(p: Path, chunk=1024*1024, cancel_event=None) -> str:
    h = hashlib.sha1()
    with p.open('rb') as f:
        while True:
            if cancel_event is not None and cancel_event.is_set():
                return ""
            b = f.read(chunk)
            if not b: break
            h.update(b)
    return h.hexdigest()

def cpu_threads() -> int:
    try:
        return len(os.sched_getaffinity(0))
    except Exception:
        return os.cpu_count() or 4

def fmt_secs(s: float) -> str:
    s = int(s)
    if s < 60: return f"{s}s"
    m, ss = divmod(s, 60)
    if m < 60: return f"{m}m {ss}s"
    h, mm = divmod(m, 60)
    return f"{h}h {mm}m"
