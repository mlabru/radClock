import json

import pygame
import pytest

from radClock.core import config
from radClock.core.maplayer import MapLayer, load_geojson
from radClock.core.projection import AzimuthalEquidistant
from radClock.ui.declutter import N_NEAR, Label, place

LAT0, LON0 = -23.21, -45.87


def write(tmp_path, features, name="m.geojson"):
    p = tmp_path / name
    p.write_text(json.dumps({"type": "FeatureCollection", "features": features}))
    return p


def feat(geom, **props):
    return {"type": "Feature", "geometry": geom, "properties": props}


def test_lines_and_points_are_loaded_with_kind_and_label(tmp_path):
    p = write(tmp_path, [
        feat({"type": "LineString", "coordinates": [[-45.9, -23.2], [-45.8, -23.3]]}),
        feat({"type": "Point", "coordinates": [-45.87, -23.23]}, kind="airport", ident="SBSJ", size=1),
        feat({"type": "Point", "coordinates": [-45.9, -23.1]}, kind="VOR", ident="SCP"),
        feat({"type": "MultiPoint", "coordinates": [[-46.0, -23.0]]}, name="Torre qualquer"),
        feat({"type": "Polygon", "coordinates": [[[-46, -23], [-45, -23], [-45, -24], [-46, -23]]]}),
    ])
    lines, points = load_geojson(p)
    assert len(lines) == 2 and len(points) == 3
    layer = MapLayer.from_geojson(p, "#C8C8C8", AzimuthalEquidistant(LAT0, LON0), 100, 50)
    kinds = [(pt.kind, pt.label, pt.size) for pt in layer.points]
    assert kinds == [("airport", "SBSJ", 1), ("vor", "SCP", 0), ("point", "Torre qu", 0)]
    sbsj = layer.points[0]
    assert sbsj.e == pytest.approx(0.0, abs=0.01) and sbsj.n == pytest.approx(-2.2, abs=0.05)
    assert layer.color == (200, 200, 200)


def test_layer_and_label_ranges():
    layer = MapLayer((1, 2, 3), [], [], max_range_km=100, label_range_km=50)
    assert layer.visible(100) and not layer.visible(101)
    assert layer.labels_visible(50) and not layer.labels_visible(51)
    always = MapLayer((1, 2, 3), [])
    assert always.visible(300) and always.labels_visible(300)


def test_map_labels_use_only_near_slots_and_custom_fit():
    lbs = [Label("a", 100, 100, 30, 10)]
    bounds = pygame.Rect(0, 0, 200, 200)
    # tudo ocupado em volta: sem linhas de chamada não há onde pôr
    around = [pygame.Rect(40, 60, 120, 80)]
    assert place(lbs, around, bounds, leaders=False) == {}
    assert place(lbs, around, bounds)["a"].slot >= N_NEAR
    # fits recusa o lado direito: vai para outra posição colada
    p = place(lbs, [], bounds, leaders=False, fits=lambda r: r.right <= 100)["a"]
    assert p.rect.right <= 100 and p.slot < N_NEAR


def test_map_symbols_and_labels_drawn_inside_circle(monkeypatch, tmp_path):
    monkeypatch.setenv("SDL_VIDEODRIVER", "dummy")
    from radClock.core.acdb import AircraftDB
    from radClock.core.alarm import AlarmClock
    from radClock.core.config import Config
    from radClock.core.tracks import Tracker
    from radClock.ui.mapsymbols import draw_map_symbol
    from radClock.ui.render import Renderer

    pygame.init()
    try:
        surf = pygame.Surface((40, 40))
        for kind in ("airport", "vor", "vordme", "dme", "ndb", "fix", "point"):
            r = draw_map_symbol(surf, kind, 20, 20, (255, 255, 255), 2)
            assert r.collidepoint(20, 20) and r.width <= 13, kind

        proj = AzimuthalEquidistant(LAT0, LON0)
        p = write(tmp_path, [
            feat({"type": "Point", "coordinates": [-45.87, -23.23]}, kind="airport", ident="SBSJ"),
            # perto da borda do alcance de 25 km: o rótulo tem de caber dentro do círculo
            feat({"type": "Point", "coordinates": [-45.87 + 0.235, -23.21]}, kind="ndb", ident="EDGE"),
            feat({"type": "Point", "coordinates": [-44.0, -23.2]}, kind="vor", ident="LONGE"),
        ])
        cfg = Config()
        screen = pygame.display.set_mode((800, 480))
        layers = [MapLayer.from_geojson(p, "#FFFFFF", proj, 0, 50)]
        r = Renderer(screen, cfg, proj, layers, Tracker(proj, cfg.radar),
                     AircraftDB("/nao/existe.db"), AlarmClock("UTC"))
        placed = {}
        import radClock.ui.render as render_mod
        orig = render_mod.place

        def spy(*a, **kw):
            out = orig(*a, **kw)
            placed.update(out)
            return out

        monkeypatch.setattr(render_mod, "place", spy)
        r._build_map(25.0)
        assert len(placed) == 2                       # LONGE está fora do alcance
        r2 = r.r_px ** 2
        for pl in placed.values():
            for x, y in (pl.rect.topleft, pl.rect.bottomright, pl.rect.topright):
                assert (x - r.cx) ** 2 + (y - r.cy) ** 2 <= r2
        placed.clear()
        r._build_map(100.0)                           # acima de label_range_km: sem rótulos
        assert placed == {}
    finally:
        pygame.quit()


@pytest.mark.parametrize("body, msg", [
    ("[[map.layers]]\npath = 'x'\ncolor = 'verde'\n", "cor"),
    ("[[map.layers]]\npath = 'x'\nmax_range_km = -1\n", "negativos"),
    ("[[map.layers]]\ncolor = '#FFFFFF'\n", "faltam"),
])
def test_config_validation(tmp_path, body, msg):
    f = tmp_path / "c.toml"
    f.write_text(body)
    with pytest.raises(ValueError, match=msg):
        config.load(f)


@pytest.mark.parametrize("preset", ["config.dev.toml", "config.api.toml"])
def test_shipped_maps_load(preset):
    """Os mapas de São José dos Campos em maps/ carregam nos dois presets, com
    os rios por baixo de tudo e as mesmas camadas do maps/layers.toml."""
    import tomllib
    from pathlib import Path
    cfg = config.load(Path(preset))
    proj = AzimuthalEquidistant(cfg.station.lat, cfg.station.lon)
    layers = [MapLayer.from_geojson(m.path, m.color, proj, m.max_range_km, m.label_range_km)
              for m in cfg.map_layers]
    pts = [pt for la in layers for pt in la.points]
    assert any(pt.label == "SBSJ" and pt.kind == "airport" for pt in pts)
    assert sum(len(la.lines_km) for la in layers) > 100

    assert cfg.map_layers[0].path == "maps/rivers.geojson" and layers[0].lines_km
    generated = tomllib.loads(Path("maps/layers.toml").read_text())["map"]["layers"]
    assert [(m.path, m.color) for m in cfg.map_layers] == \
        [(g["path"], g["color"]) for g in generated]
