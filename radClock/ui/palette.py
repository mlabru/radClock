"""Cores e fonte da interface, compartilhadas por todas as telas.

Ficam fora do ``render`` para que o escopo (``render``), o painel (``panel``),
a ficha (``card``) e a tela do relógio (``clockui``) usem as mesmas cores sem
um módulo de desenho depender do outro.
"""
from __future__ import annotations

import pygame

Color = tuple[int, int, int]

BG = (4, 12, 8)                 # fundo do escopo
CROSS = (0, 56, 32)             # cruz norte-sul / leste-oeste
RING = (0, 96, 48)              # anéis de distância
RING_TXT = (0, 140, 70)         # rótulos dos anéis e pontos cardeais
TXT = (210, 230, 215)           # texto normal
DIM = (110, 140, 120)           # texto secundário
OK = (60, 220, 120)             # fonte de dados em dia
WARN = (255, 170, 40)           # avisos (fonte atrasada, sem NTP)
AC = (0, 220, 255)              # aeronave normal
AC_TYPE = (240, 220, 90)        # tipo da aeronave no rótulo
STALE = (110, 110, 110)         # aeronave com posição velha
SELECTED = (255, 220, 0)        # aeronave selecionada
EMERG = (255, 80, 48)           # emergência (squawk 7500/7600/7700)
VECTOR = (230, 0, 230)          # vetor de velocidade (modo ATC)
PANEL_BG = (8, 20, 14)
PANEL_LINE = (0, 70, 40)
BTN_BG = (16, 52, 34)           # botão desligado
BTN_ON = (20, 70, 110)          # botão ligado
ECHO_TXT = (90, 150, 210)       # textos do eco de chuva e créditos
TRAIL = (0, 150, 180)           # rastro (modo ATC)
CONFLICT = (255, 40, 40)        # alerta de conflito
SWEEP = (60, 255, 140)          # feixe de varredura
SILHOUETTE = (255, 230, 40)     # silhueta da ficha
WX_TXT = (150, 190, 160)        # clima no canto do escopo
INHIBIT = (90, 80, 30)          # zonas de inibição de conflito


def ui_font(size: int, bold: bool = False) -> pygame.font.Font:
    """Fonte monoespaçada usada em toda a interface.

    O Raspberry Pi OS já traz a DejaVu; SysFont cai na fonte padrão se não achar.
    """
    return pygame.font.SysFont("dejavusansmono", size, bold=bold)
