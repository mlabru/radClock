#!/usr/bin/env python3
"""Gera as camadas de mapa do radClock (GeoJSON) para qualquer estação.

Saída, em --geojson-dir: um GeoJSON por camada (rivers, coastline, borders,
states, airports, runways, centerlines, navaids, fixes, airspace) e um layers.toml com
os [[map.layers]] prontos para colar no config.toml. Pontos levam as
propriedades kind/ident/name, que o radClock usa para o símbolo e o rótulo.

Fluxo:
  1. calcula a caixa (lat/lon) em volta da estação, com 30% de folga;
  2. contorno: baixa o Natural Earth 1:10m (litoral, fronteiras e, com
     --states, divisas; com --rivers, os rios principais), recorta na caixa e
     simplifica (Douglas-Peucker);
  3. aeroportos, pistas e auxílios: OurAirports, filtrados pela caixa;
  4. opcionais: fixos de um CSV próprio e espaços aéreos (CTR/TMA/CTA) de
     um GeoJSON local ou do openAIP;
  5. grava os GeoJSON e o layers.toml.

Fontes: Natural Earth e OurAirports são domínio público; openAIP é CC BY-NC
e precisa de uma chave gratuita (www.openaip.net). Só biblioteca padrão.

Exemplo (o que `./radClock.sh data` roda):
    python tools/make_map.py --lat -23.21 --lon -45.87 --radius 300 --states \\
        --rivers --geojson-dir maps

Estações a menos de ~5° do antimeridiano (lon ±180) não são suportadas.
"""
import argparse
import csv
import json
import math
import os
import re
import sys
import urllib.error
import urllib.request

import tls_option

NATURAL_EARTH_BASE = "https://raw.githubusercontent.com/nvkelso/natural-earth-vector/master/geojson/"
OURAIRPORTS_BASE = "https://davidmegginson.github.io/ourairports-data/"
SOURCE_URLS = {
    "coastline": NATURAL_EARTH_BASE + "ne_10m_coastline.geojson",
    "borders": NATURAL_EARTH_BASE + "ne_10m_admin_0_boundary_lines_land.geojson",
    "states": NATURAL_EARTH_BASE + "ne_10m_admin_1_states_provinces_lines.geojson",
    # rios e, dentro das represas, a linha central do lago (sem ela o rio teria buracos)
    "rivers": NATURAL_EARTH_BASE + "ne_10m_rivers_lake_centerlines.geojson",
    "airports": OURAIRPORTS_BASE + "airports.csv",
    "runways": OURAIRPORTS_BASE + "runways.csv",
    "navaids": OURAIRPORTS_BASE + "navaids.csv",
}
CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache")

KM_PER_DEG_LAT = 110.574
KM_PER_DEG_LON = 111.320        # no equador; multiplicar por cos(lat)
BBOX_MARGIN = 1.3               # folga para o mapa ainda cobrir um alcance maior depois

AIRPORT_SIZE = {"small_airport": 0, "medium_airport": 1, "large_airport": 2}
OPENAIP_TYPE_NAME = {4: "CTR", 7: "TMA", 26: "CTA"}     # códigos numéricos da API v2
AIRSPACE_CLASS = {"CTR": 0, "TMA": 1, "CTA": 2}         # propriedade `cls`; outro tipo = 3

# ordem de força dos auxílios: no mesmo lugar e com o mesmo nome, fica o mais forte
NAVAID_STRENGTH = {"vordme": 0, "vor": 1, "dme": 2, "ndb": 3, "point": 4}

# camada -> (cor, alcance máximo da camada, alcance máximo dos rótulos), em km;
# 0 = sem limite. A ordem é a de desenho: de baixo para cima.
# A ordem aqui é a ordem de desenho: rios primeiro, por baixo de tudo, numa cor
# apagada que não compete com os alvos (verde), o eco de chuva e os auxílios.
LAYER_STYLE = {
    "rivers": ("#2E4A66", 0, 0),
    "coastline": ("#D8C878", 0, 0), "borders": ("#B0A060", 0, 0), "states": ("#6E6640", 0, 0),
    "airspace": ("#3A7A9A", 0, 0), "centerlines": ("#4A5A50", 50, 0),
    "runways": ("#C8C8C8", 100, 0), "airports": ("#C8C8C8", 0, 100),
    "navaids": ("#8FA0C8", 200, 100), "fixes": ("#8F80B0", 100, 50),
}


class BoundingBox:
    """Caixa lat/lon centrada na estação: |lat - lat0| <= dlat e |lon - lon0| <= dlon."""

    def __init__(self, center_lat, center_lon, half_height_deg, half_width_deg):
        self.center_lat, self.center_lon = center_lat, center_lon
        self.half_height_deg, self.half_width_deg = half_height_deg, half_width_deg

    def contains(self, lat, lon):
        """True se o ponto está na caixa; coordenada ausente (None) fica de fora."""
        if lat is None or lon is None:
            return False
        return (abs(lat - self.center_lat) <= self.half_height_deg
                and abs(lon - self.center_lon) <= self.half_width_deg)


def download_cached(source_name):
    """Baixa a fonte `source_name` (chave de SOURCE_URLS) para tools/cache/.

    Se já estiver no cache, não baixa de novo. Grava em `.part` e renomeia no
    fim, para um download interrompido não deixar arquivo truncado.

    Retorna: caminho do arquivo local.
    """
    url = SOURCE_URLS[source_name]
    path = os.path.join(CACHE_DIR, os.path.basename(url))
    if os.path.exists(path) and os.path.getsize(path) > 0:
        print(f"  cache hit: {path}")
        return path
    os.makedirs(CACHE_DIR, exist_ok=True)
    print(f"  baixando {url} ...")
    partial_path = path + ".part"
    with urllib.request.urlopen(url, timeout=120, context=tls_option.context()) as response, \
            open(partial_path, "wb") as output:
        while True:
            chunk = response.read(1 << 20)
            if not chunk:
                break
            output.write(chunk)
    os.replace(partial_path, path)
    print(f"  salvo {os.path.getsize(path) / 1e6:.1f} MB")
    return path


def read_features(path):
    """Lê um GeoJSON e devolve a lista de features (um objeto solto vira lista de 1)."""
    with open(path, encoding="utf-8") as file:
        geojson = json.load(file)
    if geojson.get("type") == "FeatureCollection":
        return geojson["features"]
    return [geojson]


def to_float_or_none(text):
    """Converte um campo de CSV em float; vazio ou inválido vira None."""
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


def distance_km(lat, lon, center_lat, center_lon, cos_center_lat):
    """Distância aproximada (plano local), suficiente para ordenar por proximidade."""
    return math.hypot((lat - center_lat) * KM_PER_DEG_LAT,
                      (lon - center_lon) * KM_PER_DEG_LON * cos_center_lat)


# ------------------------------------------------------------------ contorno

def iter_polylines(geometry):
    """Gera listas de (lon, lat) de qualquer geometria GeoJSON comum."""
    geometry_type, coordinates = geometry.get("type"), geometry.get("coordinates")
    if geometry_type == "LineString":
        yield coordinates
    elif geometry_type in ("MultiLineString", "Polygon"):
        yield from coordinates
    elif geometry_type == "MultiPolygon":
        for polygon in coordinates:
            yield from polygon
    elif geometry_type == "GeometryCollection":
        for child in geometry.get("geometries", []):
            yield from iter_polylines(child)


def clip_polyline(points, box):
    """Divide uma polilinha nos trechos que tocam a caixa.

    Um ponto fica se ele ou um vizinho está dentro, para a linha que cruza a
    borda da caixa não terminar antes dela.

    Retorna: lista de trechos com pelo menos 2 pontos.
    """
    inside = [box.contains(point[1], point[0]) for point in points]     # (lon, lat)
    runs, current_run = [], []
    for index, point in enumerate(points):
        previous_inside = index > 0 and inside[index - 1]
        next_inside = index + 1 < len(points) and inside[index + 1]
        if inside[index] or previous_inside or next_inside:
            current_run.append(point)
        elif current_run:
            runs.append(current_run)
            current_run = []
    if current_run:
        runs.append(current_run)
    return [run for run in runs if len(run) >= 2]


def douglas_peucker(points, tolerance):
    """Simplifica uma linha de pontos (x, y) pelo algoritmo de Douglas-Peucker.

    Versão iterativa (pilha), para não estourar o limite de recursão em
    linhas longas como o litoral.

    Retorna: os pontos mantidos, na ordem original.
    """
    count = len(points)
    if count < 3:
        return list(points)
    keep = [False] * count
    keep[0] = keep[-1] = True
    pending = [(0, count - 1)]
    while pending:
        start, end = pending.pop()
        start_x, start_y = points[start]
        end_x, end_y = points[end]
        delta_x, delta_y = end_x - start_x, end_y - start_y
        segment_length_sq = delta_x * delta_x + delta_y * delta_y
        farthest_dist_sq, farthest_index = -1.0, -1
        for index in range(start + 1, end):
            px, py = points[index]
            if segment_length_sq == 0:
                dist_sq = (px - start_x) ** 2 + (py - start_y) ** 2
            else:
                t = ((px - start_x) * delta_x + (py - start_y) * delta_y) / segment_length_sq
                t = min(max(t, 0.0), 1.0)
                dist_sq = (px - start_x - t * delta_x) ** 2 + (py - start_y - t * delta_y) ** 2
            if dist_sq > farthest_dist_sq:
                farthest_dist_sq, farthest_index = dist_sq, index
        if farthest_dist_sq > tolerance * tolerance:
            keep[farthest_index] = True
            pending.append((start, farthest_index))
            pending.append((farthest_index, end))
    return [points[index] for index in range(count) if keep[index]]


def simplify_lonlat(points, tolerance, cos_center_lat):
    """Simplifica (lon, lat) com a longitude encolhida por cos(lat) (distâncias
    parecidas nos dois eixos). Retorna: lista de (lat, lon)."""
    scaled = [(lon * cos_center_lat, lat) for lon, lat in points]
    simplified = douglas_peucker(scaled, tolerance)
    return [(y, x / cos_center_lat) for x, y in simplified]


def build_outline(clipped_lines, tolerance, cos_center_lat):
    """Simplifica cada (camada, pontos (lon, lat)).

    Retorna: lista de (camada, [(lat, lon), ...]) com pelo menos 2 pontos.
    """
    outline = []
    for layer_name, points in clipped_lines:
        simplified = simplify_lonlat(points, tolerance, cos_center_lat)
        if len(simplified) >= 2:
            outline.append((layer_name, simplified))
    return outline


def load_outline(args, box, cos_center_lat):
    """Contorno do mapa: Natural Earth (ou --geojson), recortado e simplificado.

    Se passar de --max-points pontos, aumenta a tolerância 1,5x e tenta de novo.

    Retorna: (linhas, descrição das fontes para o cabeçalho do layers.toml,
    tolerância final — reaproveitada para os espaços aéreos).
    """
    if args.geojson:
        sources = [(path, "coastline") for path in args.geojson]   # contorno próprio
        layer_names = ["rivers"] if args.rivers else []
    else:
        layer_names = ["coastline", "borders"] + (["states"] if args.states else [])
        layer_names += ["rivers"] if args.rivers else []
        sources = []
    if layer_names:
        print("Baixando Natural Earth (domínio público):")
        sources += [(download_cached(name), name) for name in layer_names]

    clipped = []
    for path, layer_name in sources:
        for feature in read_features(path):
            geometry = feature.get("geometry") or feature
            for polyline in iter_polylines(geometry):
                clipped.extend((layer_name, run) for run in clip_polyline(polyline, box))
    if not clipped:
        sys.exit("nenhuma linha do mapa dentro da caixa: confira --lat/--lon/--radius")

    tolerance = args.tol
    while True:
        outline = build_outline(clipped, tolerance, cos_center_lat)
        point_count = sum(len(points) for _, points in outline)
        if point_count <= args.max_points or not outline:
            break
        tolerance *= 1.5
    print(f"{len(outline)} linhas, {point_count} pontos (tolerância {tolerance:.4f}°)")
    source_names = ", ".join(os.path.basename(path) for path, _ in sources)
    return outline, source_names, tolerance


# ------------------------------------------------- aeroportos, pistas e auxílios

def airport_icao_code(row):
    """Código ICAO do aeroporto: icao_code, senão gps_code, senão ident."""
    return row.get("icao_code") or row.get("gps_code") or row["ident"]


def load_airports(path, box, min_size, countries, cos_center_lat):
    """Lê o airports.csv do OurAirports.

    Entram aeroportos (não heliportos) dentro da caixa, de porte >= min_size
    ou com voo regular, com código ICAO de verdade (4 caracteres).

    Retorna: lista de (icao, lat, lon, nome, porte 0/1/2, ident), da estação
    para fora. O `ident` liga o aeroporto às pistas.
    """
    airports = []
    with open(path, encoding="utf-8") as file:
        for row in csv.DictReader(file):
            size = AIRPORT_SIZE.get(row["type"], -1)
            if size < 0:
                continue                     # heliporto, hidroavião, fechado
            if size < min_size and row.get("scheduled_service") != "yes":
                continue
            if countries and row.get("iso_country") not in countries:
                continue
            lat = to_float_or_none(row["latitude_deg"])
            lon = to_float_or_none(row["longitude_deg"])
            if not box.contains(lat, lon):
                continue
            icao = airport_icao_code(row)
            if not re.fullmatch(r"[A-Z][A-Z0-9]{3}", icao or ""):
                continue                     # pista sem código ICAO de verdade
            name = (row.get("name") or "").strip()
            airports.append((icao, lat, lon, name, size, row["ident"]))
    airports.sort(key=lambda airport: distance_km(airport[1], airport[2], box.center_lat,
                                                  box.center_lon, cos_center_lat))
    return airports


def extend_runway(lat1, lon1, lat2, lon2, extension_km, cos_center_lat):
    """Prolonga o eixo da pista `extension_km` além de cada cabeceira.

    Retorna: (lat1', lon1', lat2', lon2'), ou None se a pista tem
    comprimento quase zero (registro com defeito).
    """
    x1, y1 = lon1 * KM_PER_DEG_LON * cos_center_lat, lat1 * KM_PER_DEG_LAT
    x2, y2 = lon2 * KM_PER_DEG_LON * cos_center_lat, lat2 * KM_PER_DEG_LAT
    delta_x, delta_y = x2 - x1, y2 - y1
    length_km = math.hypot(delta_x, delta_y)
    if length_km < 0.05:
        return None
    unit_x, unit_y = delta_x / length_km, delta_y / length_km

    def to_lat_lon(x, y):
        return (y / KM_PER_DEG_LAT, x / (KM_PER_DEG_LON * cos_center_lat))

    start = to_lat_lon(x1 - unit_x * extension_km, y1 - unit_y * extension_km)
    end = to_lat_lon(x2 + unit_x * extension_km, y2 + unit_y * extension_km)
    return (start[0], start[1], end[0], end[1])


def load_runways(path, airport_idents, extension_km, cos_center_lat):
    """Lê o runways.csv do OurAirports (só pistas dos aeroportos escolhidos).

    Retorna: lista de (cabeceiras, prolongamento) — cabeceiras é
    (lat1, lon1, lat2, lon2) e prolongamento é o mesmo eixo esticado.
    """
    runways = []
    with open(path, encoding="utf-8") as file:
        for row in csv.DictReader(file):
            if row["airport_ident"] not in airport_idents or row.get("closed") == "1":
                continue
            lat1 = to_float_or_none(row["le_latitude_deg"])
            lon1 = to_float_or_none(row["le_longitude_deg"])
            lat2 = to_float_or_none(row["he_latitude_deg"])
            lon2 = to_float_or_none(row["he_longitude_deg"])
            if None in (lat1, lon1, lat2, lon2):
                continue                     # muita pista pequena não tem cabeceiras
            extended = extend_runway(lat1, lon1, lat2, lon2, extension_km, cos_center_lat)
            if extended:
                runways.append(((lat1, lon1, lat2, lon2), extended))
    return runways


def navaid_kind(navaid_type):
    """Tipo do OurAirports -> símbolo do radClock (vordme/vor/ndb/dme/point)."""
    navaid_type = (navaid_type or "").upper()
    if navaid_type in ("VOR-DME", "VORTAC"):
        return "vordme"
    if navaid_type == "VOR":
        return "vor"
    if navaid_type.startswith("NDB"):
        return "ndb"
    if navaid_type in ("DME", "TACAN"):
        return "dme"
    return "point"


def load_navaids(path, box, countries):
    """Lê o navaids.csv do OurAirports.

    Mesmo nome no mesmo lugar (VOR/DME e NDB "BGC" juntos): fica só o mais
    forte, senão a tela mostra o rótulo duas vezes.

    Retorna: lista de (ident, nome, tipo, lat, lon).
    """
    navaids = []
    with open(path, encoding="utf-8") as file:
        for row in csv.DictReader(file):
            if countries and row.get("iso_country") not in countries:
                continue
            lat = to_float_or_none(row["latitude_deg"])
            lon = to_float_or_none(row["longitude_deg"])
            if not box.contains(lat, lon):
                continue
            ident = (row.get("ident") or "").strip().upper()
            if ident:
                navaids.append((ident, (row.get("name") or "").strip(),
                                navaid_kind(row.get("type")), lat, lon))
    navaids.sort(key=lambda navaid: NAVAID_STRENGTH[navaid[2]])
    kept = []
    for navaid in navaids:
        duplicate = any(other[0] == navaid[0]
                        and abs(other[3] - navaid[3]) + abs(other[4] - navaid[4]) < 0.2
                        for other in kept)
        if not duplicate:
            kept.append(navaid)
    return kept


def load_fixes_csv(paths, box):
    """Lê CSVs próprios 'NOME,lat,lon' (cabeçalho opcional).

    Retorna: lista de (nome com até 5 letras, lat, lon) dentro da caixa.
    """
    fixes = []
    for path in paths:
        with open(path, encoding="utf-8") as file:
            for row in csv.reader(file):
                if len(row) < 3:
                    continue
                lat, lon = to_float_or_none(row[1]), to_float_or_none(row[2])
                if lat is None:
                    continue                 # linha de cabeçalho
                if box.contains(lat, lon):
                    fixes.append((row[0].strip().upper()[:5], lat, lon))
    return fixes


# --------------------------------------------------------------- espaço aéreo

def iter_outer_rings(geometry):
    """Gera os anéis externos ((lon, lat)) de Polygon/MultiPolygon."""
    geometry_type, coordinates = geometry.get("type"), geometry.get("coordinates")
    if geometry_type == "Polygon" and coordinates:
        yield coordinates[0]
    elif geometry_type == "MultiPolygon":
        for polygon in coordinates or []:
            if polygon:
                yield polygon[0]


def fetch_openaip_airspaces(api_key, box):
    """Baixa os espaços aéreos da caixa pela API do openAIP (paginada).

    Retorna: lista de features no formato GeoJSON (properties name/type).
    """
    features, page = [], 1
    while True:
        url = ("https://api.core.openaip.net/api/airspaces?limit=1000&page=%d"
               "&bbox=%.4f,%.4f,%.4f,%.4f&apiKey=%s"
               % (page, box.center_lon - box.half_width_deg, box.center_lat - box.half_height_deg,
                  box.center_lon + box.half_width_deg, box.center_lat + box.half_height_deg,
                  api_key))
        # o Cloudflare bloqueia o User-Agent padrão do urllib (erro 1010)
        request = urllib.request.Request(url, headers={"x-openaip-api-key": api_key,
                                                       "User-Agent": "Mozilla/5.0 (make_map.py)"})
        try:
            with urllib.request.urlopen(request, timeout=60,
                                        context=tls_option.context()) as response:
                data = json.load(response)
        except urllib.error.HTTPError as error:
            sys.exit(f"openAIP falhou: HTTP {error.code} {error.read()[:200]!r}\n"
                     "confira a chave da API (conta gratuita em www.openaip.net)")
        items = data.get("items", [])
        for item in items:
            type_code = item.get("type")
            features.append({"geometry": item.get("geometry") or {},
                             "properties": {"name": item.get("name", ""),
                                            "type": OPENAIP_TYPE_NAME.get(type_code,
                                                                          str(type_code))}})
        if not items or page >= int(data.get("totalPages", 1)):
            return features
        page += 1


def build_airspaces(features, box, tolerance, cos_center_lat, allowed_types):
    """Filtra, recorta e simplifica os polígonos de espaço aéreo.

    Entram só os tipos de `allowed_types` (CTR, TMA...) com algum ponto na caixa.

    Retorna: lista de (nome, classe 0=CTR 1=TMA 2=CTA 3=outro, [(lat, lon), ...]).
    """
    airspaces = []
    for feature in features:
        properties = feature.get("properties") or {}
        airspace_type = str(properties.get("type") or properties.get("class")
                            or properties.get("TYPE") or "").strip().upper()
        if airspace_type and airspace_type not in allowed_types:
            continue
        name = str(properties.get("name") or properties.get("NAME")
                   or properties.get("title") or airspace_type or "?").strip().upper()[:31]
        for ring in iter_outer_rings(feature.get("geometry") or {}):
            if not any(box.contains(point[1], point[0]) for point in ring):
                continue
            simplified = simplify_lonlat(ring, tolerance, cos_center_lat)
            if len(simplified) >= 4:         # anel fechado precisa de >= 4 pontos
                airspaces.append((name, AIRSPACE_CLASS.get(airspace_type, 3), simplified))
    return airspaces


# -------------------------------------------------------------------- saída

def line_feature(points, **properties):
    """[(lat, lon)] -> feature LineString (GeoJSON é lon, lat; 5 casas ~ 1 m)."""
    return {"type": "Feature", "properties": properties,
            "geometry": {"type": "LineString",
                         "coordinates": [[round(lon, 5), round(lat, 5)] for lat, lon in points]}}


def point_feature(lat, lon, **properties):
    return {"type": "Feature", "properties": properties,
            "geometry": {"type": "Point", "coordinates": [round(lon, 5), round(lat, 5)]}}


def write_geojson(path, features):
    with open(path, "w", encoding="utf-8") as file:
        json.dump({"type": "FeatureCollection", "features": features}, file,
                  separators=(",", ":"))
    print(f"  {os.path.basename(path)}: {len(features)} features, "
          f"{os.path.getsize(path) / 1024:.0f} KB")


def build_layers(outline, airports, runways, navaids, fixes, airspaces):
    """Converte os dados em features GeoJSON, agrupadas por nome de camada."""
    layers = {}
    for layer_name, points in outline:
        layers.setdefault(layer_name, []).append(line_feature(points))
    layers["airports"] = [point_feature(lat, lon, kind="airport", ident=icao, name=name, size=size)
                          for icao, lat, lon, name, size, _ident in airports]
    layers["runways"] = [line_feature([(lat1, lon1), (lat2, lon2)])
                         for (lat1, lon1, lat2, lon2), _ in runways]
    # prolongamento do eixo: só a parte fora da pista, nas duas cabeceiras
    layers["centerlines"] = []
    for (lat1, lon1, lat2, lon2), (ext_lat1, ext_lon1, ext_lat2, ext_lon2) in runways:
        layers["centerlines"].append(line_feature([(ext_lat1, ext_lon1), (lat1, lon1)]))
        layers["centerlines"].append(line_feature([(lat2, lon2), (ext_lat2, ext_lon2)]))
    layers["navaids"] = [point_feature(lat, lon, kind=kind, ident=ident, name=name)
                         for ident, name, kind, lat, lon in navaids]
    layers["fixes"] = [point_feature(lat, lon, kind="fix", ident=name)
                       for name, lat, lon in fixes]
    layers["airspace"] = [line_feature(ring, name=name, cls=airspace_class)
                          for name, airspace_class, ring in airspaces]
    return layers


def write_geojson_dir(output_dir, layers, header):
    """Grava um GeoJSON por camada não vazia e o layers.toml para o config.toml."""
    os.makedirs(output_dir, exist_ok=True)
    # caminho relativo ao diretório de trabalho do radClock (WorkingDirectory=/opt/radClock)
    dir_name = os.path.basename(os.path.normpath(output_dir))
    snippet = [header, ""]
    for layer_name, (color, max_range_km, label_range_km) in LAYER_STYLE.items():
        features = layers.get(layer_name)
        if not features:
            continue
        write_geojson(os.path.join(output_dir, f"{layer_name}.geojson"), features)
        snippet += ["[[map.layers]]", f'path = "{dir_name}/{layer_name}.geojson"',
                    f'color = "{color}"']
        if max_range_km:
            snippet.append(f"max_range_km = {max_range_km}")
        if label_range_km:
            snippet.append(f"label_range_km = {label_range_km}")
        snippet.append("")
    toml_path = os.path.join(output_dir, "layers.toml")
    with open(toml_path, "w", encoding="utf-8") as file:
        file.write("\n".join(snippet))
    print(f"gravado {os.path.abspath(toml_path)} (cole no config.toml)")


def parse_arguments():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--lat", type=float, required=True, help="latitude da estação")
    parser.add_argument("--lon", type=float, required=True, help="longitude da estação")
    parser.add_argument("--radius", type=float, required=True,
                        help="maior alcance que você vai usar no radar, km")
    parser.add_argument("--geojson-dir", required=True,
                        help="diretório de saída (um GeoJSON por camada + layers.toml)")
    parser.add_argument("--states", action="store_true", help="inclui as divisas estaduais")
    parser.add_argument("--rivers", action="store_true",
                        help="inclui os rios principais (Natural Earth 1:10m)")
    parser.add_argument("--geojson", action="append", default=[],
                        help="GeoJSON próprio para o contorno, no lugar do Natural Earth")
    parser.add_argument("--tol", type=float, default=0.002,
                        help="tolerância do Douglas-Peucker em graus (padrão 0.002, ~200 m)")
    parser.add_argument("--max-points", type=int, default=20000,
                        help="limite de pontos do contorno; a tolerância sobe se passar")
    parser.add_argument("--no-airports", action="store_true", help="sem aeroportos e pistas")
    parser.add_argument("--no-fixes", action="store_true", help="sem os auxílios do OurAirports")
    parser.add_argument("--fixes-csv", action="append", default=[],
                        help="CSV de fixos 'NOME,lat,lon' (ex.: fixos de 5 letras da AIP); "
                             "pode repetir")
    parser.add_argument("--airspace-geojson", action="append", default=[],
                        help="GeoJSON local com polígonos CTR/TMA (properties name, type); "
                             "pode repetir")
    parser.add_argument("--openaip-key", default="",
                        help="chave da API do openAIP: baixa CTR/TMA/CTA (dados CC BY-NC)")
    parser.add_argument("--airspace-types", default="CTR,TMA,CTA",
                        help="tipos de espaço aéreo mantidos (lista com vírgulas)")
    parser.add_argument("--rwy-ext", type=float, default=10.0,
                        help="prolongamento do eixo da pista além de cada cabeceira, km")
    parser.add_argument("--min-airport", choices=["small", "medium", "large"], default="medium",
                        help="menor porte de aeroporto (os de voo regular sempre entram)")
    parser.add_argument("--countries", default="",
                        help="só aeroportos/auxílios destes países ISO (ex.: BR,PY)")
    tls_option.add_argument(parser)
    return parser.parse_args()


def main():
    """Fluxo: caixa -> contorno -> aeroportos/pistas/auxílios -> espaço aéreo -> arquivos."""
    args = parse_arguments()
    tls_option.configure(not args.no_strict_tls)
    if abs(args.lat) > 85:
        sys.exit("latitudes além de ±85° não são suportadas")
    cos_center_lat = math.cos(math.radians(args.lat))
    half_height_deg = args.radius * BBOX_MARGIN / KM_PER_DEG_LAT
    half_width_deg = args.radius * BBOX_MARGIN / (KM_PER_DEG_LON * cos_center_lat)
    if abs(args.lon) + half_width_deg > 180:
        sys.exit("a caixa cruzaria o antimeridiano: não suportado")
    box = BoundingBox(args.lat, args.lon, half_height_deg, half_width_deg)
    countries = {code.strip().upper() for code in args.countries.split(",") if code.strip()}

    outline, outline_sources, tolerance = load_outline(args, box, cos_center_lat)

    airports, runways = [], []
    if not args.no_airports:
        print("Baixando OurAirports (domínio público):")
        min_size = AIRPORT_SIZE[args.min_airport + "_airport"]
        airports = load_airports(download_cached("airports"), box, min_size, countries,
                                 cos_center_lat)
        airport_idents = {airport[5] for airport in airports}
        runways = load_runways(download_cached("runways"), airport_idents, args.rwy_ext,
                               cos_center_lat)
    navaids = []
    if not args.no_fixes:
        navaids = load_navaids(download_cached("navaids"), box, countries)
    fixes = load_fixes_csv(args.fixes_csv, box)

    airspace_features = []
    for path in args.airspace_geojson:
        airspace_features += read_features(path)
    if args.openaip_key:
        airspace_features += fetch_openaip_airspaces(args.openaip_key, box)
    airspaces = []
    if airspace_features:
        allowed_types = {name.strip().upper() for name in args.airspace_types.split(",")
                         if name.strip()}
        airspaces = build_airspaces(airspace_features, box, tolerance, cos_center_lat,
                                    allowed_types)
    print(f"{len(airports)} aeroportos, {len(runways)} pistas, {len(navaids)} auxílios, "
          f"{len(fixes)} fixos, {len(airspaces)} espaços aéreos")

    header = (f"# make_map.py --geojson-dir: centro {args.lat:.4f},{args.lon:.4f} "
              f"raio {args.radius:.0f} km; fontes: {outline_sources}, OurAirports (domínio público)"
              + (", openAIP (CC BY-NC)" if args.openaip_key else ""))
    layers = build_layers(outline, airports, runways, navaids, fixes, airspaces)
    write_geojson_dir(args.geojson_dir, layers, header)


if __name__ == "__main__":
    main()
