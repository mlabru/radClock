"""
Tela do radar em pygame-ce: laço principal, desenho, toque e som.

Este pacote só lê o que o `radClock.core` publica (Snapshot, quadros de eco,
clima, conflitos) e desenha; rede e regras ficam no núcleo. A dependência é
de mão única: `ui` importa `core`, nunca o contrário.
"""
