"""OpenSky Network como fonte de tráfego (REST `states/all`), como no firmware.

Autenticação: desde 2025 o OpenSky só aceita OAuth2 `client_credentials` (o
usuário/senha antigo foi desligado). O token vale 30 min e é renovado 1 min
antes; um 401 no meio do caminho força um token novo e uma nova tentativa.

Cota: créditos por dia, e o custo de cada consulta cresce com a área da
caixa em graus² (até 25 = 1 crédito). A caixa é o quadrado em volta do
círculo de `radius_km`. Como o grau de longitude encolhe com a latitude, o
raio que cabe em 1 crédito também encolhe: ~265 km no Brasil, ~220 km a 45°,
~175 km a 60°. O custo é calculado na partida e entra no aviso de cota. O
cabeçalho X-Rate-Limit-Remaining traz o saldo, mostrado no painel. Um 429
traz X-Rate-Limit-Retry-After-Seconds, que pode ser de horas (até a cota do
dia voltar): o OpenSky fica parado esse tempo e a tela usa a reserva.

Falhas: como no firmware, quando o OpenSky falha a tela passa para uma API
gratuita (`fallback`) por COOLDOWN_S e depois tenta de novo. Uma falha
isolada de rede não conta; duas seguidas, sim. Credencial recusada espera
AUTH_COOLDOWN_S, porque não vai melhorar sozinha.

O OpenSky não informa o tipo da aeronave: ver lookup.TypeLookup.
"""
from __future__ import annotations

import json
import logging
import math
import time
import urllib.parse
from collections.abc import Callable
from pathlib import Path

from radClock.core.config import OpenSkyCfg
from radClock.core.model import Aircraft, number_or_none
from radClock.core.net import HttpStatus, RateLimited, SourceError, http_request

log = logging.getLogger(__name__)

TOKEN_URL = ("https://auth.opensky-network.org/auth/realms/opensky-network"
             "/protocol/openid-connect/token")
STATES_URL = "https://opensky-network.org/api/states/all"
COOLDOWN_S = 600.0              # firmware: 10 min na reserva depois de uma falha
AUTH_COOLDOWN_S = 3600.0        # credencial recusada: 1 h
TOKEN_MARGIN_S = 60.0           # renova o token 1 min antes de vencer
TOKEN_MIN_LIFETIME_S = 30.0
TOKEN_DEFAULT_LIFETIME_S = 1800.0
FAILURES_BEFORE_PAUSE = 2
SECONDS_PER_DAY = 86400

M_TO_FT = 3.28084
MS_TO_KT = 1.943844
MS_TO_FPM = 196.8504
KM_PER_DEGREE_LAT = 110.574
KM_PER_DEGREE_LON_AT_EQUATOR = 111.320

# posições no vetor de estado do `states/all` (documentação do OpenSky)
STATE_ICAO24 = 0
STATE_CALLSIGN = 1
STATE_TIME_POSITION = 3
STATE_LONGITUDE = 5
STATE_LATITUDE = 6
STATE_BARO_ALTITUDE = 7
STATE_ON_GROUND = 8
STATE_VELOCITY = 9
STATE_TRUE_TRACK = 10
STATE_VERTICAL_RATE = 11
STATE_GEO_ALTITUDE = 13
STATE_SQUAWK = 14
STATE_CATEGORY = 17             # só existe com extended=1
STATE_MIN_FIELDS = 17

# tabela de créditos do OpenSky: (área máxima em graus², créditos)
CREDIT_TABLE = [(25, 1), (100, 2), (400, 3)]
CREDITS_ABOVE_TABLE = 4


def load_credentials(cfg: OpenSkyCfg) -> tuple[str, str]:
    """Lê o par (client_id, client_secret).

    Se `cfg.credentials` aponta um arquivo (o credentials.json baixado do
    site do OpenSky), lê dele; senão usa os campos do TOML.

    Exceções:
        ValueError: o arquivo não existe ou não tem clientId/clientSecret.
    """
    if not cfg.credentials:
        return cfg.client_id, cfg.client_secret
    try:
        document = json.loads(Path(cfg.credentials).read_text(encoding="utf-8"))
        return str(document["clientId"]), str(document["clientSecret"])
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise ValueError(f"[opensky] credentials {cfg.credentials}: {error}") from error


def bbox(lat: float, lon: float, radius_km: float) -> tuple[float, float, float, float]:
    """Caixa (lamin, lomin, lamax, lomax) do quadrado em volta do círculo.

    O grau de longitude encolhe com o cosseno da latitude; o cosseno é
    limitado a 0,01 para não dividir por zero perto dos polos.
    """
    half_height_deg = radius_km / KM_PER_DEGREE_LAT
    cos_lat = max(math.cos(math.radians(lat)), 0.01)
    half_width_deg = radius_km / (KM_PER_DEGREE_LON_AT_EQUATOR * cos_lat)
    return (max(lat - half_height_deg, -90.0), max(lon - half_width_deg, -180.0),
            min(lat + half_height_deg, 90.0), min(lon + half_width_deg, 180.0))


def credits_per_call(box: tuple[float, float, float, float]) -> int:
    """Créditos que uma consulta com esta caixa custa.

    Tabela do OpenSky: 0-25 graus² = 1, 25-100 = 2, 100-400 = 3, mais = 4.
    """
    lat_min, lon_min, lat_max, lon_max = box
    area_deg2 = (lat_max - lat_min) * (lon_max - lon_min)
    for max_area, credits in CREDIT_TABLE:
        if area_deg2 <= max_area:
            return credits
    return CREDITS_ABOVE_TABLE


def _category(value) -> str:
    """Categoria do `extended=1` (0..20) -> categoria ADS-B do readsb (A1..C5).

    2..8 = A1..A7, 9..15 = B1..B7, 16..20 = C1..C5; o resto vira "".
    """
    if not isinstance(value, int) or isinstance(value, bool):
        return ""
    if 2 <= value <= 8:
        return f"A{value - 1}"
    if 9 <= value <= 15:
        return f"B{value - 8}"
    if 16 <= value <= 20:
        return f"C{value - 15}"
    return ""


def _parse_state(state: list, response_time: float | None) -> Aircraft | None:
    """Converte um vetor de estado (unidades SI) em Aircraft (pés, nós, ft/min).

    Parâmetros:
        state: um item da lista "states".
        response_time: o campo "time" da resposta, para calcular a idade da
            posição; None se a resposta não o trouxe.

    Retorno:
        a aeronave, ou None se o vetor é curto demais ou não tem posição.
    """
    if not isinstance(state, list) or len(state) < STATE_MIN_FIELDS:
        return None
    lon = number_or_none(state[STATE_LONGITUDE])
    lat = number_or_none(state[STATE_LATITUDE])
    if lat is None or lon is None:
        return None

    on_ground = state[STATE_ON_GROUND] is True
    altitude_m = number_or_none(state[STATE_BARO_ALTITUDE])
    if altitude_m is None:
        altitude_m = number_or_none(state[STATE_GEO_ALTITUDE])
    if on_ground:
        alt_ft = 0
    elif altitude_m is None:
        alt_ft = None
    else:
        alt_ft = round(altitude_m * M_TO_FT)

    speed_ms = number_or_none(state[STATE_VELOCITY])
    gs_kt = None
    if speed_ms is not None:
        gs_kt = speed_ms * MS_TO_KT

    vertical_ms = number_or_none(state[STATE_VERTICAL_RATE])
    vrate_fpm = None
    if vertical_ms is not None:
        vrate_fpm = vertical_ms * MS_TO_FPM

    position_time = number_or_none(state[STATE_TIME_POSITION])
    seen_pos_s = 0.0
    if response_time is not None and position_time is not None:
        seen_pos_s = max(0.0, response_time - position_time)

    category = ""
    if len(state) > STATE_CATEGORY:
        category = _category(state[STATE_CATEGORY])

    return Aircraft(
        hex=str(state[STATE_ICAO24] or "").lower(),
        lat=lat,
        lon=lon,
        callsign=str(state[STATE_CALLSIGN] or "").strip(),
        alt_ft=alt_ft,
        on_ground=on_ground,
        gs_kt=gs_kt,
        track=number_or_none(state[STATE_TRUE_TRACK]),
        vrate_fpm=vrate_fpm,
        squawk=str(state[STATE_SQUAWK] or ""),
        category=category,
        seen_pos_s=seen_pos_s,
    )


def parse_states(document: dict) -> list[Aircraft]:
    """Converte a resposta do `states/all` em lista de Aircraft.

    Vetores sem posição ou malformados são ignorados.

    Exceções:
        SourceError: a resposta não é um objeto JSON.
    """
    if not isinstance(document, dict):
        raise SourceError("OpenSky: resposta inesperada")
    response_time = number_or_none(document.get("time"))
    aircraft = []
    for state in document.get("states") or []:
        parsed = _parse_state(state, response_time)
        if parsed is not None:
            aircraft.append(parsed)
    return aircraft


class OpenSkySource:
    """Fonte OpenSky, com token OAuth2, controle de cota e pausas.

    Mesmo contrato das fontes de `sources`: atributo `name` e método
    `fetch()`. O atributo `remaining` (créditos do dia) chega ao painel pelo
    Snapshot.quota.

    Parâmetros do construtor:
        cfg: seção [opensky] do TOML.
        lat, lon: a estação.
        radius_km: raio desejado; limitado a `cfg.radius_km`.
        clock: relógio monotônico (injetável nos testes).
        request: função HTTP com a assinatura de `net.http_request`
            (injetável nos testes).
    """

    name = "OPENSKY"

    def __init__(self, cfg: OpenSkyCfg, lat: float, lon: float, radius_km: float,
                 clock: Callable[[], float] = time.monotonic,
                 request: Callable[..., tuple[bytes, object]] = http_request):
        self.cfg = cfg
        self._clock = clock
        self._request = request
        # sem credencial o acesso é anônimo (usado nos testes)
        self._client_id, self._client_secret = load_credentials(cfg)
        box = bbox(lat, lon, min(radius_km, cfg.radius_km))
        self.url = (f"{STATES_URL}?lamin={box[0]:.4f}&lomin={box[1]:.4f}"
                    f"&lamax={box[2]:.4f}&lomax={box[3]:.4f}&extended=1")
        self.credits = credits_per_call(box)
        self._warn_if_over_quota()
        self.remaining: int | None = None       # saldo de créditos do dia (cabeçalho)
        self._token = ""
        self._token_expires_at = 0.0
        self._cooldown_until = 0.0
        self._pause_reason = ""
        self._consecutive_failures = 0

    def _warn_if_over_quota(self) -> None:
        """Avisa na partida se o intervalo configurado gasta mais que a cota."""
        credits_per_day = SECONDS_PER_DAY / self.cfg.interval_s * self.credits
        if self._client_id and credits_per_day > self.cfg.daily_credits:
            log.warning("OpenSky: %d créditos por consulta a cada %.0f s = %.0f/dia, acima "
                        "da cota de %d; a tela vai para a reserva quando ela acabar",
                        self.credits, self.cfg.interval_s, credits_per_day,
                        self.cfg.daily_credits)

    def _token_is_valid(self) -> bool:
        return bool(self._token) and self._clock() < self._token_expires_at

    def _ensure_token(self) -> None:
        """Garante um token válido, pedindo um novo se preciso.

        Sem credencial (acesso anônimo) não faz nada.

        Exceções:
            PermissionError: o OpenSky recusou as credenciais (400/401).
            SourceError: falha de rede ou resposta sem token.
        """
        if not self._client_id or self._token_is_valid():
            return
        body = urllib.parse.urlencode({"grant_type": "client_credentials",
                                       "client_id": self._client_id,
                                       "client_secret": self._client_secret}).encode()
        try:
            data, _headers = self._request(TOKEN_URL, data=body, headers={
                "Content-Type": "application/x-www-form-urlencoded"})
        except HttpStatus as error:
            if error.code in (400, 401):
                raise PermissionError("credenciais recusadas") from error
            raise
        try:
            document = json.loads(data)
            self._token = str(document["access_token"])
            lifetime_s = float(document.get("expires_in") or TOKEN_DEFAULT_LIFETIME_S)
        except (ValueError, KeyError, TypeError) as error:
            raise SourceError(f"token: resposta inválida ({error})") from error
        usable_s = max(lifetime_s - TOKEN_MARGIN_S, TOKEN_MIN_LIFETIME_S)
        self._token_expires_at = self._clock() + usable_s
        log.info("OpenSky: token novo (%.0f min)", lifetime_s / 60)

    def _auth_headers(self) -> dict[str, str]:
        if not self._token:
            return {}
        return {"Authorization": f"Bearer {self._token}"}

    def _get_states(self) -> tuple[bytes, object]:
        """Baixa o `states/all`. Um 401 com credencial pede token novo e tenta uma vez mais."""
        self._ensure_token()
        try:
            return self._request(self.url, headers=self._auth_headers())
        except HttpStatus as error:
            if error.code != 401 or not self._client_id:
                raise
        # token revogado ou vencido antes da hora: pede outro e tenta de novo
        self._token = ""
        self._ensure_token()
        return self._request(self.url, headers=self._auth_headers())

    def _pause(self, seconds: float, reason: str) -> None:
        """Suspende as consultas por `seconds`; enquanto isso a tela usa a reserva."""
        self._cooldown_until = self._clock() + seconds
        self._pause_reason = reason
        log.warning("OpenSky: %s -- usando a reserva por %.0f min", reason, seconds / 60)

    def _read_remaining_credits(self, headers) -> None:
        """Atualiza `remaining` pelo cabeçalho X-Rate-Limit-Remaining, se houver."""
        if headers is None:
            return
        value = headers.get("X-Rate-Limit-Remaining")
        if value is not None and str(value).isdigit():
            self.remaining = int(value)

    def fetch(self) -> list[Aircraft]:
        """Consulta o OpenSky.

        Fluxo: em pausa -> erro imediato (o Fetcher usa a reserva); senão
        baixa os estados, atualiza o saldo de créditos e converte.

        Retorno: lista de Aircraft.
        Exceções:
            SourceError: em pausa, falha de rede, credencial recusada ou JSON ruim.
            RateLimited: cota do dia esgotada (429).
        """
        if self._clock() < self._cooldown_until:
            raise SourceError(f"em espera ({self._pause_reason})")
        try:
            data, headers = self._get_states()
        except RateLimited as error:
            # o OpenSky diz quanto esperar; sem essa informação, COOLDOWN_S
            self._pause(error.retry_after_s or COOLDOWN_S, "cota do dia esgotada (429)")
            self.remaining = 0
            raise
        except PermissionError as error:
            self._pause(AUTH_COOLDOWN_S, str(error))
            raise SourceError(str(error)) from error
        except SourceError:
            self._consecutive_failures += 1
            if self._consecutive_failures >= FAILURES_BEFORE_PAUSE:
                self._pause(COOLDOWN_S, f"{self._consecutive_failures} falhas seguidas")
            raise
        self._consecutive_failures = 0
        self._read_remaining_credits(headers)
        try:
            document = json.loads(data)
        except ValueError as error:
            raise SourceError(f"JSON inválido: {error}") from error
        return parse_states(document)
