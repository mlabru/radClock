"""Avisos ao systemd (sd_notify): "pronto" e "ainda vivo" para o watchdog.

Com `Type=notify` e `WatchdogSec=` no serviço, o systemd mata e reinicia o app
se o laço principal parar de avisar -- tela travada no KMS/DRM, laço preso
numa chamada que não volta. Erro que derruba o processo já é coberto por
`Restart=always`; o watchdog cobre o caso pior, o do app vivo e parado.

Sem $NOTIFY_SOCKET (PC, rodando à mão) tudo vira no-op. O protocolo é um
datagrama de texto num socket Unix; não vale a pena depender do pacote
python-systemd por isso.
"""
from __future__ import annotations

import logging
import os
import socket

log = logging.getLogger(__name__)

MICROSECONDS_PER_SECOND = 1e6


class Watchdog:
    """Cliente mínimo do protocolo sd_notify.

    Uso no laço principal: `ready()` depois do primeiro quadro, `kick(agora)`
    a cada volta e `stopping()` ao sair. Falhas de envio só geram aviso no
    log; o watchdog nunca derruba o app.

    Atributos:
        interval_s: de quanto em quanto tempo `kick` avisa (metade do
            WatchdogSec), ou None quando o systemd não pediu watchdog.
    """

    def __init__(self, environ: dict[str, str] | None = None):
        """Conecta ao socket do systemd, se houver.

        Parâmetros:
            environ: variáveis de ambiente (None = os.environ). Os testes
                passam um dicionário próprio.
        """
        if environ is None:
            environ = os.environ
        self._socket: socket.socket | None = None
        self.interval_s: float | None = None
        self._next_kick_at = 0.0

        address = environ.get("NOTIFY_SOCKET", "")
        if not address:
            return
        self._socket = self._connect(address)
        if self._socket is None:
            return
        self.interval_s = self._kick_interval(environ)

    @staticmethod
    def _connect(address: str) -> socket.socket | None:
        """Abre o socket Unix de datagrama do systemd (None se falhar)."""
        if address.startswith("@"):         # socket abstrato do Linux
            address = "\0" + address[1:]
        try:
            notify_socket = socket.socket(socket.AF_UNIX,
                                          socket.SOCK_DGRAM | socket.SOCK_CLOEXEC)
            notify_socket.connect(address)
        except OSError as error:
            log.warning("sd_notify indisponível: %s", error)
            return None
        return notify_socket

    @staticmethod
    def _kick_interval(environ: dict[str, str]) -> float | None:
        """Metade do WatchdogSec, ou None se o watchdog não é para este processo.

        WATCHDOG_USEC é o prazo em microssegundos; WATCHDOG_PID, quando
        presente, diz qual processo deve avisar.
        """
        timeout_us = environ.get("WATCHDOG_USEC", "")
        watchdog_pid = environ.get("WATCHDOG_PID", "")
        if not timeout_us.isdigit() or int(timeout_us) <= 0:
            return None
        if watchdog_pid and watchdog_pid != str(os.getpid()):
            return None
        return int(timeout_us) / MICROSECONDS_PER_SECOND / 2

    def _send(self, message: str) -> None:
        """Envia `message` ao systemd; sem socket (fora do systemd) não faz nada."""
        if self._socket is None:
            return
        try:
            self._socket.send(message.encode())
        except OSError as error:
            log.warning("sd_notify falhou: %s", error)

    def ready(self) -> None:
        """Avisa que o app subiu e desenhou o primeiro quadro."""
        self._send("READY=1")

    def kick(self, now: float) -> None:
        """Chamado a cada volta do laço; só manda o aviso quando está na hora.

        Parâmetros:
            now: instante atual em time.monotonic().
        """
        if self.interval_s is None or now < self._next_kick_at:
            return
        self._next_kick_at = now + self.interval_s
        self._send("WATCHDOG=1")

    def stopping(self) -> None:
        """Avisa que o app está encerrando de propósito."""
        self._send("STOPPING=1")
