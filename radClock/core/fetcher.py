"""
A thread que consulta as fontes de tráfego e a escolha de fonte na tela.

Peças deste módulo:
- `Snapshot`: a foto imutável do tráfego que a tela desenha;
- `Fetcher`: thread que consulta a fonte e publica o Snapshot;
- `SourceChoices`: as fontes que a tela oferece e o Fetcher de cada uma.

Regra de ouro (mesma lição da task radar_fetch do ESP32): rede e disco nunca
rodam na thread da tela. O Fetcher publica um Snapshot imutável e a tela só
lê o mais recente.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field, replace

from radClock.core.config import API_PROVIDERS, Config
from radClock.core.model import Aircraft
from radClock.core.net import SourceError
from radClock.core.opensky import OpenSkySource
from radClock.core.sources import ApiSource, ReadsbSource

log = logging.getLogger(__name__)

# as três fontes têm o mesmo `name` e o mesmo `fetch()`
Source = ReadsbSource | ApiSource | OpenSkySource


# ---------------------------------------------------------------- coleta

@dataclass(frozen=True)
class Snapshot:
    """
    Foto imutável do tráfego, publicada pelo Fetcher e lida pela tela.
    """

    aircraft: list[Aircraft] = field(default_factory=list)
    source: str = "---"         # nome da fonte que forneceu as aeronaves
    updated: float = 0.0        # time.monotonic() da última leitura boa (0 = nunca)
    error: str = ""             # vazio = a última tentativa da fonte principal deu certo
    interval_s: float = 1.0     # intervalo de quem publicou (para julgar o atraso)
    quota: int | None = None    # OpenSky: créditos que restam hoje

    def find(self, hex_id: str | None) -> Aircraft | None:
        """A aeronave com este hex, ou None se não está na lista (ou hex_id é None)."""
        for aircraft in self.aircraft:
            if aircraft.hex == hex_id:
                return aircraft
        return None


class Fetcher(threading.Thread):
    """
    Thread que consulta a fonte principal (e a reserva) e publica Snapshots.

    Fluxo de cada volta do laço (`run`):
    1. tenta a fonte principal; deu certo -> publica e espera `interval_s`;
    2. falhou e há reserva -> publica as aeronaves da reserva (com o erro da
       principal) e espera `fallback_interval_s`;
    3. falhou sem reserva (ou a reserva também falhou) -> mantém as últimas
       aeronaves com o erro preenchido; a tela as envelhece até sumirem.

    A thread nunca morre: qualquer exceção é registrada e o laço continua.

    Parâmetros do construtor:
        primary: a fonte principal.
        fallback: a fonte de reserva, ou None.
        interval_s: intervalo entre consultas à principal.
        fallback_interval_s: intervalo enquanto a reserva está em uso.
    """

    # folga além das esperas normais antes de considerar a thread presa:
    # cada leitura tem timeout de 10 s, principal + reserva somam ~20 s
    STUCK_MARGIN_S = 60.0

    def __init__(self, primary: Source, fallback: Source | None,
                 interval_s: float, fallback_interval_s: float):
        super().__init__(name="fetcher", daemon=True)
        self.primary = primary
        self.fallback = fallback
        self.interval_s = interval_s
        self.fallback_interval_s = fallback_interval_s
        self._lock = threading.Lock()
        self._snapshot = Snapshot(source=primary.name, interval_s=interval_s)
        self._stop_event = threading.Event()
        self._last_error = ""                   # para não repetir o mesmo aviso no log
        self.beat = time.monotonic()            # início da última volta do laço (ver stuck)

    def snapshot(self) -> Snapshot:
        """
        O Snapshot mais recente (seguro para chamar de outra thread).
        """
        with self._lock:
            return self._snapshot

    def stop(self) -> None:
        """
        Pede o fim da thread; ela termina depois da leitura em curso.
        """
        self._stop_event.set()

    def stuck(self, now: float) -> bool:
        """
        True se a thread morreu ou não completa uma volta há muito tempo.

        Parâmetros:
            now: o time.monotonic() atual.

        Retorno:
            True quando o laço principal deve trocar este Fetcher por outro.
        """
        if not self.is_alive():
            return True
        longest_interval = max(self.interval_s, self.fallback_interval_s)
        limit_s = self.STUCK_MARGIN_S + 2 * longest_interval
        return now - self.beat > limit_s

    def _publish(self, aircraft: list[Aircraft], source: str, error: str,
                 interval_s: float, quota: int | None = None) -> None:
        """Troca o Snapshot por um novo, com as aeronaves recém-lidas."""
        new_snapshot = Snapshot(aircraft, source, time.monotonic(), error, interval_s, quota)
        with self._lock:
            self._snapshot = new_snapshot

    def _fail(self, error: str) -> None:
        """Marca o erro e mantém as últimas aeronaves (a tela as envelhece)."""
        with self._lock:
            self._snapshot = replace(self._snapshot, error=error)

    def _log_once(self, error: str, with_traceback: bool = False) -> None:
        """Registra o erro só quando ele muda, para não inundar o log."""
        if error != self._last_error:
            if with_traceback:
                log.exception("%s", error)
            else:
                log.warning("%s", error)
        self._last_error = error

    def _fetch_primary(self) -> None:
        """Lê a fonte principal e publica. Exceções: as de `fetch()`."""
        aircraft = self.primary.fetch()
        # só o OpenSky tem `remaining` (créditos do dia)
        quota = getattr(self.primary, "remaining", None)
        self._publish(aircraft, self.primary.name, "", self.interval_s, quota)
        if self._last_error:
            log.info("%s voltou", self.primary.name)
        self._last_error = ""

    def _fetch_fallback(self, primary_error: str) -> None:
        """Lê a reserva e publica; se ela também falhar, registra os dois erros.

        Parâmetros:
            primary_error: o texto do erro da fonte principal.
        """
        try:
            aircraft = self.fallback.fetch()
        except SourceError as error:
            self._fail(f"{primary_error} / {self.fallback.name}: {error}")
            return
        except Exception as error:      # noqa: BLE001 -- a thread não pode morrer
            log.exception("%s: erro interno", self.fallback.name)
            self._fail(f"{primary_error} / {self.fallback.name}: {error}")
            return
        self._publish(aircraft, self.fallback.name, primary_error, self.fallback_interval_s)

    def _poll_once(self) -> float:
        """Uma volta completa: principal, e reserva se preciso.

        Retorno: quantos segundos esperar até a próxima volta.
        """
        try:
            self._fetch_primary()
            return self.interval_s
        except SourceError as error:
            primary_error = f"{self.primary.name}: {error}"
            self._log_once(primary_error)
        except Exception as error:      # noqa: BLE001 -- a thread não pode morrer
            # defeito, não falha de rede (JSON com formato inesperado, por
            # exemplo): traceback no log uma vez e segue tentando
            internal_error = f"erro interno: {type(error).__name__}: {error}"
            self._log_once(internal_error, with_traceback=True)
            self._fail(internal_error)
            return self.interval_s

        if self.fallback is None:
            self._fail(primary_error)
            return self.interval_s
        self._fetch_fallback(primary_error)
        return self.fallback_interval_s

    def run(self) -> None:
        """Laço da thread: uma volta, espera, repete até `stop()`."""
        while not self._stop_event.is_set():
            self.beat = time.monotonic()
            delay_s = self._poll_once()
            self._stop_event.wait(delay_s)


# ---------------------------------------------------------------- escolha na tela

class SourceChoices:
    """Fontes que a tela oferece (READSB, OPENSKY, ADSB.LOL, ADSB.FI, CUSTOM)
    e o Fetcher de cada uma.

    As fontes de internet ficam em cache: trocar de fonte e voltar não zera o
    backoff (nem o token do OpenSky), então alternar na tela não serve para
    furar o limite de taxa de uma API. OPENSKY só aparece com credencial.

    Atributos:
        names: nomes das fontes, na ordem em que a tela as alterna.
    """

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.names = ["readsb"]
        if cfg.opensky.configured:
            self.names.append("opensky")
        self.names.extend(API_PROVIDERS)
        if cfg.api.provider == "custom":
            self.names.append("custom")
        self._readsb = ReadsbSource(cfg.source)
        self._api_sources: dict[str, ApiSource] = {}
        self._opensky: OpenSkySource | None = None

    @property
    def default(self) -> str:
        """A fonte escolhida no TOML ([source] kind, ou o provedor de [api])."""
        kind = self.cfg.source.kind
        if kind in ("readsb", "opensky"):
            return kind
        return self.cfg.api.provider

    def _max_radius_km(self) -> float:
        """Raio das consultas pela internet: o maior alcance da tela."""
        return max(self.cfg.radar.ranges_km)

    def opensky(self) -> OpenSkySource:
        """A fonte OpenSky (criada na primeira chamada e reaproveitada)."""
        if self._opensky is None:
            station = self.cfg.station
            self._opensky = OpenSkySource(self.cfg.opensky, station.lat, station.lon,
                                          self._max_radius_km())
        return self._opensky

    def next(self, name: str) -> str:
        """A fonte seguinte a `name` na lista (volta ao início no fim).

        Um nome desconhecido devolve a primeira fonte.
        """
        if name in self.names:
            position = self.names.index(name)
        else:
            position = -1
        return self.names[(position + 1) % len(self.names)]

    def api(self, provider: str) -> ApiSource:
        """A ApiSource de um provedor (criada na primeira chamada e reaproveitada)."""
        if provider not in self._api_sources:
            api_cfg = self.cfg.api
            # a URL do TOML só vale para o provedor "custom"
            if provider == "custom":
                url = api_cfg.url
            else:
                url = ""
            provider_cfg = replace(api_cfg, provider=provider, url=url)
            station = self.cfg.station
            self._api_sources[provider] = ApiSource(provider_cfg, station.lat, station.lon,
                                                    self._max_radius_km())
        return self._api_sources[provider]

    def fetcher(self, name: str) -> Fetcher:
        """Cria um Fetcher novo (ainda não iniciado) para a fonte `name`."""
        api_cfg = self.cfg.api
        if name == "readsb":
            # a reserva é sempre a API do TOML, qualquer que tenha sido a última escolha
            fallback = None
            if api_cfg.fallback:
                fallback = self.api(api_cfg.provider)
            return Fetcher(self._readsb, fallback, self.cfg.source.interval_s,
                           api_cfg.interval_s)
        if name == "opensky":
            # como no firmware: OpenSky falhou, a tela segue com uma API gratuita
            opensky_cfg = self.cfg.opensky
            fallback = None
            if opensky_cfg.fallback:
                fallback = self.api(opensky_cfg.fallback)
            return Fetcher(self.opensky(), fallback, opensky_cfg.interval_s,
                           api_cfg.interval_s)
        return Fetcher(self.api(name), None, api_cfg.interval_s, api_cfg.interval_s)
