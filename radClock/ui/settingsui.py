"""
Tela de ajustes de exibição (abre pelo botão AJUSTES do painel ou pela tecla `a`).

Tudo por toque, com alvos grandes (52 px, ~9 mm na tela de 7"). Uma linha por
ajuste, com o nome à esquerda e as opções em botões; a opção em vigor fica
acesa (azul):

    SÍMBOLO     [ QUADRADO ] [  AVIÃO  ]
    VETOR       [   DESL   ] [  1 MIN  ] [  2 MIN  ]
    RASTRO      [   DESL   ] [  1 MIN  ] [  2 MIN  ]
    ALERTA      [ COR+SOM  ] [ SÓ COR  ] [  DESL   ]
    FEIXE       [   LIGA   ] [  DESL   ]
    ZONAS INIB. [  MOSTRA  ] [ OCULTA  ]
    BRILHO      [   ALTO   ] [  MÉDIO  ] [  BAIXO  ]

As linhas vêm da tabela ROWS: acrescentar um ajuste é acrescentar uma linha
nela (e o campo em options.DisplayOptions). FEIXE só aparece com
``[radar] sweep_s`` > 0, ZONAS INIB. só com zonas ``[[radar.inhibit]]`` e
BRILHO só quando há backlight controlável (``core.backlight``).

Cada botão tem uma CHAVE de texto, que é o nome do retângulo em
``SettingsScreen.rects`` e a ação devolvida por ``hit()``:

    "close"           VOLTAR
    "alert:2"         ajuste "alert", 3ª opção da linha (DESL)

A tela não grava nada: ``apply`` devolve as novas opções e o app as salva no
arquivo de estado. Cores e fonte vêm de ``palette``.
"""
from __future__ import annotations

from dataclasses import replace

import pygame

from radClock.core.options import (ALERT_COLOR, ALERT_OFF, ALERT_SOUND, SYMBOL_PLANE, SYMBOL_SQUARE,
                                  DisplayOptions)
from radClock.ui.palette import BG, BTN_BG, BTN_ON, DIM, EMERG, PANEL_LINE, TXT, WARN, ui_font

# (campo de DisplayOptions, nome na tela, [(valor, texto do botão), ...])
ROWS = [
    ("symbol", "SÍMBOLO", [(SYMBOL_SQUARE, "QUADRADO"), (SYMBOL_PLANE, "AVIÃO")]),
    ("vector_min", "VETOR", [(0.0, "DESL"), (1.0, "1 MIN"), (2.0, "2 MIN")]),
    ("trail_s", "RASTRO", [(0.0, "DESL"), (60.0, "1 MIN"), (120.0, "2 MIN")]),
    ("alert", "ALERTA", [(ALERT_SOUND, "COR+SOM"), (ALERT_COLOR, "SÓ COR"), (ALERT_OFF, "DESL")]),
    ("sweep", "FEIXE", [(True, "LIGA"), (False, "DESL")]),
    ("zones", "ZONAS INIB.", [(True, "MOSTRA"), (False, "OCULTA")]),
    ("brightness", "BRILHO", [(100, "ALTO"), (40, "MÉDIO"), (10, "BAIXO")]),
]

# Medidas da tela de ajustes, em px.
PADDING = 10
TOP_BAR_HEIGHT = 64             # título e VOLTAR
BUTTON_HEIGHT = 52
LABEL_WIDTH = 190               # coluna com o nome do ajuste
BUTTON_GAP = 10
MAX_OPTIONS = 3                 # botões por linha: todas as linhas usam a mesma grade
CLOSE_WIDTH = 130


class SettingsScreen:
    """
    Tela cheia dos ajustes: desenha (``draw``), acha o botão tocado (``hit``)
    e calcula as novas opções (``apply``).
    """

    def __init__(self, screen: pygame.Surface, sweep_available: bool, zones_available: bool,
                 brightness_available: bool = False):
        """
        Parâmetros:
            screen: superfície da tela.
            sweep_available: há feixe ([radar] sweep_s > 0)? Senão, sem a linha FEIXE.
            zones_available: há zonas de inibição? Senão, sem a linha ZONAS INIB.
            brightness_available: há backlight controlável? Senão, sem a linha BRILHO.
        """
        self.screen = screen
        self.f_big = ui_font(22, bold=True)
        self.f = ui_font(17, bold=True)
        self.f_small = ui_font(13)
        self.rows = [row for row in ROWS
                     if (row[0] != "sweep" or sweep_available)
                     and (row[0] != "zones" or zones_available)
                     and (row[0] != "brightness" or brightness_available)]
        self.rects: dict[str, pygame.Rect] = {}
        self.rows_y: list[int] = []
        self._layout()

    # ------------------------------------------------------------ layout
    def _layout(self) -> None:
        """
        Calcula o retângulo de cada botão (uma vez, na criação).

        As linhas dividem igualmente a altura abaixo da barra do topo; os
        botões de opção formam uma grade de MAX_OPTIONS colunas depois da
        coluna dos nomes, então as opções iguais ficam alinhadas entre linhas.
        """
        width, height = self.screen.get_size()
        self.rects["close"] = pygame.Rect(width - PADDING - CLOSE_WIDTH, PADDING,
                                          CLOSE_WIDTH, BUTTON_HEIGHT - 8)

        row_height = (height - TOP_BAR_HEIGHT) // len(self.rows)
        options_x = PADDING + LABEL_WIDTH
        button_width = (width - options_x - PADDING - (MAX_OPTIONS - 1) * BUTTON_GAP) // MAX_OPTIONS
        for row_index, (field, _label, choices) in enumerate(self.rows):
            row_y = TOP_BAR_HEIGHT + row_index * row_height
            button_y = row_y + (row_height - BUTTON_HEIGHT) // 2
            self.rows_y.append(row_y)
            for choice_index in range(len(choices)):
                x = options_x + choice_index * (button_width + BUTTON_GAP)
                self.rects[f"{field}:{choice_index}"] = pygame.Rect(x, button_y, button_width,
                                                                    BUTTON_HEIGHT)

    # ------------------------------------------------------------- toque
    def hit(self, pos: tuple[int, int]) -> str | None:
        """Chave do botão em ``pos``, ou None se o toque não acertou nada."""
        for key, rect in self.rects.items():
            if rect.collidepoint(pos):
                return key
        return None

    def apply(self, options: DisplayOptions, action: str) -> DisplayOptions:
        """Novas opções depois de tocar no botão ``action`` (chave de ``hit``).

        "close" não é tratado aqui (o app fecha a tela).

        Retorno:
            As opções com o ajuste trocado, ou as mesmas se a ação não é de opção.
        """
        if ":" not in action:
            return options
        field, choice_text = action.split(":")
        for row_field, _label, choices in self.rows:
            if row_field == field:
                value = choices[int(choice_text)][0]
                return replace(options, **{field: value})
        return options

    # ----------------------------------------------------------- desenho
    def draw(self, options: DisplayOptions, sound_ok: bool) -> None:
        """Desenha a tela inteira de ajustes.

        Parâmetros:
            options: os ajustes em vigor (a opção atual de cada linha fica acesa).
            sound_ok: False mostra "sem som" sob ALERTA (COR+SOM vira só cor).
        """
        screen = self.screen
        screen.fill(BG)
        title = self.f_big.render("AJUSTES DA TELA", True, TXT)
        screen.blit(title, (PADDING + 2, (TOP_BAR_HEIGHT - title.get_height()) // 2))
        self._button("close", "VOLTAR")

        for (field, label, choices), row_y in zip(self.rows, self.rows_y):
            pygame.draw.line(screen, PANEL_LINE, (PADDING, row_y),
                             (screen.get_width() - PADDING, row_y))
            row_center_y = self.rects[f"{field}:0"].centery
            name = self.f.render(label, True, TXT)
            screen.blit(name, (PADDING + 2, row_center_y - name.get_height() // 2))
            if field == "alert" and not sound_ok:
                note = self.f_small.render("sem saída de som", True, EMERG)
                screen.blit(note, (PADDING + 2, row_center_y + name.get_height() // 2 + 2))

            current = getattr(options, field)
            for choice_index, (value, text) in enumerate(choices):
                is_on = value == current
                # alerta desligado em laranja: reforça que é uma escolha arriscada
                warn = is_on and field == "alert" and value == ALERT_OFF
                self._button(f"{field}:{choice_index}", text, on=is_on, warn=warn)

    def _button(self, key: str, label: str, on: bool = False, warn: bool = False) -> None:
        """Desenha o botão ``key``: fundo azul se ``on``; texto laranja se ``warn``."""
        rect = self.rects[key]
        background = BTN_ON if on else BTN_BG
        text_color = DIM
        if on:
            text_color = TXT
        if warn:
            text_color = WARN
        pygame.draw.rect(self.screen, background, rect, border_radius=8)
        pygame.draw.rect(self.screen, PANEL_LINE, rect, 1, border_radius=8)
        rendered = self.f.render(label, True, text_color)
        self.screen.blit(rendered, rendered.get_rect(center=rect.center))
