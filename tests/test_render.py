import pytest

from radClock.ui.render import GLOW_MIN, glow, sweep_angle


def test_sweep_angle_turns_clockwise_once_per_period():
    assert sweep_angle(0.0, 4.0) == 0.0
    assert sweep_angle(1.0, 4.0) == pytest.approx(90.0)
    assert sweep_angle(103.0, 4.0) == pytest.approx(270.0)


def test_glow_peaks_right_after_the_beam_and_fades():
    assert glow(90.0, 90.0) == pytest.approx(1.0)
    assert glow(90.0, 180.0) == pytest.approx(1.0 - (1.0 - GLOW_MIN) / 4)
    # feixe logo antes do alvo: quase uma volta inteira desde a última passagem
    assert glow(90.0, 89.0) == pytest.approx(GLOW_MIN, abs=0.01)
    # atravessa o norte sem salto
    assert glow(359.0, 1.0) == pytest.approx(glow(10.0, 12.0))


@pytest.fixture
def renderer(monkeypatch):
    monkeypatch.setenv("SDL_VIDEODRIVER", "dummy")
    import pygame

    from radClock.core.acdb import AircraftDB
    from radClock.core.alarm import AlarmClock
    from radClock.core.config import Config
    from radClock.core.projection import AzimuthalEquidistant
    from radClock.core.tracks import Tracker
    from radClock.ui.render import Renderer

    pygame.init()
    screen = pygame.display.set_mode((800, 480))
    cfg = Config()
    proj = AzimuthalEquidistant(cfg.station.lat, cfg.station.lon)

    def make(echo, acdb_path="/nao/existe.db"):
        return Renderer(screen, cfg, proj, [], Tracker(proj, cfg.radar),
                        AircraftDB(acdb_path), AlarmClock("UTC"), echo_available=echo)
    yield make
    pygame.quit()


@pytest.mark.parametrize("echo", [True, False])
def test_buttons_fit_in_panel_without_overlap(renderer, echo):
    r = renderer(echo)
    rects = list(r.buttons.values())
    assert ("echo" in r.buttons) == echo and "settings" in r.buttons
    for i, a in enumerate(rects):
        assert r.panel.contains(a)
        assert all(not a.colliderect(b) for b in rects[i + 1:])
        assert a.height >= 44                      # alvo de toque ~8 mm


def test_conflict_follows_the_alert_setting(renderer):
    """Conflito aparece com o ALERTA em COR+SOM ou SÓ COR; desligado, some e o
    painel avisa ALERTA DESL. O símbolo (quadrado/avião) não interfere."""
    from radClock.core.fetcher import Snapshot
    from radClock.core.model import Aircraft
    from radClock.core.options import ALERT_COLOR, ALERT_OFF, SYMBOL_PLANE, DisplayOptions
    from radClock.core.tracks import Conflict
    from radClock.ui.palette import AC, CONFLICT

    r = renderer(True)
    a = Aircraft(hex="a", lat=-23.0, lon=-45.8, alt_ft=10000, gs_kt=300, track=90)
    r.tracker.conflicts = {"a": Conflict("b", 10.0)}
    r.tracker.pairs = [("a", "b")]
    snap = Snapshot([a], "T", 100.0)
    for options in (DisplayOptions(), DisplayOptions(alert=ALERT_COLOR, symbol=SYMBOL_PLANE)):
        r.draw(snap, None, 50.0, 100.0, options=options)
        assert r._color(a, 0.0, False) == CONFLICT

    texts = []
    original = r.info_panel._panel_text
    r.info_panel._panel_text = lambda text, *args, **kw: (texts.append(text),
                                                           original(text, *args, **kw))[1]
    r.draw(snap, None, 50.0, 100.0, options=DisplayOptions(alert=ALERT_OFF))
    assert r._color(a, 0.0, False) == AC
    assert "ALERTA DESL" in texts and not any(t.startswith("CONFLITO") for t in texts)
    assert r.hit_test(r.buttons["settings"].center).value == "settings"


def test_target_and_label_move_between_snapshots(renderer):
    from radClock.core.fetcher import Snapshot
    from radClock.core.model import Aircraft

    r = renderer(False)
    a = Aircraft(hex="a", lat=-23.21, lon=-45.87, alt_ft=10000, gs_kt=360, track=90,
                 callsign="TST1")
    snap = Snapshot([a], "T", 100.0)
    r.draw(snap, None, 10.0, 100.0)
    (_, x0, y0), = r._touch_targets
    rect0 = r._placed_labels["a"].rect
    r.draw(snap, None, 10.0, 105.0)              # 5 s depois, sem dado novo
    (_, x1, y1), = r._touch_targets
    km_px = r.r_px / 10.0
    assert x1 - x0 == pytest.approx(5 * 360 * 1.852 / 3600 * km_px, rel=0.01)
    assert y1 == pytest.approx(y0)
    # o rótulo foi junto, no mesmo lugar em relação ao alvo
    ox, oy = r._label_offsets["a"]
    assert (round(x1 + ox) - round(x0 + ox)) == pytest.approx(x1 - x0, abs=1)
    assert r._placed_labels["a"].rect == rect0


def test_card_silhouette_is_scaled_once_not_every_frame(renderer, monkeypatch, tmp_path):
    """Original de 512 px: reescalar por quadro custava ~1 ms no PC (~10 no Pi)."""
    import io
    import sqlite3

    import pygame

    from radClock.core.fetcher import Snapshot
    from radClock.core.model import Aircraft

    # base mínima: um tipo com silhueta (retângulo de 512 px) e envergadura
    surf = pygame.Surface((512, 512))
    surf.fill((0, 0, 0))
    surf.fill((255, 255, 255), (56, 200, 400, 100))
    buf = io.BytesIO()
    pygame.image.save(surf, buf, "s.png")
    path = tmp_path / "ac.db"
    db = sqlite3.connect(path)
    db.executescript("""
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE silhouettes (id INTEGER PRIMARY KEY, name TEXT, png BLOB);
        CREATE TABLE types (icao TEXT PRIMARY KEY, mfr TEXT, model TEXT, cls TEXT,
                            eng_n INTEGER, eng_t INTEGER, eng TEXT, span_m REAL,
                            len_m REAL, mtow_kg REAL, cruise_kt REAL, sil INTEGER);
        CREATE TABLE cat_sil (cat TEXT PRIMARY KEY, sil INTEGER);
        CREATE TABLE operators (code TEXT PRIMARY KEY, name TEXT, radio TEXT);
        CREATE TABLE hex_blocks (lo INTEGER PRIMARY KEY, hi INTEGER, country TEXT);
    """)
    db.execute("INSERT INTO silhouettes VALUES (0, 'T', ?)", (buf.getvalue(),))
    db.execute("INSERT INTO types VALUES ('TST1','X','Y','L2J',2,3,'',36.0,40.0,0,0,0)")
    db.commit()
    db.close()

    r = renderer(False, path)
    calls = []
    orig = pygame.transform.smoothscale
    monkeypatch.setattr(pygame.transform, "smoothscale",
                        lambda s, size: (calls.append(size), orig(s, size))[1])
    ac = Aircraft(hex="e40001", lat=-23.0, lon=-45.8, type="TST1", alt_ft=10000)
    snap = Snapshot([ac], "T", 100.0)
    for k in range(5):
        r.draw(snap, ac.hex, 50.0, 100.0 + k, card=True)
    assert len(calls) == 1
    w, _ = calls[0]
    assert w == round(36.0 * 2 * r.r_px / 80)          # largura = envergadura na escala


def test_clock_spans_panel_width_and_opens_alarm(renderer):
    """Relógio grande na largura do painel; tocar nele abre o despertador."""
    from radClock.core.fetcher import Snapshot
    from radClock.core.model import Aircraft

    r = renderer(True)
    a = Aircraft(hex="a", lat=-23.0, lon=-45.8, alt_ft=10000, callsign="TST1", type="A320")
    r.draw(Snapshot([a], "T", 100.0), "a", 50.0, 100.0)
    clock = r.info_panel._clock_button
    assert clock.width >= r.panel.width - 16
    assert clock.height >= 60                      # dígitos grandes, não a fonte de 20 px
    assert r.hit_test(clock.center).value == "clock"
    # botão do tipo na linha do indicativo, abaixo do relógio e acima dos botões fixos
    card = r.info_panel._card_button
    assert r.hit_test(card.center).value == "card"
    assert clock.bottom < card.top < min(b.top for b in r.buttons.values())


def test_footer_credits_and_alarm_fit_side_by_side(renderer):
    """Rodapé: créditos mais longos à esquerda e alarme mais longo à direita, sem sobrepor."""
    from radClock.ui.panel import PANEL_TEXT_X_PX

    r = renderer(True)
    font = r.info_panel.font_small
    credits_width = font.size(" · ".join(["Open-Meteo", "RainViewer"]))[0]
    alarm_width = max(font.size(t)[0] for t in ("ALARME QUA 07:30", "SONECA ATÉ 07:30"))
    assert credits_width + alarm_width + 3 * PANEL_TEXT_X_PX <= r.panel.width


def test_range_buttons_work_as_a_value_stepper(renderer):
    """Com o alcance escrito entre os botões, "-" (esquerda) diminui e "+" (direita) aumenta."""
    from radClock.ui.app import KEYS_RANGE_DOWN, KEYS_RANGE_UP
    import pygame

    r = renderer(True)
    down, up = r.buttons["range_down"], r.buttons["range_up"]
    assert down.right < r.info_panel._range_label.left < up.left
    assert r.hit_test(down.center).value == "range_down"
    assert pygame.K_PLUS in KEYS_RANGE_UP and pygame.K_MINUS in KEYS_RANGE_DOWN


def test_source_line_with_count_and_quota_fits(renderer):
    """Pior caso da linha da fonte: OpenSky com 3 dígitos de alvos, idade e cota."""
    from radClock.ui.panel import PANEL_TEXT_X_PX

    r = renderer(True)
    font = r.info_panel.font_medium
    width = font.size("● OPENSKY (999)  99s  cota 3999")[0] + font.size(">")[0]
    assert width + PANEL_TEXT_X_PX + 8 + 4 <= r.panel.width - 6


def test_echo_note_fits_inside_the_rain_button(renderer):
    r = renderer(True)
    rect = r.buttons["echo"]
    panel = r.info_panel
    title = panel.font_large.size("CHUVA")
    note = panel.font_small.size("há 23h59 · 12/13")
    assert note[0] < rect.width - 8
    assert title[1] + note[1] <= rect.height


def test_echo_frame_array_is_drawn_and_converted_once(renderer):
    """O núcleo publica o eco como array RGBA; a tela converte em Surface uma
    vez por quadro e solta a Surface quando o quadro sai."""
    import numpy as np

    from radClock.core.echo import EchoFrame
    from radClock.core.fetcher import Snapshot
    r = renderer(True)
    image = np.zeros((r.side, r.side, 4), np.uint8)
    image[r.cy - 5:r.cy + 5, r.cx + 30:r.cx + 90] = (0, 0, 255, 255)
    frame = EchoFrame(image, 50.0, 1000, 1)
    snap = Snapshot([], "T", 100.0)

    r.draw(snap, None, 50.0, 100.0, echo_on=True, echo=[frame])
    # anéis e cruz passam por cima do eco (fora da linha central): basta o azul aparecer
    row = [tuple(r.screen.get_at((x, r.cy - 3)))[:3] for x in range(r.cx + 30, r.cx + 90)]
    assert row.count((0, 0, 255)) > 40
    converted = r._echo_surfaces.surface(frame)
    r.draw(snap, None, 50.0, 101.0, echo_on=True, echo=[frame])
    assert r._echo_surfaces.surface(frame) is converted

    r.draw(snap, None, 50.0, 102.0, echo_on=False, echo=[frame])
    assert r._echo_surfaces._by_version == {}


def test_touch_counts_once_as_a_click():
    """kmsdrm só manda FINGERDOWN; o desktop manda também o clique emulado."""
    import pygame
    from radClock.ui.app import touch_as_click
    finger = pygame.event.Event(pygame.FINGERDOWN, x=0.5, y=1.0, touch_id=7, finger_id=1)
    click = touch_as_click(finger, (800, 480))
    assert click.type == pygame.MOUSEBUTTONDOWN and click.button == 1
    assert click.pos == (400, 479)                        # y = 1.0 ainda cai na tela
    emulated = pygame.event.Event(pygame.MOUSEBUTTONDOWN, pos=(400, 240), button=1, touch=True)
    assert touch_as_click(emulated, (800, 480)) is None
    mouse = pygame.event.Event(pygame.MOUSEBUTTONDOWN, pos=(10, 20), button=1, touch=False)
    assert touch_as_click(mouse, (800, 480)) is mouse


def test_cursor_follows_mouse_and_hides_on_touch():
    """Mouse USB no Pi: o cursor aparece ao mexer e some no toque seguinte."""
    import pygame
    from radClock.ui.app import cursor_wanted
    mouse = pygame.event.Event(pygame.MOUSEMOTION, pos=(10, 20), rel=(1, 0), touch=False)
    assert cursor_wanted(mouse) is True
    emulated = pygame.event.Event(pygame.MOUSEMOTION, pos=(10, 20), rel=(1, 0), touch=True)
    assert cursor_wanted(emulated) is False
    finger = pygame.event.Event(pygame.FINGERDOWN, x=0.5, y=0.5, touch_id=7, finger_id=1)
    assert cursor_wanted(finger) is False
    key = pygame.event.Event(pygame.KEYDOWN, key=pygame.K_a)
    assert cursor_wanted(key) is None
