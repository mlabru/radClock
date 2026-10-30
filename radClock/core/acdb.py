"""Base de aeronaves offline (SQLite gerado por tools/make_aircraft_db.py --sqlite).

O que a base responde, sem rede:
    tipo ICAO (B738)          -> fabricante, modelo, dimensões, MTOW, cruzeiro,
                                 motor e silhueta (tabela ``types``)
    prefixo do callsign (GLO) -> operador e indicativo falado (``operators``)
    endereço ICAO24 (e4917b)  -> país de registro (``hex_blocks``)
    categoria ADS-B (A3)      -> silhueta genérica (``cat_sil``)

``AircraftDB.card()`` junta tudo isso na ``Card`` que a ficha da tela mostra.

Sem o arquivo (ou com arquivo inválido) a tela funciona normalmente: as
consultas devolvem None/"" e a ficha só mostra o que veio do receptor.
"""
from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from radClock.core.model import Aircraft

log = logging.getLogger(__name__)

# código da coluna types.eng_t -> texto mostrado na ficha
ENG_KIND = {1: "pistão", 2: "turboélice", 3: "jato", 4: "elétrico"}


@dataclass(frozen=True)
class TypeInfo:
    """Dados de um tipo de aeronave (uma linha da tabela ``types``)."""
    icao: str
    mfr: str = ""
    model: str = ""
    cls: str = ""               # LandPlane, Helicopter, ...
    eng_n: int = 0
    eng_kind: str = ""          # pistão / turboélice / jato / elétrico
    eng: str = ""               # série do motor, "CFM56"
    span_m: float | None = None
    len_m: float | None = None
    mtow_kg: float | None = None
    cruise_kt: float | None = None
    sil: int | None = None      # id da silhueta


@dataclass(frozen=True)
class Card:
    """Tudo que a ficha mostra de uma aeronave."""
    type: TypeInfo | None
    operator: str = ""
    radio: str = ""             # indicativo de chamada falado (GLO -> "GOL TRANSPORTE")
    country: str = ""
    sil: int | None = None      # do tipo, ou genérica pela categoria ADS-B
    sil_generic: bool = False   # True = desenho genérico, fora de escala


class AircraftDB:
    """Consultas somente-leitura à base SQLite de aeronaves.

    Todas as consultas têm cache (``lru_cache``): a ficha é redesenhada a cada
    quadro e a mesma aeronave é consultada muitas vezes.
    """

    def __init__(self, path: str | Path):
        """Abre a base em modo somente leitura.

        Parâmetros:
            path: arquivo SQLite. Ausente ou inválido só gera um aviso no log.
        """
        self.path = Path(path)
        self._db: sqlite3.Connection | None = None
        if not self.path.exists():
            log.warning("base de aeronaves %s ausente: ficha sem dados de tipo "
                        "(gere com tools/make_aircraft_db.py --sqlite)", self.path)
            return
        try:
            self._db = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True)
            type_count = self._db.execute("SELECT count(*) FROM types").fetchone()[0]
            log.info("base de aeronaves %s: %d tipos", self.path, type_count)
        except sqlite3.Error as error:
            log.error("base de aeronaves %s inválida: %s", self.path, error)
            self._db = None

    @property
    def available(self) -> bool:
        """True se a base abriu."""
        return self._db is not None

    def _query_one(self, sql: str, args: tuple) -> tuple | None:
        """Primeira linha da consulta, ou None (sem base, sem resultado ou erro)."""
        if self._db is None:
            return None
        try:
            return self._db.execute(sql, args).fetchone()
        except sqlite3.Error as error:
            log.error("base de aeronaves: %s", error)
            return None

    @lru_cache(maxsize=512)
    def type_info(self, icao: str) -> TypeInfo | None:
        """Dados do tipo ICAO (ex. "B738"), ou None se não estiver na base."""
        row = self._query_one(
            "SELECT icao, mfr, model, cls, eng_n, eng_t, eng, span_m, len_m,"
            " mtow_kg, cruise_kt, sil FROM types WHERE icao = ?",
            (icao.upper()[:4],))
        if row is None:
            return None
        (icao, manufacturer, model, aircraft_class, engine_count, engine_type_code,
         engine, span_m, length_m, mtow_kg, cruise_kt, silhouette_id) = row
        return TypeInfo(
            icao=icao,
            mfr=manufacturer or "",
            model=model or "",
            cls=aircraft_class or "",
            eng_n=engine_count or 0,
            eng_kind=ENG_KIND.get(engine_type_code, ""),
            eng=engine or "",
            span_m=span_m,
            len_m=length_m,
            mtow_kg=mtow_kg,
            cruise_kt=cruise_kt,
            sil=silhouette_id,
        )

    @lru_cache(maxsize=512)
    def operator(self, callsign: str) -> tuple[str, str] | None:
        """Operador pelo prefixo do callsign.

        Só callsigns no formato de empresa (3 letras + dígito, ex. "GLO1234")
        são consultados; matrículas ("PRGXA") e callsigns curtos não.

        Retorno:
            ``(nome do operador, indicativo falado)`` ou None.
        """
        airline_code = callsign[:3].upper()
        is_airline_format = (len(airline_code) == 3 and airline_code.isalpha()
                             and callsign[3:4].isdigit())
        if not is_airline_format:
            return None
        row = self._query_one("SELECT name, radio FROM operators WHERE code = ?",
                              (airline_code,))
        if row is None:
            return None
        name, radio = row
        return name, radio or ""

    @lru_cache(maxsize=512)
    def country(self, hex_addr: str) -> str:
        """País de registro pelo endereço ICAO24 ("" se desconhecido).

        Endereços com "~" são não-ICAO (TIS-B) e não têm país. A tabela
        ``hex_blocks`` guarda faixas [lo, hi]: pega a de maior ``lo`` <= endereço
        e confere se o endereço não passou do ``hi`` dela.
        """
        if hex_addr.startswith("~"):
            return ""
        try:
            address = int(hex_addr, 16)
        except ValueError:
            return ""
        row = self._query_one("SELECT lo, hi, country FROM hex_blocks WHERE lo <= ?"
                              " ORDER BY lo DESC LIMIT 1", (address,))
        if row is None:
            return ""
        _block_start, block_end, country = row
        if address > block_end:
            return ""
        return country

    @lru_cache(maxsize=32)
    def cat_silhouette(self, category: str) -> int | None:
        """Id da silhueta genérica da categoria ADS-B (ex. "A1"), ou None."""
        row = self._query_one("SELECT sil FROM cat_sil WHERE cat = ?", (category.upper(),))
        if row is None:
            return None
        return row[0]

    def silhouette_png(self, sil: int) -> bytes | None:
        """Imagem PNG da silhueta ``sil``, ou None."""
        row = self._query_one("SELECT png FROM silhouettes WHERE id = ?", (sil,))
        if row is None:
            return None
        return row[0]

    def card(self, ac: Aircraft) -> Card:
        """Junta tudo o que a ficha mostra de uma aeronave.

        Silhueta: a do tipo, em escala; se o tipo não tiver, a genérica da
        categoria ADS-B (marcada ``sil_generic``, fora de escala).
        """
        type_info = self.type_info(ac.type) if ac.type else None
        operator = self.operator(ac.callsign) if ac.callsign else None

        silhouette_id = None
        silhouette_is_generic = False
        if type_info is not None and type_info.sil is not None:
            silhouette_id = type_info.sil
        elif ac.category:
            silhouette_id = self.cat_silhouette(ac.category)
            silhouette_is_generic = silhouette_id is not None

        operator_name = ""
        operator_radio = ""
        if operator is not None:
            operator_name, operator_radio = operator

        return Card(
            type=type_info,
            operator=operator_name,
            radio=operator_radio,
            country=self.country(ac.hex),
            sil=silhouette_id,
            sil_generic=silhouette_is_generic,
        )
