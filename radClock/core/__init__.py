"""
Núcleo do radar: dados, regras e rede, sem nada de tela.

Aqui ficam as fontes de tráfego, o eco de chuva (como array numpy), o clima,
o tracker (rastros e conflitos), os alarmes, a configuração e o estado salvo.

Regra: nenhum módulo deste pacote importa pygame (nem `radClock.ui`). Assim
fica claro o que é regra e o que é desenho, e o núcleo roda e é testado sem
SDL. `tests/test_layers.py` confere a regra. O que precisar de pygame (decodificar PNG, montar uma
Surface) é injetado pela `ui`.
"""
