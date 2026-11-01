"""Ajustes de exibição (options.py), a tela AJUSTES (settingsui.py) e o desenho por ajuste."""
import pytest

from radClock.core import config
from radClock.core.config import RadarCfg
from radClock.core.options import (ALERT_COLOR, ALERT_OFF, ALERT_SOUND, SYMBOL_PLANE, SYMBOL_SQUARE,
                                  DisplayOptions)


# ------------------------------------------------------------------ options
def test_defaults_come_from_the_radar_section():
    radar = RadarCfg(symbol="plane", vector_min=1.0, trail_s=120.0, alert="color",
                     sweep=False, zones=False)
    assert DisplayOptions.from_config(radar) == DisplayOptions(
        symbol=SYMBOL_PLANE, vector_min=1.0, trail_s=120.0, alert=ALERT_COLOR,
        sweep=False, zones=False)


@pytest.mark.parametrize("alert, visible, sound", [
    (ALERT_SOUND, True, True), (ALERT_COLOR, True, False), (ALERT_OFF, False, False)])
def test_alert_is_one_setting_for_color_and_sound(alert, visible, sound):
    """Cor e som juntos: nunca o alarme tocando sem a tela mostrar o motivo."""
    options = DisplayOptions(alert=alert)
    assert options.show_conflicts == visible and options.conflict_sound == sound
    assert not (options.conflict_sound and not options.show_conflicts)


def test_json_round_trip_and_lenient_reading():
    defaults = DisplayOptions()
    changed = DisplayOptions(symbol=SYMBOL_PLANE, vector_min=0.0, trail_s=120.0,
                             alert=ALERT_OFF, sweep=False, zones=False)
    assert DisplayOptions.from_json(changed.to_json(), defaults) == changed
    # inválidos (editados à mão, versão antiga) ficam com o padrão, sem erro
    bad = {"symbol": "triangle", "vector_min": True, "trail_s": -5, "alert": 1,
           "sweep": "yes", "zones": None, "chave_velha": 3}
    assert DisplayOptions.from_json(bad, defaults) == defaults
    assert DisplayOptions.from_json({"vector_min": 1}, defaults).vector_min == 1   # int serve


def test_old_atc_mode_is_migrated_but_keeps_the_alert():
    defaults = DisplayOptions(alert=ALERT_SOUND)
    assert DisplayOptions.from_old_atc_mode(True, defaults) == defaults
    radar_mode = DisplayOptions.from_old_atc_mode(False, defaults)
    assert radar_mode.symbol == SYMBOL_PLANE and radar_mode.vector_min == 0
    assert radar_mode.trail_s == 0 and not radar_mode.zones
    assert radar_mode.alert == ALERT_SOUND        # o RADAR escondia conflitos: não herda


# ------------------------------------------------------------------- config
def test_obsolete_atc_key_is_ignored_with_a_warning(tmp_path, caplog):
    f = tmp_path / "c.toml"
    f.write_text("[radar]\natc = false\n")
    cfg = config.load(f)
    assert DisplayOptions.from_config(cfg.radar) == DisplayOptions()
    assert "obsoleto" in caplog.text


@pytest.mark.parametrize("line", ['symbol = "triangle"', 'alert = "loud"', "vector_min = -1"])
def test_config_rejects_bad_display_defaults(tmp_path, line):
    f = tmp_path / "c.toml"
    f.write_text(f"[radar]\n{line}\n")
    with pytest.raises(ValueError):
        config.load(f)


# ------------------------------------------------------------- tela AJUSTES
@pytest.fixture
def pygame_screen(monkeypatch):
    monkeypatch.setenv("SDL_VIDEODRIVER", "dummy")
    import pygame

    pygame.init()
    yield pygame
    pygame.quit()


@pytest.mark.parametrize("size", [(800, 480), (1280, 720)])
def test_settings_buttons_fit_and_do_not_overlap(pygame_screen, size):
    from radClock.ui.settingsui import SettingsScreen

    screen = pygame_screen.display.set_mode(size)
    ui = SettingsScreen(screen, sweep_available=True, zones_available=True)
    ui.draw(DisplayOptions(), sound_ok=False)
    area = screen.get_rect()
    rects = list(ui.rects.items())
    for index, (key, rect) in enumerate(rects):
        assert area.contains(rect), key
        assert rect.height >= 44, key              # alvo de toque ~8 mm
        assert ui.hit(rect.center) == key
        assert all(not rect.colliderect(other) for _, other in rects[index + 1:]), key


def test_every_settings_button_applies_its_option(pygame_screen):
    from radClock.ui.settingsui import SettingsScreen

    screen = pygame_screen.display.set_mode((800, 480))
    ui = SettingsScreen(screen, sweep_available=True, zones_available=True)
    for field, _label, choices in ui.rows:
        for index, (value, _text) in enumerate(choices):
            options = ui.apply(DisplayOptions(), f"{field}:{index}")
            assert getattr(options, field) == value
    assert ui.apply(DisplayOptions(), "close") == DisplayOptions()


def test_sweep_and_zones_rows_only_when_available(pygame_screen):
    from radClock.ui.settingsui import SettingsScreen

    screen = pygame_screen.display.set_mode((800, 480))
    ui = SettingsScreen(screen, sweep_available=False, zones_available=False)
    fields = [row[0] for row in ui.rows]
    assert fields == ["symbol", "vector_min", "trail_s", "alert"]
    assert not any(key.startswith(("sweep", "zones")) for key in ui.rects)


# --------------------------------------------------------- desenho por ajuste
@pytest.fixture
def renderer(pygame_screen):
    from radClock.core.acdb import AircraftDB
    from radClock.core.alarm import AlarmClock
    from radClock.core.config import Config
    from radClock.core.projection import AzimuthalEquidistant
    from radClock.core.tracks import Tracker
    from radClock.ui.render import Renderer

    screen = pygame_screen.display.set_mode((800, 480))
    cfg = Config()
    proj = AzimuthalEquidistant(cfg.station.lat, cfg.station.lon)
    return Renderer(screen, cfg, proj, [], Tracker(proj, cfg.radar),
                    AircraftDB("/nao/existe.db"), AlarmClock("UTC"))


def test_sweep_setting_turns_beam_off(renderer):
    renderer.options = DisplayOptions(sweep=True)
    assert renderer._beam(10.0) is not None
    renderer.options = DisplayOptions(sweep=False)
    assert renderer._beam(10.0) is None            # sem feixe: alvos não apagam


def test_trail_setting_limits_points_drawn(renderer, monkeypatch):
    """O Tracker guarda 2 min; com RASTRO 1 min só os pontos do último minuto aparecem."""
    from collections import deque

    import pygame

    renderer.tracker.trails = {"a": deque((100.0 + t, 0.0, 0.0) for t in range(0, 120, 10))}
    drawn = []
    monkeypatch.setattr(pygame.draw, "rect", lambda surf, color, rect, *a, **k: drawn.append(1))
    renderer.options = DisplayOptions(trail_s=60.0)
    renderer._draw_trail(renderer.screen, "a", 1.0, now=220.0)
    assert len(drawn) == 6                         # instantes 160..210 (até 60 s atrás)


def test_vector_and_symbol_are_independent(renderer, monkeypatch):
    """Avião girado com vetor (impossível no antigo modo RADAR) e quadrado sem vetor."""
    from radClock.core.fetcher import Snapshot
    from radClock.core.model import Aircraft

    calls = []
    monkeypatch.setattr(renderer, "_draw_speed_vector",
                        lambda *a: (calls.append("vector"), True)[1])
    monkeypatch.setattr(renderer, "_draw_plane", lambda *a: calls.append("plane"))
    monkeypatch.setattr(renderer, "_draw_symbol", lambda *a, **k: calls.append("square"))
    a = Aircraft(hex="a", lat=-23.0, lon=-45.8, alt_ft=10000, gs_kt=300, track=90)
    snap = Snapshot([a], "T", 100.0)
    renderer.draw(snap, None, 50.0, 100.0,
                  options=DisplayOptions(symbol=SYMBOL_PLANE, vector_min=2.0))
    assert calls == ["vector", "plane"]
    calls.clear()
    renderer.draw(snap, None, 50.0, 100.0,
                  options=DisplayOptions(symbol=SYMBOL_SQUARE, vector_min=0.0))
    assert calls == ["square"]


def test_cropped_sweep_matches_rotating_the_whole_scope(renderer):
    """A cunha recortada, girada em torno do centro do escopo, cai no mesmo
    lugar que a cunha do escopo inteiro girada (o desenho antigo)."""
    from math import cos, radians, sin

    import numpy as np
    import pygame

    from radClock.ui.palette import SWEEP

    renderer.cfg.radar.sweep_s = 4.0
    renderer._sweep = renderer._build_sweep()
    assert renderer._sweep.get_width() < renderer.side      # recortou mesmo

    full = pygame.Surface((renderer.side, renderer.side), pygame.SRCALPHA)
    full.fill((0, 0, 0, 0))
    crop_offset = (renderer.cx, renderer.cy) - renderer._sweep_pivot \
        - pygame.math.Vector2(renderer._sweep.get_size()) / 2
    full.blit(renderer._sweep, (round(crop_offset.x), round(crop_offset.y)))
    for beam in (0.0, 37.0, 90.0, 200.0, 315.0):
        renderer.screen.fill((0, 0, 0))
        renderer._draw_sweep(beam)
        new = pygame.surfarray.array3d(renderer.screen)[:renderer.side, :renderer.side]
        renderer.screen.fill((0, 0, 0))
        rotated = pygame.transform.rotate(full, -beam)
        renderer.screen.blit(rotated, rotated.get_rect(center=(renderer.cx, renderer.cy)))
        tip = (renderer.cx + renderer.r_px * sin(radians(beam)),
               renderer.cy - renderer.r_px * cos(radians(beam)))
        pygame.draw.line(renderer.screen, SWEEP, (renderer.cx, renderer.cy), tip, 2)
        old = pygame.surfarray.array3d(renderer.screen)[:renderer.side, :renderer.side]
        # 1 px de diferença de arredondamento na borda é aceitável
        assert np.mean(np.abs(new.astype(int) - old.astype(int)) > 8) < 0.002, beam


def test_trail_layer_is_redrawn_only_when_needed(renderer, monkeypatch):
    """A camada de rastros só é refeita com alvos/alcance novos ou após TRAIL_REDRAW_S."""
    from collections import deque

    from radClock.core.model import Aircraft
    from radClock.ui.render import TRAIL_REDRAW_S, _ScreenTarget

    renderer.tracker.trails = {"a": deque([(100.0, 0.0, 0.0)])}
    renderer.options = DisplayOptions(trail_s=60.0)
    redraws = []
    monkeypatch.setattr(renderer, "_draw_trail", lambda *a: redraws.append(1))
    targets = [_ScreenTarget(Aircraft(hex="a", lat=0.0, lon=0.0), 0.0, 0.0, 0.0)]
    renderer._draw_trails(targets, 1.0, now=100.0)
    renderer._draw_trails(targets, 1.0, now=100.0 + TRAIL_REDRAW_S / 2)
    assert len(redraws) == 1                       # mesmo quadro: só blit
    renderer._draw_trails(targets, 2.0, now=100.0 + TRAIL_REDRAW_S / 2)
    assert len(redraws) == 2                       # alcance mudou
    renderer._draw_trails(targets, 2.0, now=100.0 + TRAIL_REDRAW_S * 2)
    assert len(redraws) == 3                       # esmaecimento: refaz por tempo


# ------------------------------------------------------------------ brilho
def test_brightness_is_validated_like_the_other_settings():
    defaults = DisplayOptions()
    assert DisplayOptions.from_json({"brightness": 40}, defaults).brightness == 40
    for bad in (0, 101, 40.0, True, "40", None):
        assert DisplayOptions.from_json({"brightness": bad}, defaults).brightness == 100


def test_brightness_row_only_with_a_backlight(pygame_screen):
    from radClock.ui.settingsui import SettingsScreen

    screen = pygame_screen.display.set_mode((800, 480))
    without = SettingsScreen(screen, sweep_available=True, zones_available=True)
    assert "brightness" not in [row[0] for row in without.rows]
    with_backlight = SettingsScreen(screen, sweep_available=True, zones_available=True,
                                    brightness_available=True)
    assert with_backlight.rows[-1][0] == "brightness"
    assert with_backlight.apply(DisplayOptions(), "brightness:2").brightness == 10


@pytest.mark.parametrize("size", [(800, 480), (1280, 720)])
def test_all_settings_rows_fit_with_brightness(pygame_screen, size):
    """Com as 7 linhas (feixe, zonas e brilho), os botões ainda cabem sem sobrepor."""
    from radClock.ui.settingsui import SettingsScreen

    screen = pygame_screen.display.set_mode(size)
    ui = SettingsScreen(screen, sweep_available=True, zones_available=True,
                        brightness_available=True)
    ui.draw(DisplayOptions(), sound_ok=True)
    rects = list(ui.rects.values())
    for index, rect in enumerate(rects):
        assert screen.get_rect().contains(rect)
        assert rect.height >= 44
        assert all(not rect.colliderect(other) for other in rects[index + 1:])
