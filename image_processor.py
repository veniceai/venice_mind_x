"""Download images attached to tweets."""
import logging
import requests
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0 Safari/537.36"
)


def download_image(url: str) -> Optional[Tuple[bytes, str]]:
    """Returns (image_bytes, mime_type), or None if the URL isn't an image."""
    for variant in dict.fromkeys((url.split("?")[0], url)):  # bare URL is full res
        try:
            r = requests.get(variant, headers={"User-Agent": _UA}, timeout=10)
            mime = r.headers.get("content-type", "").split(";")[0].strip()
            if r.ok and mime.startswith("image/"):
                logger.info(f"Downloaded image ({len(r.content)} bytes) from {variant}")
                return r.content, mime
        except Exception as e:
            logger.debug(f"Image download failed for {variant}: {e}")
    return None


def process_tweet_media(
    tweet, media_lookup: dict
) -> Tuple[Optional[bytes], Optional[str]]:
    """Returns (image_bytes, image_url) for the first photo, or (None, None)."""
    attachments = getattr(tweet, "attachments", None)
    if not attachments:
        return None, None

    for key in attachments.get("media_keys", []):
        media = media_lookup.get(key)
        if not (media and media.type == "photo" and getattr(media, "url", None)):
            continue
        got = download_image(media.url)
        if got:
            return got[0], media.url.split("?")[0]

    return None, None
