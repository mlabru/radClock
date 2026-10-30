"""
Ponto de entrada do radClock: monta as peças e roda o laço de
eventos/desenho.

Visão geral do fluxo (veja `RadarApp.run`):

1.  `main()` lê a linha de comando e o TOML e cria um `RadarApp`.

2.  `RadarApp.__init__` carrega os mapas, restaura os ajustes salvos na tela
    (`StateFile`), inicia as threads de coleta (tráfego, eco de chuva,
    clima) e abre a janela do pygame.

3.  A cada volta do laço principal (`_run_one_frame`):

    - trata os eventos de toque/teclado (`_handle_event`);
    - atualiza alarmes, a thread de tráfego e o rastreador de conflitos;
    - grava os ajustes que mudaram;
    - desenha a tela e avisa o watchdog do systemd.

4.  Ao sair (Esc, `q`, fechar a janela ou SIGTERM), `_shutdown` para o som e
    as threads.

Regra de ouro: rede e disco nunca rodam nesta thread (a da tela).  As
threads de coleta publicam dados prontos e o laço só os lê.
"""
from __future__ import annotations

import argparse
import logging
import os
import signal
import time
from dataclasses import replace
from pathlib import Path

import pygame

from radClock.core import config as config_module
from radClock.core.acdb import AircraftDB
from radClock.core.alarm import Alarm, AlarmClock, zone
from radClock.core.backlight import Backlight
from radClock.core.config import Config
from radClock.core.echo import EchoFetcher
from radClock.core.fetcher import Snapshot, SourceChoices
from radClock.core.lookup import TypeLookup
from radClock.core.maplayer import MapLayer
from radClock.core.net import configure_tls
from radClock.core.options import DisplayOptions
from radClock.core.projection import AzimuthalEquidistant
from radClock.core.state import StateFile
from radClock.core.tracks import Tracker
from radClock.core.watchdog import Watchdog
from radClock.core.weather import WeatherFetcher
from radClock.ui.audio import CONFLICT_BEEPS, MIXER_BUFFER, Buzzer
from radClock.ui.clockui import ClockScreen, draw_ringing, ringing_hit
from radClock.ui.echoview import decode_png
from radClock.ui.render import Renderer
from radClock.ui.settingsui import SettingsScreen

log = logging.getLogger("radClock")

SOUND_TEST_DURATION_S = 3.0         # quanto dura o botão "TESTAR" da tela do relógio
PERFORMANCE_LOG_INTERVAL_S = 60.0   # de quanto em quanto tempo o -v loga o tempo de quadro

# teclas que param o alarme de vez; qualquer outra tecla dá soneca
KEYS_STOP_ALARM = (pygame.K_ESCAPE, pygame.K_RETURN, pygame.K_p)
KEYS_QUIT = (pygame.K_ESCAPE, pygame.K_q)
# "+" aumenta o número do alcance e "-" diminui, como os botões do painel
# (o alcance aparece entre eles: a linha funciona como um seletor de valor)
KEYS_RANGE_UP = (pygame.K_PLUS, pygame.K_EQUALS, pygame.K_KP_PLUS)
KEYS_RANGE_DOWN = (pygame.K_MINUS, pygame.K_KP_MINUS)
LEFT_MOUSE_BUTTON = 1
# com o cursor visível, de quanto em quanto tempo ler eventos entre quadros
# (no kmsdrm o SDL só move o cursor quando lê os eventos)
CURSOR_PUMP_S = 0.01


def touch_as_click(event: pygame.event.Event,
                   screen_size: tuple[int, int]) -> pygame.event.Event | None:
    """
    Unifica toque e clique: todo toque chega ao app como um único clique.

    No desktop (X11/Wayland) o SDL manda o toque duas vezes: FINGERDOWN e um
    clique emulado (``touch=True``). No kmsdrm, sem desktop, só vem o
    FINGERDOWN, sem janela com foco, e o SDL não emula o clique: tratar só o
    clique deixava a tela do Pi surda ao toque. Aqui o FINGERDOWN vira o
    clique e o clique emulado é descartado, então cada toque conta uma vez.

    Parâmetros:
        event: evento recebido do pygame.
        screen_size: (largura, altura) da tela, para converter a posição do
            toque (de 0 a 1) em pixels.

    Retorna:
        O evento a tratar (o próprio, ou o clique no lugar do toque), ou None
        para ignorá-lo.
    """
    if event.type == pygame.MOUSEBUTTONDOWN and getattr(event, "touch", False):
        return None
    if event.type != pygame.FINGERDOWN:
        return event
    width, height = screen_size
    x = min(int(event.x * width), width - 1)
    y = min(int(event.y * height), height - 1)
    return pygame.event.Event(pygame.MOUSEBUTTONDOWN, pos=(x, y),
                              button=LEFT_MOUSE_BUTTON, touch=True)


# eventos que só um dedo gera: escondem de novo o cursor mostrado pelo mouse
TOUCH_EVENTS = (pygame.FINGERDOWN, pygame.FINGERMOTION, pygame.FINGERUP)


def cursor_wanted(event: pygame.event.Event) -> bool | None:
    """
    Diz se o cursor deve aparecer, conforme quem mexeu por último na tela.

    Em tela cheia o cursor começa escondido (tela de toque). Um mouse USB
    ligado ao Pi o faz aparecer ao se mover, e o próximo toque o esconde de
    novo. Eventos de mouse emulados a partir de um toque (``touch=True``)
    contam como toque, não como mouse.

    Parâmetros:
        event: evento recebido do pygame.

    Retorna:
        True para mostrar o cursor, False para escondê-lo, ou None quando o
        evento não muda nada.
    """
    if event.type in TOUCH_EVENTS:
        return False
    if event.type not in (pygame.MOUSEMOTION, pygame.MOUSEBUTTONDOWN):
        return None
    return not getattr(event, "touch", False)


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    """
    Lê as opções da linha de comando.

    Parâmetros:
        argv: lista de argumentos (None = os de `sys.argv`).

    Retorno:
        Namespace com `config` (Path ou None), `windowed` e `verbose`.
    """
    parser = argparse.ArgumentParser(prog="radClock", description=__doc__.splitlines()[0])
    parser.add_argument("-c", "--config", type=Path,
                        help="arquivo TOML (padrão: ./config.toml se existir)")
    parser.add_argument("-w", "--windowed", action="store_true",
                        help="janela em vez de tela cheia (desenvolvimento)")
    parser.add_argument("-v", "--verbose", action="store_true")
    return parser.parse_args(argv)


def _resident_memory_mb() -> float:
    """
    Memória residente do processo, em MB.

    Serve para achar vazamento de memória em uso 24/7. Lê /proc, então fora
    do Linux devolve 0.
    """
    try:
        with open("/proc/self/statm") as statm_file:
            resident_pages = int(statm_file.read().split()[1])
    except (OSError, ValueError, IndexError):
        return 0.0
    return resident_pages * os.sysconf("SC_PAGE_SIZE") / 2**20


def _fill_type(snapshot: Snapshot, selected_hex: str | None,
               lookup: TypeLookup | None) -> Snapshot:
    """
    Completa o tipo/matrícula da aeronave selecionada, se faltarem.

    Algumas fontes (OpenSky, readsb sem --db-file) não trazem o tipo. O
    `TypeLookup` consulta a internet em segundo plano; aqui só usamos o que
    ele já tiver em cache.

    O Snapshot é imutável: devolvemos uma cópia trocando só a aeronave
    selecionada e mantendo `updated`, para os caches da tela continuarem
    valendo.

    Parâmetros:
        snapshot: o snapshot atual da fonte de tráfego.
        selected_hex: endereço ICAO da aeronave selecionada (None = nenhuma).
        lookup: o consultor de tipos (None = desligado no config).

    Retorno:
        O snapshot original (nada a completar) ou uma cópia completada.
    """
    if lookup is None or not selected_hex:
        return snapshot
    selected = None
    for aircraft in snapshot.aircraft:
        if aircraft.hex == selected_hex:
            selected = aircraft
            break
    if selected is None or selected.type:
        return snapshot
    found = lookup.get(selected_hex)
    if not found or not found[0]:
        return snapshot
    found_type, found_registration = found[0], found[1]
    filled = replace(selected, type=found_type, reg=selected.reg or found_registration)
    new_list = []
    for aircraft in snapshot.aircraft:
        if aircraft is selected:
            new_list.append(filled)
        else:
            new_list.append(aircraft)
    return replace(snapshot, aircraft=new_list)


def _on_sigterm(signum, frame) -> None:
    """
    `systemctl stop` manda SIGTERM: vira SystemExit para passar pelo finally
    do laço (que para o som e as threads).
    """
    raise SystemExit(0)


class FrameTimer:
    """
    Mede o tempo de desenho de cada quadro e loga média e pior a cada minuto.

    Só aparece no log com -v (nível DEBUG). O alvo é um Raspberry Pi 3 B+,
    ~10x mais lento que um PC: acima de 1000/fps ms a tela perde quadros.
    """

    def __init__(self, target_fps: int):
        self.target_fps = target_fps
        self._reset(time.monotonic())

    def _reset(self, now: float) -> None:
        self.period_start = now
        self.total_s = 0.0
        self.worst_s = 0.0
        self.frame_count = 0

    def add(self, frame_start: float) -> None:
        """Registra um quadro que começou em `frame_start` (time.monotonic())."""
        duration = time.monotonic() - frame_start
        self.total_s += duration
        self.worst_s = max(self.worst_s, duration)
        self.frame_count += 1
        if frame_start - self.period_start < PERFORMANCE_LOG_INTERVAL_S:
            return
        log.debug("quadro: média %.1f ms, pior %.1f ms (%d quadros, alvo %.0f ms), "
                  "memória %.0f MB", 1000 * self.total_s / self.frame_count,
                  1000 * self.worst_s, self.frame_count, 1000 / self.target_fps,
                  _resident_memory_mb())
        self._reset(frame_start)


class RadarApp:
    """
    O aplicativo inteiro: estado da tela, threads de coleta e laço principal.

    Estado da tela que o usuário muda (e que é salvo no StateFile):
        range_index: posição em `cfg.radar.ranges_km` (0 = alcance mais curto).
        echo_on: eco de chuva visível.
        options: ajustes de exibição (símbolo, vetor, rastro, alerta, feixe,
            zonas), mudados na tela AJUSTES; ver options.py.
        source_name: fonte de tráfego escolhida ("readsb", "adsb.lol"...).

    Estado transitório (não é salvo):
        selected_hex: aeronave selecionada por toque.
        card_open: ficha (silhueta + dados) da selecionada aberta.
        clock_open: tela do despertador no lugar do radar.
        settings_open: tela de ajustes no lugar do radar.
    """

    def __init__(self, cfg: Config, windowed: bool):
        """
        Monta todas as peças. Fluxo: TLS -> mapas -> ajustes salvos -> fonte
        de tráfego -> pygame/tela -> eco e clima -> consulta de tipos.

        Parâmetros:
            cfg: configuração já carregada do TOML.
            windowed: força janela (opção -w), mesmo com fullscreen no TOML.
        """
        self.cfg = cfg
        configure_tls(cfg.network.strict_tls)       # antes de qualquer thread de rede
        station = cfg.station
        self.projection = AzimuthalEquidistant(station.lat, station.lon)
        self.map_layers = self._load_map_layers()

        self.state = StateFile(cfg.state.path or None)
        log.info("ajustes da tela em %s", self.state.path)
        self.ranges_km = cfg.radar.ranges_km
        self.range_index = self._restore_range_index()
        self.alarms = self._restore_alarm_clock()
        self.source_choices = SourceChoices(cfg)
        self.source_name = self._restore_source_name()
        self.fetcher = self.source_choices.fetcher(self.source_name)
        self.fetcher.start()

        self.screen = self._open_screen(windowed)
        # só em tela cheia o cursor começa escondido e passa a seguir o mouse
        self.auto_cursor = not pygame.mouse.get_visible()
        self.tracker = Tracker(self.projection, cfg.radar)
        self.renderer = Renderer(self.screen, cfg, self.projection, self.map_layers,
                                 self.tracker, AircraftDB(cfg.aircraft.db), self.alarms,
                                 echo_available=cfg.echo.enabled)
        self.alarm_buzzer = Buzzer(cfg.clock.sound, cfg.clock.volume)
        self.conflict_buzzer = self._create_conflict_buzzer()
        self.clock_screen = ClockScreen(self.screen, self.alarms)
        self.clock_open = False
        self.backlight = Backlight.find(cfg.display.backlight)
        self.settings_screen = SettingsScreen(self.screen,
                                              sweep_available=cfg.radar.sweep_s > 0,
                                              zones_available=bool(cfg.radar.inhibit),
                                              brightness_available=self.backlight is not None)
        self.settings_open = False
        self.sound_test_until = 0.0     # fim do "TESTAR" som (0 = sem teste)

        self.echo = self._start_echo()
        self.echo_on = self.echo is not None and self.state.get("echo_on", bool, True)
        self.weather = self._start_weather()
        self.type_lookup = TypeLookup() if cfg.aircraft.lookup else None

        self.selected_hex: str | None = None
        self.card_open = False
        self.options = self._restore_options()

        self.running = True
        self.fps_clock = pygame.time.Clock()
        self.frame_timer = FrameTimer(cfg.display.fps)
        self.watchdog = Watchdog()
        self.first_frame_drawn = False

    # ------------------------------------------------------------ montagem
    def _load_map_layers(self) -> list[MapLayer]:
        """
        Carrega as camadas GeoJSON do config. Arquivo ruim é pulado com
        erro no log: um mapa quebrado não pode impedir o radar de subir.
        """
        layers = []
        for layer_cfg in self.cfg.map_layers:
            try:
                layer = MapLayer.from_geojson(layer_cfg.path, layer_cfg.color, self.projection,
                                              layer_cfg.max_range_km, layer_cfg.label_range_km)
            except (OSError, ValueError, KeyError) as error:
                log.error("mapa %s ignorado: %s", layer_cfg.path, error)
                continue
            layers.append(layer)
            log.info("mapa %s: %d linhas, %d pontos", layer_cfg.path,
                     len(layer.lines_km), len(layer.points))
        return layers

    def _restore_range_index(self) -> int:
        """
        Alcance salvo na tela, se ainda estiver na lista; senão o do TOML.
        """
        saved_range = self.state.get("range_km", int)
        if saved_range in self.ranges_km:
            return self.ranges_km.index(saved_range)
        return self.ranges_km.index(self.cfg.radar.range_km)

    def _restore_alarm_clock(self) -> AlarmClock:
        """
        Recria o despertador com fuso, alarmes e soneca salvos na tela.
        """
        timezone_name = self.state.get("timezone", str, self.cfg.clock.timezone)
        if zone(timezone_name) is None:
            log.warning("fuso %r do arquivo de ajustes inválido, usando %s", timezone_name,
                        self.cfg.clock.timezone)
            timezone_name = self.cfg.clock.timezone
        saved_alarms = [Alarm.from_json(item) for item in self.state.get("alarms", list, [])]
        alarm_clock = AlarmClock(timezone_name, saved_alarms)
        alarm_clock.load_snooze(self.state.get("snooze", dict, None))
        return alarm_clock

    def _restore_options(self) -> DisplayOptions:
        """
        Ajustes de exibição salvos na tela, por cima dos padrões do TOML.

        Sem "display" salvo, migra o "atc_mode" das versões com ATC/RADAR.
        """
        defaults = DisplayOptions.from_config(self.cfg.radar)
        saved = self.state.get("display", dict, None)
        if saved is not None:
            return DisplayOptions.from_json(saved, defaults)
        old_atc_mode = self.state.get("atc_mode", bool, None)
        if old_atc_mode is None:
            return defaults
        log.info("ajustes: modo %s antigo convertido em ajustes individuais",
                 "ATC" if old_atc_mode else "RADAR")
        return DisplayOptions.from_old_atc_mode(old_atc_mode, defaults)

    def _restore_source_name(self) -> str:
        """
        Fonte de tráfego salva na tela, se ainda for oferecida (o OpenSky
        some se a credencial sair do TOML); senão a padrão do TOML.
        """
        default_name = self.source_choices.default
        saved_name = self.state.get("source", str, default_name)
        if saved_name in self.source_choices.names:
            return saved_name
        log.warning("fonte %r do arquivo de ajustes indisponível, usando %s",
                    saved_name, default_name)
        return default_name

    def _open_screen(self, windowed: bool) -> pygame.Surface:
        """
        Inicia o pygame e abre a tela (cheia ou janela).
        """
        # pygame.init() já abre o mixer: o buffer tem de vir antes (ver MIXER_BUFFER)
        pygame.mixer.pre_init(buffer=MIXER_BUFFER)
        pygame.init()
        display = self.cfg.display
        fullscreen = display.fullscreen and not windowed
        flags = 0
        if fullscreen:
            flags = pygame.FULLSCREEN
        screen = pygame.display.set_mode((display.width, display.height), flags)
        pygame.display.set_caption(self.cfg.station.name)
        if fullscreen:
            pygame.mouse.set_visible(False)     # tela de toque: sem cursor
        return screen

    def _create_conflict_buzzer(self) -> Buzzer | None:
        """
        Bipe do alerta de conflito; None quando o volume é 0 (desligado).
        """
        radar = self.cfg.radar
        if radar.conflict_volume <= 0:
            return None
        return Buzzer(radar.conflict_sound, radar.conflict_volume, CONFLICT_BEEPS,
                      "alerta de conflito")

    def _start_echo(self) -> EchoFetcher | None:
        """
        Inicia a thread do eco de chuva (RainViewer), se ligada no config.
        """
        if not self.cfg.echo.enabled:
            return None
        station = self.cfg.station
        echo = EchoFetcher(self.cfg.echo, station.lat, station.lon,
                           self.renderer.side, self.renderer.r_px, decode_png)
        echo.start()
        return echo

    def _start_weather(self) -> WeatherFetcher | None:
        """
        Inicia a thread do clima (METAR ou Open-Meteo), se ligada no config.
        """
        if not self.cfg.weather.enabled:
            return None
        station = self.cfg.station
        weather = WeatherFetcher(self.cfg.weather, station.lat, station.lon)
        weather.start()
        return weather

    # ------------------------------------------- ações (botões e teclas)
    def decrease_range(self) -> None:
        """
        Alcance menor (mais perto): botão e tecla "-". A lista de alcances é crescente.
        """
        self.range_index = max(self.range_index - 1, 0)

    def increase_range(self) -> None:
        """
        Alcance maior (mais longe): botão e tecla "+".
        """
        self.range_index = min(self.range_index + 1, len(self.ranges_km) - 1)

    def toggle_echo(self) -> None:
        if self.echo is not None:
            self.echo_on = not self.echo_on

    def toggle_card(self) -> None:
        """
        Abre/fecha a ficha; só faz sentido com uma aeronave selecionada.
        """
        if self.selected_hex:
            self.card_open = not self.card_open

    def open_settings(self) -> None:
        self.settings_open = True

    def open_clock(self) -> None:
        self.clock_open = True

    def switch_source(self) -> None:
        """
        Passa para a próxima fonte de tráfego da lista e a salva.
        """
        self.source_name = self.source_choices.next(self.source_name)
        log.info("fonte trocada na tela: %s", self.source_name)
        self._restart_fetcher()
        self.state.set("source", self.source_name)

    def _restart_fetcher(self) -> None:
        """
        Troca a thread de tráfego por uma nova, da fonte atual.

        A antiga termina sozinha após a leitura em curso (é daemon: se estiver
        presa na rede, simplesmente é abandonada).
        """
        self.fetcher.stop()
        self.fetcher = self.source_choices.fetcher(self.source_name)
        self.fetcher.start()

    def _stop_ringing(self) -> None:
        self.alarms.ringing = None
        self.alarm_buzzer.stop()

    def _snooze(self) -> None:
        """
        Silencia o alarme e o reagenda para daqui a `snooze_min` minutos (persistido).
        """
        self.alarms.snooze_now(self.cfg.clock.snooze_min)
        self.alarm_buzzer.stop()
        snooze_end = self.alarms.snooze[1].astimezone(self.alarms.tz)
        log.info("soneca até %s", f"{snooze_end:%H:%M}")
        self._save_clock()

    def _save_clock(self) -> None:
        """
        Grava fuso, alarmes e soneca no arquivo de ajustes.
        """
        self.state.set("timezone", self.alarms.tz_name)
        self.state.set("alarms", self.alarms.to_json())
        self.state.set("snooze", self.alarms.snooze_json())

    # ------------------------------------------------------------ eventos
    def _handle_event(self, event: pygame.event.Event) -> None:
        """
        Encaminha um evento para o tratador do contexto atual.

        A ordem importa: alarme tocando tem prioridade sobre tudo; depois a
        tela do relógio, se aberta; por fim teclado e toque no radar.
        """
        is_key = event.type == pygame.KEYDOWN
        is_click = event.type == pygame.MOUSEBUTTONDOWN
        if event.type == pygame.QUIT:
            self.running = False
        elif self.alarms.ringing is not None and (is_key or is_click):
            self._handle_event_while_ringing(event)
        elif self.clock_open:
            self._handle_clock_screen_event(event)
        elif self.settings_open:
            self._handle_settings_event(event)
        elif is_key:
            self._handle_radar_key(event.key)
        # toques já chegam aqui como clique (ver touch_as_click)
        elif is_click and event.button == LEFT_MOUSE_BUTTON:
            self._handle_radar_touch(event.pos)

    def _handle_event_while_ringing(self, event: pygame.event.Event) -> None:
        """
        Com o alarme tocando, toque/tecla só cuida do alarme.

        PARAR (botão, ou Esc/Enter/p) desliga; qualquer outro toque ou tecla
        dá soneca -- de propósito, para um toque sonolento nunca calar o
        alarme de vez. Com soneca desligada (snooze_min = 0) tudo para.
        """
        snooze_enabled = self.cfg.clock.snooze_min > 0
        if event.type == pygame.KEYDOWN:
            wants_stop = event.key in KEYS_STOP_ALARM
        else:
            wants_stop = ringing_hit(self.screen, snooze_enabled, event.pos) == "stop"
        if wants_stop or not snooze_enabled:
            self._stop_ringing()
        else:
            self._snooze()

    def _handle_clock_screen_event(self, event: pygame.event.Event) -> None:
        """
        Eventos da tela do despertador: fechar, testar som, ajustar alarmes.
        """
        if event.type == pygame.KEYDOWN and event.key == pygame.K_ESCAPE:
            self.clock_open = False
            return
        if event.type != pygame.MOUSEBUTTONDOWN or event.button != LEFT_MOUSE_BUTTON:
            return
        action = self.clock_screen.hit(event.pos)
        if action == "close":
            self.clock_open = False
        elif action == "test":
            self.alarm_buzzer.start()
            self.sound_test_until = time.monotonic() + SOUND_TEST_DURATION_S
        elif action and self.clock_screen.apply(action):
            self._save_clock()

    def _handle_settings_event(self, event: pygame.event.Event) -> None:
        """
        Eventos da tela de ajustes: Esc ou VOLTAR fecham; os outros botões
        trocam um ajuste (gravado no arquivo de estado pelo laço principal).
        """
        if event.type == pygame.KEYDOWN and event.key in (pygame.K_ESCAPE, pygame.K_a):
            self.settings_open = False
            return
        if event.type != pygame.MOUSEBUTTONDOWN or event.button != LEFT_MOUSE_BUTTON:
            return
        action = self.settings_screen.hit(event.pos)
        if action == "close":
            self.settings_open = False
        elif action:
            self.options = self.settings_screen.apply(self.options, action)

    def _handle_radar_key(self, key: int) -> None:
        """
        Atalhos de teclado da tela do radar (desenvolvimento no PC).
        """
        if key in KEYS_QUIT:
            self.running = False
        elif key in KEYS_RANGE_UP:
            self.increase_range()
        elif key in KEYS_RANGE_DOWN:
            self.decrease_range()
        elif key == pygame.K_r:
            self.toggle_echo()
        elif key == pygame.K_t:
            self.toggle_card()
        elif key == pygame.K_s:
            self.switch_source()
        elif key == pygame.K_a:
            self.open_settings()

    def _handle_radar_touch(self, position: tuple[int, int]) -> None:
        """
        Toque na tela do radar: botão do painel, aeronave ou área vazia.
        """
        hit = self.renderer.hit_test(position)
        if hit.kind == "button":
            self._press_button(hit.value)
        elif hit.kind == "aircraft":
            # tocar de novo na selecionada desmarca
            if hit.value == self.selected_hex:
                self.selected_hex = None
            else:
                self.selected_hex = hit.value
            self.card_open = False
        elif self.card_open:
            self.card_open = False      # toque no escopo fecha a ficha
        else:
            self.selected_hex = None

    def _press_button(self, button_name: str) -> None:
        """
        Executa a ação de um botão do painel (nome vindo de `Renderer.hit_test`).
        """
        actions = {
            "range_down": self.decrease_range,
            "range_up": self.increase_range,
            "echo": self.toggle_echo,
            "card": self.toggle_card,
            "clock": self.open_clock,
            "source": self.switch_source,
            "settings": self.open_settings,
        }
        action = actions.get(button_name)
        if action is not None:
            action()

    # ------------------------------------------------ atualização por quadro
    def _update_alarms(self, now: float) -> None:
        """
        Dispara o alarme na hora certa, para após `ring_s` e encerra o
        teste de som.

        Parâmetros:
            now: instante atual em time.monotonic().
        """
        fired_index = self.alarms.check()
        if fired_index is not None:
            fired = self.alarms.alarms[fired_index]
            log.info("alarme %d tocando (%02d:%02d)", fired_index + 1, fired.hour, fired.minute)
            self.alarms.ringing = fired_index
            self.alarms.ring_since = now
            self.alarm_buzzer.start()
            self._save_clock()          # alarme "uma vez" acabou de se desligar
        elif self.alarms.ringing is not None and now - self.alarms.ring_since > self.cfg.clock.ring_s:
            self._stop_ringing()

        if self.sound_test_until and now > self.sound_test_until:
            self.sound_test_until = 0.0
            if self.alarms.ringing is None:
                self.alarm_buzzer.stop()

    def _replace_fetcher_if_stuck(self, now: float) -> None:
        """
        Thread de tráfego morta ou presa numa leitura: abandona e cria outra.
        """
        if not self.fetcher.stuck(now):
            return
        log.error("busca de tráfego (%s) parada há %.0f s; reiniciando",
                  self.source_name, now - self.fetcher.beat)
        self._restart_fetcher()

    def _update_traffic(self) -> Snapshot:
        """
        Lê o snapshot de tráfego mais recente e atualiza o rastreador.

        O Tracker só trabalha quando o snapshot é novo. O bipe de conflito
        toca uma vez por par novo, e só com o ALERTA em COR+SOM, o radar na
        tela (relógio e ajustes fechados) e sem alarme tocando.

        Retorno:
            O snapshot a desenhar (com o tipo da selecionada completado).
        """
        snapshot = self.fetcher.snapshot()
        has_new_data = self.tracker.update(snapshot)
        if has_new_data and self.tracker.new_pairs and self._conflict_beep_allowed():
            pair_names = ", ".join(f"{first}/{second}" for first, second in self.tracker.new_pairs)
            log.info("conflito novo: %s", pair_names)
            self.conflict_buzzer.play()

        if self.selected_hex and not self._is_in_snapshot(snapshot, self.selected_hex):
            self.selected_hex = None    # saiu da cobertura
            self.card_open = False
        return _fill_type(snapshot, self.selected_hex, self.type_lookup)

    def _conflict_beep_allowed(self) -> bool:
        return (self.conflict_buzzer is not None and self.options.conflict_sound
                and not self.clock_open and not self.settings_open
                and self.alarms.ringing is None)

    @staticmethod
    def _is_in_snapshot(snapshot: Snapshot, aircraft_hex: str) -> bool:
        for aircraft in snapshot.aircraft:
            if aircraft.hex == aircraft_hex:
                return True
        return False

    def _save_screen_settings(self) -> None:
        """
        Grava os ajustes da tela e aplica o brilho. `StateFile.set` e
        `Backlight.set_percent` só escrevem quando o valor muda, então chamar
        a cada quadro é barato.
        """
        self.state.set("range_km", self.ranges_km[self.range_index])
        if self.echo is not None:
            self.state.set("echo_on", self.echo_on)
        self.state.set("display", self.options.to_json())
        if self.backlight is not None:
            self.backlight.set_percent(self.options.brightness)   # só escreve se mudou

    # ------------------------------------------------------------ desenho
    def _draw(self, snapshot: Snapshot, now: float) -> None:
        """
        Desenha o quadro: tela do relógio ou radar, e a faixa do alarme
        tocando por cima de qualquer uma das duas.
        """
        range_km = float(self.ranges_km[self.range_index])
        if self.echo is not None:
            # None pausa os downloads do eco enquanto ele está desligado
            if self.echo_on:
                self.echo.request(range_km)
            else:
                self.echo.request(None)

        if self.clock_open:
            self.clock_screen.draw(self.alarm_buzzer.sound is not None)
        elif self.settings_open:
            self.settings_screen.draw(self.options, self.conflict_buzzer is not None)
        else:
            self._draw_radar(snapshot, range_km, now)
        draw_ringing(self.screen, self.alarms, now, self.cfg.clock.snooze_min)
        pygame.display.flip()

    def _draw_radar(self, snapshot: Snapshot, range_km: float, now: float) -> None:
        """
        Desenha o escopo e o painel, com eco de chuva e clima quando ativos.
        """
        weather_now = None
        if self.weather is not None:
            weather_now = self.weather.current()
        if self.echo is None:
            self.renderer.draw(snapshot, self.selected_hex, range_km, now,
                               card=self.card_open, options=self.options, weather=weather_now)
            return
        self.renderer.draw(snapshot, self.selected_hex, range_km, now,
                           self.echo_on, self.echo.frames(), self.echo.error,
                           card=self.card_open, options=self.options, weather=weather_now)

    def _notify_watchdog(self, now: float) -> None:
        """
        systemd: READY após o primeiro quadro, depois "ainda vivo" a cada
        volta. Se o laço parar de chegar aqui, o systemd reinicia o app.
        """
        if not self.first_frame_drawn:
            self.watchdog.ready()
            self.first_frame_drawn = True
        self.watchdog.kick(now)

    # ------------------------------------------------------- laço principal
    def run(self) -> None:
        """
        Roda o laço principal até o usuário sair; sempre encerra as threads.
        """
        try:
            while self.running:
                self._run_one_frame()
        finally:
            self._shutdown()

    def _run_one_frame(self) -> None:
        """
        Uma volta do laço: eventos -> atualização -> ajustes -> desenho.
        """
        started = time.monotonic()
        screen_size = self.screen.get_size()
        for event in pygame.event.get():
            self._update_cursor(event)
            event = touch_as_click(event, screen_size)
            if event is not None:
                self._handle_event(event)

        now = time.monotonic()
        self._update_alarms(now)
        self._replace_fetcher_if_stuck(now)
        snapshot = self._update_traffic()
        self._save_screen_settings()
        self._draw(snapshot, now)
        self._notify_watchdog(now)
        self.frame_timer.add(now)
        self._wait_next_frame(started)

    def _wait_next_frame(self, started: float) -> None:
        """
        Espera até a hora do próximo quadro (``[display] fps``).

        No kmsdrm o SDL só move o cursor do mouse quando lê os eventos. Se o
        laço dormisse direto até o próximo quadro, o cursor andaria aos saltos,
        15 vezes por segundo. Por isso, com o cursor visível em tela cheia, a
        espera é picada em fatias de CURSOR_PUMP_S com ``pygame.event.pump()``
        entre elas; os eventos continuam na fila para o próximo quadro. Sem
        cursor, a espera é a de sempre.

        Parâmetros:
            started: instante (``time.monotonic``) em que o quadro começou.
        """
        fps = self.cfg.display.fps
        if not self.auto_cursor or not pygame.mouse.get_visible():
            self.fps_clock.tick(fps)
            return
        deadline = started + 1.0 / fps
        while (left := deadline - time.monotonic()) > 0:
            pygame.event.pump()
            time.sleep(min(left, CURSOR_PUMP_S))
        self.fps_clock.tick()           # só para o relógio contar este quadro

    def _update_cursor(self, event: pygame.event.Event) -> None:
        """
        Mostra o cursor quando o mouse se mexe e o esconde no próximo toque.

        Só vale em tela cheia (em janela o cursor fica sempre visível), e só
        chama o pygame quando a visibilidade muda de fato.
        """
        if not self.auto_cursor:
            return
        wanted = cursor_wanted(event)
        if wanted is None or wanted == pygame.mouse.get_visible():
            return
        pygame.mouse.set_visible(wanted)

    def _shutdown(self) -> None:
        """
        Avisa o systemd, cala o som, devolve o brilho original e para as
        threads de coleta.
        """
        self.watchdog.stopping()
        self.alarm_buzzer.stop()
        if self.backlight is not None:
            self.backlight.restore()        # sem o app, a tela não fica escura
        self.fetcher.stop()
        if self.echo is not None:
            self.echo.stop()
        if self.weather is not None:
            self.weather.stop()
        if self.type_lookup is not None:
            self.type_lookup.stop()
        pygame.quit()


def main(argv: list[str] | None = None) -> None:
    """
    Entrada do comando `radClock`: lê opções e config e roda o app.

    Parâmetros:
        argv: argumentos da linha de comando (None = os de `sys.argv`).
    """
    args = _parse_args(argv)
    log_level = logging.INFO
    if args.verbose:
        log_level = logging.DEBUG
    logging.basicConfig(level=log_level,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    config_path = args.config
    if config_path is None and Path("config.toml").exists():
        config_path = Path("config.toml")
    cfg = config_module.load(config_path)

    signal.signal(signal.SIGTERM, _on_sigterm)
    RadarApp(cfg, args.windowed).run()


if __name__ == "__main__":
    main()
