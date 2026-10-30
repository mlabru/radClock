"""Clima no canto do escopo: METAR de um aeródromo ou o modelo do Open-Meteo.

Duas fontes, escolhidas por ``[weather] icao``:

* **METAR** (``icao = "SBSJ"``): observação real do aeródromo, pela API do
  Aviation Weather Center da NOAA (sem chave, dados de domínio público). Sai de
  hora em hora (mais SPECI quando o tempo muda) e pode parar à noite se o
  aeródromo fechar, por isso a tela mostra a idade ("SBSJ há 25 min") e a
  pinta de laranja acima de METAR_OLD_S.
* **Modelo** (``icao = ""``): o "current" da previsão do Open-Meteo para as
  coordenadas da estação. É saída de modelo numérico, não observação: a tela
  escreve "MODELO" e nunca um código ICAO, para não dar a ele a autoridade de
  um METAR. O Open-Meteo recalcula a cada 15 min; dados CC BY 4.0.

Nos dois casos o vento vem em nós e é mostrado com os campos separados
("120° 8 kt", "120° 8 G18 kt"), mais claro para quem não lê METAR. O crédito
da fonte ("NOAA AWC" ou "Open-Meteo") vai para o rodapé do painel.

Thread própria, como o eco: rede nunca roda na thread da tela.
"""
from __future__ import annotations

import json
import logging
import math
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

from radClock.core.config import WeatherCfg
from radClock.core.model import number_or_none
from radClock.core.net import SourceError, http_get, http_json

log = logging.getLogger(__name__)

RETRY_S = 120.0         # espera depois de uma falha (se refresh_s não for menor)
FIELDS = "temperature_2m,relative_humidity_2m,wind_speed_10m,wind_direction_10m,weather_code"
METAR_OLD_S = 90 * 60   # METAR mais velho que isso: aeródromo fechado ou fonte parada
MODEL_PLACE = "MODELO"  # o que a tela escreve no lugar do ICAO com o Open-Meteo

# Textos de condição: até 12 letras. O canto do escopo afina para baixo (fica
# entre a borda da tela e o anel externo) e a condição é a 3ª linha.
WMO_MAX_LEN = 12

# código WMO (Open-Meteo) -> texto curto
WMO = {
    0: "CÉU LIMPO", 1: "QUASE LIMPO", 2: "PARC NUBLADO", 3: "NUBLADO",
    45: "NEVOEIRO", 48: "NEVOEIRO",
    51: "GAROA FRACA", 53: "GAROA", 55: "GAROA FORTE", 56: "GAROA GELADA", 57: "GAROA GELADA",
    61: "CHUVA FRACA", 63: "CHUVA", 65: "CHUVA FORTE", 66: "CHUVA GELADA", 67: "CHUVA GELADA",
    71: "NEVE FRACA", 73: "NEVE", 75: "NEVE FORTE", 77: "GRÃO DE NEVE",
    80: "PANCADAS", 81: "PANCADAS", 82: "PANC. FORTES", 85: "NEVE", 86: "NEVE FORTE",
    95: "TROVOADA", 96: "TROV+GRANIZO", 99: "TROV+GRANIZO",
}

# METAR sem tempo presente: a maior cobertura de nuvens (campo "cover" do AWC)
COVER = {
    "CAVOK": "CAVOK", "SKC": "CÉU LIMPO", "CLR": "CÉU LIMPO", "NSC": "CÉU LIMPO",
    "NCD": "CÉU LIMPO", "FEW": "QUASE LIMPO", "SCT": "PARC NUBLADO", "BKN": "NUBLADO",
    "OVC": "ENCOBERTO", "OVX": "CÉU OBSCURO",
}

# METAR: fenômenos sem precipitação (névoa, fumaça...)
OBSCURATION = {
    "BR": "NÉVOA", "FG": "NEVOEIRO", "HZ": "NÉVOA SECA", "FU": "FUMAÇA",
    "DU": "POEIRA", "SA": "AREIA", "VA": "CINZAS",
}

# METAR: precipitação -> textos (fraca, moderada, forte)
PRECIPITATION = {
    "DZ": ("GAROA FRACA", "GAROA", "GAROA FORTE"),
    "RA": ("CHUVA FRACA", "CHUVA", "CHUVA FORTE"),
    "SN": ("NEVE FRACA", "NEVE", "NEVE FORTE"),
}

# METAR: "VC" (nas vizinhanças) -> versão curta com "PRÓX."
NEARBY = {"TROVOADA": "TROV. PRÓX.", "PANCADAS": "PANC. PRÓX."}


@dataclass(frozen=True)
class Weather:
    """Uma leitura do clima, de METAR ou de modelo. Campos None = não informado.

    place: ICAO do aeródromo (METAR) ou MODEL_PLACE (Open-Meteo).
    credit: nome da fonte para o rodapé ("NOAA AWC" ou "Open-Meteo").
    condition: texto curto já traduzido ("NUBLADO", "CHUVA FRACA"...).
    observed: epoch (s) da observação do METAR; None no modelo.
    at: instante (time.monotonic) em que a leitura foi baixada.
    """

    place: str
    credit: str
    temp_c: float | None
    hum_pct: float | None
    wind_kt: float | None
    gust_kt: float | None
    wind_dir: float | None      # de onde o vento sopra, graus verdadeiros; None = variável
    condition: str
    observed: float | None
    at: float

    @property
    def wind(self) -> str:
        """Vento com os campos separados: "120° 8 kt", com rajada "120° 8 G18 kt".

        A direção é arredondada a 10° (como no METAR) e o norte é escrito 360.
        "CALMO" abaixo de 1 kt; "VRB 8 kt" sem direção; "" sem vento informado.
        """
        if self.wind_kt is None:
            return ""
        speed_kt = round(self.wind_kt)
        if speed_kt < 1:
            return "CALMO"
        speed = f"{speed_kt}"
        if self.gust_kt is not None and round(self.gust_kt) > speed_kt:
            speed += f" G{round(self.gust_kt)}"
        if self.wind_dir is None:
            return f"VRB {speed} kt"
        direction = round(self.wind_dir / 10) * 10 % 360
        if direction == 0:
            direction = 360
        return f"{direction:03d}° {speed} kt"

    def age_s(self, wall_now: float) -> float | None:
        """Idade da observação em segundos; None no modelo ou com o relógio
        local antes da observação (Pi sem NTP)."""
        if self.observed is None:
            return None
        age_s = wall_now - self.observed
        if age_s < 0:
            return None
        return age_s

    def header(self, wall_now: float) -> str:
        """Primeira linha: de onde vem o dado e quão velho é.

        Retorno:
            "SBSJ há 25 min" (METAR), "SBSJ 21:00Z" (METAR, relógio local sem
            hora certa) ou "MODELO" (Open-Meteo).
        """
        if self.observed is None:
            return self.place
        age_s = self.age_s(wall_now)
        if age_s is None:
            return f"{self.place} {time.strftime('%H:%MZ', time.gmtime(self.observed))}"
        minutes = int(age_s // 60)
        if minutes < 60:
            return f"{self.place} há {minutes} min"
        return f"{self.place} há {minutes // 60}h{minutes % 60:02d}"

    def is_old(self, wall_now: float) -> bool:
        """True se o METAR passou de METAR_OLD_S (a tela pinta de laranja)."""
        age_s = self.age_s(wall_now)
        return age_s is not None and age_s > METAR_OLD_S

    def lines(self, wall_now: float) -> list[str]:
        """Linhas do canto da tela: origem e idade / vento / condição /
        temperatura e umidade. Linhas vazias ficam de fora.

        É a ordem do METAR, e também a que cabe no canto: ele afina para
        baixo, e as linhas mais largas ("SBSJ há 59 min", "360° 99 G99 kt")
        ficam em cima; a de temperatura, a mais curta, fica por último.
        """
        temperature = ""
        if self.temp_c is not None:
            temperature = f"{self.temp_c:.0f}°C"
        if self.hum_pct is not None:
            temperature = f"{temperature}  {self.hum_pct:.0f}%".strip()
        candidates = [self.header(wall_now), self.wind, self.condition, temperature]
        return [line for line in candidates if line]


# ================================================================ Open-Meteo
def parse_open_meteo(document: dict, now: float) -> Weather:
    """Converte a resposta do Open-Meteo em Weather.

    Parâmetros:
        document: o JSON da resposta.
        now: o time.monotonic() da leitura.

    Exceções:
        SourceError: a resposta não tem "current" ou não tem temperatura.
    """
    current = None
    if isinstance(document, dict):
        current = document.get("current")
    if not isinstance(current, dict):
        raise SourceError("Open-Meteo sem 'current'")
    temperature = number_or_none(current.get("temperature_2m"))
    if temperature is None:
        raise SourceError("Open-Meteo sem temperatura")
    code = number_or_none(current.get("weather_code"))
    condition = ""
    if code is not None:
        condition = WMO.get(int(code), "")
    return Weather(
        place=MODEL_PLACE,
        credit="Open-Meteo",
        temp_c=temperature,
        hum_pct=number_or_none(current.get("relative_humidity_2m")),
        wind_kt=number_or_none(current.get("wind_speed_10m")),
        gust_kt=None,
        wind_dir=number_or_none(current.get("wind_direction_10m")),
        condition=condition,
        observed=None,
        at=now,
    )


# ===================================================================== METAR
def relative_humidity(temp_c: float | None, dew_c: float | None) -> float | None:
    """Umidade relativa (%) pela temperatura e pelo ponto de orvalho (Magnus).

    O METAR não traz a umidade, só o ponto de orvalho. Retorna None se
    faltar um dos dois.
    """
    if temp_c is None or dew_c is None:
        return None

    def vapor(celsius: float) -> float:
        return math.exp(17.625 * celsius / (243.04 + celsius))

    return min(100.0, 100.0 * vapor(dew_c) / vapor(temp_c))


def metar_condition(wx_string: str | None, cover: str | None) -> str:
    """Texto curto da condição de um METAR.

    Usa o primeiro grupo de tempo presente ("-RA", "TSRA", "VCSH", "BR"...);
    sem tempo presente, a maior cobertura de nuvens ("BKN" -> "NUBLADO").

    Parâmetros:
        wx_string: campo "wxString" do AWC (ex.: "-RA BR"), ou None.
        cover: campo "cover" do AWC (ex.: "BKN"), ou None.
    """
    if wx_string:
        return _weather_group_text(wx_string.split()[0])
    return COVER.get(cover or "", "")


def _weather_group_text(group: str) -> str:
    """Um grupo de tempo presente do METAR ("+TSRA") em texto curto ("TROVOADA").

    Ordem: intensidade (+/-), vizinhança (VC), trovoada, pancada,
    precipitação e, por fim, fenômenos sem precipitação. Grupo desconhecido
    aparece como veio, cortado em WMO_MAX_LEN.
    """
    intensity = 1                               # índice em PRECIPITATION: moderada
    if group[:1] == "-":
        intensity, group = 0, group[1:]
    elif group[:1] == "+":
        intensity, group = 2, group[1:]
    nearby = group.startswith("VC")
    if nearby:
        group = group[2:]

    if "TS" in group:
        text = "TROV+GRANIZO" if "GR" in group else "TROVOADA"
    elif group.startswith("SH"):
        text = ("PANC. FRACAS", "PANCADAS", "PANC. FORTES")[intensity]
    elif group.startswith("FZ"):
        text = "GAROA GELADA" if "DZ" in group else "CHUVA GELADA"
    else:
        text = _precipitation_or_obscuration(group, intensity)

    if nearby:
        return NEARBY.get(text, text)
    return text[:WMO_MAX_LEN]


def _precipitation_or_obscuration(group: str, intensity: int) -> str:
    """Garoa, chuva ou neve com a intensidade; senão névoa, nevoeiro, fumaça..."""
    for code, texts in PRECIPITATION.items():
        if code in group:
            return texts[intensity]
    return OBSCURATION.get(group, group)


def parse_metar(document: list, icao: str, now: float) -> Weather:
    """Converte a resposta do AWC (lista de METARs decodificados) em Weather.

    Parâmetros:
        document: o JSON da resposta (lista; vazia se o aeródromo não tem METAR).
        icao: o aeródromo pedido.
        now: o time.monotonic() da leitura.

    Exceções:
        SourceError: sem METAR do aeródromo ou sem horário de observação.
    """
    if not isinstance(document, list):
        raise SourceError("AWC: resposta não é uma lista")
    metar = None
    for item in document:
        if isinstance(item, dict) and item.get("icaoId") == icao:
            metar = item
            break
    if metar is None:
        raise SourceError(f"sem METAR de {icao}")
    observed = number_or_none(metar.get("obsTime"))
    if observed is None:
        raise SourceError(f"METAR de {icao} sem horário")

    temp_c = number_or_none(metar.get("temp"))
    wind_dir = number_or_none(metar.get("wdir"))      # "VRB" vira None (variável)
    return Weather(
        place=icao,
        credit="NOAA AWC",
        temp_c=temp_c,
        hum_pct=relative_humidity(temp_c, number_or_none(metar.get("dewp"))),
        wind_kt=number_or_none(metar.get("wspd")),
        gust_kt=number_or_none(metar.get("wgst")),
        wind_dir=wind_dir,
        condition=metar_condition(metar.get("wxString"), metar.get("cover")),
        observed=observed,
        at=now,
    )


def metar_json(url: str) -> list:
    """GET da API de METAR do AWC.

    Um aeródromo sem METAR responde HTTP 204 com corpo vazio; isso vira uma
    lista vazia (e daí "sem METAR de XXXX"), não um "JSON inválido".

    Exceções: as de `net.http_request`, e SourceError se o corpo não é JSON.
    """
    body = http_get(url)
    if not body.strip():
        return []
    try:
        return json.loads(body)
    except ValueError as error:
        raise SourceError(f"JSON inválido: {error}") from error


# ==================================================================== thread
class WeatherFetcher(threading.Thread):
    """Thread que busca o clima a cada `refresh_s` e guarda a última leitura.

    Com ``cfg.icao`` busca o METAR do aeródromo; sem ele, o modelo do
    Open-Meteo nas coordenadas da estação. A thread nunca morre: erros são
    registrados (uma vez por erro diferente) e a busca é repetida depois de
    RETRY_S.

    Parâmetros do construtor:
        cfg: seção [weather] do TOML.
        lat, lon: a estação (usadas só pelo Open-Meteo).
        get: função que baixa o JSON (injetável nos testes); None = a padrão
            da fonte (`metar_json` ou `http_json`).
    """

    def __init__(self, cfg: WeatherCfg, lat: float, lon: float,
                 get: Callable[[str], dict | list] | None = None):
        super().__init__(name="weather", daemon=True)
        self.cfg = cfg
        if cfg.icao:
            self.url = f"{cfg.metar_url}?ids={cfg.icao}&format=json"
            default_get = metar_json
        else:
            self.url = (f"{cfg.url}?latitude={lat:.4f}&longitude={lon:.4f}"
                        f"&current={FIELDS}&wind_speed_unit=kn")
            default_get = http_json
        self._get = get or default_get
        self._lock = threading.Lock()
        self._latest: Weather | None = None
        self._stop_event = threading.Event()
        self._last_error = ""

    def current(self) -> Weather | None:
        """A última leitura, ou None se não há nenhuma baixada há menos de `max_age_s`.

        Sem rede há muito tempo, é melhor não mostrar nada do que um dado velho.
        (Um METAR velho mas baixado agora aparece, com a idade em laranja.)
        """
        with self._lock:
            latest = self._latest
        if latest is None:
            return None
        if time.monotonic() - latest.at > self.cfg.max_age_s:
            return None
        return latest

    def stop(self) -> None:
        """Pede o fim da thread."""
        self._stop_event.set()

    def fetch_once(self) -> None:
        """Busca o clima uma vez e guarda. Exceções: SourceError e outras da rede."""
        document = self._get(self.url)
        if self.cfg.icao:
            latest = parse_metar(document, self.cfg.icao, time.monotonic())
        else:
            latest = parse_open_meteo(document, time.monotonic())
        with self._lock:
            self._latest = latest

    def _log_failure(self, error: Exception) -> None:
        """Registra a falha só quando ela muda (sem rede: um aviso, não um por tentativa)."""
        if isinstance(error, SourceError):
            message = str(error)
        else:
            message = f"erro interno: {error!r}"
        if message != self._last_error:
            if isinstance(error, SourceError):
                log.warning("clima: %s", message)
            else:
                log.exception("clima: %s", message)
        self._last_error = message

    def run(self) -> None:
        """Laço da thread: busca, espera `refresh_s` (ou RETRY_S após falha), repete."""
        while not self._stop_event.is_set():
            delay_s = self.cfg.refresh_s
            try:
                self.fetch_once()
                if self._last_error:
                    log.info("clima voltou")
                self._last_error = ""
            except Exception as error:      # noqa: BLE001 -- a thread não pode morrer
                self._log_failure(error)
                delay_s = min(RETRY_S, self.cfg.refresh_s)
            self._stop_event.wait(delay_s)
