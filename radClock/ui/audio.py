"""Sons da tela: o despertador e o alerta de conflito.

``Buzzer`` toca um arquivo de som ou uma sequência de bipes gerada na hora.
Fica num módulo próprio porque é usado por duas partes que não têm nada a ver
uma com a outra: o despertador (``alarm``) e o alerta de conflito (``tracks``),
ambos ligados em ``app.py``.
"""
from __future__ import annotations

import logging

import numpy as np
import pygame

log = logging.getLogger(__name__)

# Padrões de bipe: lista de (frequência em Hz, duração em segundos); 0 Hz = silêncio.
ALARM_BEEPS = [(880, 0.15), (0, 0.1), (880, 0.15), (0, 0.6)]     # bip-bip, 1 s em loop
CONFLICT_BEEPS = [(1000, 0.12), (750, 0.12)] * 3                    # bitonal, diferente do alarme
BEEP_RAMP_S = 0.005         # subida/descida de 5 ms nas pontas: sem estalo
BEEP_LOUDNESS = 0.8         # fração do volume máximo do formato PCM
# O mixer fica aberto o tempo todo, tocando silêncio. Com o buffer padrão
# (512 amostras, ~12 ms) qualquer aperto de CPU no Pi 3 (subtensão, carga)
# esvazia o buffer e o ALSA escreve "underrun occurred" centenas de vezes por
# minuto no journal. 4096 amostras (~93 ms) aguentaram 4 núcleos ocupados sem
# nenhum underrun; um alarme 0,1 s atrasado não faz diferença.
MIXER_BUFFER = 4096


class Buzzer:
    """Som pelo ``pygame.mixer``: arquivo (wav/ogg) ou bipes gerados.

    Sem dispositivo de áudio (driver dummy, Pi sem saída) fica mudo
    (``sound`` = None) e o alarme é só visual; nunca derruba o app.
    """

    def __init__(self, sound_path: str = "", volume: float = 0.8,
                 beeps: list[tuple[float, float]] = ALARM_BEEPS, what: str = "alarme"):
        """
        Parâmetros:
            sound_path: arquivo wav/ogg; "" = gera os bipes de ``beeps``.
            volume: 0 a 1.
            beeps: padrão de bipes (ver ``ALARM_BEEPS``).
            what: nome do som, só para a mensagem de log.
        """
        self.sound: pygame.mixer.Sound | None = None
        try:
            if pygame.mixer.get_init() is None:
                pygame.mixer.init(buffer=MIXER_BUFFER)
            if sound_path:
                self.sound = pygame.mixer.Sound(sound_path)
            else:
                self.sound = self._make_beeps(beeps)
            self.sound.set_volume(volume)
        except (pygame.error, FileNotFoundError, ValueError) as error:
            log.warning("%s sem som: %s", what, error)
            self.sound = None

    @staticmethod
    def _make_beeps(beeps: list[tuple[float, float]]) -> pygame.mixer.Sound:
        """Gera o som dos bipes no formato atual do mixer.

        Fluxo: uma senoide (ou silêncio) por trecho, com rampa nas pontas,
        concatenadas e convertidas para PCM inteiro com o número de canais
        do mixer.
        """
        sample_rate, sample_size, channels = pygame.mixer.get_init()
        pieces = []
        for frequency_hz, duration_s in beeps:
            times = np.arange(int(sample_rate * duration_s)) / sample_rate
            if frequency_hz == 0:
                pieces.append(np.zeros(len(times)))
                continue
            distance_to_edge = np.minimum(times, duration_s - times)
            ramp = np.minimum(1, distance_to_edge / BEEP_RAMP_S)
            pieces.append(np.sin(2 * np.pi * frequency_hz * times) * ramp)
        wave = np.concatenate(pieces)

        bits = abs(sample_size)                 # negativo = formato com sinal
        max_amplitude = 2 ** (bits - 1) - 1
        sample_type = np.int16 if bits == 16 else np.int32
        pcm = (wave * max_amplitude * BEEP_LOUDNESS).astype(sample_type)
        if channels > 1:
            pcm = np.repeat(pcm[:, None], channels, axis=1)
        return pygame.sndarray.make_sound(np.ascontiguousarray(pcm))

    def start(self) -> None:
        """Toca em loop até ``stop()`` (o alarme)."""
        if self.sound is not None:
            self.sound.play(loops=-1)

    def play(self) -> None:
        """Toca uma vez (o alerta de conflito)."""
        if self.sound is not None:
            self.sound.play()

    def stop(self) -> None:
        """Para o som."""
        if self.sound is not None:
            self.sound.stop()
