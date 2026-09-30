"""HellSpy API client and ffprobe-based media probe — an optional second source.

HellSpy's public gateway (api.hellspy.to/gw/) needs no account or token:

    GET gw/search?query=<text>&limit=&offset=   -> {"items": [...], "nextOffset": N}
    GET gw/video/<id>/<fileHash>/download       -> 302 to a signed CDN link

The download link points at the original upload (full size, Range supported).
The `conversions` of gw/video/<id>/<hash> are 720p/1080p transcodes that don't
say what the upload is — a 4K file is offered as 720p — so they are never used.
The link is signed and time-limited, so it is resolved only when a file is
probed or downloaded, from the ident `hs:<id>:<fileHash>`.

HellSpy has no media probe like Webshare's file_info, so the shown files are
measured with ffprobe over that link (it reads only the headers via HTTP
Range) and mapped into the same dict, and the whole quality pipeline in
torznab works unchanged.
"""

import asyncio
import json
import logging
import shutil
import time
import urllib.parse
from collections import OrderedDict

import httpx

from . import __version__
from .webshare import SearchResult

logger = logging.getLogger("websharr.hellspy")

API_BASE = "https://api.hellspy.to/gw/"
USER_AGENT = f"Websharr/{__version__} (+https://github.com/janprochy/websharr)"
IDENT_PREFIX = "hs:"
SEARCH_CACHE_SIZE = 500  # cached searches kept; the oldest go first
# HellSpy's terms forbid overloading the service with automated requests, so
# API calls are capped per client on top of the search cache.
API_CONCURRENCY = 3
# The search API returns the display title without an extension (the real
# file name only comes with the per-file details or the signed link). Calling
# details for every hit would cost a request per result, so the search uses
# this placeholder and the download renames the file after the link (see
# file_name) — the release title never carries the extension anyway.
DEFAULT_EXTENSION = "mkv"
VIDEO_EXTENSIONS = ("mkv", "mp4", "avi", "m4v", "mov", "wmv", "ts", "m2ts", "webm", "mpg", "mpeg")


class HellspyError(Exception):
    pass


def make_ident(file_id, file_hash: str) -> str:
    return f"{IDENT_PREFIX}{file_id}:{file_hash}"


def is_hellspy(ident: str) -> bool:
    return (ident or "").startswith(IDENT_PREFIX)


def _split_ident(ident: str) -> tuple[str, str]:
    file_id, _, file_hash = ident[len(IDENT_PREFIX):].partition(":")
    if not is_hellspy(ident) or not file_id.isdigit() or not file_hash.isalnum():
        raise HellspyError(f"Not a HellSpy ident: {ident!r}")
    return file_id, file_hash


def _link_extension(url: str) -> str:
    """Extension of the original file named in a signed link (`fn=...mkv`)."""
    fn = urllib.parse.parse_qs(urllib.parse.urlparse(url).query).get("fn", [""])[0]
    ext = fn.rsplit(".", 1)[-1].lower() if "." in fn else ""
    return ext if ext in VIDEO_EXTENSIONS else ""


def file_name(url: str, name: str) -> str:
    """`name` with the extension of the original upload the signed link names —
    the search only knows a placeholder."""
    ext = _link_extension(url)
    if not ext:
        return name
    stem = name.rsplit(".", 1)[0] if "." in name else name
    return f"{stem}.{ext}"


class HellspyClient:
    def __init__(self, search_cache_ttl: float = 0):
        # Same cache as WebshareClient: *arr repeat identical searches, and a
        # public API is best not asked twice (0 = off).
        self._search_ttl = search_cache_ttl
        self._search_cache: OrderedDict[tuple, tuple[float, tuple[SearchResult, ...]]] = OrderedDict()
        self._search_inflight: dict[tuple, asyncio.Task] = {}
        self._sem = asyncio.Semaphore(API_CONCURRENCY)
        self._http = httpx.AsyncClient(
            base_url=API_BASE,
            headers={"Accept": "application/json", "User-Agent": USER_AGENT},
            timeout=httpx.Timeout(20.0),
            follow_redirects=False,  # the redirect target is the answer, not its body
        )

    async def close(self) -> None:
        await self._http.aclose()

    async def search(self, query: str, limit: int = 60, offset: int = 0) -> list[SearchResult]:
        key = (query, limit, offset)
        if self._search_ttl <= 0:
            return list(await self._search(*key))
        hit = self._search_cache.get(key)
        if hit and time.monotonic() - hit[0] < self._search_ttl:
            logger.debug("Search cache hit: %r (limit=%d, offset=%d)", query, limit, offset)
            return list(hit[1])
        task = self._search_inflight.get(key)
        if task is None:
            task = asyncio.ensure_future(self._search(*key))
            self._search_inflight[key] = task
            task.add_done_callback(lambda t: self._search_done(key, t))
        return list(await asyncio.shield(task))

    def _search_done(self, key: tuple, task: asyncio.Task) -> None:
        self._search_inflight.pop(key, None)
        if task.cancelled() or task.exception() is not None:
            return  # errors are never cached
        self._search_cache[key] = (time.monotonic(), task.result())
        self._search_cache.move_to_end(key)
        while len(self._search_cache) > SEARCH_CACHE_SIZE:
            self._search_cache.popitem(last=False)

    async def _search(self, query: str, limit: int, offset: int) -> tuple[SearchResult, ...]:
        async with self._sem:
            resp = await self._http.get("search", params={"query": query, "limit": limit, "offset": offset})
        resp.raise_for_status()
        try:
            items = resp.json().get("items") or []
        except (ValueError, AttributeError) as exc:
            raise HellspyError(f"Invalid JSON from HellSpy search: {exc}") from exc
        return tuple(r for r in map(search_result, items) if r is not None)

    async def file_link(self, ident: str) -> str:
        """Signed link to the original file, read from the redirect's Location."""
        file_id, file_hash = _split_ident(ident)
        async with self._sem:
            resp = await self._http.get(f"video/{file_id}/{file_hash}/download")
        link = resp.headers.get("location", "") if resp.is_redirect else ""
        if not link:
            # a removed file answers 404 instead of redirecting
            raise HellspyError(f"HellSpy returned no download link (HTTP {resp.status_code})")
        return link


def search_result(item: dict) -> SearchResult | None:
    """A gw/search item as a SearchResult; None for anything but a video with a
    file hash, so a new object type in the response never passes as a file."""
    if not isinstance(item, dict) or item.get("objectType") != "GWSearchVideo":
        return None
    file_hash = str(item.get("fileHash") or "")
    file_id = str(item.get("id") or "")
    if not file_hash.isalnum() or not file_id.isdigit():
        return None
    title = str(item.get("title") or "").strip()
    if not title:
        return None
    ext = title.rsplit(".", 1)[-1].lower() if "." in title else ""
    name = title if ext in VIDEO_EXTENSIONS else f"{title}.{DEFAULT_EXTENSION}"
    try:
        size, duration = int(item.get("size") or 0), int(item.get("duration") or 0)
    except (TypeError, ValueError):
        return None
    return SearchResult(ident=make_ident(file_id, file_hash), name=name, size=size, duration=duration)


# --- ffprobe ----------------------------------------------------------------
# ffprobe reads a few MB of the file over HTTP Range. The limits mirror
# torznab._file_info: one process-wide cap (every probe also costs a HellSpy
# API call for the link), a hard timeout, a cache per ident (a fileHash never
# changes) and {} on any failure — the pipeline then just knows less.
PROBE_TIMEOUT = 20.0
PROBE_CONCURRENCY = 3
PROBE_CACHE_MAX = 5000
_probe_sem: asyncio.Semaphore | None = None
_probe_cache: dict[str, dict] = {}
_ffprobe: str | None = None
_ffprobe_missing = False

# ffprobe codec_name -> the format names Webshare's file_info reports.
_VIDEO_CODECS = {"h264": "H264", "hevc": "HEVC", "mpeg4": "MPEG4", "av1": "AV1", "vp9": "VP9",
                 "mpeg2video": "MPEG2", "vc1": "VC1"}
_AUDIO_CODECS = {"eac3": "EAC3", "ac3": "AC3", "dts": "DTS", "truehd": "TRUEHD", "aac": "AAC",
                 "mp3": "MP3", "flac": "FLAC", "opus": "OPUS", "vorbis": "VORBIS"}
_CONTAINERS = {"matroska": "mkv", "mov": "mp4", "avi": "avi", "mpegts": "ts", "asf": "wmv", "mpeg": "mpg"}


def _semaphore() -> asyncio.Semaphore:
    global _probe_sem
    if _probe_sem is None:
        _probe_sem = asyncio.Semaphore(PROBE_CONCURRENCY)
    return _probe_sem


def probe_info(data: dict, container: str = "") -> dict:
    """ffprobe JSON (-show_streams -show_format) as a Webshare file_info dict.

    The first real video stream (not cover art) gives size and codec; every
    audio stream its codec, channel count and ISO 639-2 language ("" for
    untagged and "und"). DTS-HD / Atmos show up only as DTS / TRUEHD, like on
    Webshare. `container` (from the link's file name) wins over format_name.
    """
    streams = data.get("streams") or []
    fmt = data.get("format") or {}
    video = next((s for s in streams if s.get("codec_type") == "video"
                  and not (s.get("disposition") or {}).get("attached_pic")), {})
    audio = []
    for s in streams:
        if s.get("codec_type") != "audio":
            continue
        lang = str((s.get("tags") or {}).get("language") or "").strip().upper()
        codec = str(s.get("codec_name") or "")
        audio.append({
            "format": _AUDIO_CODECS.get(codec, codec.upper()),
            "channels": int(s.get("channels") or 0),
            "language": "" if lang == "UND" else lang,
        })
    try:
        length = int(float(fmt.get("duration") or video.get("duration") or 0))
    except (TypeError, ValueError):
        length = 0
    codec = str(video.get("codec_name") or "")
    return {
        "length": length,
        "width": int(video.get("width") or 0),
        "height": int(video.get("height") or 0),
        "format": _VIDEO_CODECS.get(codec, codec.upper()),
        "type": container or _CONTAINERS.get(str(fmt.get("format_name") or "").split(",")[0], ""),
        "audio": audio,
        "audio_languages": [a["language"] for a in audio if a["language"]],
    }


async def _run_ffprobe(url: str) -> dict:
    """ffprobe's JSON for `url`; raises on a missing binary, error or timeout."""
    proc = await asyncio.create_subprocess_exec(
        _ffprobe or "ffprobe", "-v", "error", "-user_agent", USER_AGENT,
        "-print_format", "json", "-show_streams", "-show_format", url,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        out, err = await asyncio.wait_for(proc.communicate(), PROBE_TIMEOUT)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise
    if proc.returncode != 0:
        raise RuntimeError(err.decode(errors="replace").strip()[:200] or f"exit {proc.returncode}")
    return json.loads(out or b"{}")


def _have_ffprobe() -> bool:
    global _ffprobe, _ffprobe_missing
    if _ffprobe is None and not _ffprobe_missing:
        _ffprobe = shutil.which("ffprobe")
        if _ffprobe is None:
            _ffprobe_missing = True
            logger.warning("ffprobe not found: HellSpy files are listed without measured quality")
    return not _ffprobe_missing


async def probe(client, ident: str) -> dict:
    """Measured file_info for a HellSpy ident: cached, limited, {} on failure."""
    if ident in _probe_cache:
        return _probe_cache[ident]
    if not _have_ffprobe():
        return {}
    try:
        async with _semaphore():
            url = await client.file_link(ident)
            info = probe_info(await _run_ffprobe(url), _link_extension(url))
    except asyncio.TimeoutError:
        logger.warning("ffprobe %s timed out after %ds", ident, PROBE_TIMEOUT)
        return {}
    except Exception as exc:  # fail open: a probe must never break a search
        logger.warning("ffprobe %s failed: %s", ident, exc)
        return {}
    if not (info["width"] or info["length"] or info["audio"]):
        return {}  # nothing measured; ask again next time
    if len(_probe_cache) >= PROBE_CACHE_MAX:
        _probe_cache.pop(next(iter(_probe_cache)))
    _probe_cache[ident] = info
    return info
