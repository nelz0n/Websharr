"""TMDB title resolution: English display for foreign-origin titles."""

import asyncio

from app import tmdb


def test_pick_english():
    items = [{"iso_3166_1": "FR", "title": "Les Nineties"},
             {"iso_3166_1": "GB", "title": "Nineties"}]
    assert tmdb._pick_english(items) == "Nineties"
    assert tmdb._pick_english([{"iso_3166_1": "RU", "title": "Девяностые"}]) == ""
    assert tmdb._pick_english([]) == ""


def test_pick_czech():
    items = [{"iso_3166_1": "CZ", "title": "Kačeří příběhy"},
             {"iso_3166_1": "CZ", "title": "My z Kačerova"},
             {"iso_3166_1": "CZ", "title": "kačeří příběhy"},  # case-dup dropped
             {"iso_3166_1": "PL", "title": "Kacze Opowieści"},
             {"iso_3166_1": "US", "title": "Disney's DuckTales"}]
    assert tmdb._pick_czech(items) == ["Kačeří příběhy", "My z Kačerova"]
    assert tmdb._pick_czech([]) == []


class _Resp:
    status_code = 200

    def __init__(self, data):
        self._data = data

    def json(self):
        return self._data


class _Client:
    def __init__(self, alt_titles):
        self._alt = alt_titles

    async def get(self, url, params=None):
        return _Resp({"results": self._alt})


def test_resolve_swaps_english_for_foreign_origin():
    # CZ-origin show with no English name of its own: display should become the
    # English alternative title, the Czech name becomes the search term.
    entry = {"id": 155277, "name": "Devadesátky", "original_name": "Devadesátky",
             "original_language": "cs"}
    client = _Client([{"iso_3166_1": "GB", "title": "Nineties"}])
    disp, orig, lang, czech, year = asyncio.run(tmdb._resolve(client, entry, "tv"))
    assert disp == "Nineties" and orig == "Devadesátky" and lang == "cs"
    assert czech == ()  # Czech-origin: the original already is the Czech name


def test_resolve_keeps_existing_english_name():
    # CZ-origin show with a distinct English name must not fetch alt titles:
    # the original already is the Czech search term.
    class _Boom:
        async def get(self, *a, **k):
            raise AssertionError("should not fetch alternative titles")

    entry = {"id": 1, "name": "The Sleepers", "original_name": "Bez vědomí",
             "original_language": "cs"}
    disp, orig, lang, czech, year = asyncio.run(tmdb._resolve(_Boom(), entry, "tv"))
    assert disp == "The Sleepers" and orig == "Bez vědomí" and lang == "cs"
    assert czech == ()


def test_resolve_english_show_picks_czech_alt_titles():
    # English-origin show dubbed under Czech names: the CZ alternative titles
    # become extra search terms; display/original stay untouched.
    entry = {"id": 720, "name": "DuckTales", "original_name": "DuckTales",
             "original_language": "en", "first_air_date": "1987-09-18"}
    client = _Client([{"iso_3166_1": "CZ", "title": "Kačeří příběhy"},
                      {"iso_3166_1": "CZ", "title": "My z Kačerova"},
                      {"iso_3166_1": "US", "title": "Disney's DuckTales"}])
    disp, orig, lang, czech, year = asyncio.run(tmdb._resolve(client, entry, "tv"))
    assert disp == "DuckTales" and orig == "" and lang == "en"
    assert czech == ("Kačeří příběhy", "My z Kačerova")
    assert year == 1987


def test_resolve_english_show_without_czech_titles():
    entry = {"id": 2, "name": "The Office", "original_name": "The Office",
             "original_language": "en"}
    disp, orig, lang, czech, year = asyncio.run(tmdb._resolve(_Client([]), entry, "tv"))
    assert disp == "The Office" and orig == "" and lang == "en" and czech == ()
    assert year == 0  # no air date in the entry


class _RoutedClient:
    """Serves alternative_titles and translations from separate payloads."""

    def __init__(self, alt_titles, translations):
        self._alt, self._tr = alt_titles, translations

    async def get(self, url, params=None):
        if url.endswith("/translations"):
            return _Resp({"translations": self._tr})
        return _Resp({"results": self._alt})


def test_resolve_picks_czech_translation_name():
    # Red Dwarf has no CZ *alternative* title; its Czech name lives only in the
    # cs translation. An empty or same-as-display translation adds nothing.
    entry = {"id": 326, "name": "Red Dwarf", "original_name": "Red Dwarf",
             "original_language": "en", "first_air_date": "1988-02-15"}
    client = _RoutedClient([], [
        {"iso_639_1": "cs", "iso_3166_1": "CZ", "data": {"name": "Červený trpaslík"}},
        {"iso_639_1": "sk", "iso_3166_1": "SK", "data": {"name": "Červený trpaslík"}},
        {"iso_639_1": "de", "iso_3166_1": "DE", "data": {"name": "Red Dwarf DE"}},
    ])
    disp, orig, lang, czech, year = asyncio.run(tmdb._resolve(client, entry, "tv"))
    assert disp == "Red Dwarf" and czech == ("Červený trpaslík",)

    entry = {"id": 615, "name": "Futurama", "original_name": "Futurama",
             "original_language": "en"}
    client = _RoutedClient([], [{"iso_639_1": "cs", "data": {"name": ""}},
                                {"iso_639_1": "cs", "data": {"name": "Futurama"}}])
    assert asyncio.run(tmdb._resolve(client, entry, "tv"))[3] == ()


def test_resolve_merges_czech_alt_titles_and_translation():
    entry = {"id": 720, "name": "DuckTales", "original_name": "DuckTales",
             "original_language": "en"}
    client = _RoutedClient(
        [{"iso_3166_1": "CZ", "title": "Kačeří příběhy"}, {"iso_3166_1": "CZ", "title": "My z Kačerova"}],
        [{"iso_639_1": "cs", "data": {"name": "Kačeří příběhy"}}])
    assert asyncio.run(tmdb._resolve(client, entry, "tv"))[3] == \
        ("Kačeří příběhy", "My z Kačerova")


def test_resolve_movie_uses_translation_title():
    entry = {"id": 9, "title": "The Shawshank Redemption",
             "original_title": "The Shawshank Redemption", "original_language": "en"}
    client = _RoutedClient([], [{"iso_639_1": "cs", "data": {"title": "Vykoupení z věznice Shawshank"}}])
    assert asyncio.run(tmdb._resolve(client, entry, "movie"))[3] == \
        ("Vykoupení z věznice Shawshank",)


class _RecordingClient:
    """Stands in for httpx.AsyncClient; answers by URL and records every call."""

    calls: list[str] = []

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, params=None):
        _RecordingClient.calls.append(url)
        if url.endswith("/movie/862"):
            return _Resp({"id": 862, "title": "Toy Story", "original_title": "Toy Story",
                          "original_language": "en", "release_date": "1995-10-30", "runtime": 81})
        if url.endswith("/find/tt0114709"):
            return _Resp({"movie_results": [{"id": 862, "title": "Toy Story"}]})
        return _Resp({"results": [], "translations": []})


def test_runtime_reuses_the_id_lookup(monkeypatch):
    """lookup_by_id already fetched the movie details for this search: the
    runtime check must not ask TMDB for them a second time."""
    from app import tmdb
    monkeypatch.setattr(tmdb.httpx, "AsyncClient", _RecordingClient)
    monkeypatch.setattr(tmdb, "_cache", {})
    monkeypatch.setattr(tmdb, "_resolved", {})
    monkeypatch.setattr(tmdb, "_runtime_cache", {})
    _RecordingClient.calls = []
    asyncio.run(tmdb.lookup_by_id("tok", "movie", tmdbid="862"))
    before = list(_RecordingClient.calls)
    assert asyncio.run(tmdb.runtime("tok", "movie", tmdbid="862")) == 81
    assert _RecordingClient.calls == before


def test_runtime_resolves_an_imdb_id_once(monkeypatch):
    """An IMDb-only search: the TMDB id found by lookup_by_id is reused, so the
    runtime check needs neither /find nor the details again."""
    from app import tmdb
    monkeypatch.setattr(tmdb.httpx, "AsyncClient", _RecordingClient)
    monkeypatch.setattr(tmdb, "_cache", {})
    monkeypatch.setattr(tmdb, "_resolved", {})
    monkeypatch.setattr(tmdb, "_runtime_cache", {})
    _RecordingClient.calls = []
    asyncio.run(tmdb.lookup_by_id("tok", "movie", imdbid="tt0114709"))
    assert asyncio.run(tmdb.runtime("tok", "movie", imdbid="tt0114709")) == 81
    assert sum("/find/" in c for c in _RecordingClient.calls) == 1
    assert sum(c.endswith("/movie/862") for c in _RecordingClient.calls) == 1


class _ShowClient(_RecordingClient):
    """Serves DuckTales (1987), found by TVDB id: seasons 0-2, every season's
    names in one request per language (append_to_response)."""

    fail = False
    drop_season = None

    async def get(self, url, params=None):
        params = dict(params or {})
        _ShowClient.calls.append((url, params))
        if _ShowClient.fail:
            raise tmdb.httpx.ConnectError("down")
        if url.endswith("/find/75931"):
            return _Resp({"tv_results": [{"id": 720, "name": "DuckTales"}]})
        if url.endswith("/tv/720") and "append_to_response" not in params:
            return _Resp({"seasons": [{"season_number": n} for n in (0, 1, 2)]})
        if url.endswith("/tv/720"):
            cs = params.get("language") == "cs-CZ"
            eps = {0: [{"episode_number": 1, "name": "Special"}],
                   1: [{"episode_number": 9, "name": "Pasák" if cs else "Armstrong"},
                       {"episode_number": 1, "name": "Don't Give Up the Ship"}],  # no cs translation
                   2: [{"episode_number": 1, "name": "Tekutá aktiva" if cs else "Liquid Assets"}]}
            body = {}
            for part in params["append_to_response"].split(","):
                n = int(part.split("/")[1])
                if n != _ShowClient.drop_season:
                    body[part] = {"episodes": eps[n]}
            return _Resp(body)
        return _Resp({})


def _fresh_show(monkeypatch, fail=False, drop_season=None):
    monkeypatch.setattr(tmdb.httpx, "AsyncClient", _ShowClient)
    monkeypatch.setattr(tmdb, "_resolved", {})
    monkeypatch.setattr(tmdb, "_show_cache", {})
    _ShowClient.calls, _ShowClient.fail, _ShowClient.drop_season = [], fail, drop_season


def test_show_titles_all_seasons_once(monkeypatch):
    """Every season's names (en + cs), specials left out, in 1 + 2 requests
    after the id is found, and then from the cache."""
    _fresh_show(monkeypatch)
    for _ in range(3):
        names = asyncio.run(tmdb.show_titles("tok", tvdbid="75931"))
    assert names == {(1, 9): ("Armstrong", "Pasák"), (1, 1): ("Don't Give Up the Ship",),
                     (2, 1): ("Liquid Assets", "Tekutá aktiva")}
    assert len(_ShowClient.calls) == 4  # find + season list + en + cs
    appended = [p["append_to_response"] for _, p in _ShowClient.calls if "append_to_response" in p]
    assert appended == ["season/1,season/2"] * 2
    assert (0, 1) in asyncio.run(tmdb.show_titles("tok", tmdbid="720", specials=True))


def test_show_titles_fail_open(monkeypatch):
    _fresh_show(monkeypatch, fail=True)
    assert asyncio.run(tmdb.show_titles("tok", tmdbid="720")) == {}
    assert tmdb._show_cache == {}  # a failure is not remembered
    _fresh_show(monkeypatch, drop_season=2)  # a season missing: the show is unknown
    assert asyncio.run(tmdb.show_titles("tok", tmdbid="720")) == {}
    assert tmdb._show_cache == {}
    assert asyncio.run(tmdb.show_titles("", tvdbid="75931")) == {}
    assert asyncio.run(tmdb.show_titles("tok")) == {}


class _NamesakeClient(_RecordingClient):
    """DuckTales (2017) and its namesakes: DuckTales (1987) under both names,
    an obscure same-named show and a show that merely starts with the name."""

    fail = False

    async def get(self, url, params=None):
        _NamesakeClient.calls.append((url, dict(params or {})))
        if _NamesakeClient.fail and "/search/" in url:
            raise tmdb.httpx.ConnectError("down")
        if url.endswith("/find/330134"):
            return _Resp({"tv_results": [{"id": 72350, "name": "DuckTales", "original_name": "DuckTales",
                                          "original_language": "en", "first_air_date": "2017-08-12"}]})
        if url.endswith("/alternative_titles"):
            return _Resp({"results": [{"iso_3166_1": "CZ", "title": "Kačeří příběhy"}]})
        if url.endswith("/search/tv"):
            cs = params.get("language") == "cs-CZ"
            name = "Kačeří příběhy" if cs else "DuckTales"
            return _Resp({"results": [
                {"id": 72350, "name": name, "original_name": "DuckTales",
                 "first_air_date": "2017-08-12", "vote_count": 329},
                {"id": 720, "name": name, "original_name": "DuckTales",
                 "first_air_date": "1987-09-18", "vote_count": 764},
                {"id": 1, "name": "DuckTales", "original_name": "DuckTales",
                 "first_air_date": "1970-01-01", "vote_count": 3},
                {"id": 2, "name": "DuckTales: Remastered", "original_name": "DuckTales: Remastered",
                 "first_air_date": "2013-08-13", "vote_count": 500},
            ]})
        return _Resp({"results": [], "translations": []})


def _fresh_namesakes(monkeypatch, fail=False):
    monkeypatch.setattr(tmdb.httpx, "AsyncClient", _NamesakeClient)
    for cache in ("_cache", "_resolved", "_namesake_cache"):
        monkeypatch.setattr(tmdb, cache, {})
    _NamesakeClient.calls, _NamesakeClient.fail = [], fail


def test_namesakes_finds_the_other_show_once(monkeypatch):
    _fresh_namesakes(monkeypatch)
    for _ in range(3):
        found = asyncio.run(tmdb.namesakes("tok", tvdbid="330134"))
    assert found == ((720, 1987, ("DuckTales", "Kačeří příběhy")),)
    searches = [p for u, p in _NamesakeClient.calls if u.endswith("/search/tv")]
    assert searches == [{"query": "DuckTales", "language": "en-US"},
                        {"query": "Kačeří příběhy", "language": "cs-CZ"}]
    assert sum("/find/" in u for u, _ in _NamesakeClient.calls) == 1


def test_namesakes_fail_open(monkeypatch):
    _fresh_namesakes(monkeypatch, fail=True)
    assert asyncio.run(tmdb.namesakes("tok", tvdbid="330134")) == ()
    assert tmdb._namesake_cache == {}  # a failure is not remembered
    _NamesakeClient.fail = False
    assert asyncio.run(tmdb.namesakes("tok", tvdbid="330134"))[0][0] == 720
    assert asyncio.run(tmdb.namesakes("", tvdbid="330134")) == ()
    assert asyncio.run(tmdb.namesakes("tok")) == ()
