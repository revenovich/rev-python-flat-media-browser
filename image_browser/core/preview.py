from io import BytesIO
from pathlib import Path
from typing import Tuple
try:
    from PIL import Image, ImageOps, ImageFile
    ImageFile.LOAD_TRUNCATED_IMAGES = True
except Exception:
    Image = None
    ImageOps = None

def make_thumbnail_png_bytes(path: Path, size: Tuple[int,int]) -> bytes:
    if Image is None or ImageOps is None:
        raise RuntimeError("Pillow not installed")
    with Image.open(path) as im:
        try: im.seek(0)
        except Exception: pass
        im2 = ImageOps.contain(im, (max(1,size[0]-8), max(1,size[1]-8)))
        bio = BytesIO()
        im2.save(bio, format="PNG")
        return bio.getvalue()
