import json
import time

import pytest

from radClock.core import config
from radClock.core.config import Config, OpenSkyCfg
from radClock.core.fetcher import SourceChoices
from radClock.core.lookup import RETRY_S, TypeLookup
from radClock.core.net import HttpStatus, RateLimited, SourceError
from radClock.core.opensky import (AUTH_COOLDOWN_S, COOLDOWN_S, TOKEN_URL, OpenSkySource, bbox,
                                  credits_per_call, parse_states)

LAT0, LON0 = -23.21, -45.87

# vetor de estado do OpenSky (extended=1): 18 campos, unidades SI
STATE = ["e48f2a", "GLO1234 ", "Brazil", 1793118595, 1793118599, -45.9, -23.1, 3048.0, False,
         128.6, 91.5, -5.08, None, 3100.0, "7700", False, 0, 4]
DOC = {"time": 1793118600, "states": [STATE,
                                      ["e40000", None, "Brazil", None, 1793118599, None, None,
                                       None, True, 0, None, None, None, None, None, False, 0, 0]]}


def test_parse_states_converts_units():
    ac, = parse_states(DOC)                            # o segundo não tem posição
    assert (ac.hex, ac.callsign, ac.squawk, ac.category) == ("e48f2a", "GLO1234", "7700", "A3")
    assert ac.alt_ft == 10000 and ac.emergency
    assert ac.gs_kt == pytest.approx(250, abs=0.1)
    assert ac.vrate_fpm == pytest.approx(-1000, abs=1)
    assert ac.track == 91.5 and ac.seen_pos_s == 5
    ground = parse_states({"time": 10, "states": [STATE[:7] + [None, True] + STATE[9:]]})
    assert ground[0].on_ground and ground[0].alt_ft == 0
    assert parse_states({"time": 1, "states": None}) == []
    with pytest.raises(SourceError):
        parse_states([])


def test_credits_grow_with_radius_and_latitude():
    for lat in (0.0, LAT0, -30.0):
        assert credits_per_call(bbox(lat, LON0, 250)) == 1
    assert credits_per_call(bbox(45.0, LON0, 250)) == 2   # grau de longitude menor
    assert credits_per_call(bbox(45.0, LON0, 215)) == 1
    assert credits_per_call(bbox(LAT0, LON0, 400)) == 2
    lamin, lomin, lamax, lomax = bbox(LAT0, LON0, 250)
    assert lamax - lamin == pytest.approx(2 * 250 / 110.574)
    assert lamin < LAT0 < lamax and lomin < LON0 < lomax


class Server:
    """OpenSky falso: token e states, com falhas programadas."""

    def __init__(self):
        self.now = 1000.0
        self.calls: list[tuple[str, dict]] = []
        self.states_errors: list[Exception] = []
        self.token_error: Exception | None = None
        self.tokens = 0

    def clock(self):
        return self.now

    def request(self, url, data=None, headers=None, timeout=10.0):
        self.calls.append((url, headers or {}))
        if url == TOKEN_URL:
            assert b"grant_type=client_credentials" in data and b"client_id=id" in data
            if self.token_error:
                raise self.token_error
            self.tokens += 1
            return json.dumps({"access_token": f"tok{self.tokens}", "expires_in": 1800}).encode(), {}
        if self.states_errors:
            raise self.states_errors.pop(0)
        return json.dumps(DOC).encode(), {"X-Rate-Limit-Remaining": "3999"}

    def source(self, **kw):
        cfg = OpenSkyCfg(client_id="id", client_secret="sec", **kw)
        return OpenSkySource(cfg, LAT0, LON0, 300, clock=self.clock, request=self.request)


def test_token_is_reused_until_it_expires():
    srv = Server()
    src = srv.source()
    assert "lamin=" in src.url and "extended=1" in src.url and src.credits == 1
    assert len(src.fetch()) == 1 and src.remaining == 3999
    src.fetch()
    assert srv.tokens == 1
    assert srv.calls[-1][1]["Authorization"] == "Bearer tok1"
    srv.now += 1800 - 30                               # margem de 1 min: renova antes
    src.fetch()
    assert srv.tokens == 2 and srv.calls[-1][1]["Authorization"] == "Bearer tok2"


def test_401_gets_a_new_token_and_retries_once():
    srv = Server()
    src = srv.source()
    src.fetch()
    srv.states_errors = [HttpStatus(401)]
    assert len(src.fetch()) == 1 and srv.tokens == 2


def test_429_waits_the_time_opensky_says():
    srv = Server()
    src = srv.source()
    srv.states_errors = [RateLimited(7200)]
    with pytest.raises(RateLimited):
        src.fetch()
    assert src.remaining == 0
    n = len(srv.calls)
    srv.now += 7100
    with pytest.raises(SourceError, match="em espera"):
        src.fetch()                                    # sem ir à rede
    assert len(srv.calls) == n
    srv.now += 101
    assert len(src.fetch()) == 1


def test_one_network_blip_is_tolerated_two_pause():
    srv = Server()
    src = srv.source()
    srv.states_errors = [SourceError("timeout")]
    with pytest.raises(SourceError):
        src.fetch()
    assert len(src.fetch()) == 1                       # isolada: não pausa
    srv.states_errors = [SourceError("timeout"), SourceError("timeout")]
    for _ in range(2):
        with pytest.raises(SourceError):
            src.fetch()
    with pytest.raises(SourceError, match="em espera"):
        src.fetch()
    srv.now += COOLDOWN_S + 1
    assert len(src.fetch()) == 1


def test_rejected_credentials_pause_for_an_hour():
    srv = Server()
    srv.token_error = HttpStatus(401)
    src = srv.source()
    with pytest.raises(SourceError, match="credenciais"):
        src.fetch()
    srv.token_error = None
    srv.now += AUTH_COOLDOWN_S - 10
    with pytest.raises(SourceError, match="em espera"):
        src.fetch()


def test_credentials_file(tmp_path):
    f = tmp_path / "credentials.json"
    f.write_text(json.dumps({"clientId": "id", "clientSecret": "sec"}))
    srv = Server()
    src = OpenSkySource(OpenSkyCfg(credentials=str(f)), LAT0, LON0, 300,
                        clock=srv.clock, request=srv.request)
    src.fetch()
    assert srv.tokens == 1


def test_choices_offer_opensky_only_with_credentials_and_fall_back():
    cfg = Config()
    assert "opensky" not in SourceChoices(cfg).names
    cfg.opensky = OpenSkyCfg(client_id="id", client_secret="sec")
    ch = SourceChoices(cfg)
    assert ch.names[:3] == ["readsb", "opensky", "adsb.lol"]
    f = ch.fetcher("opensky")
    assert f.primary is ch.opensky() and f.fallback is ch.api("adsb.lol")
    assert f.interval_s == 30.0
    assert ch.fetcher("opensky").primary is f.primary  # mesmo token e espera
    cfg.opensky.fallback = ""
    assert SourceChoices(cfg).fetcher("opensky").fallback is None


def test_fetcher_publishes_quota_and_uses_fallback_when_opensky_fails():
    from radClock.core.fetcher import Fetcher
    from radClock.core.model import Aircraft

    class Free:
        name = "ADSB.LOL"

        def fetch(self):
            return [Aircraft(hex="abc", lat=0.0, lon=0.0)]

    srv = Server()
    src = srv.source()
    f = Fetcher(src, Free(), 0.01, 0.01)
    f.start()
    try:
        end = time.monotonic() + 2
        while f.snapshot().source != "OPENSKY" and time.monotonic() < end:
            time.sleep(0.01)
        assert f.snapshot().quota == 3999
        src._token_expires_at = 0
        srv.token_error = HttpStatus(401)
        while f.snapshot().source != "ADSB.LOL" and time.monotonic() < end + 2:
            time.sleep(0.01)
        snap = f.snapshot()
        assert snap.source == "ADSB.LOL" and snap.quota is None and "OPENSKY" in snap.error
    finally:
        f.stop()


@pytest.mark.parametrize("body, msg", [
    ("[source]\nkind = 'opensky'\n", "exige"),
    ("[opensky]\ninterval_s = 1\n", "interval_s"),
    ("[opensky]\nfallback = 'airplanes.live'\n", "fallback"),
    ("[opensky]\ncredentials = '/nao/existe.json'\n", "não existe"),
    ("[opensky]\nfallback = 'custom'\n", "custom"),
])
def test_config_validation(tmp_path, body, msg):
    f = tmp_path / "c.toml"
    f.write_text(body)
    with pytest.raises(ValueError, match=msg):
        config.load(f)


def test_config_opensky_source(tmp_path):
    f = tmp_path / "c.toml"
    f.write_text("[source]\nkind = 'opensky'\n[opensky]\nclient_id = 'a'\nclient_secret = 'b'\n")
    cfg = config.load(f)
    assert SourceChoices(cfg).default == "opensky"


# ------------------------------------------------------------- tipo (adsbdb)
def wait(cond):
    end = time.monotonic() + 2
    while not cond() and time.monotonic() < end:
        time.sleep(0.01)
    return cond()


class Bases:
    """adsb.lol, hexdb.io e adsbdb falsos: cada um conhece só algumas aeronaves."""

    def __init__(self, lol=None, hexdb=None, adsbdb=None, down=()):
        self.lol, self.hexdb, self.adsbdb, self.down = lol or {}, hexdb or {}, adsbdb or {}, down
        self.urls = []

    def get(self, url):
        self.urls.append(url)
        hx = url.rsplit("/", 1)[1]
        for host in self.down:
            if host in url:
                raise SourceError("timeout")
        if "adsb.lol" in url:
            t = self.lol.get(hx)
            return {"ac": [{"t": t[0], "r": t[1]}] if t else []}
        if "hexdb.io" in url:
            if hx not in self.hexdb:
                raise HttpStatus(404)
            return {"ICAOTypeCode": self.hexdb[hx][0], "Registration": self.hexdb[hx][1]}
        if hx not in self.adsbdb:
            raise HttpStatus(404)
        return {"response": {"aircraft": {"icao_type": self.adsbdb[hx][0],
                                          "registration": self.adsbdb[hx][1]}}}


def test_lookup_chain_first_with_type_wins_and_caches():
    b = Bases(lol={"e49ec9": ("A20N", "PR-YSR")}, hexdb={"e48f2a": ("B738", "PR-GXA")},
              adsbdb={"e40001": ("e195", "")})
    lk = TypeLookup(get=b.get)
    try:
        for hx, want in (("e49ec9", ("A20N", "PR-YSR")), ("e48f2a", ("B738", "PR-GXA")),
                         ("e40001", ("E195", "")), ("e40000", ("", ""))):
            assert lk.get(hx) is None                  # dispara em segundo plano
            assert wait(lambda: lk.get(hx) == want), hx
        n = len(b.urls)
        assert sum("adsb.lol" in u for u in b.urls) == 4
        assert not any("e49ec9" in u for u in b.urls if "adsb.lol" not in u)   # parou no 1º
        for hx in ("e49ec9", "e40000"):
            lk.get(hx)
        assert len(b.urls) == n                        # tudo do cache, inclusive o desconhecido
        assert lk.get("~abc123") == ("", "")           # não-ICAO: nem tenta
    finally:
        lk.stop()


def test_lookup_retries_only_after_a_while_when_a_base_is_down():
    now = [0.0]
    b = Bases(adsbdb={"e48f2a": ("A320", "")}, down=("adsbdb",))
    lk = TypeLookup(get=b.get, clock=lambda: now[0])
    try:
        lk.get("e48f2a")
        assert wait(lambda: "e48f2a" in lk._failed_at)  # ninguém achou e uma caiu
        n = len(b.urls)
        assert lk.get("e48f2a") is None and len(b.urls) == n
        b.down = ()
        now[0] = RETRY_S + 1
        lk.get("e48f2a")
        assert wait(lambda: lk.get("e48f2a") == ("A320", ""))
    finally:
        lk.stop()


def test_fill_type_replaces_only_the_selected():
    from radClock.core.fetcher import Snapshot
    from radClock.core.model import Aircraft
    from radClock.ui.app import _fill_type

    class Fake:
        def get(self, hx):
            return ("B738", "PR-GXA")

    a, b = Aircraft(hex="a", lat=0, lon=0), Aircraft(hex="b", lat=0, lon=0, reg="PP-XXX")
    snap = Snapshot([a, b], "OPENSKY", 5.0)
    out = _fill_type(snap, "b", Fake())
    assert out.aircraft[0] is a and out.aircraft[1].type == "B738"
    assert out.aircraft[1].reg == "PP-XXX" and out.updated == 5.0   # matrícula do dado vale
    assert _fill_type(snap, None, Fake()) is snap
    assert _fill_type(out, "b", Fake()) is out                      # já tem tipo
