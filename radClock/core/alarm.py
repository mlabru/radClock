"""Relógio com fuso configurável e despertador de 4 alarmes.

Visão geral
-----------
- ``Alarm``: um alarme (ligado?, hora, minuto, dias da semana). Sem nenhum
  dia marcado ele toca UMA vez (na próxima passagem pela hora) e se desliga.
- ``AlarmClock``: só DECIDE quando tocar. Quem desenha é ``clockui``/``render``
  e quem faz barulho é o ``audio.Buzzer``.

Regra do disparo
----------------
O teste é por minuto: se o relógio está em HH:MM de um alarme ligado, num dia
marcado, e ele ainda não tocou nesta data, dispara. Alarme perdido com o app
desligado não toca depois.

Saltos do relógio
-----------------
O Pi não tem RTC. Ele sobe com a hora salva pelo fake-hwclock e pula para a
hora certa quando o NTP sincroniza, às vezes horas à frente. Um quadro
travado também pode pular um minuto. Por isso ``check()`` olha todos os
minutos desde a última chamada, até ``CATCHUP_S`` para trás: o alarme cujo
minuto foi pulado ainda toca. Salto maior que isso é tratado como app
desligado (não toca), e salto para trás não repete (``_fired_on`` guarda a
data em que cada alarme tocou). ``clock_synced()`` diz se o NTP já acertou o
relógio, para a tela avisar.

Soneca
------
Enquanto toca, ``snooze_now()`` cala e marca o mesmo alarme para daqui a N
minutos (instante absoluto em UTC: mudar o fuso não a altera). A soneca vale
por cima da regra do minuto e não depende de o alarme continuar ligado (o
"uma vez" já se desligou ao tocar e a soneca dele ainda precisa tocar). Ela
vai para o arquivo de estado, para sobreviver a um reinício do app; se o app
voltar depois da hora, toca assim que subir.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

log = logging.getLogger(__name__)

N_ALARMS = 4
CATCHUP_S = 600             # minutos pulados por salto do relógio ainda disparam
SECONDS_PER_MINUTE = 60
DAYS_TO_SEARCH = 8          # next_alarm(): hoje + 7 dias cobre qualquer dia da semana
UTC = ZoneInfo("UTC")

# Fusos oferecidos na tela (o do TOML entra na lista se não estiver nela).
TIMEZONES = [
    "America/Noronha", "America/Sao_Paulo", "America/Manaus", "America/Rio_Branco",
    "UTC", "America/New_York", "Europe/Lisbon", "Europe/London", "Europe/Paris",
    "Asia/Tokyo", "Asia/Taipei", "Australia/Sydney",
]

DAY_ABBR = ["SEG", "TER", "QUA", "QUI", "SEX", "SAB", "DOM"]   # índice = weekday()
TIMESYNC_DIR = Path("/run/systemd/timesync")


def _is_int(value) -> bool:
    """True para int de verdade (em Python, True/False também são int)."""
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value) -> bool:
    """True para int ou float, excluindo bool."""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


@dataclass
class Alarm:
    """Um alarme do despertador.

    Atributos:
        enabled: ligado ou não.
        hour, minute: horário local (no fuso do ``AlarmClock``).
        days: dias da semana em que toca, no padrão ``datetime.weekday()``
            (segunda = 0 ... domingo = 6). Vazio = toca uma vez e se desliga.
    """
    enabled: bool = False
    hour: int = 7
    minute: int = 0
    days: set[int] = field(default_factory=set)

    def to_json(self) -> dict:
        """Dicionário para gravar no arquivo de estado."""
        return {"enabled": self.enabled, "hour": self.hour, "minute": self.minute,
                "days": sorted(self.days)}

    @classmethod
    def from_json(cls, data) -> Alarm:
        """Cria um alarme a partir do arquivo de estado, de forma tolerante.

        Cada campo com tipo ou faixa errados é ignorado e fica com o padrão;
        um ``data`` que nem é dicionário devolve um alarme padrão.

        Parâmetros:
            data: o que foi lido do JSON (pode ser qualquer coisa).

        Retorno:
            Um ``Alarm`` sempre válido.
        """
        alarm = cls()
        if not isinstance(data, dict):
            return alarm

        if isinstance(data.get("enabled"), bool):
            alarm.enabled = data["enabled"]

        hour = data.get("hour")
        if _is_int(hour) and 0 <= hour <= 23:
            alarm.hour = hour

        minute = data.get("minute")
        if _is_int(minute) and 0 <= minute <= 59:
            alarm.minute = minute

        days = data.get("days")
        if isinstance(days, list):
            alarm.days = {day for day in days if _is_int(day) and 0 <= day <= 6}
        return alarm


def zone(name: str) -> ZoneInfo | None:
    """Fuso IANA pelo nome, ou None se o nome não existir."""
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return None


class AlarmClock:
    """Relógio no fuso escolhido e a lógica dos alarmes (sem tela, sem som).

    Atributos públicos usados pelo resto do app:
        tz_name, tz: nome e objeto do fuso atual.
        alarms: sempre ``N_ALARMS`` alarmes.
        ringing: índice do alarme tocando agora, ou None (o app liga/desliga).
        ring_since: ``time.monotonic()`` em que começou a tocar.
        snooze: ``(índice do alarme, instante em UTC)`` da soneca, ou None.
    """

    def __init__(self, tz_name: str, alarms: list[Alarm] | None = None):
        """
        Parâmetros:
            tz_name: fuso IANA; se for inválido, usa UTC (mas guarda o nome).
            alarms: alarmes salvos; sobras são cortadas e faltas completadas
                com alarmes padrão (desligados).
        """
        self.tz_name = tz_name
        self.tz = zone(tz_name) or UTC
        kept_alarms = list(alarms or [])[:N_ALARMS]
        missing_count = N_ALARMS - len(kept_alarms)
        self.alarms = kept_alarms + [Alarm() for _ in range(missing_count)]
        self.ringing: int | None = None
        self.ring_since = 0.0
        self.snooze: tuple[int, datetime] | None = None
        # índice do alarme -> (data, hora, minuto) do último disparo
        self._fired_on: dict[int, tuple[date, int, int]] = {}
        self._last_check: datetime | None = None

    def set_tz(self, name: str) -> bool:
        """Troca o fuso. Retorna False (e não muda nada) se o nome for inválido."""
        new_zone = zone(name)
        if new_zone is None:
            return False
        self.tz_name = name
        self.tz = new_zone
        return True

    def now(self, utc: datetime | None = None) -> datetime:
        """Hora local no fuso atual.

        Parâmetros:
            utc: instante a converter (os testes passam um fixo); None = agora.
        """
        if utc is None:
            utc = datetime.now(UTC)
        return utc.astimezone(self.tz)

    def check(self, utc: datetime | None = None) -> int | None:
        """Decide se algum alarme deve começar a tocar agora.

        Chamado a cada quadro pelo laço principal.

        Fluxo:
            1. Soneca vencida? Toca o alarme dela (mesmo que esteja desligado).
            2. Monta a lista de minutos a testar: normalmente só o atual;
               depois de um salto de até ``CATCHUP_S``, todos os pulados.
            3. Testa minuto a minuto; o primeiro alarme que casar é devolvido.

        Parâmetros:
            utc: instante a testar (testes); None = agora.

        Retorno:
            Índice do alarme que deve tocar, ou None.
        """
        local_now = self.now(utc)
        if self.snooze is not None and local_now >= self.snooze[1]:
            alarm_index = self.snooze[0]
            self.snooze = None
            return alarm_index

        previous_check = self._last_check
        self._last_check = local_now
        for minute in self._minutes_to_check(previous_check, local_now):
            fired_index = self._match(minute)
            if fired_index is None:
                continue
            if minute is not local_now:
                # pode haver outro alarme mais adiante no salto: a próxima
                # chamada recomeça a partir deste minuto
                self._last_check = minute
            return fired_index
        return None

    def _minutes_to_check(self, previous: datetime | None,
                          local_now: datetime) -> list[datetime]:
        """Minutos a testar desde a última chamada.

        Retorno:
            ``[local_now]`` no caso normal; com um salto entre 1 min e
            ``CATCHUP_S``, todos os minutos cheios depois de ``previous`` até
            ``local_now``.
        """
        if previous is None:
            return [local_now]
        elapsed_s = (local_now - previous).total_seconds()
        if not SECONDS_PER_MINUTE <= elapsed_s <= CATCHUP_S:
            return [local_now]

        minutes = []
        minute = previous.replace(second=0, microsecond=0) + timedelta(minutes=1)
        while minute <= local_now:
            # o offset do fuso pode mudar no meio do salto (horário de verão)
            minutes.append(minute.astimezone(self.tz))
            minute += timedelta(minutes=1)
        return minutes

    def _match(self, local_time: datetime) -> int | None:
        """Primeiro alarme que deve tocar neste minuto (e o marca como tocado).

        Um alarme "uma vez" (sem dias) se desliga aqui mesmo.
        """
        for index, alarm in enumerate(self.alarms):
            if not alarm.enabled:
                continue
            if (alarm.hour, alarm.minute) != (local_time.hour, local_time.minute):
                continue
            if alarm.days and local_time.weekday() not in alarm.days:
                continue
            fired_key = (local_time.date(), alarm.hour, alarm.minute)
            if self._fired_on.get(index) == fired_key:
                continue                        # já tocou neste minuto desta data

            self._fired_on[index] = fired_key
            if not alarm.days:
                alarm.enabled = False           # "uma vez": desliga depois de tocar
            return index
        return None

    def snooze_now(self, minutes: float, utc: datetime | None = None) -> None:
        """Cala o alarme que está tocando e marca a soneca para daqui a ``minutes``."""
        if self.ringing is None:
            return
        snooze_until = self.now(utc) + timedelta(minutes=minutes)
        self.snooze = (self.ringing, snooze_until)
        self.ringing = None

    def next_alarm(self, utc: datetime | None = None) -> tuple[int, datetime] | None:
        """Próximo disparo nos próximos ``DAYS_TO_SEARCH`` dias (soneca inclusa).

        Retorno:
            ``(índice do alarme, instante local)`` do mais próximo, ou None.
        """
        start = self.now(utc).replace(second=0, microsecond=0)
        best: tuple[int, datetime] | None = None
        if self.snooze is not None:
            best = (self.snooze[0], self.snooze[1].astimezone(self.tz))

        for index, alarm in enumerate(self.alarms):
            if not alarm.enabled:
                continue
            candidate = self._next_time_of(alarm, start)
            if candidate is None:
                continue
            if best is None or candidate < best[1]:
                best = (index, candidate)
        return best

    def _next_time_of(self, alarm: Alarm, start: datetime) -> datetime | None:
        """Primeira vez, depois de ``start``, em que ``alarm`` tocaria."""
        for days_ahead in range(DAYS_TO_SEARCH):
            day = datetime.fromordinal(start.toordinal() + days_ahead)
            candidate = day.replace(hour=alarm.hour, minute=alarm.minute, tzinfo=self.tz)
            if candidate <= start:
                continue
            if alarm.days and candidate.weekday() not in alarm.days:
                continue
            return candidate
        return None

    def to_json(self) -> list[dict]:
        """Os alarmes, para o arquivo de estado."""
        return [alarm.to_json() for alarm in self.alarms]

    def snooze_json(self) -> dict | None:
        """A soneca, para o arquivo de estado (None = sem soneca)."""
        if self.snooze is None:
            return None
        alarm_index, until = self.snooze
        return {"alarm": alarm_index, "until": until.timestamp()}

    def load_snooze(self, data) -> None:
        """Restaura a soneca do arquivo de estado; dado inválido é ignorado."""
        if not isinstance(data, dict):
            return
        alarm_index = data.get("alarm")
        until = data.get("until")
        if not _is_int(alarm_index) or not 0 <= alarm_index < len(self.alarms):
            return
        if not _is_number(until):
            return
        try:
            self.snooze = (alarm_index, datetime.fromtimestamp(until, UTC))
        except (OverflowError, OSError, ValueError):
            pass                                # instante absurdo: sem soneca


def clock_synced(base: Path = TIMESYNC_DIR) -> bool | None:
    """Diz se o relógio já foi acertado pelo NTP (systemd-timesyncd).

    O timesyncd cria o arquivo ``synchronized`` ao acertar o relógio pela
    primeira vez. Sem o diretório (chrony, PC sem timesyncd) não dá para
    saber e a tela não avisa nada.

    Retorno:
        True/False conforme o timesyncd; None = não dá para saber.
    """
    if not base.is_dir():
        return None
    return (base / "synchronized").exists()


def next_alarm_text(clock: AlarmClock, utc: datetime | None = None) -> str:
    """Texto curto do próximo alarme para o painel ("ALARME QUA 07:30").

    Retorno:
        "SONECA ATÉ HH:MM" se o próximo for a soneca, "ALARME DIA HH:MM" para
        um alarme, ou "" se nada estiver ligado.
    """
    upcoming = clock.next_alarm(utc)
    if upcoming is None:
        return ""
    if clock.snooze is not None and clock.snooze == upcoming:
        when = upcoming[1]
        return f"SONECA ATÉ {when:%H:%M}"
    when = upcoming[1]
    return f"ALARME {DAY_ABBR[when.weekday()]} {when:%H:%M}"
