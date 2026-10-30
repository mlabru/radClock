"""Ajustes feitos na tela, guardados num JSON próprio, separado do TOML.

O TOML é a configuração de instalação (editada à mão, chave desconhecida é
erro); este arquivo é escrito pelo app e só guarda o que o usuário muda na
tela. Na partida ele vale por cima do TOML. Valor inválido aqui é ignorado
com aviso e chave desconhecida é mantida sem uso: um arquivo de uma versão
mais nova (ou corrompido) nunca pode impedir a tela de subir.

A gravação é atômica (tmp + rename) e falhas só geram log: com o cartão em
somente leitura (overlayfs) o app segue funcionando, só não lembra.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)

STATE_FILE_NAME = "state.json"


def default_path() -> Path:
    """Onde guardar os ajustes quando o config não diz.

    Ordem de preferência:
        1. $STATE_DIRECTORY (o systemd com StateDirectory=radClock exporta
           /var/lib/radClock; pode ser uma lista separada por ":").
        2. $XDG_STATE_HOME/radClock.
        3. ~/.local/state/radClock.

    Retorno:
        Caminho completo do arquivo state.json.
    """
    systemd_state_directory = os.environ.get("STATE_DIRECTORY")
    if systemd_state_directory:
        first_directory = systemd_state_directory.split(":")[0]
        return Path(first_directory) / STATE_FILE_NAME
    base = os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state"
    return Path(base) / "radClock" / STATE_FILE_NAME


class StateFile:
    """Dicionário persistente e tolerante a erro dos ajustes da tela.

    Atributos:
        path: caminho do arquivo JSON.
        data: conteúdo atual (sempre um dict; vazio se o arquivo não existe
            ou está ilegível).
    """

    def __init__(self, path: str | Path | None = None):
        """Lê o arquivo, se existir. Nunca levanta exceção.

        Parâmetros:
            path: caminho do JSON (None ou vazio = `default_path()`).
        """
        if path:
            self.path = Path(path)
        else:
            self.path = default_path()
        self.data: dict = self._read()

    def _read(self) -> dict:
        """Lê o arquivo de ajustes; arquivo ausente ou inválido vira {} (com aviso)."""
        try:
            with open(self.path, encoding="utf-8") as state_file:
                content = json.load(state_file)
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as error:
            log.warning("ajustes %s ilegível, começando do zero: %s", self.path, error)
            return {}
        if not isinstance(content, dict):
            log.warning("ajustes %s: esperado um objeto JSON, ignorado", self.path)
            return {}
        return content

    def get(self, key: str, kind: type, default=None):
        """Valor salvo, se existir e for do tipo esperado.

        Parâmetros:
            key: nome do ajuste (ex.: "range_km").
            kind: tipo esperado (int, bool, str, list, dict).
            default: o que devolver se faltar ou for inválido.

        Retorno:
            O valor salvo ou `default`. Valor do tipo errado gera aviso no log.
        """
        value = self.data.get(key)
        if value is None:
            return default
        # bool é subclasse de int: `true` não pode passar por alcance
        is_bool_posing_as_int = kind is int and isinstance(value, bool)
        if not isinstance(value, kind) or is_bool_posing_as_int:
            log.warning("ajustes: %s = %r inválido, ignorado", key, value)
            return default
        return value

    def set(self, key: str, value) -> None:
        """Guarda um ajuste e grava o arquivo, só se o valor mudou.

        Por só gravar na mudança, pode ser chamado a cada quadro sem gastar
        o cartão SD.
        """
        if self.data.get(key) == value:
            return
        self.data[key] = value
        self._save()

    def _save(self) -> None:
        """Grava atômico: escreve num .tmp, força ao disco e renomeia por cima.

        Assim um corte de energia deixa o arquivo antigo ou o novo, nunca
        meio arquivo.
        """
        temporary_path = self.path.with_suffix(".tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(temporary_path, "w", encoding="utf-8") as temporary_file:
                json.dump(self.data, temporary_file, indent=2, sort_keys=True)
                temporary_file.flush()
                os.fsync(temporary_file.fileno())
            os.replace(temporary_path, self.path)
        except OSError as error:
            log.error("ajustes: não foi possível gravar %s: %s", self.path, error)
