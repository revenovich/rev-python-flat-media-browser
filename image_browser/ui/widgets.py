
import tkinter as tk
from pathlib import Path
from typing import Tuple, List, Callable
from .popup_gallery import ThumbnailGalleryPopup

def open_gallery_popup(parent: tk.Tk,
                       title: str,
                       paths: List[str],
                       thumb_loader: Callable[[Path, Tuple[int,int]], bytes],
                       thumb_size: Tuple[int,int]=(140,140),
                       columns: int = 4):
    if not paths:
        return
    win = ThumbnailGalleryPopup(parent, title=title, thumb_size=thumb_size,
                                columns=columns, thumb_loader=thumb_loader)
    win.populate(paths)
