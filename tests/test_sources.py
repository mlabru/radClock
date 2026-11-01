import json
import time

import pytest

from radClock.core.config import SourceCfg
from radClock.core.net import SourceError
from radClock.core.sources import ReadsbSource


def _write(path, now):
    path.write_text(json.dumps({"now": now, "aircraft": [{"hex": "a", "lat": 0, "lon": 0}]}))


def test_fresh_file(tmp_path):
    f = tmp_path / "aircraft.json"
    _write(f, time.time())
    assert len(ReadsbSource(SourceCfg(path=str(f))).fetch()) == 1


def test_frozen_receiver_is_an_error(tmp_path):
    f = tmp_path / "aircraft.json"
    _write(f, time.time() - 60)
    with pytest.raises(SourceError, match="parado"):
        ReadsbSource(SourceCfg(path=str(f))).fetch()
    # com a checagem desligada o mesmo arquivo é aceito
    assert ReadsbSource(SourceCfg(path=str(f), max_age_s=0)).fetch()


def test_missing_and_corrupt_file(tmp_path):
    with pytest.raises(SourceError):
        ReadsbSource(SourceCfg(path=str(tmp_path / "nope.json"))).fetch()
    bad = tmp_path / "bad.json"
    bad.write_text("{trunc")
    with pytest.raises(SourceError):
        ReadsbSource(SourceCfg(path=str(bad))).fetch()


def test_choices_cycle_and_default():
    from radClock.core.config import ApiCfg, Config
    from radClock.core.fetcher import SourceChoices
    ch = SourceChoices(Config())
    assert ch.names == ["readsb", "adsb.lol", "adsb.fi"] and ch.default == "adsb.fi"
    assert [ch.next("readsb"), ch.next("adsb.fi"), ch.next("sumiu")] == ["adsb.lol", "readsb",
                                                                         "readsb"]
    custom = Config(source=SourceCfg(kind="api"),
                    api=ApiCfg(provider="custom", url="http://x/{lat}/{lon}/{radius_nm}"))
    ch = SourceChoices(custom)
    assert ch.names[-1] == "custom" and ch.default == "custom"
    assert ch.api("custom").url.startswith("http://x/")
    assert ch.api("adsb.lol").url.startswith("https://api.adsb.lol/")   # url só no custom


def test_choices_keep_backoff_and_fallback():
    from radClock.core.config import ApiCfg, Config
    from radClock.core.fetcher import SourceChoices
    ch = SourceChoices(Config(api=ApiCfg(provider="adsb.fi", fallback=True)))
    assert ch.api("adsb.lol") is ch.api("adsb.lol")       # mesma instância: backoff mantido
    f = ch.fetcher("readsb")
    assert f.fallback is ch.api("adsb.fi")                # reserva = API do TOML
    assert f.snapshot().source == "READSB" and not f.snapshot().updated
    g = ch.fetcher("adsb.lol")
    assert g.primary is ch.api("adsb.lol") and g.fallback is None
    assert g.snapshot().interval_s == 5.0
