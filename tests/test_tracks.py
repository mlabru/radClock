import pytest

from radClock.core import config
from radClock.core.config import InhibitCfg, RadarCfg
from radClock.core.fetcher import Snapshot
from radClock.core.model import Aircraft
from radClock.core.projection import AzimuthalEquidistant
from radClock.core.tracks import CONFLICT_QUIET_S, Tracker, extrapolate

LAT0, LON0 = -23.21, -45.87
KM_LAT = 111.195            # km por grau de latitude na esfera da projeção
V_KMS = 300 * 1.852 / 3600.0


def ac(hx, n_km=0.0, e_km=0.0, alt=10000, trk=90.0, gs=300.0, vs=0.0, **kw):
    # pontos sobre o mesmo paralelo/meridiano perto da estação: aproximação plana basta
    return Aircraft(hex=hx, lat=LAT0 + n_km / KM_LAT,
                    lon=LON0 + e_km / (KM_LAT * 0.9190), alt_ft=alt,
                    track=trk, gs_kt=gs, vrate_fpm=vs, **kw)


def run(*acs, updated=100.0, **cfg):
    t = Tracker(AzimuthalEquidistant(LAT0, LON0), RadarCfg(**cfg))
    t.update(Snapshot(list(acs), "T", updated))
    return t


def test_head_on_conflict_time():
    # 40 km entre elas, fechando a 2*V: perde 5,6 km após (40 - 5,6) / (2V) s
    t = run(ac("a", e_km=-20, trk=90), ac("b", e_km=20, trk=270, alt=10500))
    assert t.pairs == [("a", "b")]
    assert t.conflicts["a"].other == "b" and t.conflicts["b"].other == "a"
    assert t.conflicts["a"].t_s == pytest.approx((40 - 5.6) / (2 * V_KMS), rel=0.02)


def test_beyond_horizon_is_ignored():
    t = run(ac("a", e_km=-20, trk=90), ac("b", e_km=20, trk=270), conflict_s=60)
    assert not t.conflicts


def test_vertical_separation_avoids_conflict():
    t = run(ac("a", e_km=-20, trk=90), ac("b", e_km=20, trk=270, alt=12000))
    assert not t.conflicts


def test_diverging_is_not_conflict():
    t = run(ac("a", e_km=-10, trk=270), ac("b", e_km=10, trk=90))
    assert not t.conflicts


def test_climb_into_level_traffic():
    # mesma posição/velocidade horizontal (formação), 3000 ft abaixo subindo 2000 ft/min:
    # entra nos 1000 ft após 60 s
    t = run(ac("a", alt=7000, vs=2000), ac("b", e_km=1, alt=10000))
    assert t.conflicts["a"].t_s == pytest.approx(60, abs=1)


def test_current_loss_is_now():
    t = run(ac("a"), ac("b", e_km=2, alt=10300))
    assert t.conflicts["a"].t_s == 0


def test_ground_stale_and_unknown_altitude_excluded():
    base = ac("a", e_km=-1)
    assert not run(base, ac("g", e_km=1, on_ground=True)).conflicts
    assert not run(base, ac("s", e_km=1, seen_pos_s=30.0)).conflicts
    assert not run(base, ac("u", e_km=1, alt=None)).conflicts


def test_disabled():
    assert not run(ac("a"), ac("b", e_km=1), conflict_s=0).conflicts


def test_trail_keeps_the_longest_choice_of_the_settings_screen():
    """RASTRO 1 min -> 2 min tem de mostrar o histórico na hora: guarda o maior."""
    t = Tracker(AzimuthalEquidistant(LAT0, LON0), RadarCfg(trail_s=10))
    assert t.trail_keep_s == 120
    assert Tracker(AzimuthalEquidistant(LAT0, LON0), RadarCfg(trail_s=300)).trail_keep_s == 300


def test_trail_accumulates_and_expires():
    t = Tracker(AzimuthalEquidistant(LAT0, LON0), RadarCfg())
    t.trail_keep_s = 10                            # poda curta, para o teste ser rápido
    for k in range(15):
        t.update(Snapshot([ac("a", e_km=k * 0.15)], "T", 100.0 + k))
    pts = t.trails["a"]
    assert 10 <= len(pts) <= 11                    # só os últimos 10 s
    assert pts[-1][1] == pytest.approx(14 * 0.15, rel=0.01)
    # mesmo snapshot outra vez não duplica ponto
    t.update(Snapshot([ac("a")], "T", 114.0))
    assert len(t.trails["a"]) == len(pts)
    # sumiu do tráfego: o rastro expira junto
    t.update(Snapshot([], "T", 130.0))
    assert "a" not in t.trails


@pytest.mark.parametrize("line", ["trail_s = -1", "conflict_s = -5", "conflict_km = 0",
                                  "extrapolate_s = -1", "conflict_volume = 2"])
def test_config_rejects_bad_values(tmp_path, line):
    f = tmp_path / "c.toml"
    f.write_text(f"[radar]\n{line}\n")
    with pytest.raises(ValueError):
        config.load(f)


def test_extrapolate_moves_along_track_and_stops_at_limit():
    a = ac("a", trk=90.0)                       # para leste a 300 kt
    assert extrapolate(a, 1.0, 2.0, 4.0, 10.0) == pytest.approx((1.0 + 4 * V_KMS, 2.0))
    # passou do limite: fica onde estaria no limite, sem voltar
    assert extrapolate(a, 1.0, 2.0, 30.0, 10.0) == pytest.approx((1.0 + 10 * V_KMS, 2.0))
    assert extrapolate(ac("n", trk=0.0), 0.0, 0.0, 2.0, 10.0) == pytest.approx((0.0, 2 * V_KMS))
    # desligado, sem velocidade ou sem rumo: não anda
    assert extrapolate(a, 1.0, 2.0, 4.0, 0.0) == (1.0, 2.0)
    assert extrapolate(ac("g", gs=None), 1.0, 2.0, 4.0, 10.0) == (1.0, 2.0)
    assert extrapolate(ac("t", trk=None), 1.0, 2.0, 4.0, 10.0) == (1.0, 2.0)


def zone(n_km=0.0, e_km=0.0, r=10.0, ceil=5000.0):
    return InhibitCfg(lat=LAT0 + n_km / KM_LAT, lon=LON0 + e_km / (KM_LAT * 0.9190),
                      radius_km=r, ceiling_ft=ceil, name="SBXX")


def pair(alt_a=3000, alt_b=3200, e_a=-4.0, e_b=4.0):
    return (ac("a", e_km=e_a, trk=90, gs=150, alt=alt_a),
            ac("b", e_km=e_b, trk=270, gs=150, alt=alt_b))


def test_inhibit_zone_needs_both_aircraft_inside_and_below_ceiling():
    t = run(*pair(), inhibit=[zone()])
    assert t.pairs == [] and t.conflicts == {} and t.inhibited == 1
    assert t.zones[0].r_km == 10.0
    # uma delas acima do teto (sobrevoo): alerta
    assert run(*pair(alt_a=5200, alt_b=5300), inhibit=[zone()]).pairs == [("a", "b")]
    # uma delas fora do círculo (chegando de fora): alerta
    assert run(*pair(e_a=-12.0), inhibit=[zone()]).pairs == [("a", "b")]
    # cada uma numa zona diferente também inibe
    t = run(*pair(), inhibit=[zone(e_km=-6, r=3), zone(e_km=6, r=3)])
    assert t.pairs == [] and t.inhibited == 1
    assert run(*pair()).pairs == [("a", "b")]          # sem zona


def test_new_pairs_once_and_again_only_after_quiet_time():
    t = Tracker(AzimuthalEquidistant(LAT0, LON0), RadarCfg())
    acs = list(pair())
    assert t.update(Snapshot(acs, "T", 100.0)) and t.new_pairs == [("a", "b")]
    assert not t.update(Snapshot(acs, "T", 100.0))     # mesmo snapshot: nada a fazer
    assert t.update(Snapshot(acs, "T", 101.0)) and t.new_pairs == []
    t.update(Snapshot([], "T", 110.0))                  # sumiu por pouco tempo
    t.update(Snapshot(acs, "T", 111.0))
    assert t.new_pairs == []
    t.update(Snapshot([], "T", 112.0))
    t.update(Snapshot(acs, "T", 113.0 + CONFLICT_QUIET_S))   # voltou bem depois
    assert t.new_pairs == [("a", "b")]


def test_config_reads_inhibit_zones(tmp_path):
    f = tmp_path / "c.toml"
    f.write_text("[[radar.inhibit]]\nname = 'SBSJ'\nlat = -23.23\nlon = -45.86\n"
                 "radius_km = 9\nceiling_ft = 3500\n")
    z, = config.load(f).radar.inhibit
    assert (z.name, z.radius_km, z.ceiling_ft) == ("SBSJ", 9, 3500)
    f.write_text("[[radar.inhibit]]\nlat = -23.23\nlon = -45.86\nradius_km = 0\n"
                 "ceiling_ft = 3500\n")
    with pytest.raises(ValueError):
        config.load(f)
    f.write_text("[[radar.inhibit]]\nlat = -23.23\nlon = -45.86\nradius_km = 9\n")
    with pytest.raises(ValueError, match="faltam"):
        config.load(f)
