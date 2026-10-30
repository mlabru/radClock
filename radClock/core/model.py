"""Modelo de aeronave e leitura do formato JSON do readsb.

O mesmo leitor serve para dois formatos quase iguais:
- o `aircraft.json` local do readsb, com a lista na chave "aircraft";
- as APIs v2 compatíveis com o readsb (adsb.lol, adsb.fi), com a lista na
  chave "ac".

As unidades são guardadas como o readsb entrega (pés, nós, pés por minuto);
a conversão para km e afins é feita só na hora de desenhar.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

# squawk de emergência -> texto mostrado no painel
EMERGENCY_SQUAWKS = {"7500": "SEQUESTRO", "7600": "FALHA RADIO", "7700": "EMERGENCIA"}


@dataclass(slots=True)
class Aircraft:
    """Uma aeronave com posição conhecida, num instante.

    Campos opcionais ficam None (número) ou "" (texto) quando a fonte não os
    informa.
    """

    hex: str                    # ICAO24 em minúsculas; prefixo "~" = endereço não-ICAO (TIS-B)
    lat: float
    lon: float
    callsign: str = ""
    alt_ft: int | None = None
    on_ground: bool = False
    gs_kt: float | None = None  # velocidade sobre o solo
    track: float | None = None  # rumo em graus verdadeiros
    vrate_fpm: float | None = None
    squawk: str = ""
    type: str = ""              # designador ICAO (ex.: B738); readsb só tem com --db-file
    reg: str = ""               # matrícula
    category: str = ""          # categoria ADS-B: A1..A7, B1..B7, C1..C3
    seen_pos_s: float = 0.0     # idade da posição no instante em que a fonte respondeu

    @property
    def label(self) -> str:
        """Nome mostrado na tela: o indicativo, ou o hex se não houver indicativo."""
        if self.callsign:
            return self.callsign
        return self.hex.upper()

    @property
    def emergency(self) -> str | None:
        """Texto da emergência (ex.: "EMERGENCIA"), ou None se o squawk é normal."""
        return EMERGENCY_SQUAWKS.get(self.squawk)


def number_or_none(value) -> float | None:
    """Converte um valor vindo de JSON em float, ou devolve None.

    Aceita só int e float finitos. `bool` é recusado de propósito: em Python
    ele é subclasse de int, e `true` não pode virar 1.0 por engano. NaN e
    infinito também são recusados (o módulo json do Python aceita "NaN").

    Parâmetros:
        value: qualquer valor lido de um documento JSON.

    Retorno:
        o número como float, ou None se `value` não é um número válido.
    """
    if isinstance(value, bool):
        return None
    if not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value):
        return None
    return float(value)


def _first_number(row: dict, *keys: str) -> float | None:
    """Primeiro campo numérico válido entre `keys` (ex.: alt_baro e depois alt_geom)."""
    for key in keys:
        value = number_or_none(row.get(key))
        if value is not None:
            return value
    return None


def _text(row: dict, key: str) -> str:
    """Campo de texto; ausente ou nulo vira ""."""
    return str(row.get(key) or "")


def _parse_row(row: dict) -> Aircraft | None:
    """Converte uma entrada do JSON do readsb em Aircraft.

    Parâmetros:
        row: um item da lista "aircraft"/"ac".

    Retorno:
        a aeronave, ou None se a entrada não tem posição (aeronave só Mode S,
        que não há como desenhar).
    """
    lat = number_or_none(row.get("lat"))
    lon = number_or_none(row.get("lon"))
    if lat is None or lon is None:
        return None

    # o readsb escreve alt_baro = "ground" para aeronave no solo
    on_ground = row.get("alt_baro") == "ground"
    if on_ground:
        altitude = 0.0
    else:
        altitude = _first_number(row, "alt_baro", "alt_geom")

    seen_pos = _first_number(row, "seen_pos", "seen")
    if seen_pos is None:
        seen_pos = 0.0

    alt_ft = None
    if altitude is not None:
        alt_ft = int(altitude)

    return Aircraft(
        hex=str(row.get("hex", "")).lower(),
        lat=lat,
        lon=lon,
        callsign=_text(row, "flight").strip(),
        alt_ft=alt_ft,
        on_ground=on_ground,
        gs_kt=number_or_none(row.get("gs")),
        track=number_or_none(row.get("track")),
        vrate_fpm=_first_number(row, "baro_rate", "geom_rate"),
        squawk=_text(row, "squawk"),
        type=_text(row, "t"),
        reg=_text(row, "r"),
        category=_text(row, "category"),
        seen_pos_s=seen_pos,
    )


def parse_aircraft_json(document: dict) -> list[Aircraft]:
    """Lê um documento do readsb (ou de API compatível) e devolve as aeronaves.

    Fluxo: pega a lista em "aircraft" (readsb) ou "ac" (APIs), converte cada
    entrada e descarta as que não têm posição.

    Parâmetros:
        document: o JSON já decodificado.

    Retorno:
        lista de Aircraft (vazia se o documento não tem nenhuma lista).
    """
    rows = document.get("aircraft")
    if rows is None:
        rows = document.get("ac") or []
    aircraft = []
    for row in rows:
        parsed = _parse_row(row)
        if parsed is not None:
            aircraft.append(parsed)
    return aircraft
