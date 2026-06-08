"""
Fingerprinting helpers for the stage-1 fast path.

* Text   -> normalised MD5 (case-folded, whitespace-collapsed, emoji-stripped of
             zero-width junk) so trivial variations still collide.
* Media  -> perceptual hash (pHash via the `imagehash` library) so visually
             identical GIFs/photos match even after Telegram re-encodes them.
"""
from __future__ import annotations

import hashlib
import re

_WS_RE = re.compile(r"\s+")
_INVISIBLE_RE = re.compile(r"[\u200b-\u200f\u202a-\u202e\ufeff]")


def normalise_text(text: str) -> str:
    text = (text or "").strip().lower()
    text = _INVISIBLE_RE.sub("", text)
    text = _WS_RE.sub(" ", text)
    return text


def text_hash(text: str) -> str:
    norm = normalise_text(text)
    return hashlib.md5(norm.encode("utf-8")).hexdigest()


def image_phash(path: str) -> str | None:
    """Return the perceptual hash of an image/GIF first frame, or None."""
    try:
        from PIL import Image
        import imagehash
    except Exception:  # noqa: BLE001 - libs optional at runtime
        return None
    try:
        with Image.open(path) as img:
            img.seek(0)  # first frame for animated GIFs
            return str(imagehash.phash(img.convert("RGB")))
    except Exception:  # noqa: BLE001
        return None
