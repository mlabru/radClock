"""
Tela do despertador (abre ao tocar no relógio) e faixa de alarme tocando.

Tela do despertador
-------------------
Tudo por toque, com alvos de >= 44 px (~8 mm na tela de 7"): liga/desliga,
hora e minuto com -/+ (minuto de 5 em 5), dias da semana, fuso e teste do som.

Cada botão tem uma CHAVE de texto, que é ao mesmo tempo o nome do retângulo
em ``ClockScreen.rects`` e a ação devolvida por ``hit()``. O número no fim é
o índice do alarme (0 a 3):

    "close", "test", "unsnooze", "tz-", "tz+"   botões do topo
    "on0"                    liga/desliga o alarme 0
    "h-0", "h+0"             hora -1 / +1 do alarme 0
    "m-0", "m+0"             minuto para o múltiplo de 5 anterior / seguinte
    "d0:6"                   dia da semana 6 (domingo) do alarme 0

Faixa do alarme tocando
-----------------------
SONECA é o botão grande e qualquer toque fora de PARAR conta como soneca:
meio dormindo, é melhor adiar sem querer do que desligar sem querer. Para
desligar de vez é preciso acertar o PARAR.

Cores e fonte vêm de ``palette``, as mesmas do escopo e do painel.
"""
from __future__ import annotations

import pygame

from radClock.core.alarm import TIMEZONES, AlarmClock, next_alarm_text
from radClock.ui.palette import BG, BTN_BG, BTN_ON, DIM, EMERG, PANEL_LINE, SELECTED, TXT, ui_font

DAY_ORDER = [6, 0, 1, 2, 3, 4, 5]               # domingo primeiro, como no calendário
DAY_LETTER = {6: "D", 0: "S", 1: "T", 2: "Q", 3: "Q", 4: "S", 5: "S"}
MINUTE_STEP = 5

# Medidas da tela do despertador, em px.
PADDING = 10
BUTTON_HEIGHT = 52
SMALL_BUTTON_WIDTH = 44          # botões -/+ e o espaço do número entre eles
COLON_WIDTH = 14                 # espaço do ":" entre hora e minuto
TOGGLE_WIDTH = 76                # botão LIGA/DESL
TOGGLE_HEIGHT = 72
DAY_BUTTON_WIDTH = 50
DAY_BUTTON_STEP = 54             # largura + espaço entre dias
TIMEZONE_BOX_WIDTH = 290
FIRST_ROW_Y = 134
ROW_HEIGHT = 86
ROW_BUTTON_OFFSET_Y = 10         # botões da linha ficam centrados no LIGA/DESL

DIM_RED = (90, 20, 10)           # faixa do alarme no meio-ciclo apagado


class ClockScreen:
    """
    Tela cheia do despertador: desenha (``draw``), acha o botão tocado
    (``hit``) e executa a ação (``apply``).
    """

    def __init__(self, screen: pygame.Surface, clock: AlarmClock):
        self.screen = screen
        self.clock = clock
        self.f_big = ui_font(22, bold=True)
        self.f = ui_font(17, bold=True)
        self.f_small = ui_font(13)
        if clock.tz_name not in TIMEZONES:
            TIMEZONES.insert(0, clock.tz_name)
        self.rects: dict[str, pygame.Rect] = {}
        self._layout()

    # ------------------------------------------------------------ layout
    def _layout(self) -> None:
        """
        Calcula o retângulo de cada botão (uma vez, na criação).
        """
        self._layout_top_row()
        self.rows_y = [FIRST_ROW_Y + index * ROW_HEIGHT
                       for index in range(len(self.clock.alarms))]
        for alarm_index, row_y in enumerate(self.rows_y):
            self._layout_alarm_row(alarm_index, row_y)

    def _layout_top_row(self) -> None:
        """
        FECHAR, TESTAR, SEM SONECA e o seletor de fuso.
        """
        screen_width = self.screen.get_width()
        rects = self.rects
        rects["close"] = pygame.Rect(screen_width - PADDING - 110, 8, 110, BUTTON_HEIGHT)
        rects["test"] = pygame.Rect(screen_width - 2 * PADDING - 230, 8, 120, BUTTON_HEIGHT)
        # só aparece (e só responde ao toque) quando há soneca marcada
        rects["unsnooze"] = pygame.Rect(rects["test"].x - PADDING - 170, 8, 170, BUTTON_HEIGHT)

        timezone_y = 70
        arrow_width = 52
        rects["tz-"] = pygame.Rect(PADDING, timezone_y, arrow_width, BUTTON_HEIGHT)
        rects["tz+"] = pygame.Rect(PADDING + arrow_width + TIMEZONE_BOX_WIDTH, timezone_y,
                                   arrow_width, BUTTON_HEIGHT)
        self.tz_box = pygame.Rect(PADDING + arrow_width, timezone_y,
                                  TIMEZONE_BOX_WIDTH, BUTTON_HEIGHT)

    def _layout_alarm_row(self, alarm_index: int, row_y: int) -> None:
        """
        Uma linha: [LIGA] [-] HH [+] : [-] MM [+]  [D][S][T][Q][Q][S][S].
        """
        rects = self.rects
        button_y = row_y + ROW_BUTTON_OFFSET_Y
        x = PADDING
        rects[f"on{alarm_index}"] = pygame.Rect(x, row_y, TOGGLE_WIDTH, TOGGLE_HEIGHT)
        x += TOGGLE_WIDTH + 12

        # botões -/+ com o espaço do número entre eles; o ":" entre hora e minuto
        for key in ("h-", "h+", "m-", "m+"):
            rects[f"{key}{alarm_index}"] = pygame.Rect(x, button_y,
                                                       SMALL_BUTTON_WIDTH, BUTTON_HEIGHT)
            x += SMALL_BUTTON_WIDTH
            if key in ("h-", "m-"):
                x += SMALL_BUTTON_WIDTH          # espaço do número
            if key == "h+":
                x += COLON_WIDTH

        x += COLON_WIDTH
        for weekday in DAY_ORDER:
            rects[f"d{alarm_index}:{weekday}"] = pygame.Rect(x, button_y,
                                                             DAY_BUTTON_WIDTH, BUTTON_HEIGHT)
            x += DAY_BUTTON_STEP

    # ------------------------------------------------------------- toque
    def hit(self, pos: tuple[int, int]) -> str | None:
        """Chave do botão em ``pos``, ou None se o toque não acertou nada."""
        for key, rect in self.rects.items():
            if key == "unsnooze" and self.clock.snooze is None:
                continue
            if rect.collidepoint(pos):
                return key
        return None

    def apply(self, action: str) -> bool:
        """Executa a ação de um botão (chave devolvida por ``hit``).

        "close" e "test" não são tratados aqui (o app cuida deles).

        Retorno:
            True se mudou algo que deve ser gravado no arquivo de estado.
        """
        if action == "unsnooze":
            self.clock.snooze = None
            return True
        if action in ("tz-", "tz+"):
            return self._step_timezone(action == "tz+")
        if action.startswith("d"):
            alarm_text, weekday_text = action[1:].split(":")
            self.clock.alarms[int(alarm_text)].days ^= {int(weekday_text)}
            return True
        if action.startswith("on"):
            self._toggle_alarm(int(action[2:]))
            return True
        if action[:2] in ("h-", "h+", "m-", "m+"):
            self._step_time(action[:2], int(action[2:]))
            return True
        return False

    def _step_timezone(self, forward: bool) -> bool:
        """Passa para o fuso seguinte (ou anterior) da lista, em círculo."""
        current = self.clock.tz_name
        index = TIMEZONES.index(current) if current in TIMEZONES else 0
        step = 1 if forward else -1
        new_index = (index + step) % len(TIMEZONES)
        return self.clock.set_tz(TIMEZONES[new_index])

    def _toggle_alarm(self, alarm_index: int) -> None:
        """Liga/desliga; desligar também cancela a soneca desse alarme."""
        alarm = self.clock.alarms[alarm_index]
        alarm.enabled = not alarm.enabled
        snooze = self.clock.snooze
        if not alarm.enabled and snooze is not None and snooze[0] == alarm_index:
            self.clock.snooze = None

    def _step_time(self, button: str, alarm_index: int) -> None:
        """Ajusta hora (de 1 em 1) ou minuto (múltiplos de 5) e liga o alarme."""
        alarm = self.clock.alarms[alarm_index]
        if button == "h+":
            alarm.hour = (alarm.hour + 1) % 24
        elif button == "h-":
            alarm.hour = (alarm.hour - 1) % 24
        elif button == "m+":
            # vai para o próximo múltiplo de 5 (32 -> 35, 35 -> 40)
            rounded_down = alarm.minute // MINUTE_STEP * MINUTE_STEP
            alarm.minute = (rounded_down + MINUTE_STEP) % 60
        else:
            # vai para o múltiplo de 5 anterior (32 -> 30, 30 -> 25)
            alarm.minute = ((alarm.minute - 1) // MINUTE_STEP * MINUTE_STEP) % 60
        alarm.enabled = True                    # mexeu na hora: liga

    # ----------------------------------------------------------- desenho
    def _button(self, key: str, label: str, on: bool = False, dim: bool = False) -> None:
        """Desenha o botão ``key``: fundo azul se ``on``, texto apagado se ``dim``."""
        rect = self.rects[key]
        background = BTN_ON if on else BTN_BG
        text_color = DIM if dim else TXT
        pygame.draw.rect(self.screen, background, rect, border_radius=8)
        pygame.draw.rect(self.screen, PANEL_LINE, rect, 1, border_radius=8)
        self._center_text(label, rect, self.f, text_color)

    def _center_text(self, text: str, rect: pygame.Rect, font, color) -> None:
        """Escreve ``text`` centrado em ``rect``."""
        rendered = font.render(text, True, color)
        self.screen.blit(rendered, rendered.get_rect(center=rect.center))

    def draw(self, sound_ok: bool) -> None:
        """Desenha a tela inteira do despertador.

        Parâmetros:
            sound_ok: False mostra o aviso "sem saída de som" sob o TESTAR.
        """
        self.screen.fill(BG)
        local_now = self.clock.now()
        self._draw_header(local_now, sound_ok)
        self._draw_timezone(local_now)
        for alarm_index, row_y in enumerate(self.rows_y):
            self._draw_alarm_row(alarm_index, row_y)

    def _draw_header(self, local_now, sound_ok: bool) -> None:
        """Hora atual, próximo alarme e os botões do topo."""
        screen = self.screen
        clock_text = self.f_big.render(f"{local_now:%H:%M:%S}", True, SELECTED)
        screen.blit(clock_text, (12, 12))
        next_text = next_alarm_text(self.clock) or "NENHUM ALARME LIGADO"
        screen.blit(self.f_small.render(next_text, True, DIM),
                    (12, 12 + clock_text.get_height() + 2))

        self._button("test", "TESTAR")
        if self.clock.snooze is not None:
            self._button("unsnooze", "SEM SONECA")
        self._button("close", "FECHAR")
        if not sound_ok:
            test_rect = self.rects["test"]
            warning = self.f_small.render("sem saída de som", True, EMERG)
            screen.blit(warning, warning.get_rect(midtop=(test_rect.centerx, test_rect.bottom)))

    def _draw_timezone(self, local_now) -> None:
        """Seletor de fuso: < nome  sigla >."""
        self._button("tz-", "<")
        self._button("tz+", ">")
        pygame.draw.rect(self.screen, PANEL_LINE, self.tz_box, 1, border_radius=8)
        self._center_text(f"{self.clock.tz_name}  {local_now:%Z}", self.tz_box, self.f, TXT)

    def _draw_alarm_row(self, alarm_index: int, row_y: int) -> None:
        """Uma linha de alarme: liga/desliga, hora, minuto e dias."""
        screen = self.screen
        alarm = self.clock.alarms[alarm_index]
        pygame.draw.line(screen, PANEL_LINE, (10, row_y - 7), (screen.get_width() - 10, row_y - 7))
        self._button(f"on{alarm_index}", "LIGA" if alarm.enabled else "DESL", on=alarm.enabled)
        for key in ("h-", "m-"):
            self._button(f"{key}{alarm_index}", "-")
        for key in ("h+", "m+"):
            self._button(f"{key}{alarm_index}", "+")

        # números entre os botões -/+
        color = TXT if alarm.enabled else DIM
        hour_minus = self.rects[f"h-{alarm_index}"]
        hour_plus = self.rects[f"h+{alarm_index}"]
        minute_minus = self.rects[f"m-{alarm_index}"]
        hour_box = pygame.Rect(hour_minus.right, hour_minus.y, SMALL_BUTTON_WIDTH, hour_minus.h)
        colon_box = pygame.Rect(hour_plus.right, hour_minus.y, COLON_WIDTH, hour_minus.h)
        minute_box = pygame.Rect(minute_minus.right, minute_minus.y,
                                 SMALL_BUTTON_WIDTH, minute_minus.h)
        self._center_text(f"{alarm.hour:02d}", hour_box, self.f_big, color)
        self._center_text(":", colon_box, self.f_big, color)
        self._center_text(f"{alarm.minute:02d}", minute_box, self.f_big, color)

        for weekday in DAY_ORDER:
            marked = weekday in alarm.days
            self._button(f"d{alarm_index}:{weekday}", DAY_LETTER[weekday],
                         on=marked, dim=not marked)
        if not alarm.days:
            first_day = self.rects[f"d{alarm_index}:{DAY_ORDER[0]}"]
            once_text = self.f_small.render("uma vez", True, DIM)
            screen.blit(once_text, (first_day.x, first_day.bottom + 1))


def ringing_layout(screen: pygame.Surface,
                   snooze: bool) -> tuple[pygame.Rect, dict[str, pygame.Rect]]:
    """Posição da faixa do alarme e dos seus botões.

    Parâmetros:
        screen: a tela (só o tamanho importa).
        snooze: True se há soneca configurada.

    Retorno:
        ``(faixa, botões)``. Sem soneca, ``botões`` é vazio (todo toque para);
        com soneca, tem "snooze" (grande, à esquerda) e "stop" (à direita).
    """
    width, height = screen.get_size()
    if not snooze:
        return pygame.Rect(0, height // 2 - 60, width, 120), {}

    band = pygame.Rect(0, height // 2 - 110, width, 220)
    padding = 16
    button_height = 96
    button_y = band.bottom - padding - button_height
    stop_width = width // 4
    snooze_width = width - 3 * padding - stop_width
    return band, {
        "snooze": pygame.Rect(padding, button_y, snooze_width, button_height),
        "stop": pygame.Rect(width - padding - stop_width, button_y, stop_width, button_height),
    }


def ringing_hit(screen: pygame.Surface, snooze: bool, pos: tuple[int, int]) -> str:
    """O que um toque faz com o alarme tocando.

    Retorno:
        "stop" se acertou PARAR (ou se não há soneca); "snooze" em qualquer
        outro lugar da tela.
    """
    _, buttons = ringing_layout(screen, snooze)
    if not buttons:
        return "stop"
    if buttons["stop"].collidepoint(pos):
        return "stop"
    return "snooze"


def draw_ringing(screen: pygame.Surface, clock: AlarmClock, now_mono: float,
                 snooze_min: float = 0) -> None:
    """Faixa piscante sobre a tela enquanto o alarme toca (nada se não toca).

    Parâmetros:
        screen: onde desenhar (por cima do radar ou do despertador).
        clock: o ``AlarmClock`` (``ringing`` diz qual alarme toca).
        now_mono: ``time.monotonic()``, para piscar 2x por segundo.
        snooze_min: minutos de soneca; 0 = sem botões (todo toque para).
    """
    if clock.ringing is None:
        return
    alarm = clock.alarms[clock.ringing]
    screen_width = screen.get_width()
    band, buttons = ringing_layout(screen, snooze_min > 0)
    blink_on = int(now_mono * 2) % 2 == 0
    pygame.draw.rect(screen, EMERG if blink_on else DIM_RED, band)

    big_font = ui_font(40, bold=True)
    title = big_font.render(f"ALARME  {alarm.hour:02d}:{alarm.minute:02d}", True, TXT)
    if not buttons:
        screen.blit(title, title.get_rect(center=(screen_width // 2, band.centery - 16)))
        hint = ui_font(18).render("TOQUE NA TELA PARA PARAR", True, TXT)
        screen.blit(hint, hint.get_rect(center=(screen_width // 2, band.centery + 30)))
        return

    screen.blit(title, title.get_rect(center=(screen_width // 2, band.top + 44)))
    button_font = ui_font(26, bold=True)
    labels = {"snooze": f"SONECA {snooze_min:g} min", "stop": "PARAR"}
    for key, label in labels.items():
        rect = buttons[key]
        background = BTN_ON if key == "snooze" else BTN_BG
        pygame.draw.rect(screen, background, rect, border_radius=10)
        pygame.draw.rect(screen, TXT, rect, 2, border_radius=10)
        text = button_font.render(label, True, TXT)
        screen.blit(text, text.get_rect(center=rect.center))
