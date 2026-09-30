"""HellSpy source: API mapping, ffprobe mapping, /hellspy/api through the shared
pipeline, and downloads of "hs:" idents."""

import asyncio
import json
import urllib.parse
import xml.etree.ElementTree as ET
from pathlib import Path

import httpx
import pytest

from app import hellspy
from app.hellspy import HellspyClient, HellspyError, probe_info
from app.nzb import build_nzb
from app.webshare import SearchResult

from .conftest import FakeHellspyClient, wait_for

NZNS = "{http://www.newznab.com/DTD/2010/feeds/attributes/}"

SEARCH_JSON = {
    "items": [
        {"objectType": "GWSearchVideo", "id": 22962021, "fileHash": "74d66787cf988025",
         "title": "Bluey S01E02 Nemocnice 1080p CZ", "size": 355127285, "duration": 438,
         "thumbs": ["https://thumbs.example/1.jpg"]},
        {"objectType": "GWSearchVideo", "id": 5, "fileHash": "", "title": "no hash", "size": 1},
        {"objectType": "GWSearchFolder", "id": 6, "fileHash": "abc", "title": "a folder"},
        {"objectType": "GWSearchVideo", "id": 7, "fileHash": "beef", "title": "Film.2020.mp4",
         "size": 900, "duration": 60},
    ],
    "nextOffset": 4,
}


def _client(handler, ttl=0) -> HellspyClient:
    client = HellspyClient(search_cache_ttl=ttl)
    client._http = httpx.AsyncClient(base_url=hellspy.API_BASE, transport=httpx.MockTransport(handler))
    return client


def test_search_maps_videos_only():
    seen = []

    def handler(request: httpx.Request):
        seen.append(request)
        return httpx.Response(200, json=SEARCH_JSON)

    results = asyncio.run(_client(handler).search("Bluey S01E02", limit=20, offset=40))
    assert [r.ident for r in results] == ["hs:22962021:74d66787cf988025", "hs:7:beef"]
    first = results[0]
    # No extension in the API title: a placeholder the download corrects later.
    assert first.name == "Bluey S01E02 Nemocnice 1080p CZ.mkv"
    assert (first.size, first.duration) == (355127285, 438)
    assert results[1].name == "Film.2020.mp4"  # an extension already there is kept
    assert seen[0].url.params["query"] == "Bluey S01E02"
    assert (seen[0].url.params["limit"], seen[0].url.params["offset"]) == ("20", "40")


def test_search_is_cached_within_ttl():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json=SEARCH_JSON)

    client = _client(handler, ttl=600)

    async def run():
        await client.search("x")
        await client.search("x")
        await client.search("x", offset=10)

    asyncio.run(run())
    assert len(calls) == 2


def test_file_link_reads_the_redirect_without_following_it():
    target = "https://cdn.example/file?token=t&fn=Bluey+S01E02.mkv"

    def handler(request):
        assert request.url.path == "/gw/video/22962021/74d66787cf988025/download"
        return httpx.Response(302, headers={"Location": target})

    assert asyncio.run(_client(handler).file_link("hs:22962021:74d66787cf988025")) == target

    gone = _client(lambda request: httpx.Response(404, json={"error": "Not found"}))
    with pytest.raises(HellspyError):
        asyncio.run(gone.file_link("hs:1:ab"))
    with pytest.raises(HellspyError):
        asyncio.run(gone.file_link("hs:../x:ab"))


def test_file_name_takes_the_extension_from_the_link():
    url = "https://cdn.example/file?token=t&fn=Bluey+S01E02+Nemocnice.avi"
    assert hellspy.file_name(url, "Bluey S01E02 Nemocnice.mkv") == "Bluey S01E02 Nemocnice.avi"
    assert hellspy.file_name("https://cdn.example/file?token=t", "a.mkv") == "a.mkv"
    assert hellspy.file_name("https://cdn.example/file?fn=a.exe", "a.mkv") == "a.mkv"


FFPROBE_JSON = {
    "streams": [
        {"codec_type": "video", "codec_name": "hevc", "width": 3840, "height": 1608},
        {"codec_type": "audio", "codec_name": "eac3", "channels": 6, "tags": {"language": "cze"}},
        {"codec_type": "audio", "codec_name": "truehd", "channels": 8, "tags": {"language": "eng"}},
        {"codec_type": "audio", "codec_name": "aac", "channels": 2, "tags": {"language": "und"}},
        {"codec_type": "subtitle", "codec_name": "subrip", "tags": {"language": "cze"}},
        {"codec_type": "video", "codec_name": "mjpeg", "width": 600, "height": 900,
         "disposition": {"attached_pic": 1}},
    ],
    "format": {"format_name": "matroska,webm", "duration": "6512.480000"},
}


def test_probe_info_maps_ffprobe_to_file_info():
    info = probe_info(FFPROBE_JSON)
    assert info == {
        "length": 6512, "width": 3840, "height": 1608, "format": "HEVC", "type": "mkv",
        "audio": [{"format": "EAC3", "channels": 6, "language": "CZE"},
                  {"format": "TRUEHD", "channels": 8, "language": "ENG"},
                  {"format": "AAC", "channels": 2, "language": ""}],
        "audio_languages": ["CZE", "ENG"],
    }
    # Cover art first doesn't count as the video; the link's extension wins.
    art_first = {"streams": [FFPROBE_JSON["streams"][5], {"codec_type": "video", "codec_name": "h264",
                                                           "width": 1280, "height": 720}],
                 "format": {"format_name": "mov,mp4,m4a,3gp,3g2,mj2", "duration": "60"}}
    info = probe_info(art_first)
    assert (info["width"], info["format"], info["type"]) == (1280, "H264", "mp4")
    assert probe_info(art_first, "m4v")["type"] == "m4v"
    slovak = {"streams": [{"codec_type": "audio", "codec_name": "dts", "channels": 6,
                           "tags": {"language": "slo"}}], "format": {}}
    assert probe_info(slovak)["audio"] == [{"format": "DTS", "channels": 6, "language": "SLO"}]
    assert probe_info({}) == {"length": 0, "width": 0, "height": 0, "format": "", "type": "",
                              "audio": [], "audio_languages": []}


@pytest.fixture
def probe_state(monkeypatch):
    """Clean probe cache/limiter, ffprobe "installed" and faked per test."""
    hellspy._probe_cache.clear()
    hellspy._probe_failed.clear()
    monkeypatch.setattr(hellspy, "_probe_sem", None)
    monkeypatch.setattr(hellspy, "_ffprobe", "/usr/bin/ffprobe")
    monkeypatch.setattr(hellspy, "_ffprobe_missing", False)
    runs = []

    def fake(outputs: dict):
        async def run(url):
            runs.append(url)
            out = outputs.get(url, outputs.get("*"))
            if isinstance(out, BaseException):
                raise out
            return out
        monkeypatch.setattr(hellspy, "_run_ffprobe", run)

    fake.runs = runs
    return fake


def test_probe_caches_and_fails_open(probe_state):
    client = FakeHellspyClient()
    client.file_link_url = "https://cdn.example/f?fn=a.mkv"
    probe_state({"*": FFPROBE_JSON})
    info = asyncio.run(hellspy.probe(client, "hs:1:a"))
    assert info["height"] == 1608 and info["type"] == "mkv"
    asyncio.run(hellspy.probe(client, "hs:1:a"))
    assert len(probe_state.runs) == 1 and client.links_asked == ["hs:1:a"]

    for n, failure in enumerate((asyncio.TimeoutError(), RuntimeError("Server returned 403 Forbidden"),
                                 json.JSONDecodeError("x", "", 0))):
        probe_state({"*": failure})
        assert asyncio.run(hellspy.probe(client, f"hs:2:{n}")) == {}
    assert not any(k.startswith("hs:2:") for k in hellspy._probe_cache)

    # a failed file is left alone for a while instead of costing a probe on every search
    runs = len(probe_state.runs)
    assert asyncio.run(hellspy.probe(client, "hs:2:0")) == {}
    assert len(probe_state.runs) == runs
    hellspy._probe_failed["hs:2:0"] -= hellspy.PROBE_RETRY_AFTER
    probe_state({"*": FFPROBE_JSON})
    assert asyncio.run(hellspy.probe(client, "hs:2:0"))["height"] == 1608

    async def dead(ident):
        raise HellspyError("HellSpy returned no download link (HTTP 404)")

    client.file_link = dead
    assert asyncio.run(hellspy.probe(client, "hs:3:c")) == {}


def test_probe_times_out_a_hanging_ffprobe(monkeypatch, tmp_path):
    """The real subprocess path: a stand-in ffprobe that never answers is
    killed after PROBE_TIMEOUT and the probe fails open."""
    hellspy._probe_cache.clear()
    hellspy._probe_failed.clear()
    monkeypatch.setattr(hellspy, "_probe_sem", None)
    slow = tmp_path / "ffprobe"
    slow.write_text("#!/bin/sh\nsleep 30\n")
    slow.chmod(0o755)
    monkeypatch.setattr(hellspy, "_ffprobe", str(slow))
    monkeypatch.setattr(hellspy, "_ffprobe_missing", False)
    monkeypatch.setattr(hellspy, "PROBE_TIMEOUT", 0.3)
    assert asyncio.run(hellspy.probe(FakeHellspyClient(), "hs:4:d")) == {}


def test_missing_ffprobe_is_logged_once(monkeypatch, caplog):
    hellspy._probe_cache.clear()
    hellspy._probe_failed.clear()
    monkeypatch.setattr(hellspy, "_ffprobe", None)
    monkeypatch.setattr(hellspy, "_ffprobe_missing", False)
    monkeypatch.setattr(hellspy.shutil, "which", lambda name: None)
    client = FakeHellspyClient()
    for i in range(3):
        assert asyncio.run(hellspy.probe(client, f"hs:{i}:e")) == {}
    assert client.links_asked == []  # no API call without a way to use the link
    assert sum("ffprobe not found" in r.message for r in caplog.records) == 1


# --- /hellspy/api -----------------------------------------------------------

@pytest.fixture
def enabled(monkeypatch):
    from app.settings import settings
    monkeypatch.setattr(settings, "hellspy_enabled", True)
    monkeypatch.setattr(settings, "aliases", [])
    monkeypatch.setattr(settings, "tmdb_token", "")
    monkeypatch.setattr(settings, "release_tags", True)


def test_disabled_answers_a_newznab_error(client):
    resp = client.get("/hellspy/api", params={"t": "caps", "apikey": "testkey"})
    assert resp.status_code == 200
    root = ET.fromstring(resp.content)
    assert root.tag == "error" and root.get("code") == "910"
    # a wrong key is still a key error, not a hint that the source exists
    root = ET.fromstring(client.get("/hellspy/api", params={"t": "caps", "apikey": "no"}).content)
    assert root.get("code") == "100"


def test_caps_has_its_own_title(client, enabled):
    root = ET.fromstring(client.get("/hellspy/api", params={"t": "caps", "apikey": "testkey"}).content)
    assert root.find("server").get("title") == "Websharr HellSpy"
    assert "tmdbid" in root.find("searching/tv-search").get("supportedParams")


def test_search_runs_the_shared_pipeline(client, fake_hellspy, enabled, probe_state):
    gb = 1024 ** 3
    fake_hellspy.results = [
        SearchResult("hs:1:aa", "Vlny 2024 CZ dabing.mkv", 3 * gb, duration=6000),
        SearchResult("hs:2:bb", "Vlny 2024 HDTS.mkv", 2 * gb, duration=6000),     # junk
        SearchResult("hs:3:cc", "Vlnobiti 2024.mkv", 2 * gb, duration=6000),      # other title
        SearchResult("hs:4:dd", "Vlny 2024 stub.mkv", 10 * 1024 ** 2, duration=6000),  # torso
    ]
    fake_hellspy.file_link_url = "https://cdn.example/f?fn=Vlny.mkv"
    probe_state({"*": {
        "streams": [{"codec_type": "video", "codec_name": "h264", "width": 1920, "height": 1080},
                    {"codec_type": "audio", "codec_name": "ac3", "channels": 6,
                     "tags": {"language": "cze"}}],
        "format": {"format_name": "matroska,webm", "duration": "6000"}}})
    resp = client.get("/hellspy/api", params={"t": "movie", "apikey": "testkey", "q": "Vlny 2024"})
    items = ET.fromstring(resp.content).findall("channel/item")
    assert len(items) == 1
    item = items[0]
    assert item.findtext("title") == "Vlny 2024 CZ dabing 1080p x264 DD5.1 CZaudio"
    assert item.findtext("guid") == "websharr-hs:1:aa"
    link = item.findtext("link")
    assert "/torznab/nzb/hs%3A1%3Aaa?" in link
    attrs = {a.get("name"): a.get("value") for a in item.findall(f"{NZNS}attr")}
    assert attrs["language"] == "Czech"

    # The feed link is a working NZB link, and the NZB carries the ident.
    path = urllib.parse.urlparse(link)
    nzb = client.get(path.path + "?" + path.query)
    assert nzb.status_code == 200 and b">hs:1:aa<" in nzb.content


def test_runtime_check_uses_the_search_duration(client, fake_hellspy, enabled, probe_state, monkeypatch):
    """ffprobe failed (fail open): the duration from HellSpy's search still
    drops a file far off the TMDB runtime."""
    from app import torznab
    from app.settings import settings
    monkeypatch.setattr(settings, "tmdb_token", "tok")

    async def by_id(token, kind, tmdbid=None, imdbid=None, tvdbid=None):
        return ("Bluey", "", "en", (), 2018)

    async def no_name(token, kind, q):
        return None

    async def runtime(*args, **kwargs):
        return 7

    monkeypatch.setattr(torznab, "tmdb_lookup_by_id", by_id)
    monkeypatch.setattr(torznab, "tmdb_lookup", no_name)
    monkeypatch.setattr(torznab, "tmdb_runtime", runtime)
    fake_hellspy.results = [
        SearchResult("hs:1:ep", "Bluey S01E02 Nemocnice 1080p CZ.mkv", 355127285, duration=438),
        SearchResult("hs:2:long", "Bluey S01E02 1080p CZ compilation.mkv", 3_000_000_000, duration=3600),
    ]
    probe_state({"*": RuntimeError("probe failed")})
    resp = client.get("/hellspy/api", params={
        "t": "tvsearch", "apikey": "testkey", "tvdbid": "353818", "season": "1", "ep": "2"})
    guids = [i.findtext("guid") for i in ET.fromstring(resp.content).findall("channel/item")]
    assert guids == ["websharr-hs:1:ep"]


def test_search_failure_is_a_newznab_error(client, fake_hellspy, enabled):
    async def broken(query, limit=60, offset=0):
        raise HellspyError("Invalid JSON from HellSpy search")

    fake_hellspy.search = broken
    root = ET.fromstring(client.get("/hellspy/api", params={
        "t": "search", "apikey": "testkey", "q": "Vlny"}).content)
    assert root.tag == "error" and "HellSpy search failed" in root.get("description")


def test_settings_toggle(client):
    from app.settings import settings
    resp = client.post("/ui/api/settings", params={"apikey": "testkey"}, json={"hellspy_enabled": True})
    assert resp.status_code == 200 and settings.hellspy_enabled is True
    assert client.get("/ui/api/settings", params={"apikey": "testkey"}).json()["hellspy_enabled"] is True
    settings.load()
    assert settings.hellspy_enabled is True  # persisted
    bad = client.post("/ui/api/settings", params={"apikey": "testkey"}, json={"hellspy_enabled": "yes"})
    assert bad.status_code == 400


# --- downloads ----------------------------------------------------------------

def test_hs_download_uses_the_hellspy_client(client, fake_webshare, fake_hellspy, monkeypatch):
    from .test_downloads import PAYLOAD, _serve

    async def no_webshare(ident):
        raise AssertionError("a HellSpy ident went to Webshare")

    monkeypatch.setattr(fake_webshare, "file_link", no_webshare)
    httpd, handler = _serve(PAYLOAD, support_range=True)
    try:
        port = httpd.server_address[1]
        fake_hellspy.file_link_url = f"http://127.0.0.1:{port}/file?token=t&fn=Bluey+S01E02.mp4"
        feed_link = (f"http://testserver/torznab/nzb/{urllib.parse.quote('hs:1:aa', safe='')}"
                     f"?apikey=testkey&name=Bluey%20S01E02.mkv&size={len(PAYLOAD)}")
        nzo_id = client.get("/sabnzbd/api", params={
            "mode": "addurl", "apikey": "testkey", "cat": "tv", "name": feed_link,
            "nzbname": "Bluey S01E02 - Bluey"}).json()["nzo_ids"][0]
        manager = client.app.state.downloads
        assert wait_for(lambda: manager.get(nzo_id).status == "completed")
        job = manager.get(nzo_id)
        assert job.ident == "hs:1:aa" and fake_hellspy.links_asked == ["hs:1:aa"]
        # the link names the original upload: .mp4, not the search's placeholder
        assert (Path(job.storage) / "Bluey S01E02.mp4").read_bytes() == PAYLOAD
    finally:
        httpd.shutdown()


def test_hs_dropped_connection_resumes_through_hellspy(client, fake_webshare, fake_hellspy, monkeypatch):
    """A dropped HellSpy download is resumed with a fresh HellSpy link, never a
    Webshare one."""
    from .test_downloads import _serve_dropping

    async def no_webshare(ident):
        raise AssertionError("a HellSpy ident went to Webshare")

    monkeypatch.setattr(fake_webshare, "file_link", no_webshare)
    payload = bytes(range(256)) * 16_384  # 4 MiB, bigger than the write chunk
    httpd, handler = _serve_dropping(payload, drop_at=1_500_000, drops=1)
    try:
        port = httpd.server_address[1]
        fake_hellspy.file_link_url = f"http://127.0.0.1:{port}/file?token=t&fn=Film.mkv"
        feed_link = (f"http://testserver/torznab/nzb/{urllib.parse.quote('hs:9:bb', safe='')}"
                     f"?apikey=testkey&name=Film.mkv&size={len(payload)}")
        nzo_id = client.get("/sabnzbd/api", params={
            "mode": "addurl", "apikey": "testkey", "cat": "movies", "name": feed_link}).json()["nzo_ids"][0]
        manager = client.app.state.downloads
        assert wait_for(lambda: manager.get(nzo_id).status == "completed")
        assert fake_hellspy.links_asked == ["hs:9:bb", "hs:9:bb"]
        assert (Path(manager.get(nzo_id).storage) / "Film.mkv").read_bytes() == payload
    finally:
        httpd.shutdown()


def test_hs_dead_link_falls_back_to_a_copy(client, fake_hellspy, monkeypatch):
    from .test_downloads import PAYLOAD, _serve

    httpd, _ = _serve(PAYLOAD, support_range=True)
    try:
        url = f"http://127.0.0.1:{httpd.server_address[1]}/file?fn=Film.mkv"
        asked = []

        async def file_link(ident):
            asked.append(ident)
            if ident == "hs:1:dead":
                raise HellspyError("HellSpy returned no download link (HTTP 404)")
            return url

        monkeypatch.setattr(fake_hellspy, "file_link", file_link)
        nzb = build_nzb("hs:1:dead", "Film.mkv", len(PAYLOAD), ["hs:2:good"])
        nzo_id = client.post(
            "/sabnzbd/api",
            params={"mode": "addfile", "apikey": "testkey", "cat": "movies"},
            files={"nzbfile": ("Film 2024.nzb", nzb.encode(), "application/x-nzb")},
        ).json()["nzo_ids"][0]
        manager = client.app.state.downloads
        assert wait_for(lambda: manager.get(nzo_id).status == "completed")
        assert asked == ["hs:1:dead", "hs:2:good"]
        assert manager.get(nzo_id).ident == "hs:2:good"
    finally:
        httpd.shutdown()
