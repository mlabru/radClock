"""
Ficha da aeronave no escopo: a silhueta em escala real, no lugar do tráfego.

A escala é fixa: CARD_SCALE_SPAN_M metros ocupam o diâmetro do escopo, então
dá para comparar o tamanho de tipos diferentes (barra de 10 m embaixo). Sem
envergadura conhecida, ou com o desenho genérico da categoria, a silhueta sai
"fora de escala".

A silhueta acende e apaga com o feixe, como os alvos. Na primeira volta depois
de abrir a ficha, só aparece o que o feixe já varreu.

Caches (não remova sem medir no Pi):
* a silhueta recortada, por número de desenho (``_silhouette_cache``);
* a silhueta reescalada, por (desenho, tamanho): reescalar o original de
  512 px a cada quadro custava ~1 ms no PC (~10 no Pi).

Os textos da ficha (modelo, dimensões, operador) ficam no painel: ver
``panel.Panel._draw_card_panel``.
"""
from __future__ import annotations

import io
from math import cos, radians, sin

import numpy as np
import pygame

from radClock.core.acdb import AircraftDB, Card
from radClock.core.model import Aircraft
from radClock.ui.palette import DIM, RING_TXT, SILHOUETTE, ui_font

CARD_SCALE_SPAN_M = 80  # envergadura que ocupa o diâmetro do escopo (A380: 79,8 m)
UNSCALED_WIDTH = 0.8    # fora de escala: largura em fração do raio do escopo
SCALE_BAR_M = 10        # comprimento da barra de escala
SCALE_BAR_UP_PX = 70    # a barra fica acima do "S" e do rótulo "km"
REVEAL_STEP_DEG = 5     # resolução da "fatia de pizza" da primeira volta


class CardView:
    """Guarda o estado da ficha aberta e desenha a silhueta no escopo.

    Uso (ver render.Renderer.draw):
        card = card_view.update(selected_aircraft, card_wanted, now)
        if card is not None:
            card_view.draw(card, now, beam, glow_mask)
    """

    def __init__(self, screen: pygame.Surface, acdb: AircraftDB, cx: int, cy: int,
                 r_px: int, sweep_s: float):
        """
        Parâmetros:
            screen: superfície da tela.
            acdb: base offline de aeronaves (dados e desenhos).
            cx, cy: centro do escopo (px).
            r_px: raio útil do escopo (px).
            sweep_s: período do feixe (s); 0 = sem feixe.
        """
        self.screen = screen
        self.acdb = acdb
        self.cx = cx
        self.cy = cy
        self.r_px = r_px
        self.sweep_s = sweep_s
        self.font_medium = ui_font(15)
        self.font_small = ui_font(12)

        # ficha aberta: hex da aeronave e instante em que abriu (animação da 1ª volta)
        self._card_hex: str | None = None
        self._opened_at = 0.0
        self._silhouette_cache: dict[int, pygame.Surface | None] = {}
        self._scaled_silhouette: pygame.Surface | None = None
        self._scaled_silhouette_key: tuple | None = None

    def update(self, selected_aircraft: Aircraft | None, card_wanted: bool,
               now: float) -> Card | None:
        """Atualiza o estado da ficha e devolve seus dados (None = ficha fechada).

        Guarda o instante em que a ficha abriu para cada aeronave: na primeira
        volta do feixe só aparece a parte da silhueta já varrida.

        Parâmetros:
            selected_aircraft: a aeronave selecionada, ou None.
            card_wanted: se o usuário pediu a ficha.
            now: instante atual (relógio monotônico).
        """
        if not card_wanted or selected_aircraft is None:
            self._card_hex = None
            return None
        if self._card_hex != selected_aircraft.hex:
            self._card_hex = selected_aircraft.hex
            self._opened_at = now
        return self.acdb.card(selected_aircraft)

    def draw(self, card: Card, now: float, beam: float | None,
             glow_mask: pygame.Surface | None) -> None:
        """
        Silhueta (em escala quando possível) + barra de 10 m no centro do escopo.

        Parâmetros:
            card: dados da ficha (de ``update``).
            now: instante atual (relógio monotônico).
            beam: azimute atual do feixe (graus), ou None se não há feixe.
            glow_mask: máscara de brilho do feixe (nariz para o norte), ou None.
        """
        if card.sil is None:
            text = self.font_medium.render("SEM DESENHO PARA ESTE TIPO", True, DIM)
            self.screen.blit(text, text.get_rect(center=(self.cx, self.cy)))
            return
        silhouette = self._silhouette(card.sil)
        if silhouette is None:
            return

        px_per_m = 2 * self.r_px / CARD_SCALE_SPAN_M
        span_m = None
        if card.type is not None:
            span_m = card.type.span_m
        to_scale = span_m is not None and not card.sil_generic
        if to_scale:
            width = span_m * px_per_m
        else:
            width = self.r_px * UNSCALED_WIDTH
        height = width * silhouette.get_height() / silhouette.get_width()
        size = (max(1, round(width)), max(1, round(height)))

        image = self._scaled_copy(silhouette, card.sil, size)
        rect = image.get_rect(center=(self.cx, self.cy))
        if beam is not None and glow_mask is not None:
            self._apply_glow(image, rect, now, beam, glow_mask, width + height)
        self.screen.blit(image, rect)

        self._draw_scale_note(card, to_scale, px_per_m)

    # ============================================================ silhueta
    def _silhouette(self, sil: int) -> pygame.Surface | None:
        """Silhueta amarela recortada no contorno (largura = envergadura), com cache.

        Retorno:
            Superfície RGBA, ou None se a base não tem imagem válida.
        """
        if sil in self._silhouette_cache:
            return self._silhouette_cache[sil]
        surface = self._load_silhouette(sil)
        self._silhouette_cache[sil] = surface
        return surface

    def _load_silhouette(self, sil: int) -> pygame.Surface | None:
        """Lê o PNG da base e converte o branco em alfa sobre a cor SILHOUETTE."""
        png = self.acdb.silhouette_png(sil)
        if not png:
            return None
        try:
            image = pygame.image.load(io.BytesIO(png), "s.png")
            gray = pygame.surfarray.array3d(image)[..., 0]
        except (pygame.error, ValueError):
            return None
        if not gray.any():
            return None
        # recorta na caixa dos pixels acesos: a largura passa a ser a envergadura
        xs, ys = np.nonzero(gray)
        gray = gray[xs.min():xs.max() + 1, ys.min():ys.max() + 1]
        surface = pygame.Surface(gray.shape, pygame.SRCALPHA)
        surface.fill((*SILHOUETTE, 0))
        alpha = pygame.surfarray.pixels_alpha(surface)
        alpha[:] = gray
        del alpha                               # libera o lock da superfície
        return surface

    def _scaled_copy(self, silhouette: pygame.Surface, sil: int,
                     size: tuple[int, int]) -> pygame.Surface:
        """
        Silhueta reescalada para `size`, com cache.

        Reescalar o original de 512 px a cada quadro custava ~1 ms no PC (~10 no
        Pi); o tamanho só muda com a aeronave. Devolve uma cópia porque o
        brilho do feixe multiplica a imagem.
        """
        key = (sil, size)
        if self._scaled_silhouette_key != key:
            self._scaled_silhouette = pygame.transform.smoothscale(silhouette, size)
            self._scaled_silhouette_key = key
        return self._scaled_silhouette.copy()

    # =============================================================== feixe
    def _apply_glow(self, image: pygame.Surface, rect: pygame.Rect, now: float,
                    beam: float, glow_mask: pygame.Surface, reveal_radius: float) -> None:
        """
        Faz a silhueta acender e apagar com o feixe, como os alvos.

        Na primeira volta depois de abrir a ficha, só aparece o que o feixe já
        varreu (uma "fatia de pizza" que cresce até a volta completa).

        Parâmetros:
            image: silhueta já na escala (é alterada).
            rect: posição da imagem na tela.
            beam: azimute atual do feixe (graus).
            glow_mask: máscara de brilho com o feixe apontando para o norte.
            reveal_radius: raio da fatia (maior que a imagem).
        """
        rotated_mask = pygame.transform.rotate(glow_mask, -beam)
        mask_rect = rotated_mask.get_rect(center=(self.cx, self.cy))
        image.blit(rotated_mask, (0, 0), area=rect.move(-mask_rect.x, -mask_rect.y),
                   special_flags=pygame.BLEND_RGBA_MULT)

        swept_deg = (now - self._opened_at) / self.sweep_s * 360.0
        if swept_deg >= 360.0:
            return
        start_deg = beam - swept_deg
        center = (self.cx - rect.x, self.cy - rect.y)     # centro do escopo, na imagem
        points = [center]
        for step_deg in [*range(0, int(swept_deg), REVEAL_STEP_DEG), swept_deg]:
            angle = radians(start_deg + step_deg)
            points.append((center[0] + reveal_radius * sin(angle),
                           center[1] - reveal_radius * cos(angle)))
        reveal_mask = pygame.Surface(image.get_size(), pygame.SRCALPHA)
        if len(points) >= 3:
            pygame.draw.polygon(reveal_mask, (255, 255, 255, 255), points)
        image.blit(reveal_mask, (0, 0), special_flags=pygame.BLEND_RGBA_MULT)

    def _draw_scale_note(self, card: Card, to_scale: bool, px_per_m: float) -> None:
        """
        Barra de 10 m sob a silhueta, ou o aviso "FORA DE ESCALA".
        """
        y = self.cy + self.r_px - SCALE_BAR_UP_PX
        if not to_scale:
            if card.sil_generic:
                note = "FORA DE ESCALA (desenho genérico)"
            else:
                note = "FORA DE ESCALA (envergadura desconhecida)"
            text = self.font_small.render(note, True, DIM)
            self.screen.blit(text, text.get_rect(midtop=(self.cx, y)))
            return
        bar_px = SCALE_BAR_M * px_per_m
        left_x = self.cx - bar_px / 2
        pygame.draw.line(self.screen, RING_TXT, (left_x, y), (left_x + bar_px, y), 2)
        for end_x in (left_x, left_x + bar_px):
            pygame.draw.line(self.screen, RING_TXT, (end_x, y - 4), (end_x, y + 4), 2)
        text = self.font_small.render(f"{SCALE_BAR_M} m", True, RING_TXT)
        self.screen.blit(text, text.get_rect(midtop=(self.cx, y + 6)))
