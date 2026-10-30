"""
Desenho da tela: escopo de radar à esquerda, painel de informações à direita.

Visão geral
-----------
A tela tem duas regiões:

* **Escopo** (quadrado à esquerda, lado = altura da tela): mapa, eco de chuva,
  anéis de distância, feixe de varredura, aeronaves e rótulos.

* **Painel** (o resto, à direita): desenhado por ``panel.Panel``.

Com a ficha aberta, a silhueta da aeronave substitui o tráfego no escopo
(``card.CardView``). Cores e fonte vêm de ``palette``; os símbolos de carta do
mapa, de ``mapsymbols``.

Estratégia de desempenho (alvo: Raspberry Pi 3 B+, ~10x mais lento que um PC)
-----------------------------------------------------------------------------
Mesma estratégia do firmware ESP32: tudo o que é estático vai para superfícies
em cache e só é refeito quando muda.

1. ``_build_map``     fundo + mapa (opaco)           -> refeito ao trocar o alcance
2. ``_build_overlay`` cruz, anéis, rótulos (alfa)    -> refeito ao trocar o alcance
3. ``_compose_base``  mapa + eco de chuva + overlay  -> refeito ao trocar o alcance
                                                         ou o quadro de eco mostrado
4. A cada quadro: blit opaco da base + feixe girado + aeronaves + rótulos + painel.

Outros caches importantes (não remova sem medir no Pi):

* o feixe (``_build_sweep``) é uma cunha calculada uma vez com numpy,
  recortada no seu retângulo útil e só girada por quadro (``transform.rotate``);
* os rastros são desenhados numa camada própria (``_trail_layer``), refeita no
  máximo uma vez por TRAIL_REDRAW_S, e só blitada nos outros quadros;
* a posição dos rótulos (declutter) só é recalculada com dado novo; entre
  leituras o rótulo acompanha o alvo com o mesmo deslocamento;
* o texto dos rótulos é renderizado uma vez por (texto, cor);
* a silhueta da ficha é reescalada uma vez por (silhueta, tamanho) (ver card.py).

Fluxo de um quadro: ``app.py`` chama ``Renderer.draw`` e, num toque,
``Renderer.hit_test`` para saber o que foi tocado.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from math import atan2, cos, degrees, radians, sin

import numpy as np
import pygame

from radClock.core.acdb import AircraftDB
from radClock.core.alarm import AlarmClock
from radClock.core.config import Config
from radClock.core.echo import EchoFrame, pick_frame
from radClock.core.fetcher import Snapshot
from radClock.core.maplayer import MapLayer
from radClock.core.model import Aircraft
from radClock.core.options import SYMBOL_SQUARE, DisplayOptions
from radClock.core.projection import AzimuthalEquidistant
from radClock.core.tracks import Tracker, extrapolate
from radClock.core.weather import Weather
from radClock.ui.card import CardView
from radClock.ui.declutter import Label, leader_end, place
from radClock.ui.echoview import EchoSurfaces
from radClock.ui.mapsymbols import draw_map_symbol
from radClock.ui.palette import (AC, AC_TYPE, BG, CONFLICT, CROSS, DIM, ECHO_TXT, EMERG, INHIBIT,
                                RING, RING_TXT, SELECTED, STALE, SWEEP, TRAIL, TXT, VECTOR, WARN,
                                WX_TXT,
                                Color, ui_font)
from radClock.ui.panel import Panel

log = logging.getLogger(__name__)

# cor usada como "transparente" na máscara que recorta o mapa no círculo
_MASK_KEY_COLOR = (255, 0, 255)

# ------------------------------------------------------------ feixe/brilho
SWEEP_TAIL_DEG = 60     # rastro luminoso atrás da linha do feixe
SWEEP_ALPHA = 70        # opacidade máxima do rastro, junto à linha
GLOW_MIN = 0.35         # brilho do alvo logo antes de o feixe voltar

# ------------------------------------------------------------------- rastros
TRAIL_REDRAW_S = 1.0    # a camada de rastros é refeita no máximo 1x/s (o dado chega a cada ~1 s)
TRAIL_SHADES = 16       # tons do rastro, do recente (TRAIL) ao velho (quase BG)

# ícone do modo RADAR: avião visto de cima, nariz para o norte, em px. Asas e
# empenagem largas deixam claro onde é o nariz (a seta antiga era ambígua).
PLANE = [(0, -8), (1.2, -6), (1.2, -2), (8, 2), (8, 3.5), (1.2, 1.5), (1, 5.5),
         (3.5, 7.5), (3.5, 8.5), (0, 7.5), (-3.5, 8.5), (-3.5, 7.5), (-1, 5.5),
         (-1.2, 1.5), (-8, 3.5), (-8, 2), (-1.2, -2), (-1.2, -6)]

# ------------------------------------------------------------ alvos/toque
HIT_RADIUS_PX = 24          # dedo na tela de 7" é impreciso; ~4 mm
MIN_VECTOR_KT = 40          # abaixo disso (helicóptero pairando, táxi) sem vetor
VRATE_TREND_FPM = 300       # razão de subida/descida que ganha "^" ou "v" no rótulo
SQUARE_SYMBOL_HALF_PX = 4   # símbolo quadrado de 7 px (obstáculo p/ rótulos)
PLANE_SYMBOL_HALF_PX = 9    # símbolo de avião (~17 px)
HEADING_TICK_PX = 10        # traço de rumo de quem não tem vetor de velocidade
SELECTION_RING_PX = 13      # raio do círculo em volta do selecionado
LEADER_GAP_PX = 9           # a linha guia do rótulo começa fora do ícone
TEXT_CACHE_MAX = 3000       # limite do cache de textos de rótulo (esvazia ao passar)
LABELS_LOG_S = 60.0         # alvos sem rótulo: no máximo uma linha de log por minuto


# ============================================================ funções puras
def sweep_angle(now: float, period_s: float) -> float:
    """Azimute do feixe no instante `now`.

    Parâmetros:
        now: instante (s, relógio monotônico).
        period_s: tempo de uma volta completa do feixe.

    Retorno:
        Graus, sentido horário a partir do norte, em [0, 360).
    """
    return (now % period_s) / period_s * 360.0


def glow(bearing: float, beam: float) -> float:
    """Brilho de um alvo conforme o tempo desde que o feixe passou por ele.

    Parâmetros:
        bearing: azimute do alvo (graus).
        beam: azimute atual do feixe (graus).

    Retorno:
        1.0 logo após o feixe passar, caindo linearmente até GLOW_MIN quando o
        feixe está prestes a voltar.
    """
    degrees_since_beam = (beam - bearing) % 360.0
    return 1.0 - (1.0 - GLOW_MIN) * degrees_since_beam / 360.0


def _mix_color(start: Color, end: Color, fraction: float) -> Color:
    """Cor intermediária: fraction 0 = `start`, 1 = `end`."""
    return tuple(int(a + (b - a) * fraction) for a, b in zip(start, end))


@dataclass
class Hit:
    """Resultado de `Renderer.hit_test`.

    kind: "aircraft" (value = hex da aeronave), "button" (value = nome do
    botão: range_down, range_up, echo, settings, card, clock, source) ou "none".
    """
    kind: str
    value: str = ""


@dataclass
class _ScreenTarget:
    """Uma aeronave visível neste quadro, já na posição de tela (extrapolada)."""
    aircraft: Aircraft
    x: float
    y: float
    age_s: float                                    # idade da posição
    label_lines: list[pygame.Surface] = field(default_factory=list)


class Renderer:
    """Desenha um quadro completo (escopo + painel) e responde aos toques.

    Uso (ver app.py):
        renderer = Renderer(screen, cfg, proj, layers, tracker, acdb, alarms, echo_available)
        renderer.draw(snapshot, selected_hex, range_km, now, ...)   # a cada quadro
        hit = renderer.hit_test(touch_position)                     # a cada toque

    Atributos públicos usados fora daqui:
        side, r_px, cx, cy: tamanho do escopo, raio útil e centro (px).
        panel: retângulo do painel (o desenho é do ``info_panel``).
        buttons: retângulos dos botões fixos do painel, por nome.
        options: ajustes de exibição do último draw() (ver options.py).
        labels_hidden: alvos sem rótulo no último quadro (sem lugar livre; vai
            para o log com -v, no máximo uma vez por minuto).
    """

    def __init__(self, screen: pygame.Surface, cfg: Config,
                 proj: AzimuthalEquidistant, layers: list[MapLayer],
                 tracker: Tracker, acdb: AircraftDB, clock: AlarmClock,
                 echo_available: bool = False):
        """Calcula a geometria da tela e pré-renderiza o feixe.

        Parâmetros:
            screen: superfície da tela.
            cfg: configuração completa.
            proj: projeção centrada na estação.
            layers: camadas de mapa já projetadas.
            tracker: rastros e conflitos (lido a cada quadro).
            acdb: base offline de aeronaves (ficha).
            clock: despertador (hora local, próximo alarme).
            echo_available: se há eco de chuva (mostra o botão CHUVA).
        """
        self.screen = screen
        self.cfg = cfg
        self.proj = proj
        self.layers = layers
        self.tracker = tracker
        self.clock = clock
        self.echo_available = echo_available

        # geometria: escopo quadrado à esquerda, painel no resto
        width, height = screen.get_size()
        self.side = min(width, height)
        self.cx = self.side // 2
        self.cy = self.side // 2
        self.r_px = self.side // 2 - 4
        self.panel = pygame.Rect(self.side, 0, width - self.side, height)

        self.font_medium = ui_font(15)
        self.font_small = ui_font(12)

        # estado do último draw()
        self.options = DisplayOptions()
        self.labels_hidden = 0

        # caches da base do escopo (ver docstring do módulo)
        self._layers_range: float | None = None             # alcance de _map/_overlay
        self._map: pygame.Surface | None = None             # fundo + mapa (opaco)
        self._overlay: pygame.Surface | None = None         # anéis/rótulos (transparente)
        self._base: pygame.Surface | None = None            # composição final
        self._base_key: tuple | None = None                 # (alcance, versão do eco)
        self._echo_surfaces = EchoSurfaces()                # quadro de eco -> Surface

        # feixe de varredura (None = desligado em [radar] sweep_s)
        self._sweep: pygame.Surface | None = None
        self._glow_mask: pygame.Surface | None = None
        self._sweep_pivot = pygame.math.Vector2()           # centro do escopo -> centro da cunha
        if cfg.radar.sweep_s > 0:
            self._sweep = self._build_sweep()
            self._glow_mask = self._build_glow_mask()

        # rastros: camada transparente (colorkey) refeita só de tempos em tempos
        self._trail_layer = pygame.Surface((self.side, self.side))
        self._trail_layer.set_colorkey(_MASK_KEY_COLOR)
        self._trail_key: tuple | None = None                # (alcance, rastro, alvos)
        self._trail_drawn_at = float("-inf")
        self._trail_shades = [_mix_color(BG, TRAIL, (level + 1) / TRAIL_SHADES)
                              for level in range(TRAIL_SHADES)]

        # painel da direita e ficha da aeronave (cada um com seu estado e caches)
        self.info_panel = Panel(screen, self.panel, cfg, proj, tracker, clock, echo_available)
        self.buttons = self.info_panel.buttons
        self.card_view = CardView(screen, acdb, self.cx, self.cy, self.r_px, cfg.radar.sweep_s)

        # alvos tocáveis do último quadro: (hex, x, y)
        self._touch_targets: list[tuple[str, float, float]] = []
        self._targets_shown = 0                             # com a ficha aberta, mantém o último

        # clima: texto só é renderizado de novo quando muda
        # (_wx_surfs é lido por tests/test_weather.py)
        self._weather_text_key: tuple[tuple[str, ...], bool] | None = None
        self._wx_surfs: list[pygame.Surface] = []

        # rótulos das aeronaves: só reposicionados com dado novo
        self._label_slot: dict[str, int] = {}               # posição no quadro anterior
        self._placed_key: tuple | None = None
        self._placed_labels: dict = {}
        self._label_offsets: dict[str, tuple[float, float]] = {}   # rótulo em relação ao alvo
        self._text_cache: dict[tuple, pygame.Surface] = {}
        self._labels_logged_at = float("-inf")              # último log de rótulos ocultos

    @property
    def _scope_rect(self) -> pygame.Rect:
        return pygame.Rect(0, 0, self.side, self.side)

    # ================================================================ API pública
    def draw(self, snap: Snapshot, selected: str | None, rng: float, now: float,
             echo_on: bool = False, echo: list[EchoFrame] | None = None, echo_err: str = "",
             card: bool = False, options: DisplayOptions = DisplayOptions(),
             weather: Weather | None = None) -> None:
        """Desenha um quadro completo na tela (sem o flip).

        Fluxo:
            1. decide se a ficha está aberta e busca seus dados na base;
            2. escolhe o quadro de eco da animação e blita a base em cache;
            3. feixe, clima;
            4. ficha (silhueta) ou aeronaves + rótulos;
            5. painel.

        Parâmetros:
            snap: leitura mais recente da fonte de tráfego.
            selected: hex da aeronave selecionada, ou None.
            rng: alcance (raio do escopo) em km.
            now: instante atual (relógio monotônico).
            echo_on: se o eco de chuva está ligado.
            echo: quadros de eco disponíveis, do mais velho ao mais novo.
            echo_err: erro do eco de chuva para o painel ("" = sem erro).
            card: mostrar a ficha do selecionado (silhueta no escopo, dados no painel).
            options: ajustes de exibição (símbolo, vetor, rastro, alerta, feixe, zonas).
            weather: clima atual, ou None para não mostrar.
        """
        self.options = options
        selected_aircraft = snap.find(selected)
        card_info = self.card_view.update(selected_aircraft, card, now)

        echo_frames = echo or []
        shown_frame = None
        frames_for_range: list[EchoFrame] = []
        if echo_on:
            # animação: um quadro por vez, só os reprojetados para este alcance
            frames_for_range = [f for f in echo_frames if f.range_km == rng]
            shown_frame = pick_frame(frames_for_range, now, self.cfg.echo.frame_s,
                                     self.cfg.echo.hold_s)
        self._echo_surfaces.keep_only(frames_for_range)
        wall_now = time.time()

        self.screen.blit(self._compose_base(rng, shown_frame), (0, 0))
        beam = self._beam(now)
        if beam is not None:
            self._draw_sweep(beam)
        self._draw_weather(weather, wall_now)

        if card_info is not None:
            self.card_view.draw(card_info, now, beam, self._glow_mask)
            self._touch_targets = []            # tocar no escopo fecha a ficha
        else:
            self._touch_targets = self._draw_aircraft(snap, selected, rng, now)
            self._targets_shown = len(self._touch_targets)

        # créditos das fontes em uso (termos da RainViewer e CC BY do Open-Meteo)
        credits = []
        if weather is not None:
            credits.append(weather.credit)
        if shown_frame is not None and shown_frame.range_km == rng:
            credits.append("RainViewer")
        self.info_panel.draw(snap, selected_aircraft, rng, now, wall_now, echo_on, echo_frames,
                             echo_err, card_info, options, self._targets_shown, credits)

    def hit_test(self, pos: tuple[int, int]) -> Hit:
        """Diz o que está sob o toque `pos` (px), conforme o último quadro desenhado.

        Ordem: botões do painel (fixos, linha da fonte, relógio, ficha) e, por
        fim, a aeronave mais próxima dentro de HIT_RADIUS_PX.

        Retorno:
            Hit("button", nome), Hit("aircraft", hex) ou Hit("none").
        """
        button = self.info_panel.hit_test(pos)
        if button is not None:
            return Hit("button", button)

        nearest_hex = None
        nearest_dist_sq = HIT_RADIUS_PX ** 2
        for hex_id, x, y in self._touch_targets:
            dist_sq = (x - pos[0]) ** 2 + (y - pos[1]) ** 2
            if dist_sq <= nearest_dist_sq:
                nearest_hex = hex_id
                nearest_dist_sq = dist_sq
        if nearest_hex:
            return Hit("aircraft", nearest_hex)
        return Hit("none")

    # ===================================================== base do escopo (cache)
    def _compose_base(self, rng: float, echo: EchoFrame | None) -> pygame.Surface:
        """Devolve a base do escopo (mapa + eco + anéis), refazendo só o que mudou.

        Parâmetros:
            rng: alcance em km.
            echo: quadro de eco a mostrar, ou None.

        Retorno:
            Superfície side x side pronta para blit.
        """
        if self._layers_range != rng:
            self._map = self._build_map(rng)
            self._overlay = self._build_overlay(rng)
            self._layers_range = rng

        # eco reprojetado para outro alcance (troca em andamento) não é mostrado
        if echo is not None and echo.range_km != rng:
            echo = None

        echo_version = echo.version if echo is not None else None
        key = (rng, echo_version)
        if key == self._base_key:
            return self._base

        base = self._map.copy()
        if echo is not None:
            base.blit(self._echo_surfaces.surface(echo), (0, 0))
        base.blit(self._overlay, (0, 0))
        self._base = base
        self._base_key = key
        return base

    def _build_map(self, rng: float) -> pygame.Surface:
        """Fundo + linhas e pontos do mapa, recortados no círculo do escopo.

        Só roda ao trocar o alcance (o resultado fica em cache).
        """
        surf = pygame.Surface((self.side, self.side))
        surf.fill(BG)
        px_per_km = self.r_px / rng
        # 5 % de folga: um segmento que sai do círculo ainda é desenhado até a borda
        limit_sq = (rng * 1.05) ** 2

        for layer in self.layers:
            if not layer.visible(rng):          # pistas, eixos: só nos alcances curtos
                continue
            for line in layer.lines_km:
                self._draw_map_line(surf, line, layer.color, px_per_km, limit_sq)

        self._draw_map_points(surf, rng, px_per_km)
        self._clip_to_scope_circle(surf)
        return surf

    def _draw_map_line(self, surf: pygame.Surface, line: list[tuple[float, float]],
                       color: Color, px_per_km: float, limit_sq: float) -> None:
        """Desenha uma linha do mapa, só os segmentos com uma ponta dentro do alcance.

        Os segmentos visíveis consecutivos formam uma "corrida" desenhada com uma
        única chamada a draw.lines (bem mais rápido que um draw.line por segmento).

        Parâmetros:
            line: pontos (leste, norte) em km a partir da estação.
            limit_sq: quadrado da distância máxima (km²) para um ponto contar como dentro.
        """
        run: list[tuple[float, float]] = []
        previous_inside = False
        previous_point = None
        for east_km, north_km in line:
            inside = east_km * east_km + north_km * north_km <= limit_sq
            point = (self.cx + east_km * px_per_km, self.cy - north_km * px_per_km)
            segment_visible = previous_point is not None and (inside or previous_inside)
            if segment_visible:
                if not run:
                    run.append(previous_point)
                run.append(point)
            else:
                if len(run) >= 2:
                    pygame.draw.lines(surf, color, False, run)
                run = []
            previous_inside = inside
            previous_point = point
        if len(run) >= 2:
            pygame.draw.lines(surf, color, False, run)

    def _draw_map_points(self, surf: pygame.Surface, rng: float, px_per_km: float) -> None:
        """Aeroportos, navaids e fixos: símbolo + rótulo sem sobreposição.

        Roda só ao trocar o alcance (vai para a superfície do mapa), então o
        declutter aqui não pesa. Os rótulos escolhem lugar na ordem das
        camadas: aeroportos antes de navaids, na saída do make_map.py.
        """
        symbols: list[pygame.Rect] = []
        labels: list[tuple[Label, pygame.Surface]] = []
        for layer in self.layers:
            if not layer.visible(rng) or not layer.points:
                continue
            show_labels = layer.labels_visible(rng)
            for index, point in enumerate(layer.points):
                if point.e * point.e + point.n * point.n > rng * rng:
                    continue
                x = round(self.cx + point.e * px_per_km)
                y = round(self.cy - point.n * px_per_km)
                symbols.append(draw_map_symbol(surf, point.kind, x, y, layer.color, point.size))
                if show_labels and point.label:
                    text = self.font_small.render(point.label, True, layer.color)
                    label = Label(f"{id(layer)}:{index}", x, y, text.get_width(),
                                  text.get_height())
                    labels.append((label, text))

        placed = place([label for label, _ in labels], symbols, self._scope_rect,
                       leaders=False, fits=self._rect_inside_scope)
        for label, text in labels:
            placement = placed.get(label.key)
            if placement is not None:
                surf.blit(text, placement.rect.topleft)

    def _rect_inside_scope(self, rect: pygame.Rect) -> bool:
        """True se os 4 cantos do retângulo estão dentro do círculo.

        Um rótulo de mapa cortado pela borda do escopo não serve.
        """
        limit_sq = (self.r_px - 2) ** 2
        for x, y in (rect.topleft, rect.topright, rect.bottomleft, rect.bottomright):
            if (x - self.cx) ** 2 + (y - self.cy) ** 2 > limit_sq:
                return False
        return True

    def _clip_to_scope_circle(self, surf: pygame.Surface) -> None:
        """Pinta de BG tudo o que está fora do círculo do escopo."""
        mask = pygame.Surface((self.side, self.side))
        mask.fill(BG)
        pygame.draw.circle(mask, _MASK_KEY_COLOR, (self.cx, self.cy), self.r_px)
        mask.set_colorkey(_MASK_KEY_COLOR)      # o círculo vira "furo" na máscara
        surf.blit(mask, (0, 0))

    def _build_overlay(self, rng: float) -> pygame.Surface:
        """Cruz, 4 anéis com a distância, unidade "km" e pontos cardeais.

        Fica por cima do eco, como no firmware: chuva forte não apaga os anéis.
        """
        surf = pygame.Surface((self.side, self.side), pygame.SRCALPHA)
        cx = self.cx
        cy = self.cy
        pygame.draw.line(surf, CROSS, (0, cy), (self.side, cy))
        pygame.draw.line(surf, CROSS, (cx, 0), (cx, self.side))

        # rótulo de cada anel a 45° (NE), longe dos eixos e dos pontos cardeais
        label_angle = radians(45)
        for ring in (1, 2, 3, 4):
            radius = self.r_px * ring // 4
            pygame.draw.circle(surf, RING, (cx, cy), radius, 1)
            text = self.font_small.render(f"{rng * ring / 4:g}", True, RING_TXT)
            x = cx + radius * sin(label_angle) + 2
            y = cy - radius * cos(label_angle) - text.get_height()
            surf.blit(text, (x, y))

        unit = self.font_small.render("km", True, RING_TXT)
        surf.blit(unit, (self.side - unit.get_width() - 6, self.side - unit.get_height() - 4))

        cardinals = (("N", 0, -1), ("S", 0, 1), ("E", 1, 0), ("W", -1, 0))
        inset = self.r_px - 12
        for letter, dir_x, dir_y in cardinals:
            text = self.font_medium.render(letter, True, RING_TXT)
            x = cx + dir_x * inset - text.get_width() / 2
            y = cy + dir_y * inset - text.get_height() / 2
            # desloca a letra para fora da linha da cruz, que a cortaria ao meio
            if dir_x == 0:
                x += 6
            else:
                y += 6
            surf.blit(text, (x, y))
        return surf

    # ============================================================== feixe
    def _pixel_offsets(self) -> tuple[np.ndarray, np.ndarray]:
        """Distância (px) do centro de cada pixel do escopo até o centro do escopo.

        Retorno:
            (dx, dy) em matrizes [x, y] (é como o surfarray indexa); dy cresce para o norte.
        """
        pixel_centers = np.arange(self.side) + 0.5
        dx = pixel_centers[:, None] - self.cx
        dy = self.cy - pixel_centers[None, :]
        return dx, dy

    @staticmethod
    def _degrees_behind_north(dx: np.ndarray, dy: np.ndarray) -> np.ndarray:
        """Para cada pixel, quantos graus o feixe (agora no norte) já andou desde ele."""
        return np.degrees(-np.arctan2(dx, dy)) % 360.0

    def _build_sweep(self) -> pygame.Surface:
        """Cunha do feixe apontando para o norte (calculada uma vez com numpy).

        A cada quadro ela só é girada: calcular a cunha por quadro seria caro
        demais no Pi. A superfície é recortada no retângulo que contém a cunha
        e o centro do escopo (cerca de 1/4 do escopo com SWEEP_TAIL_DEG = 60):
        girar o escopo inteiro custava ~19 ms por quadro no Pi 3 B+, quase tudo
        em pixels transparentes. `_sweep_pivot` guarda onde fica o centro do
        escopo em relação ao centro do recorte, para `_draw_sweep` girar em
        torno dele.
        """
        surf = pygame.Surface((self.side, self.side), pygame.SRCALPHA)
        surf.fill((*SWEEP, 0))
        dx, dy = self._pixel_offsets()
        behind = self._degrees_behind_north(dx, dy)
        intensity = np.clip(1.0 - behind / SWEEP_TAIL_DEG, 0.0, 1.0)
        intensity[dx * dx + dy * dy > self.r_px * self.r_px] = 0.0
        alpha = pygame.surfarray.pixels_alpha(surf)
        alpha[:] = (SWEEP_ALPHA * intensity * intensity).astype(np.uint8)   # ²: some mais rápido
        del alpha                               # libera o lock da superfície

        crop = surf.get_bounding_rect().union(pygame.Rect(self.cx - 1, self.cy - 1, 2, 2))
        self._sweep_pivot = pygame.math.Vector2(self.cx - crop.centerx, self.cy - crop.centery)
        return surf.subsurface(crop).copy()

    def _build_glow_mask(self) -> pygame.Surface:
        """Máscara com alfa = glow() para o feixe no norte.

        Girada junto com o feixe, multiplica a silhueta da ficha para ela
        acender e apagar como os alvos.
        """
        surf = pygame.Surface((self.side, self.side), pygame.SRCALPHA)
        surf.fill((255, 255, 255, 0))
        behind = self._degrees_behind_north(*self._pixel_offsets())
        alpha = pygame.surfarray.pixels_alpha(surf)
        alpha[:] = (255 * (1.0 - (1.0 - GLOW_MIN) * behind / 360.0)).astype(np.uint8)
        del alpha                               # libera o lock da superfície
        return surf

    def _draw_sweep(self, beam: float) -> None:
        """Gira a cunha pré-renderizada até `beam` (graus) e desenha a linha do feixe.

        rotate gira em torno do centro do recorte, que não é o centro do
        escopo: o centro do recorte é então posto no centro do escopo menos o
        `_sweep_pivot` girado do mesmo ângulo (Vector2.rotate, com y para
        baixo, gira no sentido horário como o feixe).
        """
        rotated = pygame.transform.rotate(self._sweep, -beam)   # rotate é anti-horário
        crop_center = pygame.math.Vector2(self.cx, self.cy) - self._sweep_pivot.rotate(beam)
        self.screen.blit(rotated, rotated.get_rect(center=(round(crop_center.x),
                                                           round(crop_center.y))))
        angle = radians(beam)
        tip = (self.cx + self.r_px * sin(angle), self.cy - self.r_px * cos(angle))
        pygame.draw.line(self.screen, SWEEP, (self.cx, self.cy), tip, 2)

    # ============================================================ aeronaves
    def _color(self, ac: Aircraft, age: float, selected: bool) -> Color:
        """Cor do alvo. Prioridade: emergência > conflito (com alerta) > selecionado > velho."""
        if ac.emergency:
            return EMERG
        if self.options.show_conflicts and ac.hex in self.tracker.conflicts:
            return CONFLICT
        if selected:
            return SELECTED
        if age > self.cfg.radar.stale_s:
            return STALE
        return AC

    def _draw_aircraft(self, snap: Snapshot, selected: str | None, rng: float,
                       now: float) -> list[tuple[str, float, float]]:
        """Desenha todas as aeronaves visíveis e seus rótulos.

        Fluxo:
            1. calcula a posição de tela de cada alvo (extrapolada);
            2. desenha embaixo zonas de inibição, rastros e linhas de conflito
               (cada um conforme o seu ajuste);
            3. desenha o símbolo de cada alvo;
            4. posiciona e desenha os rótulos por cima de tudo.

        Retorno:
            (hex, x, y) de cada alvo desenhado, para o hit_test.
        """
        px_per_km = self.r_px / rng
        targets = self._visible_targets(snap, selected, rng, now, px_per_km)
        self._draw_underlay(targets, px_per_km, now)

        beam = self._beam(now)
        symbol_rects = []
        for target in targets:
            symbol_rects.append(self._draw_target(target, selected, px_per_km, beam))

        conflicts = self._visible_conflicts()
        must_show = set()
        for target in targets:
            aircraft = target.aircraft
            if aircraft.hex == selected or aircraft.emergency or aircraft.hex in conflicts:
                must_show.add(aircraft.hex)
        # o último da lista foi desenhado por cima e é o mais importante:
        # na lista invertida ele escolhe o lugar do rótulo primeiro
        self._draw_labels(targets[::-1], symbol_rects, must_show,
                          (snap.updated, rng, self.options, selected))
        return [(target.aircraft.hex, target.x, target.y) for target in targets]

    def _visible_targets(self, snap: Snapshot, selected: str | None, rng: float, now: float,
                         px_per_km: float) -> list[_ScreenTarget]:
        """Aeronaves a desenhar, já na posição de tela, em ordem de desenho.

        Ficam de fora: no solo (salvo show_ground), sem posição há mais de
        drop_s e fora do alcance. Emergência, conflito e selecionado vão para o
        fim da lista, para ficarem por cima.
        """
        radar_cfg = self.cfg.radar
        snapshot_age = 0.0
        if snap.updated:
            snapshot_age = now - snap.updated
        conflicts = self._visible_conflicts()

        def drawing_priority(aircraft: Aircraft) -> tuple[bool, bool, bool]:
            return (aircraft.hex == selected, aircraft.hex in conflicts,
                    aircraft.emergency is not None)

        targets = []
        for aircraft in sorted(snap.aircraft, key=drawing_priority):
            if aircraft.on_ground and not radar_cfg.show_ground:
                continue
            age = aircraft.seen_pos_s + snapshot_age
            if age > radar_cfg.drop_s:
                continue
            # posição estimada agora (a leitura tem `age` segundos)
            received_east, received_north = self.proj.forward(aircraft.lat, aircraft.lon)
            east_km, north_km = extrapolate(aircraft, received_east, received_north, age,
                                            radar_cfg.extrapolate_s)
            if east_km * east_km + north_km * north_km > rng * rng:
                continue
            x = self.cx + east_km * px_per_km
            y = self.cy - north_km * px_per_km
            targets.append(_ScreenTarget(aircraft, x, y, age))
        return targets

    def _beam(self, now: float) -> float | None:
        """Azimute atual do feixe, ou None sem feixe ([radar] sweep_s = 0 ou
        o ajuste FEIXE desligado). Sem feixe, os alvos não apagam e brilham."""
        if self._sweep is None or not self.options.sweep:
            return None
        return sweep_angle(now, self.cfg.radar.sweep_s)

    def _visible_conflicts(self) -> dict:
        """Conflitos a mostrar: os do Tracker, ou nenhum com o ALERTA desligado."""
        if self.options.show_conflicts:
            return self.tracker.conflicts
        return {}

    def _draw_underlay(self, targets: list[_ScreenTarget], px_per_km: float,
                       now: float) -> None:
        """Embaixo dos alvos: zonas de inibição, rastros e linhas de conflito,
        cada um só se o seu ajuste estiver ligado."""
        options = self.options
        if options.zones:
            self.screen.set_clip(self._scope_rect)
            for zone in self.tracker.zones:
                center = (self.cx + zone.e * px_per_km, self.cy - zone.n * px_per_km)
                pygame.draw.circle(self.screen, INHIBIT, center,
                                   max(2, zone.r_km * px_per_km), 1)
            self.screen.set_clip(None)

        if options.trail_s > 0:
            self._draw_trails(targets, px_per_km, now)

        if not options.show_conflicts:
            return
        position_by_hex = {target.aircraft.hex: (target.x, target.y) for target in targets}
        for hex_a, hex_b in self.tracker.pairs:
            if hex_a in position_by_hex and hex_b in position_by_hex:
                pygame.draw.line(self.screen, CONFLICT, position_by_hex[hex_a],
                                 position_by_hex[hex_b])

    def _draw_trails(self, targets: list[_ScreenTarget], px_per_km: float,
                     now: float) -> None:
        """Blita a camada de rastros, refazendo-a se preciso.

        Os pontos do rastro só mudam com um snapshot novo (~1 s) e o
        esmaecimento é lento, mas desenhar ~30 pontos por alvo a cada quadro
        custava ~16 ms no Pi 3 B+. Por isso a camada é refeita só quando muda
        o alcance, o ajuste RASTRO ou o conjunto de alvos visíveis, ou quando
        passou TRAIL_REDRAW_S desde a última vez; nos outros quadros é só um
        blit com colorkey. Não use RLEACCEL nesta camada: o SDL recodifica o
        RLE da superfície inteira a cada draw.rect, e o quadro foi de 15 para
        80 ms no Pi.
        """
        key = (px_per_km, self.options.trail_s,
               tuple(target.aircraft.hex for target in targets))
        if key != self._trail_key or now - self._trail_drawn_at >= TRAIL_REDRAW_S:
            self._trail_layer.fill(_MASK_KEY_COLOR)
            for target in targets:
                self._draw_trail(self._trail_layer, target.aircraft.hex, px_per_km, now)
            self._trail_key = key
            self._trail_drawn_at = now
        self.screen.blit(self._trail_layer, (0, 0))

    def _draw_trail(self, surface: pygame.Surface, hex_id: str, px_per_km: float,
                    now: float) -> None:
        """Pontos do rastro dos últimos `options.trail_s` segundos em `surface`,
        do mais recente (cor TRAIL) ao mais velho (some no fundo).

        O Tracker guarda mais que isso (o maior rastro que a tela oferece);
        os pontos mais velhos que o ajuste são pulados. A cor vem da tabela
        `_trail_shades` (TRAIL_SHADES tons), em vez de misturar cores por ponto.
        """
        trail = self.tracker.trails.get(hex_id)
        if not trail:
            return
        trail_span_s = self.options.trail_s
        for timestamp, east_km, north_km in trail:
            if now - timestamp > trail_span_s:
                continue
            freshness = 1.0 - (now - timestamp) / trail_span_s   # 1 = recente
            shade = min(int(freshness * TRAIL_SHADES), TRAIL_SHADES - 1)
            x = self.cx + east_km * px_per_km - 1
            y = self.cy - north_km * px_per_km - 1
            pygame.draw.rect(surface, self._trail_shades[shade], (x, y, 2, 2))

    def _draw_target(self, target: _ScreenTarget, selected: str | None, px_per_km: float,
                     beam: float | None) -> pygame.Rect:
        """Desenha o símbolo de um alvo e prepara as linhas do seu rótulo.

        Parâmetros:
            target: alvo na posição de tela (recebe `label_lines`).
            beam: azimute do feixe, ou None sem feixe.

        Retorno:
            Área do símbolo (os rótulos a evitam).
        """
        aircraft = target.aircraft
        x = target.x
        y = target.y
        is_selected = aircraft.hex == selected
        is_stale = target.age_s > self.cfg.radar.stale_s
        color = self._color(aircraft, target.age_s, is_selected)

        # o símbolo apaga depois que o feixe passa; emergência, conflito e
        # selecionado não apagam (alerta sempre visível)
        symbol_color = color
        if beam is not None and color in (AC, STALE):
            bearing = degrees(atan2(x - self.cx, self.cy - y)) % 360.0
            symbol_color = _mix_color(BG, color, glow(bearing, beam))

        has_vector = False
        if self.options.vector_min > 0:
            has_vector = self._draw_speed_vector(aircraft, x, y, px_per_km, is_stale)
        if self.options.symbol == SYMBOL_SQUARE:
            self._draw_symbol(x, y, aircraft.track, symbol_color, tick=not has_vector)
            half = SQUARE_SYMBOL_HALF_PX
        else:
            self._draw_plane(x, y, aircraft.track, symbol_color)
            half = PLANE_SYMBOL_HALF_PX
        if is_selected:
            pygame.draw.circle(self.screen, SELECTED, (x, y), SELECTION_RING_PX, 1)

        target.label_lines = self._label_lines(aircraft, color, is_stale)
        return pygame.Rect(round(x) - half, round(y) - half, 2 * half, 2 * half)

    def _draw_speed_vector(self, aircraft: Aircraft, x: float, y: float, px_per_km: float,
                           is_stale: bool) -> bool:
        """Vetor de velocidade: linha até onde o alvo estará daqui a
        `options.vector_min` minutos.

        Retorno:
            True se desenhou o vetor (posição recente, rumo e velocidade ≥ MIN_VECTOR_KT).
        """
        if is_stale or aircraft.track is None or aircraft.gs_kt is None:
            return False
        if aircraft.gs_kt < MIN_VECTOR_KT:
            return False
        km_per_min = aircraft.gs_kt * 1.852 / 60.0
        length_px = km_per_min * self.options.vector_min * px_per_km
        angle = radians(aircraft.track)
        end = (x + length_px * sin(angle), y - length_px * cos(angle))
        pygame.draw.line(self.screen, VECTOR, (x, y), end)
        return True

    def _draw_symbol(self, x: float, y: float, track: float | None,
                     color: Color, tick: bool) -> None:
        """Símbolo quadrado: quadrado cheio de 7 px (estilo tela de controle).

        O rumo normalmente vem do vetor de velocidade. Uma seta em 8 px é
        ambígua (as pontas da cauda parecem o nariz), então só quem não tem
        vetor (`tick` = True: lento ou posição velha) ganha um traço de rumo.
        """
        pygame.draw.rect(self.screen, color, (x - 3, y - 3, 7, 7))
        if tick and track is not None:
            angle = radians(track)
            end = (x + HEADING_TICK_PX * sin(angle), y - HEADING_TICK_PX * cos(angle))
            pygame.draw.line(self.screen, color, (x, y), end)

    def _draw_plane(self, x: float, y: float, track: float | None, color: Color) -> None:
        """Símbolo de avião: ícone PLANE girado para o rumo (círculo sem rumo)."""
        if track is None:
            pygame.draw.circle(self.screen, color, (x, y), 4)
            return
        angle = radians(track)
        cos_a = cos(angle)
        sin_a = sin(angle)
        points = [(x + px * cos_a - py * sin_a, y + px * sin_a + py * cos_a) for px, py in PLANE]
        pygame.draw.polygon(self.screen, color, points)
        pygame.draw.aalines(self.screen, color, True, points)    # borda suave

    # ============================================================== rótulos
    def _label_lines(self, ac: Aircraft, color: Color, dim: bool) -> list[pygame.Surface]:
        """Linhas do rótulo: identificação, tipo (se houver) e nível de voo com tendência.

        Parâmetros:
            color: cor do alvo (usada na linha da altitude).
            dim: posição velha (tudo em cinza).
        """
        name_color = STALE if dim else TXT
        type_color = STALE if dim else AC_TYPE
        lines: list[tuple[str, Color]] = [(ac.label, name_color)]
        if ac.type:
            lines.append((ac.type, type_color))
        if ac.alt_ft is not None:
            trend = ""
            if ac.vrate_fpm is not None and abs(ac.vrate_fpm) >= VRATE_TREND_FPM:
                if ac.vrate_fpm > 0:
                    trend = "^"
                else:
                    trend = "v"
            flight_level = ac.alt_ft // 100
            lines.append((f"{flight_level:03d}{trend}", color))
        return [self._small_text(text, line_color) for text, line_color in lines]

    def _small_text(self, text: str, color: Color) -> pygame.Surface:
        """Texto pequeno com cache por (texto, cor).

        Os rótulos mudam 1x/s e a tela é desenhada 15x/s: renderizar a fonte a
        cada quadro custava ~3 ms no PC (dezenas no Pi 3 B+) com 100 alvos.
        """
        key = (text, color)
        surface = self._text_cache.get(key)
        if surface is not None:
            return surface
        if len(self._text_cache) > TEXT_CACHE_MAX:
            self._text_cache.clear()
        surface = self.font_small.render(text, True, color)
        self._text_cache[key] = surface
        return surface

    def _draw_labels(self, targets: list[_ScreenTarget], symbols: list[pygame.Rect],
                     must: set[str], key: tuple) -> None:
        """Posiciona (se preciso) e desenha os rótulos.

        A posição só é recalculada quando `key` muda (snapshot, alcance, modo,
        seleção, quantidade). Entre leituras o alvo anda pela extrapolação e o
        rótulo vai junto, com o mesmo deslocamento: todos andam quase igual,
        então a folga entre eles se mantém até a próxima leitura.

        Parâmetros:
            targets: em ordem de prioridade (quem escolhe lugar primeiro).
            symbols: áreas dos símbolos (obstáculos).
            must: hex dos alvos cujo rótulo aparece mesmo sem lugar livre.
            key: chave do cache da posição.
        """
        line_step = self.font_small.get_linesize() - 2
        key = (*key, len(targets))
        if key != self._placed_key:
            self._place_labels(targets, symbols, must, line_step)
            self._placed_key = key

        self.labels_hidden = len(targets) - len(self._placed_labels)
        self._log_labels_hidden(len(targets))
        for target in targets:
            placement = self._placed_labels.get(target.aircraft.hex)
            if placement is None:
                continue                        # sem lugar livre: só o símbolo
            offset_x, offset_y = self._label_offsets[target.aircraft.hex]
            rect = pygame.Rect(round(target.x + offset_x), round(target.y + offset_y),
                               placement.rect.w, placement.rect.h)
            if placement.leader:
                self._draw_leader(target.x, target.y, rect)
            text_y = rect.y
            for line in target.label_lines:
                self.screen.blit(line, (rect.x, text_y))
                text_y += line_step

    def _log_labels_hidden(self, target_count: int) -> None:
        """Registra (com -v) quantos alvos ficaram sem rótulo, no máximo 1x por minuto.

        Antes isso aparecia no painel; é diagnóstico de tela cheia, não
        informação operacional, e a linha do painel fazia falta.
        """
        if not self.labels_hidden:
            return
        now = time.monotonic()
        if now - self._labels_logged_at < LABELS_LOG_S:
            return
        self._labels_logged_at = now
        log.debug("rótulos: %d de %d alvos sem lugar livre", self.labels_hidden, target_count)

    def _place_labels(self, targets: list[_ScreenTarget], symbols: list[pygame.Rect],
                      must: set[str], line_step: int) -> None:
        """Roda o declutter e guarda o deslocamento de cada rótulo em relação ao alvo."""
        labels = []
        for target in targets:
            width = max(line.get_width() for line in target.label_lines)
            height = line_step * len(target.label_lines) + 2
            labels.append(Label(target.aircraft.hex, target.x, target.y, width, height))

        self._placed_labels = place(labels, symbols, self._scope_rect, self._label_slot, must)
        self._label_slot = {hex_id: p.slot for hex_id, p in self._placed_labels.items()}
        self._label_offsets = {}
        for label in labels:
            placement = self._placed_labels.get(label.key)
            if placement is not None:
                self._label_offsets[label.key] = (placement.rect.x - label.x,
                                                  placement.rect.y - label.y)

    def _draw_leader(self, x: float, y: float, label_rect: pygame.Rect) -> None:
        """Linha guia do alvo até o rótulo afastado, começando fora do ícone."""
        end_x, end_y = leader_end(x, y, label_rect)
        length = max(1.0, ((end_x - x) ** 2 + (end_y - y) ** 2) ** 0.5)
        start_x = x + (end_x - x) * LEADER_GAP_PX / length
        start_y = y + (end_y - y) * LEADER_GAP_PX / length
        pygame.draw.line(self.screen, DIM, (start_x, start_y), (end_x, end_y))

    # ================================================================ clima
    def _draw_weather(self, wx: Weather | None, wall_now: float) -> None:
        """
        Clima no canto superior esquerdo do escopo, fora do anel externo.

        O canto do quadrado em volta do círculo nunca tem alvo (fica além do
        alcance), então o texto não disputa espaço com os rótulos. A primeira
        linha diz de onde vem o dado ("SBSJ há 25 min" ou "MODELO") e fica
        laranja com METAR velho. O crédito da fonte está no rodapé do painel.

        Parâmetros:
            wx: leitura atual, ou None para não mostrar.
            wall_now: time.time() deste quadro (idade do METAR).
        """
        if wx is None:
            return
        lines = tuple(wx.lines(wall_now))
        key = (lines, wx.is_old(wall_now))
        if key != self._weather_text_key:          # muda no máximo uma vez por minuto
            self._weather_text_key = key
            self._wx_surfs = []
            for index, line in enumerate(lines):
                color = WX_TXT
                if index == 0:
                    color = WARN if wx.is_old(wall_now) else ECHO_TXT
                self._wx_surfs.append(self.font_small.render(line, True, color))
        y = 4
        for surface in self._wx_surfs:
            self.screen.blit(surface, (6, y))
            y += surface.get_height()
