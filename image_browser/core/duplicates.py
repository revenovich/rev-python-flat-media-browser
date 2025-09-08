
from pathlib import Path
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, List, Tuple, Iterable, Optional, Set
from PIL import Image
from .utils import sha1_of_file
from .phash import ahash, dhash, phash, hamming

HashGroups = Dict[str, List[str]]

def compute_sha1_map(paths: Iterable[Path], threads: int = 8, cancel_event=None, q=None) -> HashGroups:
    paths = list(paths)
    total = len(paths)
    groups: Dict[str, List[str]] = defaultdict(list)
    done = 0

    def work(p: Path):
        if cancel_event is not None and cancel_event.is_set():
            return None
        try:
            h = sha1_of_file(p, cancel_event=cancel_event)
            return (h, str(p.resolve()))
        except Exception:
            return None

    with ThreadPoolExecutor(max_workers=max(1,int(threads))) as ex:
        futs = {ex.submit(work, p): p for p in paths}
        for fut in as_completed(futs):
            if cancel_event is not None and cancel_event.is_set():
                break
            res = fut.result()
            done += 1
            if q is not None and (done % 50 == 0 or done == total):
                q.put({"type":"manage_progress","done":done,"total":total,"phase":"sha1"})
            if res is None:
                continue
            h, path = res
            if h:
                groups[h].append(path)

    # Keep only groups with >1
    out = {k:v for k,v in groups.items() if len(v) > 1 and k}
    if q is not None:
        q.put({"type":"manage_done","found":sum(len(v) for v in out.values())})
    return out

def compute_perceptual_groups(paths: Iterable[Path], method: str = "ahash", threshold: int = 5, threads: int = 8, cancel_event=None, q=None) -> List[List[str]]:
    from typing import Tuple
    entries: List[Tuple[str, int]] = []
    paths = list(paths)
    total = len(paths)
    done = 0

    def ph_for(p: Path):
        if cancel_event is not None and cancel_event.is_set():
            return None
        try:
            with Image.open(p) as im:
                if method == "ahash":
                    v = ahash(im)
                elif method == "dhash":
                    v = dhash(im)
                else:
                    v = phash(im)
            return (str(p.resolve()), v)
        except Exception:
            return None

    # Hash phase
    if q is not None:
        q.put({"type":"manage_progress","done":0,"total":total,"phase":method})
    from concurrent.futures import ThreadPoolExecutor, as_completed
    with ThreadPoolExecutor(max_workers=max(1,int(threads))) as ex:
        futs = {ex.submit(ph_for, p): p for p in paths}
        for fut in as_completed(futs):
            if cancel_event is not None and cancel_event.is_set():
                break
            res = fut.result()
            done += 1
            if q is not None and (done % 50 == 0 or done == total):
                q.put({"type":"manage_progress","done":done,"total":total,"phase":method})
            if res:
                entries.append(res)

    # Grouping phase (naive)
    groups: List[List[str]] = []
    used: Set[int] = set()
    for i in range(len(entries)):
        if cancel_event is not None and cancel_event.is_set():
            break
        if i in used: continue
        pi, hi = entries[i]
        cur = [pi]
        used.add(i)
        for j in range(i+1, len(entries)):
            if j in used: continue
            pj, hj = entries[j]
            if hamming(hi, hj) <= threshold:
                cur.append(pj); used.add(j)
        if len(cur) > 1:
            groups.append(cur)

    if q is not None:
        q.put({"type":"manage_done","found":sum(len(g) for g in groups)})
    return groups
