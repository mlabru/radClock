"""
A parte pygame do eco de chuva.

O `radClock.core.echo` não importa pygame: recebe um decodificador de PNG e
publica cada quadro como array numpy RGBA. Este módulo fornece esse
decodificador (pygame lê PNG; a biblioteca padrão não) e converte os arrays
em Surface para o `Renderer`.

A conversão array -> Surface copia ~1 MB (480 x 480 x 4) e por isso é feita
uma vez por quadro, e não a cada desenho: as Surfaces ficam em cache pela
`version` do quadro enquanto ele está na janela de animação.
"""
from __future__ import annotations

import io

import numpy as np
import pygame

from radClock.core.echo import EchoFrame
from radClock.core.net import SourceError


def decode_png(data: bytes) -> np.ndarray:
    """Bytes PNG -> array RGBA (altura, largura, 4). SourceError se inválido."""
    try:
        surface = pygame.image.load(io.BytesIO(data), "tile.png")
    except pygame.error as e:
        raise SourceError(f"PNG inválido: {e}") from e
    width, height = surface.get_size()
    rgba = pygame.image.tobytes(surface, "RGBA")
    return np.frombuffer(rgba, np.uint8).reshape(height, width, 4)


class EchoSurfaces:
    """Cache de Surfaces dos quadros de eco, por `EchoFrame.version`.

    Uso pelo Renderer, a cada desenho:
      1. `keep_only(quadros)` com os quadros que ainda podem aparecer, para
         soltar a memória dos que saíram da janela;
      2. `surface(quadro)` para o quadro que vai ser mostrado.
    """

    def __init__(self):
        self._by_version: dict[int, pygame.Surface] = {}

    def surface(self, frame: EchoFrame) -> pygame.Surface:
        """Surface do quadro, convertida só na primeira vez."""
        cached = self._by_version.get(frame.version)
        if cached is not None:
            return cached
        height, width = frame.image.shape[:2]
        converted = pygame.image.frombytes(frame.image.tobytes(), (width, height), "RGBA")
        self._by_version[frame.version] = converted
        return converted

    def keep_only(self, frames: list[EchoFrame]) -> None:
        """Descarta as Surfaces de quadros que não estão em `frames`."""
        wanted = {frame.version for frame in frames}
        for version in list(self._by_version):
            if version not in wanted:
                del self._by_version[version]
