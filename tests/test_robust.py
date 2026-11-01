"""Robustez 24/7: watchdog do systemd, threads que não morrem, saltos do relógio."""
import socket
import time
from datetime import datetime, timedelta, timezone

import pytest

from radClock.core.alarm import CATCHUP_S, Alarm, AlarmClock, clock_synced
from radClock.core.fetcher import Fetcher
from radClock.core.model import Aircraft
from radClock.core.net import SourceError
from radClock.core.watchdog import Watchdog

UTC = timezone.utc
MON = datetime(2026, 10, 26, 10, 0, 0, tzinfo=UTC)    # segunda, 07:00 em Brasília


# ------------------------------------------------------------------ watchdog
def test_watchdog_is_a_noop_without_systemd():
    w = Watchdog({})
    assert w.interval_s is None
    w.ready()
    w.kick(0.0)
    w.stopping()


def test_watchdog_sends_ready_and_throttled_pings(tmp_path):
    path = str(tmp_path / "notify")
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    srv.bind(path)
    srv.settimeout(1.0)
    try:
        w = Watchdog({"NOTIFY_SOCKET": path, "WATCHDOG_USEC": "30000000"})
        assert w.interval_s == 15.0                   # metade do WatchdogSec
        w.ready()
        assert srv.recv(64) == b"READY=1"
        w.kick(100.0)
        w.kick(101.0)                                 # cedo demais: não manda
        w.kick(115.0)
        assert [srv.recv(64), srv.recv(64)] == [b"WATCHDOG=1", b"WATCHDOG=1"]
        srv.setblocking(False)
        with pytest.raises(BlockingIOError):
            srv.recv(64)
        # WATCHDOG_PID de outro processo: o watchdog não é nosso
        assert Watchdog({"NOTIFY_SOCKET": path, "WATCHDOG_USEC": "30000000",
                         "WATCHDOG_PID": "1"}).interval_s is None
    finally:
        srv.close()
    Watchdog({"NOTIFY_SOCKET": str(tmp_path / "nao-existe")}).ready()   # só avisa no log


# ------------------------------------------------------------------- fetcher
class Flaky:
    """Fonte que alterna defeito (exceção qualquer), falha de rede e sucesso."""
    name = "FLAKY"

    def __init__(self, script):
        self.script = list(script)

    def fetch(self):
        what = self.script.pop(0) if self.script else "ok"
        if what == "bug":
            raise KeyError("lat")
        if what == "net":
            raise SourceError("timeout")
        return [Aircraft(hex="abc123", lat=0.0, lon=0.0)]


def wait_for(cond, timeout=2.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.01)
    return False


def test_fetcher_survives_unexpected_errors():
    f = Fetcher(Flaky(["bug", "bug", "net"]), None, 0.01, 0.01)
    f.start()
    try:
        assert wait_for(lambda: f.snapshot().aircraft and not f.snapshot().error)
        assert f.is_alive() and not f.stuck(time.monotonic())
    finally:
        f.stop()


def test_fallback_bug_does_not_kill_the_thread():
    f = Fetcher(Flaky(["net"] * 1000), Flaky(["bug"]), 0.01, 0.01)
    f.start()
    try:
        assert wait_for(lambda: f.snapshot().source == "FLAKY" and f.snapshot().updated)
        assert f.is_alive()
    finally:
        f.stop()


def test_stuck_when_dead_or_silent():
    f = Fetcher(Flaky([]), None, 1.0, 5.0)
    assert f.stuck(time.monotonic())                  # não rodando
    f.start()
    try:
        assert wait_for(lambda: f.snapshot().updated)
        assert not f.stuck(time.monotonic())
        assert f.stuck(f.beat + 60 + 2 * 5.0 + 1)      # sem dar volta há muito tempo
    finally:
        f.stop()


# ----------------------------------------------------------- salto do relógio
def test_alarm_fires_when_ntp_jump_skips_its_minute():
    c = AlarmClock("America/Sao_Paulo", [Alarm(True, 7, 5, {0})])
    assert c.check(MON) is None                       # 07:00, relógio do fake-hwclock
    assert c.check(MON + timedelta(minutes=8)) == 0   # NTP pulou para 07:08
    assert c.check(MON + timedelta(minutes=8, seconds=1)) is None


def test_two_alarms_in_one_jump_both_fire():
    c = AlarmClock("America/Sao_Paulo", [Alarm(True, 7, 2, {0}), Alarm(True, 7, 4, {0})])
    c.check(MON)
    t = MON + timedelta(minutes=6)
    assert c.check(t) == 0
    assert c.check(t + timedelta(seconds=0.1)) == 1
    assert c.check(t + timedelta(seconds=0.2)) is None


def test_long_jump_is_like_app_off_and_backward_jump_does_not_repeat():
    c = AlarmClock("America/Sao_Paulo", [Alarm(True, 7, 5, {0})])
    c.check(MON)
    assert c.check(MON + timedelta(seconds=CATCHUP_S + 120)) is None
    c = AlarmClock("America/Sao_Paulo", [Alarm(True, 7, 5, {0})])
    assert c.check(MON + timedelta(minutes=5)) == 0
    c.check(MON + timedelta(minutes=2))               # relógio voltou 3 min
    assert c.check(MON + timedelta(minutes=5)) is None


def test_clock_synced(tmp_path):
    assert clock_synced(tmp_path / "nao-existe") is None
    assert clock_synced(tmp_path) is False
    (tmp_path / "synchronized").touch()
    assert clock_synced(tmp_path) is True
