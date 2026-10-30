"""Símbolos de carta dos pontos do mapa (aeroporto, VOR, NDB, fixo...).

Usados uma vez por alcance, quando ``Renderer._build_map`` desenha o mapa na
superfície em cache; nunca a cada quadro.
"""
from __future__ import annotations

from math import cos, radians, sin

import pygame

from radClock.ui.palette import Color


def draw_map_symbol(surf: pygame.Surface, kind: str, x: int, y: int,
                    color: Color, size: int = 0) -> pygame.Rect:
    """Desenha o símbolo de carta de um ponto do mapa.

    Símbolos: aeroporto = círculo, VOR = hexágono, VOR/DME = hexágono no
    quadrado, DME = quadrado, NDB = círculo pontilhado, fixo = triângulo,
    qualquer outro = cruz.

    Parâmetros:
        surf: superfície onde desenhar.
        kind: tipo do ponto ("airport", "vor", "vordme", "dme", "ndb", "fix"...).
        x, y: centro do símbolo, em px.
        color: cor do traço.
        size: porte do aeroporto (0 pequeno, 1 médio, 2 grande); ignorado nos demais.

    Retorno:
        A área ocupada pelo símbolo (os rótulos do mapa a evitam).
    """
    if kind == "airport":
        return _draw_airport_symbol(surf, x, y, color, size)
    if kind in ("vor", "vordme"):
        return _draw_vor_symbol(surf, x, y, color, with_dme=(kind == "vordme"))
    if kind == "dme":
        pygame.draw.rect(surf, color, (x - 4, y - 4, 9, 9), 1)
        return pygame.Rect(x - 4, y - 4, 9, 9)
    if kind == "ndb":
        return _draw_ndb_symbol(surf, x, y, color)
    if kind == "fix":
        pygame.draw.polygon(surf, color, [(x, y - 4), (x + 4, y + 3), (x - 4, y + 3)], 1)
        return pygame.Rect(x - 4, y - 4, 9, 8)
    pygame.draw.line(surf, color, (x - 3, y), (x + 3, y))
    pygame.draw.line(surf, color, (x, y - 3), (x, y + 3))
    return pygame.Rect(x - 3, y - 3, 7, 7)


def _draw_airport_symbol(surf: pygame.Surface, x: int, y: int, color: Color,
                         size: int) -> pygame.Rect:
    """Círculo; quanto maior o aeroporto, maior o raio. Grande ganha um ponto central."""
    clamped_size = min(max(size, 0), 2)
    radius = 3 + clamped_size
    pygame.draw.circle(surf, color, (x, y), radius, 1)
    if size >= 2:
        pygame.draw.circle(surf, color, (x, y), 1)
    return pygame.Rect(x - radius, y - radius, 2 * radius + 1, 2 * radius + 1)


def _draw_vor_symbol(surf: pygame.Surface, x: int, y: int, color: Color,
                     with_dme: bool) -> pygame.Rect:
    """Hexágono com ponto central; com DME, também um quadrado em volta."""
    hexagon = []
    for angle_deg in range(0, 360, 60):
        hexagon.append((x + round(5 * cos(radians(angle_deg))),
                        y + round(5 * sin(radians(angle_deg)))))
    pygame.draw.polygon(surf, color, hexagon, 1)
    pygame.draw.circle(surf, color, (x, y), 1)
    if with_dme:
        pygame.draw.rect(surf, color, (x - 6, y - 6, 13, 13), 1)
    return pygame.Rect(x - 6, y - 6, 13, 13)


def _draw_ndb_symbol(surf: pygame.Surface, x: int, y: int, color: Color) -> pygame.Rect:
    """Círculo pontilhado (como na carta) com um ponto cheio no centro."""
    for angle_deg in range(0, 360, 45):
        dot = (x + round(5 * cos(radians(angle_deg))), y + round(5 * sin(radians(angle_deg))))
        surf.set_at(dot, color)
    pygame.draw.circle(surf, color, (x, y), 2)
    return pygame.Rect(x - 5, y - 5, 11, 11)
