from typing import Tuple
from PIL import Image, ImageOps
import numpy as np

try:
    # Prefer scipy if available
    from scipy.fftpack import dct as _dct
    HAS_DCT = True
except Exception:
    _dct = None
    HAS_DCT = False

def _to_grey(im: Image.Image, size: Tuple[int,int]):
    im = ImageOps.exif_transpose(im)
    im = im.convert("L").resize(size, Image.Resampling.LANCZOS)
    return np.asarray(im, dtype=np.float32)

def ahash(im: Image.Image, size: int = 8) -> int:
    arr = _to_grey(im, (size, size))
    avg = arr.mean()
    bits = (arr > avg).astype(np.uint8).ravel()
    val = 0
    for b in bits:
        val = (val << 1) | int(b)
    return val

def dhash(im: Image.Image, size: int = 8) -> int:
    arr = _to_grey(im, (size+1, size))
    diff = arr[:,1:] > arr[:,:-1]
    bits = diff.astype(np.uint8).ravel()
    val = 0
    for b in bits:
        val = (val << 1) | int(b)
    return val

def phash(im: Image.Image, size: int = 32, reduced: int = 8) -> int:
    if not HAS_DCT:
        raise RuntimeError("pHash requires SciPy (scipy.fftpack.dct). Install scipy or use aHash/dHash.")
    arr = _to_grey(im, (size, size))
    # 2D DCT-II via scipy
    dct_rows = _dct(arr, type=2, norm="ortho", axis=0)
    dct_full = _dct(dct_rows, type=2, norm="ortho", axis=1)
    dct_low = dct_full[:reduced, :reduced]
    avg = dct_low.mean()
    bits = (dct_low > avg).astype(np.uint8).ravel()
    val = 0
    for b in bits:
        val = (val << 1) | int(b)
    return val

def hamming(a: int, b: int) -> int:
    return (a ^ b).bit_count()
