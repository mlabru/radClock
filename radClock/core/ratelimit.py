"""Limitador de requisições por minuto (balde de fichas).

Hoje só o eco de chuva (``echo``) usa, para ficar abaixo do limite da
RainViewer. Fica num módulo próprio porque não tem nada de chuva: serve para
qualquer API com limite de taxa.
"""
from __future__ import annotations

import threading
import time


class RateLimiter:
    """Balde de fichas: no máximo `per_min` pedidos por minuto, rajada de `burst`.

    O balde enche continuamente (per_min/60 fichas por segundo) até `burst`;
    cada pedido gasta uma ficha. Seguro para várias threads.
    """

    def __init__(self, per_min: float, burst: int, clock=time.monotonic):
        self.rate = per_min / 60.0          # fichas por segundo
        self.burst = burst
        self.clock = clock
        self._tokens = float(burst)
        self._last_refill = clock()
        self._lock = threading.Lock()

    def try_acquire(self) -> float:
        """Tenta pegar uma ficha.

        Retorno:
            0 se pegou; senão quantos segundos esperar até haver uma.
        """
        with self._lock:
            now = self.clock()
            refill = (now - self._last_refill) * self.rate
            self._tokens = min(self.burst, self._tokens + refill)
            self._last_refill = now
            if self._tokens >= 1:
                self._tokens -= 1
                return 0.0
            return (1 - self._tokens) / self.rate

    def acquire(self, stop: threading.Event) -> bool:
        """Espera até pegar uma ficha.

        Retorno:
            True se pegou; False se `stop` foi sinalizado durante a espera.
        """
        while True:
            wait_s = self.try_acquire()
            if wait_s <= 0:
                return True
            if stop.wait(wait_s):
                return False
