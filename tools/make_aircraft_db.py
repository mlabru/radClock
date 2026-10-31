#!/usr/bin/env python3
"""Gera a base de aeronaves offline do radClock (aircraft.db, SQLite).

A ficha do radClock (tocar no tipo da aeronave selecionada) mostra a silhueta
vista de cima e os dados técnicos do tipo: fabricante, modelo, envergadura,
velocidade de cruzeiro, MTOW e motor. O radClock lê tudo desta base
(radClock/core/acdb.py); sem ela o app funciona, só que a ficha fica sem tipo.

Fluxo:
  1. baixa (ou reaproveita de tools/cache/) as fontes públicas;
  2. monta as silhuetas: folha de sprites do pw-silhouettes, trocada pelos SVG
     originais com --svg, e por cima os desenhos próprios de
     tools/data/silhouettes/ (gerados por tools/make_local_silhouettes.py);
  3. junta os dados de cada tipo (Doc 8643 + openap + CSVs locais);
  4. grava o SQLite em arquivo temporário e renomeia no fim.

Fontes (todas gratuitas, creditadas na tabela `meta` e no README.md):
  silhuetas         plane-watch/pw-silhouettes (CC BY-NC-SA 4.0)
  tipos/fabricantes ICAO Doc 8643, via rikgale/ICAOList
  operadores        ICAO Doc 8585, via rikgale/ICAOList
  país do ICAO24    ICAO Anexo 10, via rikgale/ICAOList
  desempenho        openap (TU Delft, ~37 aviões de linha)

Entradas locais, versionadas (edite à mão; são o motivo deste script):
  tools/data/common_types.txt      quais códigos ICAO de tipo entram na base
  tools/data/aircraft_specs.csv    envergadura/comprimento/MTOW/cruzeiro dos
                                   tipos que o openap não cobre (vazio = "-")
  tools/data/engines.csv           família de motor dos tipos sem openap
  tools/data/silhouette_alias.csv  tipo -> tipo quase idêntico cuja silhueta
                                   é emprestada (B739 -> B738, ...)
  tools/data/silhouettes/*.png     desenhos próprios, <ICAO>.png (alfa = forma)

Precisa de internet (na primeira vez), Pillow e, com --svg, pygame-ce e numpy:

    pip install pillow
    python3 tools/make_aircraft_db.py --sqlite --svg --size 512 --out data/aircraft.db
"""
import argparse
import csv
import io
import json
import math
import os
import re
import sqlite3
import sys
import urllib.request

import tls_option

UTILS_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(UTILS_DIR, "data")
CACHE_DIR = os.path.join(UTILS_DIR, "cache")
LOCAL_SILHOUETTES_DIR = os.path.join(DATA_DIR, "silhouettes")   # vencem a folha de sprites

USER_AGENT = "make_aircraft_db/1.0"
PW_RELEASE_API = "https://api.github.com/repos/plane-watch/pw-silhouettes/releases/latest"
PW_DOWNLOAD_URL = "https://github.com/plane-watch/pw-silhouettes/releases/download/{tag}/{name}"
PW_RAW_URL = "https://raw.githubusercontent.com/plane-watch/pw-silhouettes/{tag}/{path}"
RIKGALE_BASE = "https://raw.githubusercontent.com/rikgale/ICAOList/main/"
ICAO_TYPES_URL = RIKGALE_BASE + "ICAOList.csv"
AIRLINES_URL = RIKGALE_BASE + "Airlines.csv"          # 3 letras do indicativo -> empresa
HEX_RANGES_URL = RIKGALE_BASE + "ICAOHexRange.csv"    # faixa de endereço ICAO24 -> país
OPENAP_INDEX_URL = ("https://api.github.com/repos/TUDelft-CNS-ATM/openap/contents/"
                    "openap/data/aircraft")
OPENAP_RAW_URL = ("https://raw.githubusercontent.com/TUDelft-CNS-ATM/openap/master/"
                  "openap/data/aircraft/{name}")

# tipo de motor do Doc 8643 -> código gravado na coluna types.eng_t
ENGINE_TYPE_CODE = {"piston": 1, "turboprop": 2, "turboshaft": 2, "jet": 3, "electric": 4}

# categoria de emissor ADS-B no pw-silhouettes: "4/3" = A3, "3/1" = B1, "2/4" = C4
PW_CATEGORY_LETTER = {"4": "A", "3": "B", "2": "C"}


def download_cached(url, path):
    """Baixa `url` para `path`, a menos que o arquivo já esteja no cache.

    Grava primeiro em `path.part` e renomeia no fim, para um download
    interrompido nunca virar um arquivo de cache truncado.

    Retorna: o próprio `path`.
    """
    if os.path.exists(path) and os.path.getsize(path) > 0:
        print(f"  cache hit: {os.path.basename(path)}")
        return path
    os.makedirs(os.path.dirname(path), exist_ok=True)
    print(f"  baixando {url} ...")
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    partial_path = path + ".part"
    with urllib.request.urlopen(request, timeout=120, context=tls_option.context()) as response, \
            open(partial_path, "wb") as output:
        while True:
            chunk = response.read(1 << 20)
            if not chunk:
                break
            output.write(chunk)
    os.replace(partial_path, path)
    return path


def latest_silhouette_tag():
    """Retorna o nome da versão mais recente do pw-silhouettes (ex.: "20260908")."""
    request = urllib.request.Request(PW_RELEASE_API, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=60, context=tls_option.context()) as response:
        return json.load(response)["tag_name"]


def read_json_file(path):
    with open(path, encoding="utf-8") as file:
        return json.load(file)


# ---------------------------------------------------------------- silhuetas

def load_sprite_sheet(tag, size):
    """Recorta a folha de sprites do pw-silhouettes em silhuetas de size x size.

    Cada silhueta é só o canal alfa (1 byte por pixel, 0 = vazio): a cor
    amarela é aplicada pelo radClock na hora de desenhar.

    Retorna: (tiles, sprite_by_type, sprite_by_category)
      tiles               nome do sprite -> bytes da máscara (size*size)
      sprite_by_type      código ICAO do tipo -> nome do sprite
      sprite_by_category  categoria ADS-B ("A3", "A5"...) -> sprite genérico,
                          usado quando o tipo não tem desenho
    """
    from PIL import Image

    print(f"Baixando pw-silhouettes {tag} (CC BY-NC-SA 4.0):")
    json_path = download_cached(PW_DOWNLOAD_URL.format(tag=tag, name="spritesheet.json"),
                                os.path.join(CACHE_DIR, f"pw-{tag}-spritesheet.json"))
    png_path = download_cached(PW_DOWNLOAD_URL.format(tag=tag, name="spritesheet.png"),
                               os.path.join(CACHE_DIR, f"pw-{tag}-spritesheet.png"))
    sheet_info = read_json_file(json_path)

    sprite_width = sheet_info["metadata"]["spriteWidth"]
    sprite_height = sheet_info["metadata"]["spriteHeight"]
    sheet = Image.open(png_path).convert("RGBA")
    columns = sheet.width // sprite_width

    tiles = {}
    for name, sprite in sheet_info["sprites"].items():
        index = sprite["ids"][0]             # sprite animado (vários ids): fica o 1º quadro
        row, column = divmod(index, columns)
        left, top = column * sprite_width, row * sprite_height
        alpha = sheet.crop((left, top, left + sprite_width, top + sprite_height)).split()[3]
        if not alpha.getbbox():
            continue                         # posição vazia na folha
        tiles[name] = alpha.resize((size, size), Image.LANCZOS).tobytes()

    # vários tipos dividem o mesmo desenho
    sprite_by_type = {}
    for type_code, sprite_name in sheet_info["airframeToSprite"].items():
        if sprite_name in tiles:
            sprite_by_type[type_code.upper()] = sprite_name

    sprite_by_category = {}
    for key, sprite_name in sheet_info.get("genericToSprite", {}).items():
        head, _, number = key.partition("/")
        letter = PW_CATEGORY_LETTER.get(head)
        target = sheet_info["airframeToSprite"].get(sprite_name, sprite_name)
        if letter and number.isdigit() and target in tiles:
            sprite_by_category[letter + number] = target

    print(f"  {len(tiles)} silhuetas, {len(sprite_by_type)} códigos de tipo, "
          f"{len(sprite_by_category)} categorias de emissor")
    return tiles, sprite_by_type, sprite_by_category


def replace_tiles_with_svg(tag, size, tiles):
    """Troca as silhuetas da folha (72 px, borradas quando ampliadas) pelos SVG.

    O repositório guarda o SVG de onde saiu cada sprite:
    airframes/<nome>.json -> art.frames[0].src. O pygame-ce (>= 2.4) rasteriza
    o SVG direto no tamanho final, sem cairo. Os SVG têm preenchimento branco e
    contorno preto; a máscara é só o preenchimento (vermelho x alfa), então a
    envergadura medida não inclui o traço e as junções (nacele sobre a asa)
    ficam como linhas finas. Sprites sem SVG (os genéricos "2/1") mantêm o
    recorte da folha.

    Parâmetros: tag da versão, tamanho em px e o dicionário `tiles`, que é
    alterado no lugar.
    """
    os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
    try:
        import numpy as np
        import pygame
        pygame.image.load_sized_svg
    except (ImportError, AttributeError):
        sys.exit("--svg precisa de pygame-ce >= 2.4 e numpy (o venv do radClock tem os dois)")

    svg_cache_dir = os.path.join(CACHE_DIR, f"pw-{tag}-svg")
    print(f"Rasterizando os SVG do pw-silhouettes {tag} a {size} px:")
    converted_count, kept_from_sheet = 0, []
    for name in sorted(tiles):
        if "/" in name:
            continue                         # sprite genérico, sem SVG próprio
        try:
            airframe_json = download_cached(
                PW_RAW_URL.format(tag=tag, path=f"airframes/{name}.json"),
                os.path.join(svg_cache_dir, f"{name}.json"))
            svg_source = read_json_file(airframe_json)["art"]["frames"][0]["src"]
            svg_path = download_cached(PW_RAW_URL.format(tag=tag, path=svg_source),
                                       os.path.join(svg_cache_dir, os.path.basename(svg_source)))
            surface = pygame.image.load_sized_svg(svg_path, (size, size))
        except (OSError, ValueError, KeyError, IndexError, TypeError, pygame.error) as error:
            kept_from_sheet.append(f"{name} ({error.__class__.__name__})")
            continue
        if surface.get_size() != (size, size):      # viewBox não quadrado: centraliza
            square = pygame.Surface((size, size), pygame.SRCALPHA)
            square.blit(surface, surface.get_rect(center=(size // 2, size // 2)))
            surface = square
        red = pygame.surfarray.pixels3d(surface)[..., 0].astype(np.uint16)
        alpha = pygame.surfarray.pixels_alpha(surface).astype(np.uint16)
        mask = (red * alpha // 255).astype(np.uint8).T     # surfarray é [x, y]
        if not mask.any():
            kept_from_sheet.append(f"{name} (vazio)")
            continue
        tiles[name] = np.ascontiguousarray(mask).tobytes()
        converted_count += 1
    print(f"  {converted_count} silhuetas de SVG"
          + (f"; mantidas da folha: {', '.join(kept_from_sheet)}" if kept_from_sheet else ""))


def add_local_silhouettes(tiles, sprite_by_type, size):
    """Acrescenta os desenhos próprios de tools/data/silhouettes/<ICAO>.png.

    O pw-silhouettes não desenha todos os tipos (não tem o 747, nem ERJ-145,
    Brasília, Bandeirante...), e um tipo sem desenho cairia no sprite genérico
    da categoria (um bimotor para "pesado"). O canal alfa do PNG é a forma,
    nariz para cima; o desenho local vence o da folha.

    Parâmetros: `tiles` e `sprite_by_type` são alterados no lugar; `size` é o
    lado da silhueta em px.
    """
    if not os.path.isdir(LOCAL_SILHOUETTES_DIR):
        return
    from PIL import Image
    added_count = 0
    for file_name in sorted(os.listdir(LOCAL_SILHOUETTES_DIR)):
        if not file_name.lower().endswith(".png"):
            continue
        type_code = os.path.splitext(file_name)[0].upper()
        image_path = os.path.join(LOCAL_SILHOUETTES_DIR, file_name)
        alpha = Image.open(image_path).convert("RGBA").split()[3]
        if not alpha.getbbox():
            print(f"  ignorando {file_name}: totalmente transparente")
            continue
        sprite_name = "local/" + type_code
        tiles[sprite_name] = alpha.resize((size, size), Image.LANCZOS).tobytes()
        sprite_by_type[type_code] = sprite_name
        added_count += 1
    print(f"  {added_count} silhuetas próprias de tools/data/silhouettes/")


# ------------------------------------------------------------ rikgale/ICAOList

def remove_trailing_note(name):
    """Tira uma anotação entre parênteses do fim do nome.

    A tabela de ICAO24 traz notas ("Taiwan (unofficial)") e a de empresas traz
    a cidade ("ABX AIR, INC. (WILMINGTON, OH)"); nenhuma cabe no painel.
    """
    return re.sub(r"\s*\([^()]*\)\s*$", "", name).strip()


def load_icao_types():
    """Lê o Doc 8643: código do tipo -> (fabricante, modelo, classe, nº de motores,
    código do tipo de motor)."""
    print("Baixando os designadores de tipo do ICAO Doc 8643 (rikgale/ICAOList):")
    path = download_cached(ICAO_TYPES_URL, os.path.join(CACHE_DIR, "ICAOList.csv"))
    types = {}
    with open(path, encoding="utf-8-sig", newline="") as file:
        for row in csv.DictReader(file):
            type_code = (row.get("Aircraft TypeDesignator") or "").strip().upper()
            if not type_code or type_code in types:
                continue                     # a 1ª linha vence (a lista tem quase-duplicatas)
            maker, _, model = (row.get("MANUFACTURER, Model") or "").partition(",")
            engine_count, _, engine_type = (row.get("Number+Engine Type") or "").partition("/")
            engine_type = engine_type.split("/")[0].strip().lower()
            if engine_count.strip().isdigit():
                engine_count = int(engine_count)
            else:
                engine_count = 0
            types[type_code] = (maker.strip(), model.strip(), (row.get("Class") or "").strip(),
                                engine_count, ENGINE_TYPE_CODE.get(engine_type, 0))
    print(f"  {len(types)} designadores")
    return types


def load_airlines():
    """Lê o Doc 8585: código de 3 letras -> (empresa, indicativo falado no rádio)."""
    print("Baixando os códigos de empresa (rikgale/ICAOList):")
    path = download_cached(AIRLINES_URL, os.path.join(CACHE_DIR, "Airlines.csv"))
    airlines = {}
    with open(path, encoding="utf-8-sig", newline="") as file:
        for row in csv.DictReader(file):
            code = (row.get("3Ltr") or "").strip().upper()
            company = (row.get("Company") or "").strip()
            if len(code) != 3 or not code.isalpha() or not company or code in airlines:
                continue
            radio_callsign = (row.get("Telephony") or "").strip()[:20]
            airlines[code] = (remove_trailing_note(company)[:38], radio_callsign)
    print(f"  {len(airlines)} operadores")
    return airlines


def load_hex_ranges():
    """Lê as faixas de endereço ICAO24 -> lista ordenada de (início, fim, país)."""
    print("Baixando as faixas de endereço ICAO24 (rikgale/ICAOList):")
    path = download_cached(HEX_RANGES_URL, os.path.join(CACHE_DIR, "ICAOHexRange.csv"))
    ranges = []
    with open(path, encoding="utf-8-sig", newline="") as file:
        for row in csv.reader(file):
            if len(row) < 3:
                continue
            try:
                first, last = int(row[0].strip(), 16), int(row[1].strip(), 16)
            except ValueError:
                continue
            country = row[2].strip()
            if country.startswith("("):      # "(unallocated)", "(reserved, AFI)"
                continue
            country = remove_trailing_note(country)
            if country:
                ranges.append((first, last, country[:28]))
    ranges.sort()
    print(f"  {len(ranges)} faixas")
    return ranges


# -------------------------------------------------------------------- openap

def read_openap_yaml(text):
    """Leitor mínimo dos .yml do openap (dois níveis, só valores simples).

    Retorna: dicionário com chaves "secao.chave" (ex.: "wing.span") ou "chave".
    """
    values, section = {}, None
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        match = re.match(r"^(\s*)([\w/.+-]+):\s*(.*)$", line)
        if not match:
            continue
        indent, key, value = len(match.group(1)), match.group(2), match.group(3).strip()
        if indent == 0:
            section = key if value == "" else None
            if value:
                values[key] = value
        elif section:
            values[f"{section}.{key}"] = value
    return values


def to_float_or_none(text):
    """Converte para float; vazio, ausente ou inválido vira None."""
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


def mach_to_true_airspeed_kt(mach, altitude_m):
    """Velocidade verdadeira (kt) de um Mach de cruzeiro numa altitude, na ISA."""
    if altitude_m < 11000:
        temperature_k = 288.15 - 0.0065 * altitude_m
    else:
        temperature_k = 216.65
    return mach * 20.0468 * math.sqrt(temperature_k) * 1.94384


def engine_family(engine_name):
    """"CFM56-7B26" -> "CFM56": só a série, a variante não interessa no painel."""
    return (engine_name or "").split("-")[0].strip()[:16]


def load_openap():
    """Lê o openap: código do tipo -> dict(span_m, len_m, mtow_kg, cruise_kt, engine)."""
    print("Baixando os dados de desempenho do openap (TU Delft):")
    index_path = download_cached(OPENAP_INDEX_URL, os.path.join(CACHE_DIR, "openap-index.json"))
    file_names = [entry["name"] for entry in read_json_file(index_path)
                  if entry["name"].endswith(".yml")]
    performance = {}
    for file_name in file_names:
        path = download_cached(OPENAP_RAW_URL.format(name=file_name),
                               os.path.join(CACHE_DIR, "openap", file_name))
        with open(path, encoding="utf-8") as file:
            values = read_openap_yaml(file.read())
        mach = to_float_or_none(values.get("cruise.mach"))
        altitude_m = to_float_or_none(values.get("cruise.height"))
        cruise_kt = None
        if mach and altitude_m:
            cruise_kt = mach_to_true_airspeed_kt(mach, altitude_m)
        type_code = file_name[:-len(".yml")].upper()
        performance[type_code] = {
            "span_m": to_float_or_none(values.get("wing.span")),
            "len_m": to_float_or_none(values.get("fuselage.length")),
            "mtow_kg": to_float_or_none(values.get("mtow")),
            "cruise_kt": cruise_kt,
            "engine": engine_family(values.get("engine.default")),
        }
    print(f"  {len(performance)} aeronaves")
    return performance


# ------------------------------------------------- arquivos locais (tools/data)

def read_data_csv(file_name):
    """Lê um CSV de tools/data como lista de dicts, pulando as linhas '#'."""
    path = os.path.join(DATA_DIR, file_name)
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as file:
        lines = [line for line in file if not line.lstrip().startswith("#")]
    return list(csv.DictReader(lines))


def load_manual_specs():
    """Lê aircraft_specs.csv: código do tipo -> dados conhecidos (None = vazio)."""
    specs = {}
    for row in read_data_csv("aircraft_specs.csv"):
        type_code = (row.get("icao") or "").strip().upper()
        if not type_code:
            continue
        mtow_tonnes = to_float_or_none((row.get("mtow_t") or "").strip())
        specs[type_code] = {
            "span_m": to_float_or_none((row.get("span_m") or "").strip()),
            "len_m": to_float_or_none((row.get("len_m") or "").strip()),
            "mtow_kg": mtow_tonnes * 1000 if mtow_tonnes else None,
            "cruise_kt": to_float_or_none((row.get("cruise_kt") or "").strip()),
            # só usados quando o Doc 8643 não tem o tipo
            "mfr": (row.get("mfr") or "").strip(),
            "model": (row.get("model") or "").strip(),
        }
    print(f"  {len(specs)} linhas de tools/data/aircraft_specs.csv")
    return specs


def load_engines():
    """Lê engines.csv: código do tipo -> família de motor."""
    engines = {}
    for row in read_data_csv("engines.csv"):
        type_code = (row.get("icao") or "").strip().upper()
        engine = (row.get("engine") or "").strip()
        if type_code and engine:
            engines[type_code] = engine[:16]
    print(f"  {len(engines)} linhas de tools/data/engines.csv")
    return engines


def load_wanted_types():
    """Lê common_types.txt: conjunto de códigos de tipo (separados por espaço)."""
    path = os.path.join(DATA_DIR, "common_types.txt")
    if not os.path.exists(path):
        return set()
    type_codes = set()
    with open(path, encoding="utf-8") as file:
        for line in file:
            type_codes.update(line.split("#")[0].split())
    return {code.upper() for code in type_codes}


def load_silhouette_aliases():
    """Lê silhouette_alias.csv: código do tipo -> tipo que empresta a silhueta."""
    aliases = {}
    for row in read_data_csv("silhouette_alias.csv"):
        type_code = (row.get("icao") or "").strip().upper()
        donor_code = (row.get("use_silhouette_of") or "").strip().upper()
        if type_code and donor_code:
            aliases[type_code] = donor_code
    print(f"  {len(aliases)} apelidos de silhueta")
    return aliases


# ------------------------------------------------------------ montagem e saída

def build_type_rows(wanted_types, icao_types, performance, manual_specs, engines,
                    aliases, sprite_by_type, tile_index):
    """Monta uma linha da tabela `types` por código de tipo.

    Prioridades: o openap vence e o aircraft_specs.csv preenche as lacunas;
    nomes vêm do Doc 8643 e, se faltarem, do CSV. A silhueta é a do próprio
    tipo ou, se não houver, a do tipo indicado no silhouette_alias.csv.

    Retorna: lista de tuplas na ordem das colunas de `types`.
    """
    rows = []
    for type_code in sorted(wanted_types):
        maker, model, aircraft_class, engine_count, engine_type = \
            icao_types.get(type_code, ("", "", "", 0, 0))
        values = dict(performance.get(type_code) or {})
        manual = manual_specs.get(type_code) or {}
        for key in ("span_m", "len_m", "mtow_kg", "cruise_kt"):
            if values.get(key) is None and manual.get(key) is not None:
                values[key] = manual[key]
        maker = maker or manual.get("mfr", "")
        model = model or manual.get("model", "")

        sprite = sprite_by_type.get(type_code) or sprite_by_type.get(aliases.get(type_code, ""))
        silhouette_id = tile_index.get(sprite) if sprite else None

        # arredondamentos: envergadura e comprimento a 0,1 m, MTOW a 100 kg,
        # cruzeiro a 1 kt; zero ou ausente vira NULL (a ficha mostra "-")
        span_m = round((values.get("span_m") or 0) * 10) / 10 or None
        length_m = round((values.get("len_m") or 0) * 10) / 10 or None
        mtow_kg = round((values.get("mtow_kg") or 0) / 100) * 100 or None
        cruise_kt = round(values.get("cruise_kt") or 0) or None
        engine = values.get("engine") or engines.get(type_code, "")

        # o código é cortado em 4 letras (B190C/B190D -> B190); na gravação fica o 1º
        rows.append((type_code[:4], maker, model, aircraft_class, engine_count, engine_type,
                     engine, span_m, length_m, mtow_kg, cruise_kt, silhouette_id))
    return rows


def write_sqlite(path, tag, size, tiles, tile_order, type_rows, category_silhouettes,
                 airlines, hex_ranges):
    """Grava a base SQLite lida por radClock/acdb.py.

    Tabelas: meta, silhouettes (PNG em tons de cinza, cinza = alfa), types,
    cat_sil, operators e hex_blocks. Grava em `path.part` e renomeia no fim,
    para o radClock nunca abrir uma base pela metade.
    """
    from PIL import Image
    os.makedirs(os.path.dirname(path), exist_ok=True)   # data/ não existe num clone novo
    partial_path = path + ".part"
    if os.path.exists(partial_path):
        os.remove(partial_path)
    database = sqlite3.connect(partial_path)
    database.executescript("""
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE silhouettes (id INTEGER PRIMARY KEY, name TEXT, png BLOB);
        CREATE TABLE types (icao TEXT PRIMARY KEY, mfr TEXT, model TEXT, cls TEXT,
                            eng_n INTEGER, eng_t INTEGER, eng TEXT, span_m REAL,
                            len_m REAL, mtow_kg REAL, cruise_kt REAL, sil INTEGER);
        CREATE TABLE cat_sil (cat TEXT PRIMARY KEY, sil INTEGER);
        CREATE TABLE operators (code TEXT PRIMARY KEY, name TEXT, radio TEXT);
        CREATE TABLE hex_blocks (lo INTEGER PRIMARY KEY, hi INTEGER, country TEXT);
    """)
    database.executemany("INSERT INTO meta VALUES (?, ?)", [
        ("generator", "tools/make_aircraft_db.py"), ("silhouette_tag", tag),
        ("silhouette_px", str(size)),
        ("credits", "silhouettes: plane-watch/pw-silhouettes (CC BY-NC-SA 4.0); "
                    "types/operators/ICAO24: rikgale/ICAOList; performance: openap")])
    for silhouette_id, sprite_name in enumerate(tile_order):
        png_buffer = io.BytesIO()
        Image.frombytes("L", (size, size), tiles[sprite_name]).save(png_buffer, "PNG",
                                                                    optimize=True)
        database.execute("INSERT INTO silhouettes VALUES (?, ?, ?)",
                         (silhouette_id, sprite_name, png_buffer.getvalue()))
    database.executemany("INSERT OR IGNORE INTO types VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                         type_rows)
    database.executemany("INSERT INTO cat_sil VALUES (?, ?)",
                         sorted(category_silhouettes.items()))
    database.executemany("INSERT INTO operators VALUES (?, ?, ?)",
                         [(code, name, radio) for code, (name, radio) in sorted(airlines.items())])
    database.executemany("INSERT OR IGNORE INTO hex_blocks VALUES (?, ?, ?)", hex_ranges)
    database.commit()
    database.execute("VACUUM")
    database.close()
    os.replace(partial_path, path)
    print(f"gravado {path} ({os.path.getsize(path) / 1e6:.1f} MB)")


def parse_arguments():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", help="arquivo de saída (padrão: data/aircraft.db na raiz do projeto)")
    parser.add_argument("--sqlite", action="store_true",
                        help="mantido por compatibilidade: a saída é sempre SQLite")
    parser.add_argument("--tag", help="versão do pw-silhouettes (padrão: a mais recente)")
    parser.add_argument("--size", type=int, default=512,
                        help="lado da silhueta em px (padrão 512)")
    parser.add_argument("--svg", action="store_true",
                        help="rasteriza os SVG do pw-silhouettes em --size em vez de "
                             "ampliar a folha de 72 px (precisa de pygame-ce)")
    tls_option.add_argument(parser)
    return parser.parse_args()


def main():
    """Fluxo: silhuetas -> dados dos tipos -> escolha do que entra -> SQLite."""
    args = parse_arguments()
    tls_option.configure(not args.no_strict_tls)
    try:
        import PIL  # noqa: F401
    except ImportError:
        sys.exit("Pillow é necessário: pip install pillow")

    tag = args.tag or latest_silhouette_tag()
    tiles, sprite_by_type, sprite_by_category = load_sprite_sheet(tag, args.size)
    if args.svg:
        replace_tiles_with_svg(tag, args.size, tiles)
    add_local_silhouettes(tiles, sprite_by_type, args.size)

    icao_types = load_icao_types()
    airlines = load_airlines()
    hex_ranges = load_hex_ranges()
    performance = load_openap()
    print("Lendo os arquivos locais:")
    manual_specs = load_manual_specs()
    engines = load_engines()
    aliases = load_silhouette_aliases()

    # entram os tipos da lista mantida à mão e todos os que têm desenho
    wanted_types = {code for code in load_wanted_types() | set(sprite_by_type) if code}
    unnamed = sorted(code for code in wanted_types
                     if code not in icao_types and not (manual_specs.get(code) or {}).get("mfr"))
    if unnamed:
        print(f"  aviso: {len(unnamed)} códigos sem nome no Doc 8643 nem nas colunas "
              f"mfr/model do aircraft_specs.csv: {' '.join(unnamed)}")

    # só gravamos as silhuetas que algum tipo (direto ou por apelido) ou
    # alguma categoria realmente usa
    used_sprites = {sprite_by_type[code] for code in wanted_types if code in sprite_by_type}
    used_sprites |= {sprite_by_type[aliases[code]] for code in wanted_types
                     if code in aliases and aliases[code] in sprite_by_type}
    used_sprites |= set(sprite_by_category.values())
    tile_order = sorted(used_sprites)
    tile_index = {name: position for position, name in enumerate(tile_order)}

    type_rows = build_type_rows(wanted_types, icao_types, performance, manual_specs,
                                engines, aliases, sprite_by_type, tile_index)
    category_silhouettes = {category: tile_index[sprite]
                            for category, sprite in sprite_by_category.items()
                            if sprite in tile_index}

    with_silhouette = sum(1 for row in type_rows if row[11] is not None)
    with_span = sum(1 for row in type_rows if row[7])
    with_engine = sum(1 for row in type_rows if row[6])
    print(f"{len(type_rows)} tipos: {with_silhouette} com silhueta, {with_span} com "
          f"dimensões, {with_engine} com motor, {len(tile_order)} desenhos distintos")

    output_path = args.out or os.path.join(UTILS_DIR, os.pardir, "data", "aircraft.db")
    write_sqlite(os.path.abspath(output_path), tag, args.size, tiles, tile_order, type_rows,
                 category_silhouettes, airlines, hex_ranges)


if __name__ == "__main__":
    main()
