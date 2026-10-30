"""
Painel de informações, à direita do escopo.

De cima para baixo: relógio grande na largura toda (botão do despertador),
linha da fonte de dados com a contagem de alvos (botão que troca a fonte),
erro da fonte, conflitos e o aviso ALERTA DESL, detalhes ou ficha da
aeronave selecionada, os botões fixos ("-", alcance, "+", AJUSTES e CHUVA com
a idade da chuva) e o rodapé (créditos das fontes em uso, próximo alarme ou
aviso SEM NTP).

Altura limitada: em 800x480 os detalhes e a ficha têm de terminar acima das
linhas de botões. Ao acrescentar uma linha, confira com uma captura de tela
headless (ver CLAUDE.md).

Quem usa: ``render.Renderer`` cria um ``Panel``, chama ``draw`` a cada quadro
e pergunta a ``hit_test`` se um toque caiu num botão do painel.
"""
from __future__ import annotations

from datetime import datetime

import pygame

from radClock.core.acdb import Card, TypeInfo
from radClock.core.alarm import AlarmClock, clock_synced, next_alarm_text
from radClock.core.config import Config
from radClock.core.echo import ECHO_OLD_S, EchoFrame, age_text, frame_age_s
from radClock.core.fetcher import Snapshot
from radClock.core.model import Aircraft
from radClock.core.options import DisplayOptions
from radClock.core.projection import AzimuthalEquidistant, dist_bearing
from radClock.core.tracks import Tracker, extrapolate
from radClock.ui.palette import (AC_TYPE, BTN_BG, BTN_ON, CONFLICT, DIM, ECHO_TXT, EMERG, OK,
                                PANEL_BG, PANEL_LINE, SELECTED, STALE, TXT, WARN, Color, ui_font)

BUTTON_HEIGHT_PX = 44       # ~8 mm na tela de 7": o mínimo para um toque firme
BUTTON_GAP_PX = 8
PANEL_TEXT_X_PX = 12        # margem esquerda do texto do painel
PANEL_MAX_CHARS = 40        # linhas longas da ficha são cortadas aqui
CLOCK_SYNC_CHECK_S = 30.0   # de quanto em quanto tempo perguntar se o NTP acertou a hora
CLOCK_SAMPLE = "88:88"      # texto mais largo do relógio (fonte monoespaçada: vale p/ todos)
CLOCK_MARGIN_PX = 6         # do botão do relógio até a borda do painel
CLOCK_PAD_PX = 6            # dos dígitos até a borda do botão
FOOTER_MARGIN_PX = 4        # espaço livre sob o texto do rodapé
SEPARATOR_MARGIN_PX = 5     # acima e abaixo da linha que separa estado e selecionado


def _format_value(value: float | None, unit: str, decimals: int = 1) -> str:
    """Número com unidade ("35.8 m"), ou "-" quando o valor é desconhecido."""
    if value is None:
        return "-"
    return f"{value:.{decimals}f} {unit}"


class Panel:
    """Desenha o painel da direita e diz qual botão dele foi tocado.

    Atributos públicos:
        rect: retângulo do painel na tela.
        buttons: retângulos dos botões fixos ("range_down", "range_up", "settings" e,
            com eco de chuva, "echo"), por nome.
    """

    def __init__(self, screen: pygame.Surface, rect: pygame.Rect, cfg: Config,
                 proj: AzimuthalEquidistant, tracker: Tracker, clock: AlarmClock,
                 echo_available: bool):
        """
        Parâmetros:
            screen: superfície da tela.
            rect: área do painel (o que sobra à direita do escopo).
            cfg: configuração completa.
            proj: projeção da estação (DIST/BRG do selecionado).
            tracker: conflitos (linha CONFLITO e detalhes).
            clock: despertador (hora local, próximo alarme).
            echo_available: se há eco de chuva (mostra o botão CHUVA).
        """
        self.screen = screen
        self.rect = rect
        self.cfg = cfg
        self.proj = proj
        self.tracker = tracker
        self.clock = clock
        self.echo_available = echo_available

        self.font_large = ui_font(20, bold=True)
        self.font_medium = ui_font(15)
        self.font_small = ui_font(12)
        self.footer_height = self.font_small.get_linesize() + FOOTER_MARGIN_PX

        # relógio grande: fonte escolhida uma vez para a largura do painel e
        # texto renderizado só quando muda (uma vez por minuto); renderizar
        # uma fonte de ~95 px a cada quadro pesaria no Pi
        self._clock_font = self._fit_clock_font()
        self._clock_text_key: tuple[str, Color] | None = None
        self._clock_surface: pygame.Surface | None = None

        # estado do último draw() (o Renderer passa a cada quadro)
        self.options = DisplayOptions()
        self.targets_shown = 0

        # áreas tocáveis: as fixas são calculadas uma vez; as outras a cada quadro
        self.buttons: dict[str, pygame.Rect] = {}
        self._range_label = pygame.Rect(0, 0, 0, 0)        # alcance entre "-" e "+"
        self._layout_buttons()
        self._card_button: pygame.Rect | None = None        # botão do tipo/ficha
        self._clock_button: pygame.Rect | None = None       # tocar abre o despertador
        self._source_button: pygame.Rect | None = None      # tocar troca a fonte

        # relógio: o NTP é consultado no máximo a cada CLOCK_SYNC_CHECK_S
        self._clock_is_synced: bool | None = None
        self._clock_checked_at = float("-inf")

    def hit_test(self, pos: tuple[int, int]) -> str | None:
        """Nome do botão do painel sob o toque `pos`, conforme o último quadro.

        Retorno:
            "range_down", "range_up", "settings", "echo", "source", "clock", "card",
            ou None se o toque não caiu num botão do painel.
        """
        for name, rect in self.buttons.items():
            if rect.collidepoint(pos):
                return name
        if self._source_button is not None and self._source_button.collidepoint(pos):
            return "source"
        if self._clock_button is not None and self._clock_button.collidepoint(pos):
            return "clock"
        if self._card_button is not None and self._card_button.collidepoint(pos):
            return "card"
        return None

    def draw(self, snap: Snapshot, selected: Aircraft | None, rng: float, now: float,
             wall_now: float, echo_on: bool, echo: list[EchoFrame], echo_err: str,
             card: Card | None, options: DisplayOptions, targets_shown: int,
             credits: list[str]) -> None:
        """
        Painel da direita, de cima para baixo.

        Relógio -> linha da fonte (com a contagem de alvos) -> erro da fonte
        e conflitos -> separador e detalhes/ficha (só com alvo selecionado) -> botões (com o alcance e
        a idade da chuva) -> rodapé.

        O painel é limitado em altura em 800x480: detalhes e ficha têm de
        terminar acima das linhas de botões.

        Parâmetros (além dos de ``Renderer.draw``):
            selected: a aeronave selecionada, ou None.
            wall_now: time.time() deste quadro (idade do eco de chuva).
            card: dados da ficha aberta, ou None.
            targets_shown: alvos desenhados no escopo (contagem na linha da fonte).
            credits: nomes das fontes de dados em uso, para o rodapé.
        """
        self.options = options
        self.targets_shown = targets_shown
        self._card_button = None                # recriado abaixo se houver ficha/tipo
        panel = self.rect
        pygame.draw.rect(self.screen, PANEL_BG, panel)
        pygame.draw.line(self.screen, PANEL_LINE, panel.topleft, panel.bottomleft)

        y = self._draw_clock(now)
        y = self._draw_source_line(snap, y, now)
        y = self._draw_status_lines(snap, y)

        # sem alvo selecionado o meio do painel fica vazio (nem separador)
        if selected is not None:
            y += SEPARATOR_MARGIN_PX
            pygame.draw.line(self.screen, PANEL_LINE, (panel.x + 8, y), (panel.right - 8, y))
            y += SEPARATOR_MARGIN_PX
            if card is not None:
                self._draw_card_panel(selected, card, y)
            else:
                self._draw_details(selected, y, snap, now)

        self._draw_panel_buttons(echo_on, rng, wall_now, echo, echo_err)
        self._draw_footer(now, credits)

    # ============================================================ partes
    def _layout_buttons(self) -> None:
        """Posiciona os botões fixos no pé do painel, logo acima do rodapé.

        Linha de baixo, em três colunas: "-" (alcance menor), o alcance atual
        (só texto, ``_range_label``) e "+" (alcance maior). Com o número no
        meio, a linha é um seletor de valor: "+" aumenta o número.
        Linha de cima: AJUSTES e CHUVA lado a lado, ou só AJUSTES em largura
        total quando não há eco de chuva.
        """
        panel = self.rect
        gap = BUTTON_GAP_PX
        height = BUTTON_HEIGHT_PX
        half_width = (panel.width - 3 * gap) // 2
        left_x = panel.x + gap
        right_x = panel.x + 2 * gap + half_width

        bottom_row_y = panel.bottom - self.footer_height - height
        third_width = (panel.width - 4 * gap) // 3
        self.buttons = {
            "range_down": pygame.Rect(left_x, bottom_row_y, third_width, height),
            "range_up": pygame.Rect(panel.right - gap - third_width, bottom_row_y,
                                    third_width, height),
        }
        self._range_label = pygame.Rect(left_x + third_width + gap, bottom_row_y,
                                        third_width, height)

        top_row_y = bottom_row_y - height - gap
        if self.echo_available:
            self.buttons["settings"] = pygame.Rect(left_x, top_row_y, half_width, height)
            self.buttons["echo"] = pygame.Rect(right_x, top_row_y, half_width, height)
        else:
            self.buttons["settings"] = pygame.Rect(left_x, top_row_y, panel.width - 2 * gap,
                                                   height)

    def _fit_clock_font(self) -> pygame.font.Font:
        """Maior fonte em que "88:88" cabe na largura útil do botão do relógio.

        Chamada uma vez, no construtor: o tamanho só depende da largura do
        painel (320 px a 800x480 dá ~95 px de fonte).
        """
        usable_width = self.rect.width - 2 * (CLOCK_MARGIN_PX + CLOCK_PAD_PX)
        size = 10
        while ui_font(size + 1, bold=True).size(CLOCK_SAMPLE)[0] <= usable_width:
            size += 1
        return ui_font(size, bold=True)

    def _clock_unsynced(self, now: float) -> bool:
        """True se o NTP ainda não acertou o relógio (o Pi não tem RTC).

        Até lá a hora é a do fake-hwclock. A pergunta ao sistema é feita no
        máximo a cada CLOCK_SYNC_CHECK_S, não a cada quadro.
        """
        if now - self._clock_checked_at > CLOCK_SYNC_CHECK_S:
            self._clock_is_synced = clock_synced()
            self._clock_checked_at = now
        return self._clock_is_synced is False

    def _clock_digits(self, text: str, color: Color) -> pygame.Surface:
        """Dígitos do relógio recortados rente ao desenho, com cache.

        O recorte tira o espaço vazio que a fonte reserva acima e abaixo dos
        números (acentos, descendentes): o botão fica só da altura dos dígitos.
        """
        key = (text, color)
        if key != self._clock_text_key:
            rendered = self._clock_font.render(text, True, color)
            self._clock_surface = rendered.subsurface(rendered.get_bounding_rect()).copy()
            self._clock_text_key = key
        return self._clock_surface

    def _draw_clock(self, now: float) -> int:
        """
        Relógio grande (HH:MM) na largura toda do painel; tocar abre o despertador.

        Fica laranja enquanto o NTP não acertou a hora.

        Retorno:
            y da próxima linha.
        """
        color = WARN if self._clock_unsynced(now) else TXT
        digits = self._clock_digits(f"{self.clock.now():%H:%M}", color)
        rect = pygame.Rect(self.rect.x + CLOCK_MARGIN_PX, CLOCK_MARGIN_PX,
                           self.rect.width - 2 * CLOCK_MARGIN_PX,
                           digits.get_height() + 2 * CLOCK_PAD_PX)
        self._draw_rounded_button(rect, BTN_BG, 8)
        self.screen.blit(digits, digits.get_rect(center=rect.center))
        self._clock_button = rect
        return rect.bottom + 6

    def _draw_source_line(self, snap: Snapshot, y: int, now: float) -> int:
        """
        Linha da fonte de dados, que também é botão: cada toque passa para a próxima.

        "● READSB (12)  1s": nome da fonte, alvos desenhados no escopo (entre
        parênteses, apagado) e idade da última leitura. Verde quando a leitura
        é boa e recente; laranja quando atrasada, com erro ou ainda sem leitura.
        O OpenSky acrescenta os créditos que restam no dia ("cota 3999").
        """
        if not snap.updated:
            parts = [(f"AGUARDANDO {snap.source}", WARN)]
        else:
            age = now - snap.updated
            is_fresh = age < max(5.0, 2 * snap.interval_s)
            color = OK if (is_fresh and not snap.error) else WARN
            parts = [(f"● {snap.source}", color), (f" ({self.targets_shown})", DIM),
                     (f"  {age:.0f}s", color)]
            if snap.quota is not None:          # OpenSky: créditos que restam hoje
                parts.append((f"  cota {snap.quota}", color))

        panel = self.rect
        height = self.font_medium.get_height()
        rect = pygame.Rect(panel.x + 6, y - 2, panel.width - 12, height + 6)
        self._draw_rounded_button(rect, BTN_BG, 6)
        x = panel.x + PANEL_TEXT_X_PX
        for text, color in parts:
            surface = self.font_medium.render(text, True, color)
            self.screen.blit(surface, (x, y + 1))
            x += surface.get_width()
        arrow = self.font_medium.render(">", True, DIM)
        self.screen.blit(arrow, (rect.right - arrow.get_width() - 8, y + 1))
        self._source_button = rect
        return rect.bottom + 4

    def _draw_status_lines(self, snap: Snapshot, y: int) -> int:
        """
        Erro da fonte, CONFLITO e ALERTA DESL; nada quando está tudo bem.

        Com o ALERTA desligado nos ajustes, o conflito não aparece nem toca:
        a linha laranja ALERTA DESL lembra disso o tempo todo.

        A contagem de alvos fica na linha da fonte, o alcance entre os botões
        "-" e "+", e a idade da chuva no botão CHUVA.

        Retorno:
            y da próxima linha.
        """
        if snap.error:
            y = self._panel_text(snap.error[:34], y, self.font_small, WARN)

        if not self.options.show_conflicts:
            return self._panel_text("ALERTA DESL", y, self.font_small, WARN)
        if self.tracker.pairs:
            conflict = f"CONFLITO {len(self.tracker.pairs)}"
            if self.tracker.inhibited:
                conflict += f" ({self.tracker.inhibited} inib.)"
            y = self._panel_text(conflict, y, color=CONFLICT)
        return y

    def _echo_note(self, rng: float, wall_now: float, echo: list[EchoFrame],
                   echo_err: str) -> tuple[str, Color]:
        """
        Texto pequeno sob "CHUVA" no botão: estado do eco e a sua idade.

        "há 14 min" diz o quanto a chuva é mais velha que o tráfego; acima de
        ECHO_OLD_S fica laranja (RainViewer ou rede parou). "ERRO" em laranja
        quando a última busca falhou (a mensagem completa está no log).
        Sem hora certa (Pi sem NTP), a hora do quadro. Com animação
        (``[echo] frames`` > 1), também o progresso ("3/7") ou os quadros.

        Retorno:
            (texto, cor).
        """
        if echo_err:
            return "ERRO", WARN
        frames = [frame for frame in echo if frame.range_km == rng]
        if not frames:
            return "carregando", DIM

        newest = frames[-1]
        age_s = frame_age_s(newest.time, wall_now)
        if age_s is None:
            note = f"{datetime.fromtimestamp(newest.time, self.clock.tz):%H:%M}"
        else:
            note = age_text(age_s)
        color = TXT
        if age_s is not None and age_s > ECHO_OLD_S:
            color = WARN
        loaded = len(frames)
        wanted = self.cfg.echo.frames
        if loaded < wanted:
            note += f" · {loaded}/{wanted}"
        elif loaded > 1:
            note += f" · {loaded}"
        return note, color

    def _draw_panel_buttons(self, echo_on: bool, rng: float, wall_now: float,
                            echo: list[EchoFrame], echo_err: str) -> None:
        """
        Botões fixos "-", "+", AJUSTES e CHUVA (aceso quando ligada) e, entre
        "-" e "+", o alcance atual em km. Com a chuva ligada, o botão CHUVA
        tem uma segunda linha pequena com o estado e a idade do eco.
        """
        text = self.font_large.render(f"{rng:g} km", True, TXT)
        self.screen.blit(text, text.get_rect(center=self._range_label.center))

        labels = (("range_down", "-"), ("range_up", "+"), ("settings", "AJUSTES"),
                  ("echo", "CHUVA"))
        for name, label in labels:
            rect = self.buttons.get(name)
            if rect is None:
                continue
            is_on = name == "echo" and echo_on
            self._draw_rounded_button(rect, BTN_ON if is_on else BTN_BG, 8)
            text = self.font_large.render(label, True, TXT)
            if name == "echo" and echo_on:
                self._draw_two_line_label(rect, text, *self._echo_note(rng, wall_now, echo,
                                                                       echo_err))
                continue
            self.screen.blit(text, text.get_rect(center=rect.center))

    def _draw_two_line_label(self, rect: pygame.Rect, title: pygame.Surface, note: str,
                             color: Color) -> None:
        """Rótulo do botão em duas linhas: título grande e nota pequena embaixo,
        o par centrado no botão."""
        note_surface = self.font_small.render(note, True, color)
        total_height = title.get_height() + note_surface.get_height()
        top = rect.centery - total_height // 2
        self.screen.blit(title, title.get_rect(midtop=(rect.centerx, top)))
        self.screen.blit(note_surface,
                         note_surface.get_rect(midtop=(rect.centerx, top + title.get_height())))

    def _draw_footer(self, now: float, credits: list[str]) -> None:
        """
        Rodapé: créditos das fontes em uso à esquerda ("NOAA AWC · RainViewer");
        à direita o aviso SEM NTP (laranja) ou o próximo alarme, quando houver.

        Os créditos atendem aos termos da RainViewer e à licença CC BY do
        Open-Meteo: cada um aparece enquanto a sua camada está na tela.
        """
        y = self.rect.bottom - self.footer_height
        if credits:
            text = self.font_small.render(" · ".join(credits), True, ECHO_TXT)
            self.screen.blit(text, (self.rect.x + PANEL_TEXT_X_PX, y))

        if self._clock_unsynced(now):
            note, color = "SEM NTP", WARN
        else:
            note, color = next_alarm_text(self.clock), SELECTED
        if not note:
            return
        text = self.font_small.render(note, True, color)
        self.screen.blit(text, (self.rect.right - PANEL_TEXT_X_PX - text.get_width(), y))

    # ================================================= selecionado / ficha
    def _draw_details(self, ac: Aircraft, y: int, snap: Snapshot, now: float) -> None:
        """
        Detalhes do selecionado (sem ficha): squawk, conflito, ALT, GS, DIST/BRG.

        Compacto: com os botões AJUSTES/CHUVA e -/+ embaixo, cada linha conta.
        DIST/BRG usam a mesma posição extrapolada do símbolo na tela.
        """
        y = self._draw_title_line(ac.label, f"{ac.type or 'FICHA'} >", y)

        # matrícula (ou hex) à direita da linha do squawk
        self._panel_text_right(ac.reg or ac.hex.upper(), y, color=DIM)
        if ac.emergency:
            y = self._panel_text(f"SQUAWK {ac.squawk} {ac.emergency}", y, color=EMERG)
        else:
            y = self._panel_text(f"SQUAWK {ac.squawk or '----'}", y)

        conflict = None
        if self.options.show_conflicts:
            conflict = self.tracker.conflicts.get(ac.hex)
        if conflict is not None:
            other = snap.find(conflict.other)
            other_label = other.label if other is not None else conflict.other.upper()
            when = "AGORA" if conflict.t_s < 1 else f"{conflict.t_s:.0f}s"
            y = self._panel_text(f"CONFL {other_label} {when}", y, color=CONFLICT)

        if ac.on_ground:
            altitude = "SOLO"
        elif ac.alt_ft is not None:
            altitude = f"{ac.alt_ft} ft"
        else:
            altitude = "--"
        vertical_speed = ""
        if ac.vrate_fpm is not None:
            vertical_speed = f"  V/S {ac.vrate_fpm:+.0f}"
        y = self._panel_text(f"ALT  {altitude}{vertical_speed}", y)

        ground_speed = f"{ac.gs_kt:.0f} kt" if ac.gs_kt is not None else "--"
        track = f"{ac.track:03.0f}" if ac.track is not None else "---"
        y = self._panel_text(f"GS   {ground_speed}  TRK {track}", y)

        age = ac.seen_pos_s + (now - snap.updated)
        east_km, north_km = extrapolate(ac, *self.proj.forward(ac.lat, ac.lon), age,
                                        self.cfg.radar.extrapolate_s)
        distance_km, bearing = dist_bearing(east_km, north_km)
        y = self._panel_text(f"DIST {distance_km:.1f} km  BRG {bearing:03.0f}", y)
        if age > self.cfg.radar.stale_s:        # idade só importa quando a posição é velha
            self._panel_text(f"POS  {age:.0f}s SEM ATUALIZAR", y, color=STALE)

    def _draw_card_panel(self, ac: Aircraft, card: Card, y: int) -> None:
        """
        Dados da ficha no painel: tipo, dimensões, motor, matrícula, operador.
        """
        small = self.font_small
        y = self._draw_title_line(ac.label, f"< {ac.type or 'VOLTAR'}", y, on=True)
        aircraft_type = card.type
        if aircraft_type is not None:
            model = f"{aircraft_type.mfr} {aircraft_type.model}".strip()
            y = self._panel_text(model[:PANEL_MAX_CHARS], y, small, AC_TYPE)
            span = _format_value(aircraft_type.span_m, "m")
            length = _format_value(aircraft_type.len_m, "m")
            y = self._panel_text(f"ENV  {span}  COMP {length}", y, small)
            mtow_t = None
            if aircraft_type.mtow_kg is not None:
                mtow_t = aircraft_type.mtow_kg / 1000
            mtow = _format_value(mtow_t, "t")
            cruise = _format_value(aircraft_type.cruise_kt, "kt", 0)
            y = self._panel_text(f"MTOW {mtow}  CRUZ {cruise}", y, small)
            y = self._panel_text(f"MOTOR {self._engine_text(aircraft_type) or '-'}", y, small)
        elif ac.type:
            y = self._panel_text(f"TIPO {ac.type} FORA DA BASE", y, small, DIM)
        else:
            y = self._panel_text("TIPO DESCONHECIDO", y, small, DIM)
        registration = f"MATR {ac.reg or '-'}  PAIS {card.country or '-'}"
        y = self._panel_text(registration[:PANEL_MAX_CHARS], y, small)
        if card.operator:
            y = self._panel_text(f"OPER {card.operator}"[:PANEL_MAX_CHARS], y, small)
            if card.radio:
                self._panel_text(f'     "{card.radio}"', y, small, DIM)

    @staticmethod
    def _engine_text(aircraft_type: TypeInfo) -> str:
        """
        "2x CFM56 jato": quantidade, modelo e tipo, omitindo o que faltar.
        """
        parts = []
        if aircraft_type.eng_n:
            parts.append(f"{aircraft_type.eng_n}x")
        if aircraft_type.eng:
            parts.append(aircraft_type.eng)
        if aircraft_type.eng_kind:
            parts.append(aircraft_type.eng_kind)
        return " ".join(parts)

    def _draw_title_line(self, title: str, button_label: str, y: int, on: bool = False) -> int:
        """
        Indicativo do selecionado à esquerda e, na mesma linha, à direita, o
        botão que abre/fecha a ficha (com o tipo da aeronave).

        Parâmetros:
            title: indicativo (ou hex) da aeronave.
            button_label: texto do botão.
            y: topo da linha.
            on: botão aceso (ficha aberta).

        Retorno:
            y da próxima linha do painel.
        """
        text = self.font_medium.render(button_label, True, TXT)
        rect = pygame.Rect(0, y, max(text.get_width() + 20, 90), text.get_height() + 12)
        rect.right = self.rect.right - 10
        self._draw_rounded_button(rect, BTN_ON if on else BTN_BG, 6)
        self.screen.blit(text, text.get_rect(center=rect.center))
        self._card_button = rect

        title_surface = self.font_large.render(title, True, SELECTED)
        title_y = rect.centery - title_surface.get_height() // 2
        self.screen.blit(title_surface, (self.rect.x + PANEL_TEXT_X_PX, title_y))
        return rect.bottom + 4

    # ========================================================== auxiliares
    def _panel_text(self, text: str, y: int, font: pygame.font.Font | None = None,
                    color: Color = TXT) -> int:
        """
        Escreve uma linha no painel e devolve o y da próxima linha.
        """
        if font is None:
            font = self.font_medium
        surface = font.render(text, True, color)
        self.screen.blit(surface, (self.rect.x + PANEL_TEXT_X_PX, y))
        return y + surface.get_height() + 2

    def _panel_text_right(self, text: str, y: int, font: pygame.font.Font | None = None,
                          color: Color = TXT) -> None:
        """
        Escreve um texto encostado à direita do painel, na linha que começa em y.
        """
        if font is None:
            font = self.font_medium
        surface = font.render(text, True, color)
        self.screen.blit(surface, (self.rect.right - PANEL_TEXT_X_PX - surface.get_width(), y))

    def _draw_rounded_button(self, rect: pygame.Rect, fill: Color, radius: int) -> None:
        """
        Fundo + contorno de botão com cantos arredondados.
        """
        pygame.draw.rect(self.screen, fill, rect, border_radius=radius)
        pygame.draw.rect(self.screen, PANEL_LINE, rect, 1, border_radius=radius)
