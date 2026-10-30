"""
Configuração do radClock, lida de um arquivo TOML.

Como funciona
-------------
1. Cada seção do TOML (``[station]``, ``[radar]``, ...) corresponde a uma
   dataclass deste módulo (``StationCfg``, ``RadarCfg``, ...). O nome de cada
   campo da dataclass é exatamente o nome da chave no TOML.
2. Todo campo tem valor padrão: o arquivo só precisa conter o que muda.
3. ``load()`` lê o arquivo, monta a ``Config`` e valida os valores, seção por
   seção (uma função ``_validate_<seção>`` para cada uma).
4. Chaves ou seções desconhecidas são ERRO: um erro de digitação no TOML não
   pode passar em silêncio.

Ajustes feitos na tela (alcance, fonte, alarmes...) NÃO ficam aqui: ficam no
arquivo de estado (``state.py``), que vale por cima do TOML na partida.

Como adicionar uma opção nova
-----------------------------
1. Acrescente o campo, com valor padrão e um comentário curto, na dataclass da
   seção certa (ex.: ``RadarCfg``).
2. Se o valor tiver limites, acrescente a checagem na função
   ``_validate_<seção>`` correspondente, com mensagem de erro em português
   dizendo a seção, a chave e a faixa válida.
3. Se for uma SEÇÃO nova: crie a dataclass, acrescente um campo dela em
   ``Config``, acrescente o nome em ``KNOWN_SECTIONS`` e a leitura em
   ``load()``.
4. Documente a opção em ``config.example.toml`` e no README.
5. Use a opção no código lendo ``cfg.<seção>.<chave>``.
"""
from __future__ import annotations

import logging
import tomllib
from dataclasses import MISSING, dataclass, field, fields
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


# ------------------------------------------------------------------ seções

@dataclass
class StationCfg:
    """``[station]``: nome e posição da estação (centro do radar)."""
    name: str = "radClock"
    lat: float = -23.2100       # São José dos Campos
    lon: float = -45.8700


@dataclass
class DisplayCfg:
    """``[display]``: tamanho da tela e taxa de quadros."""
    width: int = 800            # Touch Display v1: 800x480
    height: int = 480           # Touch Display 2 em paisagem: 1280x720
    fullscreen: bool = True
    fps: int = 15               # o dado muda 1x/s; 15 fps basta para o toque
    backlight: str = "auto"     # brilho (AJUSTES): "auto", "" (não mexer) ou pasta em /sys/class/backlight


@dataclass
class InhibitCfg:
    """``[[radar.inhibit]]``: zona de inibição do alerta de conflito.

    É um círculo em volta de (lat, lon) que vale do solo até ``ceiling_ft``.
    Um par de aeronaves em que AMBAS estão dentro de alguma zona não gera
    alerta (ex.: tráfego de circuito de um aeródromo).
    """
    lat: float
    lon: float
    radius_km: float
    ceiling_ft: float           # altitude do ADS-B (pressão), não altura sobre o solo
    name: str = ""


@dataclass
class RadarCfg:
    """``[radar]``: alcances, envelhecimento dos alvos, conflito e os padrões
    dos ajustes de exibição (a tela AJUSTES vale por cima; ver options.py)."""
    ranges_km: list[int] = field(default_factory=lambda: [10, 25, 50, 100, 200, 300])
    range_km: int = 50
    stale_s: float = 15.0       # posição mais velha que isso: desenha em cinza
    drop_s: float = 60.0        # mais velha que isso: some da tela
    atc: bool | None = None     # obsoleto (modo ATC/RADAR): ignorado, com aviso no log
    symbol: str = "square"      # padrão do ajuste SÍMBOLO: "square" ou "plane"
    vector_min: float = 2.0     # padrão do VETOR: posição daqui a N minutos (0 desliga)
    show_ground: bool = False
    sweep_s: float = 4.0        # período de uma volta do feixe (0 = sem feixe, nem o ajuste)
    sweep: bool = True          # padrão do ajuste FEIXE
    trail_s: float = 60.0       # padrão do RASTRO: posições dos últimos N segundos (0 desliga)
    alert: str = "sound"        # padrão do ALERTA de conflito: "sound", "color" ou "off"
    zones: bool = True          # padrão do ajuste ZONAS (círculos de [[radar.inhibit]])
    conflict_s: float = 120.0   # alerta de conflito: horizonte de previsão (0 desliga)
    conflict_km: float = 5.6    # separação horizontal mínima (3 NM, radar terminal)
    conflict_ft: float = 1000.0 # separação vertical mínima
    extrapolate_s: float = 10.0 # avança a posição pela velocidade até N s (0 desliga)
    conflict_sound: str = ""    # wav/ogg do alerta de conflito; "" = bipe gerado
    conflict_volume: float = 0.6    # 0 = alerta só visual
    inhibit: list[InhibitCfg] = field(default_factory=list)   # [[radar.inhibit]]


@dataclass
class SourceCfg:
    """``[source]``: de onde vem o tráfego (receptor local, API ou OpenSky)."""
    kind: str = "api"           # "api" (pública, ver [api]) | "readsb" (receptor local) | "opensky"
    path: str = "/run/readsb/aircraft.json"
    url: str = ""               # se definido, lê o readsb por HTTP (outra máquina)
    interval_s: float = 1.0
    max_age_s: float = 10.0     # "now" do arquivo mais velho que isso = receptor parado
                                # (0 desliga a checagem)


# APIs públicas no formato do readsb, sem chave. airplanes.live ficou de fora:
# fechou a API pública em 2026-08-13.
API_PROVIDERS = {
    "adsb.lol": "https://api.adsb.lol/v2/point/{lat:.4f}/{lon:.4f}/{radius_nm:.0f}",
    "adsb.fi": "https://opendata.adsb.fi/api/v2/lat/{lat:.4f}/lon/{lon:.4f}/dist/{radius_nm:.0f}",
}


@dataclass
class ApiCfg:
    """``[api]``: API pública no formato do readsb (adsb.lol, adsb.fi ou própria)."""
    provider: str = "adsb.fi"   # chave de API_PROVIDERS, ou "custom"
    url: str = ""               # só com provider = "custom"; aceita {lat} {lon} {radius_nm}
    interval_s: float = 5.0     # APIs públicas limitam taxa (adsb.fi: 1 req/s por IP)
    fallback: bool = False      # com source.kind = "readsb": usa a API se o receptor falhar


@dataclass
class OpenSkyCfg:
    """``[opensky]``: OpenSky Network, conta gratuita com OAuth2 (desde 2025).

    A cota é diária e em créditos: uma consulta numa caixa de até 25 graus²
    custa 1 crédito (250 km no Brasil; mais ao norte, menos km). Contas comuns
    têm 4000 créditos por dia (8000 para quem alimenta a rede com um
    receptor). Com 30 s entre consultas são 2880 por dia.
    """
    client_id: str = ""
    client_secret: str = ""
    credentials: str = ""       # credentials.json baixado da conta (clientId/clientSecret)
    interval_s: float = 30.0
    radius_km: float = 250.0    # caixa em volta da estação; 1 crédito até ~265 km no Brasil
    daily_credits: int = 4000   # só para avisar no log se o intervalo estoura a cota
    fallback: str = "adsb.lol"  # API gratuita enquanto o OpenSky falha ("" = nenhuma)

    @property
    def configured(self) -> bool:
        """True se há credenciais (arquivo ou id + segredo) para usar o OpenSky."""
        has_client_pair = bool(self.client_id and self.client_secret)
        return bool(self.credentials) or has_client_pair


@dataclass
class EchoCfg:
    """``[echo]``: eco de chuva da RainViewer sobre o escopo."""
    enabled: bool = False
    opacity: float = 0.6
    refresh_s: float = 300.0    # a RainViewer publica um quadro novo a cada 10 min
    frames: int = 1             # 1 = só o quadro atual; >1 = animação dos últimos N (máx 13)
    frame_s: float = 0.5        # tempo de cada quadro na animação
    hold_s: float = 2.0         # pausa no quadro mais novo antes de recomeçar
    color_scheme: int = 2       # desde 2026 a API gratuita só tem o 2 (Universal Blue)
    url: str = "https://api.rainviewer.com/public/weather-maps.json"


@dataclass
class WeatherCfg:
    """``[weather]``: clima no canto do escopo (METAR de um aeródromo ou modelo)."""
    enabled: bool = True        # clima no canto do escopo (precisa de internet)
    icao: str = ""              # aeródromo do METAR (ex.: "SBSJ"); "" = modelo do Open-Meteo
    refresh_s: float = 900.0    # METAR sai de hora em hora; o Open-Meteo muda a cada 15 min
    max_age_s: float = 3600.0   # sem leitura nova há mais que isso: some da tela
    url: str = "https://api.open-meteo.com/v1/forecast"
    metar_url: str = "https://aviationweather.gov/api/data/metar"


@dataclass
class ClockCfg:
    """``[clock]``: fuso padrão e som do despertador."""
    timezone: str = "America/Sao_Paulo"   # padrão; o escolhido na tela vale por cima
    sound: str = ""             # wav/ogg do alarme; "" = bipe gerado
    volume: float = 0.8
    ring_s: float = 120.0       # para sozinho depois de N segundos tocando
    snooze_min: float = 10.0    # soneca; 0 = sem soneca (qualquer toque para)


@dataclass
class AircraftCfg:
    """``[aircraft]``: base offline de tipos e busca do tipo pela internet."""
    db: str = "data/aircraft.db"  # tools/make_aircraft_db.py --sqlite; ausente = ficha sem tipo
    lookup: bool = True         # alvo selecionado sem tipo: busca no adsbdb (internet)


@dataclass
class StateCfg:
    """``[state]``: onde fica o arquivo com os ajustes feitos na tela."""
    path: str = ""              # ajustes feitos na tela (JSON); "" = padrão, ver state.py


@dataclass
class NetworkCfg:
    """``[network]``: acesso à internet (todas as fontes HTTPS)."""
    strict_tls: bool = True     # false: aceita CA de proxy TLS sem AKI (ver net.configure_tls)


@dataclass
class MapLayerCfg:
    """``[[map.layers]]``: uma camada de mapa em GeoJSON."""
    path: str
    color: str = "#D8C878"
    max_range_km: float = 0.0   # alcance acima disso: a camada some (0 = sempre aparece)
    label_range_km: float = 0.0 # alcance acima disso: pontos sem rótulo (0 = sempre)


@dataclass
class Config:
    """Configuração completa: uma dataclass por seção do TOML."""
    station: StationCfg = field(default_factory=StationCfg)
    display: DisplayCfg = field(default_factory=DisplayCfg)
    radar: RadarCfg = field(default_factory=RadarCfg)
    source: SourceCfg = field(default_factory=SourceCfg)
    api: ApiCfg = field(default_factory=ApiCfg)
    opensky: OpenSkyCfg = field(default_factory=OpenSkyCfg)
    echo: EchoCfg = field(default_factory=EchoCfg)
    weather: WeatherCfg = field(default_factory=WeatherCfg)
    clock: ClockCfg = field(default_factory=ClockCfg)
    aircraft: AircraftCfg = field(default_factory=AircraftCfg)
    state: StateCfg = field(default_factory=StateCfg)
    network: NetworkCfg = field(default_factory=NetworkCfg)
    map_layers: list[MapLayerCfg] = field(default_factory=list)


# Seções aceitas no TOML. "map" guarda a lista [[map.layers]].
KNOWN_SECTIONS = {"station", "display", "radar", "source", "api", "opensky", "echo",
                  "weather", "clock", "aircraft", "state", "network", "map"}

SOURCE_KINDS = ("readsb", "api", "opensky")
OPENSKY_MIN_INTERVAL_S = 5.0        # o OpenSky só atualiza os estados a cada 5 s
OPENSKY_MAX_RADIUS_KM = 1000.0
API_MIN_INTERVAL_S = 1.0            # limite de taxa das APIs públicas
WEATHER_MIN_REFRESH_S = 300.0
ECHO_MAX_FRAMES = 13                # a RainViewer guarda só 2 h de quadros
ECHO_MIN_FRAME_S = 0.1
SNOOZE_MAX_MIN = 60


# ------------------------------------------------------------- utilitários

def _hex_color(text: str) -> tuple[int, int, int]:
    """Converte "#RRGGBB" (o "#" é opcional) em (r, g, b).

    Parâmetros:
        text: cor em hexadecimal, ex. "#D8C878".

    Retorno:
        Tupla (vermelho, verde, azul), cada um de 0 a 255.

    Erros:
        ValueError se o texto não tiver 6 dígitos hexadecimais.
    """
    digits = text.lstrip("#")
    if len(digits) != 6:
        raise ValueError(text)
    red = int(digits[0:2], 16)
    green = int(digits[2:4], 16)
    blue = int(digits[4:6], 16)
    return red, green, blue


def _build_section(section_class, data: dict):
    """Cria a dataclass de uma seção a partir do dicionário lido do TOML.

    Parâmetros:
        section_class: a dataclass da seção (ex. ``RadarCfg``).
        data: as chaves e valores dessa seção no TOML.

    Retorno:
        Uma instância de ``section_class``.

    Erros:
        ValueError se houver chave desconhecida ou faltar chave obrigatória
        (campo sem valor padrão, como ``lat`` em ``[[radar.inhibit]]``).
    """
    all_fields = fields(section_class)
    known_keys = {f.name for f in all_fields}
    unknown_keys = set(data) - known_keys
    if unknown_keys:
        raise ValueError(f"[{section_class.__name__}] chaves desconhecidas: "
                         f"{sorted(unknown_keys)}")

    required_keys = {f.name for f in all_fields
                     if f.default is MISSING and f.default_factory is MISSING}
    missing_keys = required_keys - set(data)
    if missing_keys:
        raise ValueError(f"[{section_class.__name__}] faltam as chaves: "
                         f"{sorted(missing_keys)}")
    return section_class(**data)


def _read_toml(path: Path | None) -> dict:
    """Lê o TOML; sem arquivo (``path`` None) devolve dicionário vazio (só padrões)."""
    if path is None:
        return {}
    with open(path, "rb") as toml_file:
        return tomllib.load(toml_file)


# ---------------------------------------------------------------- validação

def _validate_map_layers(layers: list[MapLayerCfg]) -> None:
    """Alcances não negativos e cor no formato #RRGGBB."""
    for layer in layers:
        if layer.max_range_km < 0 or layer.label_range_km < 0:
            raise ValueError(f"[[map.layers]] {layer.path}: alcances não podem ser negativos")
        try:
            _hex_color(layer.color)
        except ValueError:
            raise ValueError(f"[[map.layers]] {layer.path}: cor {layer.color!r} "
                             "não é #RRGGBB") from None


def _validate_source(cfg: Config) -> None:
    """``[source] kind`` conhecido; "opensky" exige credenciais."""
    kind = cfg.source.kind
    if kind not in SOURCE_KINDS:
        raise ValueError("[source] kind deve ser 'readsb', 'api' ou 'opensky', "
                         f"não {kind!r}")
    if kind == "opensky" and not cfg.opensky.configured:
        raise ValueError("[source] kind = 'opensky' exige [opensky] credentials ou "
                         "client_id + client_secret (o acesso anônimo dá só 400 créditos/dia)")


def _validate_opensky(opensky: OpenSkyCfg, api: ApiCfg) -> None:
    """Arquivo de credenciais existe, intervalo, raio e API de reserva válidos."""
    if opensky.credentials and not Path(opensky.credentials).is_file():
        raise ValueError(f"[opensky] credentials: arquivo {opensky.credentials!r} não existe")
    if opensky.interval_s < OPENSKY_MIN_INTERVAL_S:
        raise ValueError("[opensky] interval_s < 5 s: o OpenSky só atualiza a cada 5 s")
    if not 0 < opensky.radius_km <= OPENSKY_MAX_RADIUS_KM:
        raise ValueError("[opensky] radius_km deve estar entre 0 e 1000")

    fallback = opensky.fallback
    if fallback == "":
        return
    if fallback == "custom":
        if api.provider != "custom":
            raise ValueError("[opensky] fallback = 'custom' exige [api] provider = 'custom'")
        return
    if fallback not in API_PROVIDERS:
        raise ValueError(f"[opensky] fallback desconhecido {fallback!r}; "
                         f"use {sorted(API_PROVIDERS)}, 'custom' ou ''")


def _validate_api(api: ApiCfg) -> None:
    """Provedor conhecido; ``url`` só (e sempre) com provider "custom"; taxa mínima."""
    if api.provider == "custom":
        if not api.url:
            raise ValueError("[api] provider = 'custom' exige url")
    elif api.provider not in API_PROVIDERS:
        raise ValueError(f"[api] provider desconhecido {api.provider!r}; "
                         f"use {sorted(API_PROVIDERS)} ou 'custom'")
    elif api.url:
        raise ValueError("[api] url só vale com provider = 'custom'")

    if api.interval_s < API_MIN_INTERVAL_S:
        raise ValueError("[api] interval_s < 1 s viola o limite das APIs públicas")


def _validate_radar(radar: RadarCfg) -> None:
    """Tempos não negativos, separações positivas, volume e zonas de inibição."""
    times = (radar.trail_s, radar.conflict_s, radar.sweep_s, radar.extrapolate_s,
             radar.vector_min)
    if any(value < 0 for value in times):
        raise ValueError("[radar] trail_s, conflict_s, sweep_s, extrapolate_s e vector_min "
                         "não podem ser negativos")
    if radar.symbol not in ("square", "plane"):
        raise ValueError('[radar] symbol deve ser "square" ou "plane"')
    if radar.alert not in ("sound", "color", "off"):
        raise ValueError('[radar] alert deve ser "sound", "color" ou "off"')
    if radar.conflict_km <= 0 or radar.conflict_ft <= 0:
        raise ValueError("[radar] conflict_km e conflict_ft devem ser positivos")
    if not 0.0 <= radar.conflict_volume <= 1.0:
        raise ValueError("[radar] conflict_volume deve estar entre 0 e 1")

    for zone in radar.inhibit:
        position_ok = -90 <= zone.lat <= 90 and -180 <= zone.lon <= 180
        if zone.radius_km <= 0 or not position_ok:
            zone_label = zone.name or (zone.lat, zone.lon)
            raise ValueError(f"[[radar.inhibit]] {zone_label}: "
                             "raio deve ser positivo e lat/lon válidos")


def _validate_clock(clock: ClockCfg) -> None:
    """Fuso IANA existente, volume, duração do toque e soneca."""
    try:
        ZoneInfo(clock.timezone)
    except (ZoneInfoNotFoundError, ValueError):
        raise ValueError(f"[clock] timezone desconhecido {clock.timezone!r} "
                         "(use um nome IANA, ex. 'America/Sao_Paulo')") from None
    if not 0.0 <= clock.volume <= 1.0:
        raise ValueError("[clock] volume deve estar entre 0 e 1")
    if clock.ring_s <= 0:
        raise ValueError("[clock] ring_s deve ser positivo")
    if not 0 <= clock.snooze_min <= SNOOZE_MAX_MIN:
        raise ValueError("[clock] snooze_min deve estar entre 0 e 60")


def _validate_weather(weather: WeatherCfg) -> None:
    """Respeita o ritmo das fontes, a validade mínima da leitura e o formato do ICAO."""
    if weather.icao and not (len(weather.icao) == 4 and weather.icao.isalnum()
                             and weather.icao.isascii() and weather.icao.isupper()):
        raise ValueError('[weather] icao deve ter 4 letras maiúsculas (ex.: "SBSJ") ou ser ""')
    if weather.refresh_s < WEATHER_MIN_REFRESH_S:
        raise ValueError("[weather] refresh_s < 300 s: METAR e Open-Meteo mudam bem mais devagar")
    if weather.max_age_s < weather.refresh_s:
        raise ValueError("[weather] max_age_s deve ser >= refresh_s")


def _validate_echo(echo: EchoCfg) -> None:
    """Número de quadros, tempos da animação e opacidade."""
    if not 1 <= echo.frames <= ECHO_MAX_FRAMES:
        raise ValueError("[echo] frames deve estar entre 1 e 13 (a RainViewer guarda 2 h)")
    if echo.frame_s < ECHO_MIN_FRAME_S or echo.hold_s < 0:
        raise ValueError("[echo] frame_s deve ser >= 0,1 s e hold_s >= 0")
    if not 0.0 <= echo.opacity <= 1.0:
        raise ValueError("[echo] opacity deve estar entre 0 e 1")


# -------------------------------------------------------------------- load

def _validate_network(network: NetworkCfg) -> None:
    # "false" entre aspas é texto, e texto não vazio conta como verdadeiro
    if not isinstance(network.strict_tls, bool):
        raise ValueError("[network] strict_tls deve ser true ou false (sem aspas)")


def load(path: Path | None) -> Config:
    """Lê o TOML, monta a ``Config`` e valida tudo.

    Fluxo:
        1. Lê o arquivo (sem arquivo = todos os padrões).
        2. Recusa seções desconhecidas.
        3. Monta cada seção com ``_build_section`` (recusa chaves desconhecidas).
        4. Normaliza: ``range_km`` sempre entra na lista ``ranges_km`` (ordenada).
        5. Valida seção por seção (``_validate_*``).

    Parâmetros:
        path: caminho do TOML, ou None para usar só os valores padrão.

    Retorno:
        A ``Config`` pronta para uso.

    Erros:
        ValueError com mensagem em português dizendo seção e chave erradas;
        OSError/TOMLDecodeError se o arquivo não abrir ou não for TOML.
    """
    raw = _read_toml(path)
    unknown_sections = set(raw) - KNOWN_SECTIONS
    if unknown_sections:
        raise ValueError(f"seções desconhecidas: {sorted(unknown_sections)}")

    map_layers_raw = raw.get("map", {}).get("layers", [])
    cfg = Config(
        station=_build_section(StationCfg, raw.get("station", {})),
        display=_build_section(DisplayCfg, raw.get("display", {})),
        radar=_build_section(RadarCfg, raw.get("radar", {})),
        source=_build_section(SourceCfg, raw.get("source", {})),
        api=_build_section(ApiCfg, raw.get("api", {})),
        opensky=_build_section(OpenSkyCfg, raw.get("opensky", {})),
        echo=_build_section(EchoCfg, raw.get("echo", {})),
        weather=_build_section(WeatherCfg, raw.get("weather", {})),
        clock=_build_section(ClockCfg, raw.get("clock", {})),
        aircraft=_build_section(AircraftCfg, raw.get("aircraft", {})),
        state=_build_section(StateCfg, raw.get("state", {})),
        network=_build_section(NetworkCfg, raw.get("network", {})),
        map_layers=[_build_section(MapLayerCfg, layer) for layer in map_layers_raw],
    )

    radar = cfg.radar
    radar.ranges_km = sorted(set(radar.ranges_km) | {radar.range_km})
    _validate_map_layers(cfg.map_layers)
    # o TOML entrega as zonas como dicionários; viram InhibitCfg aqui
    radar.inhibit = [_build_section(InhibitCfg, zone) for zone in radar.inhibit]

    _validate_source(cfg)
    _validate_opensky(cfg.opensky, cfg.api)
    _validate_api(cfg.api)
    _validate_radar(radar)
    if radar.atc is not None:
        # o par ATC/RADAR virou ajustes independentes; a chave antiga não quebra o app
        logging.getLogger(__name__).warning(
            "[radar] atc é obsoleto e foi ignorado: use symbol, vector_min, trail_s, "
            "alert, sweep e zones (ou a tela AJUSTES)")
    _validate_clock(cfg.clock)
    _validate_weather(cfg.weather)
    _validate_echo(cfg.echo)
    _validate_network(cfg.network)
    return cfg
