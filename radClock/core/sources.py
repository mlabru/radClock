"""
Fontes de tráfego: de onde vem a lista de aeronaves.

Peças deste módulo:
- `ReadsbSource`: lê o aircraft.json do receptor local (readsb);
- `ApiSource`: consulta uma API pública no formato do readsb (adsb.lol...).

Toda fonte tem o mesmo contrato: um atributo `name` (o texto do painel) e um
método `fetch()` que devolve a lista de Aircraft ou levanta `SourceError`.
`opensky.OpenSkySource` segue o mesmo contrato. Quem consulta as fontes em
segundo plano é o `fetcher.Fetcher`.
"""
from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable

from radClock.core.config import API_PROVIDERS, ApiCfg, SourceCfg
from radClock.core.model import Aircraft, parse_aircraft_json
from radClock.core.net import RateLimited, SourceError, http_json

log = logging.getLogger(__name__)

READSB_URL_TIMEOUT_S = 5.0
KM_PER_NAUTICAL_MILE = 1.852


# ---------------------------------------------------------------- fontes

class ReadsbSource:
    """
    Lê o aircraft.json do readsb: arquivo local (padrão) ou URL.

    Além de ler, detecta receptor morto: se o campo "now" do arquivo for mais
    velho que `max_age_s`, o readsb travou ou o dongle caiu.
    """

    name = "READSB"

    def __init__(self, cfg: SourceCfg):
        self.cfg = cfg

    def _read_document(self) -> dict:
        """Lê o JSON da URL ou do arquivo. Exceção: SourceError."""
        if self.cfg.url:
            return http_json(self.cfg.url, timeout=READSB_URL_TIMEOUT_S)
        # o readsb grava num arquivo temporário e renomeia: nunca lemos meio arquivo
        try:
            with open(self.cfg.path, "rb") as file:
                return json.load(file)
        except (OSError, ValueError) as error:
            raise SourceError(str(error)) from error

    def _check_not_frozen(self, document: dict) -> None:
        """Erro se o arquivo existe mas parou de ser atualizado."""
        if self.cfg.max_age_s <= 0:
            return
        written_at = document.get("now")
        if not isinstance(written_at, (int, float)):
            return
        age_s = time.time() - written_at
        if age_s > self.cfg.max_age_s:
            raise SourceError(f"aircraft.json parado há {age_s:.0f}s")

    def fetch(self) -> list[Aircraft]:
        """Lê as aeronaves atuais.

        Retorno: lista de Aircraft.
        Exceções: SourceError se não deu para ler ou se o arquivo está parado.
        """
        document = self._read_document()
        self._check_not_frozen(document)
        return parse_aircraft_json(document)


class ApiSource:
    """
    API pública no formato do readsb (adsb.lol, adsb.fi ou URL própria).

    Backoff exponencial: um 429, ou duas falhas de conexão seguidas (alguns
    servidores derrubam o TLS em vez de responder 429), suspendem os pedidos
    por 30 s; cada nova suspensão dobra o tempo (60 s, 120 s...) até 10 min.
    Uma consulta bem-sucedida volta a espera para 30 s. Insistir no mesmo
    ritmo só prolongaria o bloqueio.

    Parâmetros do construtor:
        cfg: seção [api] do TOML (provedor e, para "custom", a URL).
        lat, lon: centro da consulta (a estação).
        radius_km: raio da consulta; limitado a MAX_RADIUS_NM.
        clock: relógio monotônico (injetável nos testes).
        get: função que baixa um JSON (injetável nos testes).
    """

    BACKOFF_MIN_S = 30.0
    BACKOFF_MAX_S = 600.0
    MAX_RADIUS_NM = 250.0                    # limite das APIs v2
    FAILURES_BEFORE_BACKOFF = 2

    def __init__(self, cfg: ApiCfg, lat: float, lon: float, radius_km: float,
                 clock: Callable[[], float] = time.monotonic,
                 get: Callable[[str], dict] = http_json):
        if cfg.provider == "custom":
            url_template = cfg.url
        else:
            url_template = API_PROVIDERS[cfg.provider]
        radius_nm = min(radius_km / KM_PER_NAUTICAL_MILE, self.MAX_RADIUS_NM)
        self.url = url_template.format(lat=lat, lon=lon, radius_nm=radius_nm)
        self.name = cfg.provider.upper()
        self._clock = clock
        self._get = get
        self._cooldown_until = 0.0              # instante (clock) em que a espera acaba
        self._penalty = self.BACKOFF_MIN_S      # duração da próxima espera
        self._consecutive_failures = 0

    def _back_off(self, reason: str) -> None:
        """Começa uma espera e dobra a duração da próxima."""
        self._cooldown_until = self._clock() + self._penalty
        log.warning("%s: %s -- em espera por %.0fs", self.name, reason, self._penalty)
        self._penalty = min(self._penalty * 2, self.BACKOFF_MAX_S)

    def fetch(self) -> list[Aircraft]:
        """Consulta a API, respeitando o backoff.

        Retorno: lista de Aircraft.
        Exceções:
            SourceError: em espera, falha de rede ou resposta ruim.
            RateLimited: a API respondeu 429.
        """
        if self._clock() < self._cooldown_until:
            # texto fixo (sem os segundos restantes) para o log não repetir a cada ciclo
            raise SourceError("em espera (backoff)")
        try:
            document = self._get(self.url)
        except RateLimited:
            self._back_off("limite de taxa (429)")
            raise
        except SourceError:
            self._consecutive_failures += 1
            if self._consecutive_failures >= self.FAILURES_BEFORE_BACKOFF:
                self._back_off(f"{self._consecutive_failures} falhas seguidas")
            raise
        self._consecutive_failures = 0
        self._penalty = self.BACKOFF_MIN_S
        return parse_aircraft_json(document)

