"""
Public image URLs. Photos live in the Supabase Storage bucket as:

    full/<key>                  full-size photo   (key like "00/005c70....jpg")
    thumb/<key without ext>.webp   400 px thumbnail

product_photos.image_key holds <key>. Until a photo has a key (or if a file
is ever missing) the old eBay link in source_url is used instead.
"""
from typing import Optional

from app.config import get_settings


def _base() -> str:
    return get_settings().image_base_url.rstrip("/")


def full_url(key: Optional[str]) -> Optional[str]:
    return f"{_base()}/full/{key}" if key else None


def thumb_url(key: Optional[str]) -> Optional[str]:
    if not key:
        return None
    return f"{_base()}/thumb/{key.rsplit('.', 1)[0]}.webp"


def card_image(key: Optional[str], source_url: Optional[str]) -> dict:
    """For product grids: the small thumbnail, with the eBay link as backup."""
    best = thumb_url(key) or source_url
    return {"image_url": best, "image_fallback": source_url if best != source_url else None}


def gallery(rows) -> list[dict]:
    """
    For a product page. `rows` must already be in display order (main photo
    first). The same image is listed only once, even if the product has it
    attached several times.
    """
    seen, out = set(), []
    for r in rows:
        ident = r.image_key or r.source_url
        if not ident or ident in seen:
            continue
        seen.add(ident)
        url = full_url(r.image_key) or r.source_url
        out.append(
            {
                "url": url,
                "thumb": thumb_url(r.image_key) or r.source_url,
                "fallback": r.source_url if url != r.source_url else None,
            }
        )
    return out
