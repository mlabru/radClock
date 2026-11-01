import time

import pytest

from radClock.core import config
from radClock.core.config import WeatherCfg
from radClock.core.net import SourceError
from radClock.core.weather import (METAR_OLD_S, MODEL_PLACE, Weather, WeatherFetcher,
                                  metar_condition, parse_metar, parse_open_meteo,
                                  relative_humidity)

DOC = {"current": {"time": "2026-10-27T12:45", "interval": 900, "temperature_2m": 24.4,
                   "relative_humidity_2m": 65, "wind_speed_10m": 8.3,
                   "wind_direction_10m": 124, "weather_code": 3}}

# resposta real do AWC (aviationweather.gov/api/data/metar?ids=SBSJ&format=json)
METAR_DOC = [{
    "icaoId": "SBSJ", "obsTime": 1793145600, "temp": 17, "dewp": 13, "wdir": 120, "wspd": 8,
    "visib": "6+", "altim": 1023, "metarType": "METAR",
    "rawOb": "METAR SBSJ 280000Z 12008KT 9999 BKN030 17/13 Q1023",
    "name": "Sao Jose dos Campos Arpt, SP, BR", "cover": "BKN",
    "clouds": [{"cover": "BKN", "base": 3000}],
}]
OBS = 1793145600


def wx(**kw):
    base = dict(place="SBSJ", credit="NOAA AWC", temp_c=24.4, hum_pct=65.0, wind_kt=8.3,
                gust_kt=None, wind_dir=124.0, condition="NUBLADO", observed=None, at=0.0)
    return Weather(**{**base, **kw})


def test_parse_open_meteo_is_labelled_as_model():
    w = parse_open_meteo(DOC, 5.0)
    assert (w.temp_c, w.hum_pct, w.wind_kt, w.wind_dir, w.at) == (24.4, 65, 8.3, 124, 5.0)
    assert (w.place, w.credit, w.condition, w.observed) == (MODEL_PLACE, "Open-Meteo", "NUBLADO", None)
    assert w.lines(0.0) == ["MODELO", "120° 8 kt", "NUBLADO", "24°C  65%"]


def test_parse_metar():
    w = parse_metar(METAR_DOC, "SBSJ", 5.0)
    assert (w.place, w.credit, w.temp_c, w.wind_dir, w.wind_kt) == ("SBSJ", "NOAA AWC", 17, 120, 8)
    assert w.hum_pct == pytest.approx(77, abs=1)            # 17/13: ~77 %
    assert w.observed == OBS and w.condition == "NUBLADO"
    assert w.lines(OBS + 25 * 60) == ["SBSJ há 25 min", "120° 8 kt", "NUBLADO", "17°C  77%"]


def test_metar_variable_wind_and_gust():
    doc = [{**METAR_DOC[0], "wdir": "VRB", "wspd": 5}]
    assert parse_metar(doc, "SBSJ", 0.0).wind == "VRB 5 kt"
    doc = [{**METAR_DOC[0], "wdir": 280, "wspd": 15, "wgst": 27}]
    assert parse_metar(doc, "SBSJ", 0.0).wind == "280° 15 G27 kt"


@pytest.mark.parametrize("doc", [[], [{"icaoId": "SBGR", "obsTime": 1}], {"icaoId": "SBSJ"},
                                 [{"icaoId": "SBSJ"}]])
def test_parse_metar_rejects_missing_or_bad(doc):
    with pytest.raises(SourceError):
        parse_metar(doc, "SBSJ", 0.0)


@pytest.mark.parametrize("wx_string, cover, text", [
    (None, "BKN", "NUBLADO"), (None, "CAVOK", "CAVOK"), (None, "OVC", "ENCOBERTO"),
    (None, None, ""), ("-RA", "BKN", "CHUVA FRACA"), ("RA BR", None, "CHUVA"),
    ("+RA", None, "CHUVA FORTE"), ("-DZ", None, "GAROA FRACA"), ("TSRA", None, "TROVOADA"),
    ("+TSRAGR", None, "TROV+GRANIZO"), ("VCTS", None, "TROV. PRÓX."), ("VCSH", None, "PANC. PRÓX."),
    ("-SHRA", None, "PANC. FRACAS"), ("SHRA", None, "PANCADAS"), ("BR", None, "NÉVOA"),
    ("FG", None, "NEVOEIRO"), ("HZ", None, "NÉVOA SECA"), ("FU", None, "FUMAÇA"),
    ("FZDZ", None, "GAROA GELADA"), ("SS", None, "SS")])
def test_metar_condition(wx_string, cover, text):
    assert metar_condition(wx_string, cover) == text


def test_relative_humidity():
    assert relative_humidity(17, 13) == pytest.approx(77, abs=1)
    assert relative_humidity(20, 20) == pytest.approx(100)
    assert relative_humidity(None, 13) is None


@pytest.mark.parametrize("kw, wind", [
    ({}, "120° 8 kt"), ({"wind_dir": 356.0}, "360° 8 kt"), ({"wind_dir": 2.0}, "360° 8 kt"),
    ({"wind_kt": 0.4}, "CALMO"), ({"wind_dir": None}, "VRB 8 kt"), ({"wind_kt": None}, ""),
    ({"wind_kt": 23.6, "wind_dir": 275.0}, "280° 24 kt"),
    ({"gust_kt": 18.0}, "120° 8 G18 kt"), ({"gust_kt": 8.0}, "120° 8 kt")])
def test_wind_with_separate_fields(kw, wind):
    assert wx(**kw).wind == wind


def test_metar_age_header_and_old_flag():
    w = wx(observed=OBS)
    assert w.header(OBS + 59 * 60) == "SBSJ há 59 min"
    assert w.header(OBS + 65 * 60) == "SBSJ há 1h05"
    assert w.header(OBS - 10) == "SBSJ 00:00Z"               # Pi sem NTP: relógio atrás
    assert not w.is_old(OBS + METAR_OLD_S)
    assert w.is_old(OBS + METAR_OLD_S + 1)
    assert not wx(place=MODEL_PLACE).is_old(1e12)            # modelo não tem idade


def test_missing_fields_are_dropped_from_lines():
    assert wx(hum_pct=None, wind_kt=None, condition="").lines(0.0) == ["SBSJ", "24°C"]
    assert wx(temp_c=None).lines(0.0)[-1] == "65%"


def test_weather_corner_stays_outside_the_outer_ring(monkeypatch):
    """As linhas mais largas possíveis não podem entrar no círculo do escopo."""
    monkeypatch.setenv("SDL_VIDEODRIVER", "dummy")
    import pygame

    from radClock.core.acdb import AircraftDB
    from radClock.core.alarm import AlarmClock
    from radClock.core.config import Config
    from radClock.core.fetcher import Snapshot
    from radClock.core.projection import AzimuthalEquidistant
    from radClock.core.tracks import Tracker
    from radClock.core.weather import COVER, NEARBY, OBSCURATION, WMO, WMO_MAX_LEN
    from radClock.ui.render import Renderer

    texts = [*WMO.values(), *COVER.values(), *OBSCURATION.values(), *NEARBY.values(),
             "PANC. FRACAS"]
    assert max(len(t) for t in texts) <= WMO_MAX_LEN
    widest = [t for t in texts if len(t) == WMO_MAX_LEN]
    pygame.init()
    try:
        for size in ((800, 480), (1280, 720)):
            screen = pygame.display.set_mode(size)
            cfg = Config()
            proj = AzimuthalEquidistant(cfg.station.lat, cfg.station.lon)
            r = Renderer(screen, cfg, proj, [], Tracker(proj, cfg.radar),
                         AircraftDB("/nao/existe.db"), AlarmClock("UTC"))
            # "SBSJ há 59 min" e "360° 99 G99 kt" são as linhas mais largas
            observed = time.time() - 59 * 60 - 30
            for condition in widest:
                r.draw(Snapshot(), None, 50.0, 1.0,
                       weather=wx(temp_c=-10.4, hum_pct=100.0, wind_kt=99, gust_kt=99.4,
                                  wind_dir=360, condition=condition, observed=observed))
                y = 4
                for g in r._wx_surfs:
                    right, bottom = 6 + g.get_width(), y + g.get_height()
                    assert (r.cx - right) ** 2 + (r.cy - bottom) ** 2 > r.r_px ** 2, (size, condition)
                    y = bottom
    finally:
        pygame.quit()


@pytest.mark.parametrize("doc", [{}, {"current": None}, {"current": {"temperature_2m": None}},
                                 {"current": {"temperature_2m": "x"}}, []])
def test_parse_open_meteo_rejects_bad_documents(doc):
    with pytest.raises(SourceError):
        parse_open_meteo(doc, 0.0)


def test_fetcher_open_meteo_url_and_max_age():
    urls = []

    def get(url):
        urls.append(url)
        return DOC

    f = WeatherFetcher(WeatherCfg(max_age_s=3600), -23.21, -45.87, get=get)
    assert f.current() is None
    f.fetch_once()
    assert "latitude=-23.2100" in urls[0] and "wind_speed_unit=kn" in urls[0]
    assert "weather_code" in urls[0]
    assert f.current().temp_c == 24.4
    f._latest = wx(at=time.monotonic() - 3601)            # sem rede há mais de 1 h
    assert f.current() is None


def test_fetcher_metar_url():
    urls = []

    def get(url):
        urls.append(url)
        return METAR_DOC

    f = WeatherFetcher(WeatherCfg(icao="SBSJ"), -23.21, -45.87, get=get)
    f.fetch_once()
    assert urls == ["https://aviationweather.gov/api/data/metar?ids=SBSJ&format=json"]
    assert f.current().place == "SBSJ"


def test_metar_json_empty_body_means_no_metar(monkeypatch):
    """O AWC responde 204 sem corpo para aeródromo sem METAR."""
    from radClock.core import weather

    monkeypatch.setattr(weather, "http_get", lambda url: b"")
    assert weather.metar_json("x") == []
    monkeypatch.setattr(weather, "http_get", lambda url: b"<html>")
    with pytest.raises(SourceError):
        weather.metar_json("x")


def test_fetcher_thread_survives_errors():
    calls = []

    def get(url):
        calls.append(1)
        if len(calls) == 1:
            raise KeyError("bug")
        if len(calls) == 2:
            raise SourceError("timeout")
        return DOC

    f = WeatherFetcher(WeatherCfg(), 0.0, 0.0, get=get)
    f.cfg = WeatherCfg(refresh_s=0.01, max_age_s=10)   # sem passar pela validação
    f.start()
    try:
        end = time.monotonic() + 2
        while f.current() is None and time.monotonic() < end:
            time.sleep(0.01)
        assert f.current() is not None and f.is_alive()
    finally:
        f.stop()


@pytest.mark.parametrize("body", ["refresh_s = 60", "max_age_s = 100", "chave = 1",
                                  'icao = "sbsj"', 'icao = "SBS"', 'icao = "SB-J"'])
def test_config_validation(tmp_path, body):
    f = tmp_path / "c.toml"
    f.write_text(f"[weather]\n{body}\n")
    with pytest.raises(ValueError):
        config.load(f)


def test_config_accepts_icao(tmp_path):
    f = tmp_path / "c.toml"
    f.write_text('[weather]\nicao = "SBSJ"\n')
    assert config.load(f).weather.icao == "SBSJ"
