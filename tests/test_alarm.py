import os
from datetime import datetime, timedelta, timezone

import pygame
import pytest

from radClock.core.alarm import Alarm, AlarmClock, next_alarm_text
from radClock.ui.audio import CONFLICT_BEEPS, Buzzer
from radClock.ui.clockui import ClockScreen, ringing_hit, ringing_layout

UTC = timezone.utc
MON = datetime(2026, 10, 26, 10, 30, 20, tzinfo=UTC)   # segunda, 07:30:20 em Brasília


def clock(*alarms, tz="America/Sao_Paulo"):
    return AlarmClock(tz, list(alarms))


def test_weekly_alarm_fires_once_per_day_and_only_on_marked_days():
    c = clock(Alarm(True, 7, 30, {0, 2}))                 # seg e qua
    assert c.check(MON) == 0
    assert c.check(MON + timedelta(seconds=30)) is None    # mesmo minuto: não repete
    assert c.check(MON + timedelta(days=1)) is None        # terça não está marcada
    assert c.check(MON + timedelta(days=2)) == 0
    assert c.alarms[0].enabled                             # semanal continua ligado


def test_wrong_minute_or_disabled_does_not_fire():
    assert clock(Alarm(True, 7, 31, {0})).check(MON) is None
    assert clock(Alarm(False, 7, 30, {0})).check(MON) is None


def test_one_shot_alarm_disables_itself():
    c = clock(Alarm(True, 7, 30, set()))
    assert c.check(MON) == 0
    assert not c.alarms[0].enabled
    assert c.check(MON + timedelta(days=1)) is None


def test_timezone_is_applied():
    # 07:30 em Taipei = 23:30 UTC do dia anterior
    c = clock(Alarm(True, 7, 30, set()), tz="Asia/Taipei")
    assert c.check(datetime(2026, 10, 25, 23, 30, tzinfo=UTC)) == 0
    assert not c.set_tz("Nowhere/Atlantis") and c.tz_name == "Asia/Taipei"


def test_next_alarm_skips_past_and_unmarked_days():
    c = clock(Alarm(True, 7, 30, {0, 2}), Alarm(True, 22, 0, {6}), Alarm(False, 8, 0, set()))
    i, when = c.next_alarm(MON + timedelta(minutes=1))     # depois do de segunda
    assert i == 0 and when.weekday() == 2 and (when.hour, when.minute) == (7, 30)
    assert next_alarm_text(c, MON + timedelta(minutes=1)) == "ALARME QUA 07:30"
    assert next_alarm_text(clock()) == ""


def test_from_json_is_lenient():
    a = Alarm.from_json({"enabled": "yes", "hour": 25, "minute": 15, "days": [0, 7, True, 3]})
    assert (a.enabled, a.hour, a.minute, a.days) == (False, 7, 15, {0, 3})
    assert Alarm.from_json("lixo") == Alarm()
    assert len(AlarmClock("UTC", [Alarm()] * 9).alarms) == 4
    assert len(AlarmClock("UTC").alarms) == 4


@pytest.fixture
def ui():
    pygame.font.init()
    return ClockScreen(pygame.Surface((800, 480)), clock(Alarm(False, 23, 30, set())))


@pytest.mark.parametrize("start, action, expect", [
    (30, "m+0", 35), (30, "m-0", 25), (32, "m-0", 30), (32, "m+0", 35),
    (0, "m-0", 55), (55, "m+0", 0)])
def test_minute_steps_of_five(ui, start, action, expect):
    ui.clock.alarms[0].minute = start
    assert ui.apply(action)
    assert ui.clock.alarms[0].minute == expect
    assert ui.clock.alarms[0].enabled                     # mexer na hora liga o alarme


def test_hour_wraps_days_toggle_and_tz_cycles(ui):
    ui.apply("h+0")
    assert ui.clock.alarms[0].hour == 0
    ui.apply("d0:6")
    ui.apply("d0:6")
    ui.apply("d0:1")
    assert ui.clock.alarms[0].days == {1}
    ui.apply("on0")
    assert not ui.clock.alarms[0].enabled
    tz = ui.clock.tz_name
    ui.apply("tz+")
    assert ui.clock.tz_name != tz
    ui.apply("tz-")
    assert ui.clock.tz_name == tz


def test_every_button_is_inside_the_screen_and_hit_finds_it(ui):
    area = pygame.Rect(0, 0, 800, 480)
    assert ui.hit(ui.rects["unsnooze"].center) is None     # só existe com soneca
    ui.clock.snooze = (0, MON)
    rects = list(ui.rects.values())
    assert all(not a.colliderect(b) for i, a in enumerate(rects) for b in rects[i + 1:])
    for key, r in ui.rects.items():
        assert area.contains(r), key
        assert ui.hit(r.center) == key


def test_buzzer_never_raises(monkeypatch):
    monkeypatch.setenv("SDL_AUDIODRIVER", "dummy")
    b = Buzzer()
    b.start()
    b.stop()
    Buzzer("/nao/existe.wav").start()                    # arquivo ausente: mudo
    c = Buzzer("", 0.5, CONFLICT_BEEPS, "alerta de conflito")
    c.play()
    if c.sound is not None:                             # 6 bipes de 0,12 s
        assert c.sound.get_length() == pytest.approx(0.72, abs=0.01)
    pygame.mixer.quit()


def test_snooze_rings_again_after_n_minutes_and_shows_in_next_alarm():
    c = clock(Alarm(True, 7, 30, set()))                  # "uma vez": desliga ao tocar
    c.ringing = c.check(MON)
    assert c.ringing == 0 and not c.alarms[0].enabled
    c.snooze_now(10, MON)
    assert c.ringing is None
    assert next_alarm_text(c, MON) == "SONECA ATÉ 07:40"
    assert c.check(MON + timedelta(minutes=9, seconds=30)) is None
    assert c.check(MON + timedelta(minutes=10)) == 0     # toca mesmo com o alarme desligado
    assert c.snooze is None and next_alarm_text(c, MON) == ""


def test_snooze_survives_restart_and_bad_json_is_ignored():
    c = clock(Alarm(True, 7, 30, {0}))
    c.ringing = 0
    c.snooze_now(5, MON)
    d = clock(Alarm(True, 7, 30, {0}))
    d.load_snooze(c.snooze_json())
    assert d.snooze == c.snooze
    for bad in (None, [], {"alarm": 9, "until": 1.0}, {"alarm": True, "until": 1.0},
                {"alarm": 0, "until": "x"}, {"alarm": 0, "until": 1e30}):
        e = clock()
        e.load_snooze(bad)
        assert e.snooze is None
    c.ringing = None
    c.snooze_now(5, MON)                                  # nada tocando: nada a adiar
    assert c.snooze_json()["alarm"] == 0


def test_unsnooze_button_and_disabling_the_alarm_cancel_snooze(ui):
    c = ui.clock
    c.snooze = (0, MON)
    assert ui.hit(ui.rects["unsnooze"].center) == "unsnooze"
    assert ui.apply("unsnooze") and c.snooze is None
    c.alarms[0].enabled = False
    c.snooze = (0, MON)
    ui.apply("on0")                                       # religar não cancela
    assert c.snooze is not None
    ui.apply("on0")                                       # desligar cancela
    assert c.snooze is None


def test_ringing_touch_snoozes_except_on_stop():
    s = pygame.Surface((800, 480))
    band, btns = ringing_layout(s, True)
    assert btns["snooze"].width > btns["stop"].width
    assert all(band.contains(r) and r.height >= 44 for r in btns.values())
    assert ringing_hit(s, True, btns["stop"].center) == "stop"
    assert ringing_hit(s, True, btns["snooze"].center) == "snooze"
    assert ringing_hit(s, True, (5, 5)) == "snooze"         # fora dos botões: adia
    assert ringing_hit(s, False, btns["snooze"].center) == "stop"   # sem soneca: para


@pytest.mark.parametrize("line", ["snooze_min = -1", "snooze_min = 90", "ring_s = 0"])
def test_clock_config_rejects_bad_values(tmp_path, line):
    from radClock.core import config
    f = tmp_path / "c.toml"
    f.write_text(f"[clock]\n{line}\n")
    with pytest.raises(ValueError):
        config.load(f)
