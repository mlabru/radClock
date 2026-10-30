"""
Tipo e matrícula pela internet, só para o alvo selecionado.

O OpenSky não informa o tipo da aeronave, e o readsb só informa com
`--db-file`. Sem tipo não há ficha nem silhueta. Como no firmware, a busca é
feita uma vez por aeronave e só para a selecionada: varrer o tráfego todo
estouraria o limite de uma API gratuita, e a ficha só mostra uma de cada vez.

O firmware usava só o adsbdb, mas a cobertura dele no Brasil é fraca (em
2026-10, 0 de 4 aeronaves de São Paulo). As bases são consultadas em cadeia:
1. adsb.lol `/v2/hex`: conhece toda aeronave que está rastreando agora (é o
   caso do alvo na tela) e devolve o `t`/`r` da base do readsb (4 de 4 no
   mesmo teste);
2. hexdb.io;
3. adsbdb: para o que o adsb.lol não está vendo.
A primeira que tiver o tipo vence; 404 ou resposta sem tipo passa para a
próxima.

Resultado em cache (inclusive "não existe", que não é buscado de novo). Falha
de rede não entra no cache, mas só é tentada de novo depois de RETRY_S. A rede
roda numa thread do pool, nunca na da tela: `get()` devolve None até a
resposta chegar.
"""
from __future__ import annotations

import logging
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor

from radClock.core.net import HttpStatus, SourceError, http_json

log = logging.getLogger(__name__)

RETRY_S = 60.0          # espera antes de tentar de novo uma aeronave cuja busca falhou
MAX_ENTRIES = 500       # tamanho do cache (as mais antigas saem primeiro)

TypeAndRegistration = tuple[str, str]
UNKNOWN: TypeAndRegistration = ("", "")


def _parse_adsb_lol(document) -> TypeAndRegistration:
    """Resposta do adsb.lol: {"ac": [{"t": tipo, "r": matrícula}]}."""
    if not isinstance(document, dict):
        return UNKNOWN
    rows = document.get("ac") or [{}]
    first = rows[0]
    return str(first.get("t") or ""), str(first.get("r") or "")


def _parse_hexdb(document) -> TypeAndRegistration:
    """Resposta do hexdb.io: {"ICAOTypeCode": tipo, "Registration": matrícula}."""
    if not isinstance(document, dict):
        return UNKNOWN
    return str(document.get("ICAOTypeCode") or ""), str(document.get("Registration") or "")


def _parse_adsbdb(document) -> TypeAndRegistration:
    """Resposta do adsbdb: {"response": {"aircraft": {"icao_type", "registration"}}}."""
    if not isinstance(document, dict):
        return UNKNOWN
    response = document.get("response") or {}
    if not isinstance(response, dict):
        return UNKNOWN
    aircraft = response.get("aircraft")
    if not isinstance(aircraft, dict):
        return UNKNOWN
    return str(aircraft.get("icao_type") or ""), str(aircraft.get("registration") or "")


# (modelo de URL, função que extrai tipo e matrícula), na ordem de consulta
PROVIDERS = [
    ("https://api.adsb.lol/v2/hex/{hex}", _parse_adsb_lol),
    ("https://hexdb.io/api/v1/aircraft/{hex}", _parse_hexdb),
    ("https://api.adsbdb.com/v0/aircraft/{hex}", _parse_adsbdb),
]


class TypeLookup:
    """Busca e guarda em cache o tipo e a matrícula de uma aeronave.

    Uso: chamar `get(hex)` a cada quadro; a primeira chamada dispara a busca
    em segundo plano e devolve None; quando a resposta chega, as chamadas
    seguintes devolvem o resultado do cache.

    Parâmetros do construtor:
        get: função que baixa um JSON (injetável nos testes).
        clock: relógio monotônico (injetável nos testes).
    """

    def __init__(self, get: Callable[[str], dict] = http_json,
                 clock: Callable[[], float] = time.monotonic):
        self._get = get
        self._clock = clock
        self._lock = threading.Lock()           # protege os três dicionários abaixo
        self._cache: OrderedDict[str, TypeAndRegistration] = OrderedDict()
        self._pending: set[str] = set()         # buscas em andamento
        self._failed_at: dict[str, float] = {}  # instante da última falha de rede
        self._pool = ThreadPoolExecutor(1, thread_name_prefix="lookup")

    def _recently_failed(self, icao_hex: str) -> bool:
        """True se a última busca desta aeronave falhou há menos de RETRY_S."""
        failed_at = self._failed_at.get(icao_hex)
        if failed_at is None:
            return False
        return self._clock() - failed_at < RETRY_S

    def get(self, icao_hex: str) -> TypeAndRegistration | None:
        """Tipo ICAO e matrícula da aeronave.

        Parâmetros:
            icao_hex: o endereço ICAO24 em minúsculas.

        Retorno:
            (tipo, matrícula); strings vazias se nenhuma base conhece; None
            enquanto a busca está em andamento (a primeira chamada a dispara).
        """
        # endereço não-ICAO (TIS-B): não há o que buscar
        if not icao_hex or icao_hex.startswith("~"):
            return UNKNOWN
        with self._lock:
            if icao_hex in self._cache:
                self._cache.move_to_end(icao_hex)
                return self._cache[icao_hex]
            if icao_hex in self._pending or self._recently_failed(icao_hex):
                return None
            self._pending.add(icao_hex)
        self._pool.submit(self._fetch, icao_hex)
        return None

    def _query(self, icao_hex: str) -> TypeAndRegistration | None:
        """Consulta as bases em ordem até achar o tipo (roda na thread do pool).

        Retorno:
            (tipo em maiúsculas, matrícula) da primeira base que tem o tipo;
            ("", matrícula) se nenhuma conhece o tipo; None se alguma falhou
            (rede) e nenhuma achou: vale tentar de novo depois.
        """
        some_provider_failed = False
        registration_seen = ""          # matrícula de uma base que não tinha o tipo
        for url_template, parse in PROVIDERS:
            try:
                aircraft_type, registration = parse(self._get(url_template.format(hex=icao_hex)))
            except HttpStatus as error:
                # 404: esta base não conhece a aeronave; outro código conta como falha
                if error.code != 404:
                    some_provider_failed = True
                continue
            except SourceError as error:
                log.warning("tipo de %s: %s", icao_hex, error)
                some_provider_failed = True
                continue
            except Exception as error:      # noqa: BLE001 -- JSON estranho, defeito: próxima
                log.warning("tipo de %s: %r", icao_hex, error)
                some_provider_failed = True
                continue
            if not registration_seen:
                registration_seen = registration
            if aircraft_type:
                return aircraft_type.upper(), registration or registration_seen
        if some_provider_failed:
            return None
        return "", registration_seen

    def _fetch(self, icao_hex: str) -> None:
        """Faz a busca e grava o resultado (roda na thread do pool)."""
        result = self._query(icao_hex)
        with self._lock:
            self._pending.discard(icao_hex)
            if result is None:
                self._failed_at[icao_hex] = self._clock()
                return
            self._failed_at.pop(icao_hex, None)
            self._cache[icao_hex] = result
            while len(self._cache) > MAX_ENTRIES:
                self._cache.popitem(last=False)

    def stop(self) -> None:
        """Encerra o pool sem esperar a busca em curso."""
        self._pool.shutdown(wait=False, cancel_futures=True)
