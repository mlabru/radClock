#!/usr/bin/env python3
"""Desenha as silhuetas que faltam no pw-silhouettes em tools/data/silhouettes/.

O pw-silhouettes (fonte dos demais desenhos da base) não tem o 747 nem vários
tipos comuns no Brasil: família ERJ-145 (motores na traseira, cauda em T),
EMB-120 Brasília, EMB-110 Bandeirante e 767. Sem desenho, o tipo cairia no
sprite genérico da categoria (um bimotor para "pesado"). Em vez de emprestar
um desenho errado, este script traça a planta a partir das dimensões
públicas: fuselagem, asa (cordas na raiz/ponta e enflechamento), empenagem,
naceles e hélices, no mesmo enquadramento da folha de sprites (nariz para
cima, comprimento ocupando o quadro, envergadura em escala).

Fluxo: cada função de DRAWINGS monta um Plan em metros -> Plan.render
desenha os polígonos supersampled e reduz -> PNG com a forma no canal alfa.

O resultado é trabalho próprio, geometria a partir de dados públicos: não
tem a licença CC BY-NC-SA da folha de sprites.

    python3 tools/make_local_silhouettes.py         # grava tools/data/silhouettes/*.png
    python3 tools/make_local_silhouettes.py --show  # prévia em ASCII, não grava nada

Depois rode de novo o tools/make_aircraft_db.py para colocá-las no aircraft.db.
"""
import argparse
import math
import os

from PIL import Image, ImageDraw

UTILS_DIR = os.path.dirname(os.path.abspath(__file__))
OUTPUT_DIR = os.path.join(UTILS_DIR, "data", "silhouettes")

SUPERSAMPLING = 8   # desenha 8x maior e reduz: só as bordas importam
TILE_PX = 512       # lado do PNG (o make_aircraft_db reamostra se --size for outro)


class Plan:
    """Planta da aeronave em metros: nariz em y=0, eixo em x=0, +y para trás.

    Cada método acrescenta polígonos (lista de (x, y)) em `self.polygons`;
    peças simétricas acrescentam também o espelho em x.
    """

    def __init__(self, length, span):
        self.length, self.span = length, span
        self.polygons = []

    # -- peças ---------------------------------------------------------------
    def fuselage(self, width, nose, tail_start, tail_width):
        """Corpo reto com nariz parabólico e cone de cauda afinando.

        width: largura; nose: comprimento do nariz; tail_start: onde começa o
        cone de cauda; tail_width: largura na ponta da cauda (todos em m).
        """
        half_width = width / 2.0
        steps = [i / 12.0 for i in range(13)]
        left = [(-half_width * math.sqrt(t), nose * t * t) for t in steps]   # nariz: perfil x²
        left += [(-half_width, tail_start), (-tail_width / 2.0, self.length)]
        right = [(-x, y) for x, y in reversed(left)]
        self.polygons.append(left + right)

    def wing(self, y_root, root_chord, tip_chord, semi_span, sweep_deg):
        """Asa (ou estabilizador horizontal) trapezoidal, nos dois lados.

        y_root: bordo de ataque na raiz; semi_span: meia envergadura;
        sweep_deg: enflechamento do bordo de ataque.
        """
        tip_offset = semi_span * math.tan(math.radians(sweep_deg))
        half = [(0.0, y_root), (semi_span, y_root + tip_offset),
                (semi_span, y_root + tip_offset + tip_chord), (0.0, y_root + root_chord)]
        self.polygons.append(half)
        self.polygons.append([(-x, y) for x, y in half])

    def nacelle(self, x, y, length, width):
        """Nacele do motor: cápsula de pontas arredondadas, nos dois lados."""
        h = width / 2.0
        self.polygons.append([(x - h, y + h), (x - h, y + length - h),
                              (x - h * .7, y + length), (x + h * .7, y + length),
                              (x + h, y + length - h), (x + h, y + h),
                              (x + h * .7, y), (x - h * .7, y)])
        self.polygons.append([(-p[0], p[1]) for p in self.polygons[-1]])

    def pylon(self, x0, x1, y0, y1):
        """Suporte que liga o motor traseiro à fuselagem (nos dois lados)."""
        self.polygons.append([(x0, y0), (x1, y0 + 0.3), (x1, y1 - 0.3), (x0, y1)])
        self.polygons.append([(-x, y) for x, y in self.polygons[-1]])

    def prop(self, x, y, diameter, blade=0.25):
        """Hélice vista de cima: barra fina na frente da nacele."""
        r = diameter / 2.0
        self.polygons.append([(x - r, y), (x + r, y), (x + r, y + blade), (x - r, y + blade)])
        if x:                                        # fora do eixo: espelha
            self.polygons.append([(-px, py) for px, py in self.polygons[-1]])

    def fin(self, y_root, root_chord, tip_chord, height, sweep_deg):
        """Deriva vista de cima: uma lasca fina sobre o eixo."""
        tip_offset = height * math.tan(math.radians(sweep_deg))
        w = 0.55                                     # meia espessura na raiz
        self.polygons.append([(-w, y_root), (-w * .35, y_root + tip_offset),
                              (w * .35, y_root + tip_offset + tip_chord),
                              (w, y_root + root_chord)])

    # -- desenho -------------------------------------------------------------
    def render(self, tile_px=TILE_PX):
        """Desenha a planta num PNG quadrado de tile_px.

        Retorna: imagem RGBA branca com a forma no canal alfa (nariz para cima,
        comprimento ocupando a altura do quadro).
        """
        big_px = tile_px * SUPERSAMPLING
        px_per_metre = big_px / self.length
        mask = Image.new("L", (big_px, big_px), 0)
        draw = ImageDraw.Draw(mask)
        for polygon in self.polygons:
            draw.polygon([(x * px_per_metre + big_px / 2.0, y * px_per_metre)
                          for x, y in polygon], fill=255)
        mask = mask.resize((tile_px, tile_px), Image.LANCZOS)
        image = Image.new("RGBA", (tile_px, tile_px), (255, 255, 255, 0))
        image.putalpha(mask)
        return image


def b747_400():
    """747-400: 70,66 m de comprimento, 64,44 m de envergadura, enflechamento 37,5°."""
    p = Plan(70.66, 64.44)
    p.fuselage(width=6.5, nose=9.0, tail_start=52.0, tail_width=1.8)
    p.wing(y_root=25.5, root_chord=16.6, tip_chord=4.1, semi_span=32.22, sweep_deg=37.5)
    p.wing(y_root=57.6, root_chord=9.0, tip_chord=2.8, semi_span=11.1, sweep_deg=40.0)
    p.fin(y_root=55.0, root_chord=12.0, tip_chord=4.0, height=10.0, sweep_deg=45.0)
    for x, y in ((11.6, 27.5), (21.9, 36.0)):        # motores interno / externo
        p.nacelle(x, y, length=8.4, width=3.1)
    return p


def b747_8():
    """747-8: 5,6 m mais longo, pontas de asa inclinadas, mesma planta da família."""
    p = Plan(76.25, 68.45)
    p.fuselage(width=6.5, nose=9.5, tail_start=57.0, tail_width=1.8)
    p.wing(y_root=28.0, root_chord=17.4, tip_chord=3.4, semi_span=34.22, sweep_deg=37.5)
    p.wing(y_root=62.5, root_chord=9.0, tip_chord=2.8, semi_span=11.1, sweep_deg=40.0)
    p.fin(y_root=59.5, root_chord=12.5, tip_chord=4.0, height=10.0, sweep_deg=45.0)
    for x, y in ((12.2, 30.0), (23.0, 39.0)):
        p.nacelle(x, y, length=9.2, width=3.4)
    return p


def e145():
    """Embraer ERJ-145: 29,87 m, envergadura 20,04 m, enflechamento 22,7°, cauda
    em T, dois AE3007 na traseira. E135 e E35L (Legacy 600) usam o mesmo desenho
    pelo tools/data/silhouette_alias.csv."""
    p = Plan(29.87, 20.04)
    p.fuselage(width=2.28, nose=4.0, tail_start=23.0, tail_width=0.9)
    p.wing(y_root=11.6, root_chord=4.0, tip_chord=1.05, semi_span=10.02, sweep_deg=22.7)
    p.wing(y_root=25.8, root_chord=2.4, tip_chord=1.1, semi_span=3.78, sweep_deg=26.0)  # cauda em T
    p.fin(y_root=23.8, root_chord=3.6, tip_chord=2.2, height=2.6, sweep_deg=35.0)
    p.nacelle(2.25, 19.8, length=4.1, width=1.45)
    p.pylon(1.0, 1.7, 20.8, 23.2)
    return p


def e120():
    """Embraer EMB-120 Brasília: 20,07 m, envergadura 19,78 m, asa reta, cauda
    em T, dois turboélices PW118 na asa."""
    p = Plan(20.07, 19.78)
    p.fuselage(width=2.28, nose=3.0, tail_start=14.5, tail_width=0.8)
    p.wing(y_root=8.3, root_chord=2.9, tip_chord=1.25, semi_span=9.89, sweep_deg=2.0)
    p.wing(y_root=17.6, root_chord=1.9, tip_chord=1.0, semi_span=3.45, sweep_deg=20.0)  # cauda em T
    p.fin(y_root=15.6, root_chord=3.2, tip_chord=1.6, height=2.0, sweep_deg=30.0)
    p.nacelle(3.45, 6.4, length=4.6, width=1.15)
    p.prop(3.45, 6.0, diameter=3.2)
    return p


def e110():
    """Embraer EMB-110 Bandeirante: 15,10 m, envergadura 15,33 m, asa baixa
    reta, cauda convencional, dois turboélices PT6A."""
    p = Plan(15.10, 15.33)
    p.fuselage(width=1.7, nose=2.4, tail_start=11.0, tail_width=0.6)
    p.wing(y_root=5.4, root_chord=2.3, tip_chord=1.3, semi_span=7.66, sweep_deg=0.0)
    p.wing(y_root=12.9, root_chord=1.6, tip_chord=0.9, semi_span=3.1, sweep_deg=8.0)
    p.fin(y_root=11.6, root_chord=2.6, tip_chord=1.2, height=2.5, sweep_deg=30.0)
    p.nacelle(2.9, 3.9, length=3.7, width=0.85)
    p.prop(2.9, 3.6, diameter=2.36)
    return p


def b767_300():
    """767-300: 54,94 m, envergadura 47,57 m, enflechamento 31,5°, bimotor sob a asa."""
    p = Plan(54.94, 47.57)
    p.fuselage(width=5.03, nose=7.0, tail_start=42.0, tail_width=1.4)
    p.wing(y_root=19.8, root_chord=11.0, tip_chord=2.3, semi_span=23.78, sweep_deg=31.5)
    p.wing(y_root=45.5, root_chord=6.2, tip_chord=2.0, semi_span=9.3, sweep_deg=34.0)
    p.fin(y_root=43.0, root_chord=9.0, tip_chord=3.0, height=8.0, sweep_deg=40.0)
    p.nacelle(7.6, 19.5, length=7.0, width=2.8)     # fica à frente do bordo de ataque
    return p


# código ICAO do tipo -> função que monta a planta
DRAWINGS = {"B744": b747_400, "B748": b747_8, "B763": b767_300,
            "E110": e110, "E120": e120, "E145": e145}


def print_ascii_preview(image, step=2):
    """Mostra a silhueta no terminal: '#' cheio, '.' borda, ' ' vazio."""
    alpha = image.split()[3].load()
    width, height = image.size
    for y in range(0, height, step):
        row = ""
        for x in range(width):
            if alpha[x, y] > 128:
                row += "#"
            elif alpha[x, y] > 32:
                row += "."
            else:
                row += " "
        print(row)


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--show", action="store_true", help="prévia em ASCII, não grava nada")
    parser.add_argument("--size", type=int, default=TILE_PX, help="lado do PNG em px")
    args = parser.parse_args()
    if not args.show:
        os.makedirs(OUTPUT_DIR, exist_ok=True)
    for type_code, build_plan in sorted(DRAWINGS.items()):
        image = build_plan().render(args.size)
        if args.show:
            print(f"---- {type_code} ----")
            print_ascii_preview(image)
            continue
        path = os.path.join(OUTPUT_DIR, type_code + ".png")
        image.save(path)
        print(f"  gravado {os.path.relpath(path, UTILS_DIR)}")


if __name__ == "__main__":
    main()
