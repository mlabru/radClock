"""Ajustes de exibição do tráfego, escolhidos na tela AJUSTES.

Antes havia dois pacotes fixos, ATC e RADAR, que ligavam e desligavam tudo de
uma vez. Agora cada item é independente:

- ``symbol``: quadrado (estilo tela de controle) ou avião girado no rumo;
- ``vector_min``: vetor de velocidade até a posição daqui a N min (0 desliga);
- ``trail_s``: rastro com as posições dos últimos N s (0 desliga);
- ``alert``: alerta de conflito com cor e som, só cor, ou desligado;
- ``sweep``: feixe de varredura (e o brilho dos alvos que vem com ele);
- ``zones``: círculos das zonas de inibição de conflito;
- ``brightness``: brilho da tela em % (só com backlight, ver backlight.py).

O alerta de conflito é um item só, de propósito: cor e som separados
permitiriam o alarme tocar sem a tela mostrar o motivo. Desligado, o painel
avisa "ALERTA DESL" em laranja. Os critérios de segurança (separações
mínimas, horizonte, zonas) continuam só no TOML.

Os padrões vêm de ``[radar]`` no TOML; o que se muda na tela vai para o
arquivo de estado (chave "display", um dicionário) e vale por cima. Este
módulo é lógica pura (sem pygame).
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, fields, replace

from radClock.core.config import RadarCfg

SYMBOL_SQUARE = "square"
SYMBOL_PLANE = "plane"
SYMBOLS = (SYMBOL_SQUARE, SYMBOL_PLANE)

ALERT_SOUND = "sound"       # cor + som
ALERT_COLOR = "color"       # só cor
ALERT_OFF = "off"
ALERTS = (ALERT_SOUND, ALERT_COLOR, ALERT_OFF)

# Opções oferecidas na tela (o TOML pode dar outro valor como padrão)
VECTOR_CHOICES_MIN = (0.0, 1.0, 2.0)
TRAIL_CHOICES_S = (0.0, 60.0, 120.0)
BRIGHTNESS_CHOICES = (100, 40, 10)      # % do máximo do backlight


@dataclass(frozen=True)
class DisplayOptions:
    """Os ajustes de exibição em vigor. Imutável: mudar = criar outro.

    Por ser imutável (e comparável), entra direto na chave do cache de
    posição dos rótulos do Renderer.
    """

    symbol: str = SYMBOL_SQUARE
    vector_min: float = 2.0
    trail_s: float = 60.0
    alert: str = ALERT_SOUND
    sweep: bool = True
    zones: bool = True
    brightness: int = 100       # % do máximo; não vem do TOML (é conforto, não critério)

    @property
    def show_conflicts(self) -> bool:
        """Conflitos aparecem na tela (cor, linha e painel)?"""
        return self.alert != ALERT_OFF

    @property
    def conflict_sound(self) -> bool:
        """Conflito novo toca o bipe?"""
        return self.alert == ALERT_SOUND

    @classmethod
    def from_config(cls, radar: RadarCfg) -> DisplayOptions:
        """Os padrões de fábrica, tirados da seção [radar] do TOML."""
        return cls(symbol=radar.symbol, vector_min=radar.vector_min, trail_s=radar.trail_s,
                   alert=radar.alert, sweep=radar.sweep, zones=radar.zones)

    def to_json(self) -> dict:
        """Dicionário para o arquivo de estado."""
        return asdict(self)

    @classmethod
    def from_json(cls, data: dict, defaults: DisplayOptions) -> DisplayOptions:
        """Ajustes salvos no arquivo de estado, por cima dos padrões.

        Leniente como o resto do arquivo de estado: um valor ausente ou
        inválido (editado à mão, versão antiga) fica com o padrão, sem erro.

        Parâmetros:
            data: o dicionário salvo (chave "display").
            defaults: os padrões do TOML (``from_config``).
        """
        values = {}
        for option_field in fields(cls):
            value = data.get(option_field.name)
            if _is_valid(option_field.name, value):
                values[option_field.name] = value
        return replace(defaults, **values)

    @classmethod
    def from_old_atc_mode(cls, atc_mode: bool, defaults: DisplayOptions) -> DisplayOptions:
        """Migra o antigo "atc_mode" do arquivo de estado (versões com ATC/RADAR).

        ATC = os padrões; RADAR = avião girado, sem vetor, sem rastro e sem
        zonas. O ALERTA fica com o padrão de propósito: o modo RADAR escondia
        os conflitos, e isso não deve passar adiante sem alguém escolher.
        """
        if atc_mode:
            return defaults
        return replace(defaults, symbol=SYMBOL_PLANE, vector_min=0.0, trail_s=0.0, zones=False)


def _is_valid(name: str, value) -> bool:
    """O valor salvo serve para o ajuste `name`? (tipo e opções permitidas)"""
    if name == "symbol":
        return value in SYMBOLS
    if name == "alert":
        return value in ALERTS
    if name in ("sweep", "zones"):
        return isinstance(value, bool)
    if name == "brightness":
        return isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= 100
    # vector_min e trail_s: número >= 0 (bool é subclasse de int: recusado)
    is_number = isinstance(value, (int, float)) and not isinstance(value, bool)
    return is_number and value >= 0
