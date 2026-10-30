"""Brilho da tela pelo backlight do kernel (/sys/class/backlight).

A tela DSI do Pi aparece como /sys/class/backlight/<nome>/ com dois arquivos
que importam: ``max_brightness`` (o máximo, 255 na Touch Display) e
``brightness`` (o valor em vigor). Escrever em ``brightness`` muda o brilho na
hora; o usuário precisa estar no grupo ``video`` (o serviço já está, em
``SupplementaryGroups``).

O brilho é escolhido em porcentagem na tela AJUSTES (``DisplayOptions.brightness``).
Sem backlight (PC, HDMI) ou sem permissão, tudo vira no-op com um aviso no
log: brilho é conforto, nunca motivo para o radar parar.
"""
from __future__ import annotations

import logging
from pathlib import Path

log = logging.getLogger(__name__)

SYSFS_BACKLIGHT = Path("/sys/class/backlight")
BACKLIGHT_AUTO = "auto"         # [display] backlight: o primeiro de SYSFS_BACKLIGHT
MIN_LEVEL = 1                   # 0 apaga a tela de vez em muitos painéis: nunca escrever 0


class Backlight:
    """Um dispositivo de backlight do kernel.

    Uso: ``Backlight.find(cfg.display.backlight)`` uma vez; depois
    ``set_percent`` a cada quadro (só escreve quando o valor muda) e
    ``restore`` ao sair, para não deixar a tela escura sem o app.
    """

    def __init__(self, folder: Path):
        """Lê o máximo e o valor atual (este fica guardado para ``restore``).

        Parâmetros:
            folder: pasta do dispositivo, ex. /sys/class/backlight/10-0045.

        Erros:
            OSError ou ValueError se a pasta não tem os arquivos esperados.
        """
        self.folder = folder
        self.max_level = int((folder / "max_brightness").read_text())
        self.original_level = int((folder / "brightness").read_text())
        self._written_level = self.original_level
        self._failed = False            # já avisou no log que não consegue escrever

    @classmethod
    def find(cls, setting: str, sysfs: Path = SYSFS_BACKLIGHT) -> Backlight | None:
        """Backlight a controlar conforme ``[display] backlight``.

        Parâmetros:
            setting: "auto" (o primeiro dispositivo em `sysfs`), "" (não mexer
                no brilho) ou o caminho da pasta do dispositivo.
            sysfs: onde procurar no modo "auto" (os testes passam uma pasta falsa).

        Retorno:
            O Backlight, ou None se não há (ou não se quer) controle de brilho.
        """
        if not setting:
            return None
        if setting == BACKLIGHT_AUTO:
            candidates = sorted(sysfs.glob("*")) if sysfs.is_dir() else []
            if not candidates:
                log.info("brilho: nenhum backlight em %s, ajuste BRILHO oculto", sysfs)
                return None
            folder = candidates[0]
        else:
            folder = Path(setting)
        try:
            backlight = cls(folder)
        except (OSError, ValueError) as error:
            log.warning("brilho: backlight %s ilegível (%s), ajuste BRILHO oculto", folder, error)
            return None
        log.info("brilho: %s (máximo %d, atual %d)", folder, backlight.max_level,
                 backlight.original_level)
        return backlight

    def level_for(self, percent: int) -> int:
        """Valor do kernel para `percent` % do máximo (nunca abaixo de MIN_LEVEL)."""
        return max(MIN_LEVEL, round(self.max_level * percent / 100))

    def set_percent(self, percent: int) -> None:
        """Ajusta o brilho para `percent` % do máximo.

        Barato para chamar a cada quadro: só escreve no arquivo quando o valor
        muda. Erro de escrita (sem permissão) gera um aviso só, e o app segue.
        """
        self._write(self.level_for(percent))

    def restore(self) -> None:
        """Volta ao brilho que estava antes do app abrir."""
        self._write(self.original_level)

    def _write(self, level: int) -> None:
        if level == self._written_level or self._failed:
            return
        try:
            (self.folder / "brightness").write_text(f"{level}\n")
        except OSError as error:
            self._failed = True
            log.warning("brilho: sem permissão para mudar %s (%s); o usuário precisa "
                        "estar no grupo video", self.folder, error)
            return
        self._written_level = level
