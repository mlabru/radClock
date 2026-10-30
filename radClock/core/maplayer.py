"""
Camadas de mapa a partir de GeoJSON (litoral, fronteiras, aeroportos, navaids...).

Linhas e pontos são projetados uma única vez na partida (a estação é fixa),
então redesenhar o mapa ao trocar de alcance é só multiplicar por uma escala.

Aceita qualquer GeoJSON em WGS84: a saída de `tools/make_map.py --geojson-dir`
(Natural Earth + OurAirports), Natural Earth direto ou OpenAIP.

Regras de leitura:
  - Linhas (LineString, MultiLineString) viram polilinhas.
  - Polígonos (Polygon, MultiPolygon) são desenhados só pelo contorno.
  - Pontos (Point, MultiPoint) viram símbolo + rótulo. O símbolo vem da
    propriedade `kind` (airport, vor, vordme, ndb, dme, fix; outro valor =
    cruz) e o rótulo de `ident`, `icao` ou `name`, nessa ordem.
  - Atenção: GeoJSON guarda as coordenadas como [lon, lat] (x antes de y).
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from radClock.core.config import _hex_color
from radClock.core.projection import AzimuthalEquidistant

Polyline = list[tuple[float, float]]
POINT_KINDS = {"airport", "vor", "vordme", "ndb", "dme", "fix"}
GENERIC_POINT_KIND = "point"     # desenhado como cruz
MAX_LABEL_CHARS = 8


@dataclass(frozen=True)
class MapPoint:
    """
    Ponto do mapa já projetado.

    e, n: km a leste / ao norte da estação.
    kind: um de POINT_KINDS, ou "point".
    label: texto do rótulo (até MAX_LABEL_CHARS caracteres; pode ser vazio).
    size: aeroporto: 0 pequeno, 1 médio, 2 grande (OurAirports).
    """
    e: float
    n: float
    kind: str
    label: str
    size: int = 0


def _lines_of(geometry: dict | None) -> list[list]:
    """
    Lista de linhas (cada uma, lista de [lon, lat]) de uma geometria GeoJSON.
    """
    if not geometry:
        return []
    geometry_type = geometry.get("type")
    coordinates = geometry.get("coordinates")
    if geometry_type == "LineString":
        return [coordinates]
    if geometry_type in ("MultiLineString", "Polygon"):
        return list(coordinates)
    if geometry_type == "MultiPolygon":
        return [ring for polygon in coordinates for ring in polygon]
    if geometry_type == "GeometryCollection":
        lines = []
        for child in geometry.get("geometries", []):
            lines.extend(_lines_of(child))
        return lines
    return []


def _points_of(geometry: dict | None) -> list[list]:
    """
    Lista de pontos ([lon, lat]) de uma geometria GeoJSON.
    """
    if not geometry:
        return []
    geometry_type = geometry.get("type")
    coordinates = geometry.get("coordinates")
    if geometry_type == "Point":
        return [coordinates]
    if geometry_type == "MultiPoint":
        return list(coordinates)
    if geometry_type == "GeometryCollection":
        points = []
        for child in geometry.get("geometries", []):
            points.extend(_points_of(child))
        return points
    return []


def _features(document: dict) -> list[dict]:
    """
    Normaliza o documento em lista de features (aceita geometria solta).
    """
    document_type = document.get("type")
    if document_type == "FeatureCollection":
        return [feature for feature in document.get("features", [])
                if isinstance(feature, dict)]
    if document_type == "Feature":
        return [document]
    return [{"geometry": document, "properties": {}}]


def load_geojson(path: str | Path) -> tuple[list[Polyline], list[tuple[float, float, dict]]]:
    """
    Lê um arquivo GeoJSON.

    Parâmetros:
        path: caminho do arquivo.
    Retorno:
        (polilinhas, pontos): polilinhas como listas de (lat, lon); pontos como
        (lat, lon, propriedades). Linhas com menos de 2 pontos são ignoradas.
    Erros:
        OSError / ValueError se o arquivo não abre ou não é JSON.
    """
    with open(path, encoding="utf-8") as file:
        document = json.load(file)
    lines: list[Polyline] = []
    points: list[tuple[float, float, dict]] = []
    for feature in _features(document):
        geometry = feature.get("geometry")
        properties = feature.get("properties") or {}
        for line in _lines_of(geometry):
            # [lon, lat] -> (lat, lon)
            lat_lon = [(float(coord[1]), float(coord[0])) for coord in line if len(coord) >= 2]
            if len(lat_lon) >= 2:
                lines.append(lat_lon)
        for coord in _points_of(geometry):
            if len(coord) >= 2:
                points.append((float(coord[1]), float(coord[0]), properties))
    return lines, points


def _point_label(properties: dict) -> str:
    """
    Texto do rótulo: `ident`, senão `icao`, senão `name`; cortado.
    """
    text = properties.get("ident") or properties.get("icao") or properties.get("name") or ""
    return str(text).strip()[:MAX_LABEL_CHARS]


def _point_kind(properties: dict) -> str:
    kind = str(properties.get("kind") or "").lower()
    if kind in POINT_KINDS:
        return kind
    return GENERIC_POINT_KIND


def _point_size(properties: dict) -> int:
    """
    Porte do ponto (propriedade `size`; 0 se ausente ou inválida).
    """
    size = properties.get("size")
    # bool é subclasse de int em Python: "size": true não é um tamanho
    if isinstance(size, int) and not isinstance(size, bool):
        return size
    return 0


def _make_point(projection: AzimuthalEquidistant, lat: float, lon: float,
                properties: dict) -> MapPoint:
    """
    Projeta um ponto do GeoJSON e lê seu símbolo, rótulo e tamanho.
    """
    east_km, north_km = projection.forward(lat, lon)
    return MapPoint(east_km, north_km, _point_kind(properties),
                    _point_label(properties), _point_size(properties))


@dataclass
class MapLayer:
    """
    Uma camada do mapa, já projetada em km da estação.

    color: cor RGB das linhas, símbolos e rótulos.
    lines_km: polilinhas em (leste_km, norte_km).
    points: pontos com símbolo/rótulo.
    max_range_km: com alcance acima disso a camada some (0 = sempre visível).
    label_range_km: com alcance acima disso só os símbolos aparecem (0 = sempre).
    """
    color: tuple[int, int, int]
    lines_km: list[Polyline]
    points: list[MapPoint] = field(default_factory=list)
    max_range_km: float = 0.0
    label_range_km: float = 0.0

    def visible(self, rng: float) -> bool:
        """
        A camada aparece no alcance `rng` (km)?
        """
        if not self.max_range_km:
            return True
        return rng <= self.max_range_km

    def labels_visible(self, rng: float) -> bool:
        """
        Os rótulos da camada aparecem no alcance `rng` (km)?
        """
        if not self.label_range_km:
            return True
        return rng <= self.label_range_km

    @classmethod
    def from_geojson(cls, path: str | Path, color: str, proj: AzimuthalEquidistant,
                     max_range_km: float = 0.0, label_range_km: float = 0.0) -> MapLayer:
        """
        Lê um GeoJSON e projeta tudo uma vez.

        Parâmetros:
            path: arquivo GeoJSON.
            color: cor em "#rrggbb".
            proj: projeção da estação.
            max_range_km, label_range_km: ver a docstring da classe.
        Retorno:
            a camada pronta para desenhar.
        """
        lines, points = load_geojson(path)
        projected_lines = []
        for line in lines:
            projected_lines.append([proj.forward(lat, lon) for lat, lon in line])
        projected_points = [_make_point(proj, lat, lon, properties)
                            for lat, lon, properties in points]
        return cls(_hex_color(color), projected_lines, projected_points,
                   max_range_km, label_range_km)
