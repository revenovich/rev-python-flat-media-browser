
from pathlib import Path
from typing import Iterable, List, Set, Optional

def filter_paths(paths: Iterable[str], substring: Optional[str]=None, exts: Optional[Set[str]]=None) -> List[str]:
    out = []
    subs = (substring or "").lower().strip()
    for p in paths:
        name = Path(p).name.lower()
        if subs and subs not in name:
            continue
        if exts and Path(p).suffix.lower() not in exts:
            continue
        out.append(p)
    return out
