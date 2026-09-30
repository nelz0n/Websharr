"""Series titles as Sonarr knows them, from Sonarr's own metadata service.

A TV release is named "<series> SxxEyy - …" so Sonarr can map it by title. Its
series titles come from TVDB via Skyhook, and they can differ from TMDB's:
"Maxipes Fík" (1976) is "Maxidog Fík" in Sonarr, and a release named after the
TMDB title was matched to the series only by id — "Automatic import is not
possible" for all 13 episodes. Skyhook also carries the disambiguation Sonarr
uses ("DuckTales (2017)", "House of Cards (US)").

One request per show, cached for hours (Skyhook itself is cached for 6 h behind
a CDN); any failure falls back to the TMDB title.
"""

import logging
import time

import httpx

from . import __version__

logger = logging.getLogger("websharr.skyhook")

_URL = "https://skyhook.sonarr.tv/v1/tvdb/shows/en/{tvdbid}"
_TTL = 6 * 3600
_CACHE_MAX = 2000
_cache: dict[str, tuple[float, str]] = {}


async def series_title(tvdbid) -> str:
    """Sonarr's title of the series with this TVDB id, or "" when unknown."""
    key = str(tvdbid or "").strip()
    if not key.isdigit():
        return ""
    hit = _cache.get(key)
    if hit and time.monotonic() - hit[0] < _TTL:
        return hit[1]
    title = ""
    try:
        async with httpx.AsyncClient(timeout=10.0, headers={"User-Agent": f"Websharr/{__version__}"}) as client:
            r = await client.get(_URL.format(tvdbid=key))
            if r.status_code == 200:
                title = str(r.json().get("title") or "").strip()
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("Skyhook title for tvdb %s failed: %s", key, str(exc) or type(exc).__name__)
        return ""  # not cached: ask again next time
    if len(_cache) >= _CACHE_MAX:
        _cache.pop(next(iter(_cache)))
    _cache[key] = (time.monotonic(), title)
    return title
