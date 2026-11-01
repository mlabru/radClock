import numpy as np
import pytest

from radClock.core.echo import (TILE, EchoFetcher, EchoFrame, Reprojector, age_text, frame_age_s,
                               inverse_azeq, pick_frame, reproject, tiles_for, to_world_px)
from radClock.core.projection import AzimuthalEquidistant
from radClock.core.ratelimit import RateLimiter

LAT0, LON0 = -23.21, -45.87


@pytest.mark.parametrize("lat0, lon0", [(LAT0, LON0), (60.0, 10.0), (-17.0, 179.8)])
def test_inverse_is_inverse_of_forward(lat0, lon0):
    proj = AzimuthalEquidistant(lat0, lon0)
    pts = [(lat0 + 1.5, lon0 - 2.0), (lat0 - 0.3, lon0 + 0.7), (lat0, lon0)]
    for la, lo in pts:
        e, n = proj.forward(la, lo)
        lat, lon = inverse_azeq(np.array(e), np.array(n), lat0, lon0)
        assert float(lat) == pytest.approx(la, abs=1e-9)
        # longitude volta "desenrolada" (pode passar de 180), o que é intencional
        assert (float(lon) - lo + 180) % 360 - 180 == pytest.approx(0, abs=1e-9)


def test_zoom_7_for_usual_ranges():
    for rng in (10, 50, 300):
        box = tiles_for(LAT0, LON0, rng)
        assert box.z == 7 and box.count <= 16


def test_zoom_drops_for_huge_range():
    box = tiles_for(LAT0, LON0, 2000)
    assert box.z < 7 and box.count <= 16


def test_antimeridian_box_is_contiguous():
    box = tiles_for(-17.0, 179.9, 100)
    assert box.x1 >= 1 << box.z      # passa do último tile; wrap só no download
    assert box.count <= 16


def test_reproject_puts_rain_in_the_right_place():
    rng, side, r_px = 50.0, 200, 96
    box = tiles_for(LAT0, LON0, rng)
    mosaic = np.zeros(((box.y1 - box.y0 + 1) * TILE, (box.x1 - box.x0 + 1) * TILE, 4), np.uint8)
    # "chuva" num ponto 25 km a leste da estação
    proj = AzimuthalEquidistant(LAT0, LON0)
    lat, lon = inverse_azeq(np.array(25.0), np.array(0.0), LAT0, LON0)
    x, y = to_world_px(lat, lon, box.z)
    px, py = int(x - box.x0 * TILE), int(y - box.y0 * TILE)
    mosaic[py - 3:py + 4, px - 3:px + 4] = (200, 50, 50, 255)

    out = reproject(mosaic, box, LAT0, LON0, rng, side, r_px, opacity=0.5)
    ys, xs = np.nonzero(out[..., 3])
    # 25 km a leste com 96 px por 50 km = ~48 px à direita do centro (100)
    assert xs.mean() == pytest.approx(100 + 48, abs=2)
    assert ys.mean() == pytest.approx(100, abs=2)
    assert out[..., 3].max() == 127                # opacidade aplicada
    assert proj.forward(float(lat), float(lon))[0] == pytest.approx(25.0)


def test_nothing_outside_the_circle():
    rng, side, r_px = 50.0, 120, 50
    box = tiles_for(LAT0, LON0, rng)
    mosaic = np.full(((box.y1 - box.y0 + 1) * TILE, (box.x1 - box.x0 + 1) * TILE, 4), 255, np.uint8)
    out = reproject(mosaic, box, LAT0, LON0, rng, side, r_px, opacity=1.0)
    assert out[0, 0, 3] == 0 and out[60, 60, 3] == 255


# ------------------------------------------------------------------ animação
def test_reprojector_is_reused_across_frames():
    rng, side, r_px = 50.0, 120, 56
    box = tiles_for(LAT0, LON0, rng)
    rep = Reprojector(box, LAT0, LON0, rng, side, r_px)
    a = np.zeros((*rep.shape, 4), np.uint8)
    b = a.copy()
    a[..., :] = (10, 20, 30, 255)
    b[..., :] = (90, 80, 70, 200)
    for mosaic in (a, b):
        assert np.array_equal(rep.apply(mosaic, 0.6),
                              reproject(mosaic, box, LAT0, LON0, rng, side, r_px, 0.6))


def test_pick_frame_runs_oldest_to_newest_and_holds():
    frames = [EchoFrame(None, 50.0, t, t) for t in (0, 600, 1200)]
    pick = [pick_frame(frames, now, 0.5, 2.0).time for now in (0.0, 0.6, 1.1, 2.9, 3.0, 3.1)]
    assert pick == [0, 600, 1200, 1200, 0, 0]          # ciclo de 2 * 0,5 + 2 = 3 s
    assert pick_frame([], 1.0, 0.5, 2.0) is None
    assert pick_frame(frames[:1], 1.0, 0.5, 2.0).time == 0


def test_rate_limiter_allows_burst_then_rate():
    now = [0.0]
    lim = RateLimiter(60, 3, clock=lambda: now[0])     # 1 por segundo
    assert [lim.try_acquire() for _ in range(3)] == [0.0, 0.0, 0.0]
    assert lim.try_acquire() == pytest.approx(1.0)
    now[0] = 2.5
    assert lim.try_acquire() == 0.0 and lim.try_acquire() == 0.0
    assert lim.try_acquire() == pytest.approx(0.5)


@pytest.fixture
def png_tile():
    import io

    import pygame
    surf = pygame.Surface((TILE, TILE), pygame.SRCALPHA)
    surf.fill((0, 0, 255, 180))
    buf = io.BytesIO()
    pygame.image.save(surf, buf, "t.png")
    return buf.getvalue()


class FakeRainViewer:
    def __init__(self, png, n=13):
        self.png, self.tiles, self.meta = png, [], 0
        self.past = [{"time": 1000 + 600 * i, "path": f"/v2/radar/p{i}"} for i in range(n)]

    def get_json(self, url):
        self.meta += 1
        return {"host": "https://tiles", "radar": {"past": list(self.past)}}

    def get(self, url):
        self.tiles.append(url)
        return self.png


def make_fetcher(rv, frames=3):
    from radClock.core.config import EchoCfg
    from radClock.ui.echoview import decode_png
    f = EchoFetcher(EchoCfg(enabled=True, frames=frames), LAT0, LON0, 100, 46, decode_png,
                    get=rv.get, get_json=rv.get_json)
    f._requested_range_km = 25.0
    return f


def test_fetcher_builds_newest_first_and_reuses_tiles(png_tile):
    rv = FakeRainViewer(png_tile)
    f = make_fetcher(rv)
    try:
        f._refresh_metadata()
        assert [p for _, p in f._frame_paths] == ["/v2/radar/p12", "/v2/radar/p11", "/v2/radar/p10"]
        f._build_frames(25.0)
        frames = f.frames()
        assert [fr.time for fr in frames] == [1000 + 600 * i for i in (10, 11, 12)]
        assert all(fr.range_km == 25.0 for fr in frames)
        per_frame = tiles_for(LAT0, LON0, 25.0).count
        assert len(rv.tiles) == 3 * per_frame
        # o mais novo é pedido primeiro (4 downloads em paralelo: entre os 4 primeiros)
        assert any("/v2/radar/p12/" in u for u in rv.tiles[:4])
        # outro alcance com o mesmo retângulo de tiles e volta: não baixa de novo
        f._requested_range_km = 10.0
        f._build_frames(10.0)
        f._requested_range_km = 25.0
        f._build_frames(25.0)
        if tiles_for(LAT0, LON0, 10.0) == tiles_for(LAT0, LON0, 25.0):
            assert len(rv.tiles) == 3 * per_frame
        # saiu um quadro novo: só ele é baixado, o mais velho sai da janela
        rv.past.append({"time": 1000 + 600 * 13, "path": "/v2/radar/p13"})
        n = len(rv.tiles)
        f._refresh_metadata()
        f._build_frames(25.0)
        assert [fr.time for fr in f.frames()] == [1000 + 600 * i for i in (11, 12, 13)]
        assert len(rv.tiles) == n + per_frame
        assert not any(k[0] == "/v2/radar/p10" for k in f._tile_png_cache)
    finally:
        f.stop()


def test_build_stops_when_range_changes(png_tile):
    rv = FakeRainViewer(png_tile)
    f = make_fetcher(rv, frames=5)
    try:
        f._refresh_metadata()
        orig = f._build_mosaic

        def mosaic_then_switch(path, box):
            f._requested_range_km = 100.0                                # usuário trocou o alcance
            return orig(path, box)

        f._build_mosaic = mosaic_then_switch
        f._build_frames(25.0)
        assert len(f.frames()) == 1                        # parou depois do quadro em curso
    finally:
        f.stop()


def test_tile_error_keeps_frames_already_built(png_tile):
    from radClock.core.net import SourceError
    rv = FakeRainViewer(png_tile)
    f = make_fetcher(rv)
    calls = []

    def flaky(url):
        calls.append(url)
        if "/p11/" in url:
            raise SourceError("HTTP 500")
        return png_tile

    f._http_get = flaky
    try:
        f._refresh_metadata()
        with pytest.raises(SourceError):
            f._build_frames(25.0)
        assert [fr.time for fr in f.frames()] == [1000 + 600 * 12]
    finally:
        f.stop()


def test_frame_age_and_text():
    """A tela mostra a idade do eco; relógio antes do quadro (Pi sem NTP) não tem idade."""
    assert frame_age_s(1000, 1000 + 14 * 60 + 30) == 14 * 60 + 30
    assert frame_age_s(1000, 999) is None
    assert age_text(0) == "há 0 min"
    assert age_text(14 * 60 + 59) == "há 14 min"
    assert age_text(65 * 60) == "há 1h05"
    assert age_text(24 * 3600 - 60) == "há 23h59"
    assert age_text(10 ** 8) == "há >1 dia"            # não estoura o botão CHUVA
