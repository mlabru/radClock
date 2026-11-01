import pytest

from radClock.core import config
from radClock.core.config import ApiCfg
from radClock.core.net import RateLimited, SourceError
from radClock.core.sources import ApiSource

DOC = {"now": 1, "ac": [{"hex": "e49eed", "lat": -23.0, "lon": -45.9}]}


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def make(get, clock=None, **kw):
    return ApiSource(ApiCfg(**kw), -23.21, -45.87, 300.0, clock=clock or Clock(), get=get)


def test_provider_urls():
    lol = make(None, provider="adsb.lol")
    assert lol.url == "https://api.adsb.lol/v2/point/-23.2100/-45.8700/162"
    fi = make(None)                                       # adsb.fi é o padrão
    assert fi.url == "https://opendata.adsb.fi/api/v2/lat/-23.2100/lon/-45.8700/dist/162"
    assert fi.name == "ADSB.FI"


def test_radius_is_capped():
    src = ApiSource(ApiCfg(), 0, 0, 1000.0, get=None)
    assert src.url.endswith("/250")


def test_custom_url():
    src = make(None, provider="custom", url="http://x/{lat:.1f},{lon:.1f},{radius_nm:.0f}")
    assert src.url == "http://x/-23.2,-45.9,162"


def test_backoff_on_429_then_recovers():
    clock, calls = Clock(), []

    def get(url):
        calls.append(clock.t)
        if len(calls) == 1:
            raise RateLimited()
        return DOC

    src = make(get, clock)
    with pytest.raises(RateLimited):
        src.fetch()
    clock.t += 10                       # ainda em espera: nem tenta
    with pytest.raises(SourceError, match="espera"):
        src.fetch()
    assert len(calls) == 1
    clock.t += 25                       # passou dos 30 s
    assert len(src.fetch()) == 1
    assert src._penalty == ApiSource.BACKOFF_MIN_S   # sucesso zera a penalidade


def test_two_connection_failures_back_off_and_penalty_doubles():
    clock = Clock()

    def get(url):
        raise SourceError("reset")

    src = make(get, clock)
    with pytest.raises(SourceError):
        src.fetch()                     # 1ª falha: sem espera
    assert src._cooldown_until == 0
    with pytest.raises(SourceError):
        src.fetch()                     # 2ª: espera 30 s
    assert src._cooldown_until == clock.t + 30
    clock.t += 31
    with pytest.raises(SourceError):
        src.fetch()                     # continua falhando: agora 60 s
    assert src._cooldown_until == clock.t + 60


@pytest.mark.parametrize("bad, msg", [
    ({"source": {"kind": "sdr"}}, "kind"),
    ({"api": {"provider": "airplanes.live"}}, "provider desconhecido"),
    ({"api": {"provider": "custom"}}, "exige url"),
    ({"api": {"url": "http://x"}}, "custom"),
    ({"api": {"interval_s": 0.2}}, "limite"),
    ({"echo": {"opacity": 2}}, "opacity"),
])
def test_config_validation(tmp_path, bad, msg):
    lines = []
    for sec, kv in bad.items():
        lines.append(f"[{sec}]")
        for k, v in kv.items():
            lines.append(f"{k} = {v!r}" if not isinstance(v, str) else f'{k} = "{v}"')
    p = tmp_path / "c.toml"
    p.write_text("\n".join(lines))
    with pytest.raises(ValueError, match=msg):
        config.load(p)
