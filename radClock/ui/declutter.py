"""
Posicionamento dos rótulos sem sobreposição (declutter), com linhas de chamada.

Ideia geral:
  - Cada rótulo tem 16 posições candidatas ("slots"): 8 direções coladas no
    alvo (slots 0..7) e as mesmas 8 direções mais longe (slots 8..15). Os
    afastados ganham uma linha de chamada até o alvo.
  - Os rótulos são colocados um a um, em ordem de prioridade. Cada um fica na
    primeira posição livre: sem cobrir rótulo já colocado, símbolo de outro
    alvo nem sair do escopo.
  - Sem posição livre, o rótulo é omitido (o alvo continua na tela e tocável;
    os dados vão para o painel), como nas telas de controle: empilhar rótulos
    ilegíveis não ajuda ninguém.
  - Os obrigatórios (`must`: selecionado, emergência, conflito) sempre
    aparecem, na posição que cobrir menos área dos outros.

Estabilidade ("histerese"): a posição colada usada no quadro anterior é
tentada primeiro, então um rótulo só muda de lugar quando o dele deixa de
estar livre (não fica "pulando" a cada quadro). Um rótulo afastado volta a
colar assim que houver espaço.

Desempenho: os testes de colisão usam `Rect.collidelist`, um laço escrito em
C dentro do pygame -- importante no Raspberry Pi 3 B+.

Também serve aos rótulos do mapa (aeroportos, navaids), sem linhas de chamada.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

import pygame

# direções (dx, dy) em ordem de preferência: à direita (o costume),
# diagonais da direita, esquerda, diagonais da esquerda, cima, baixo.
# Na tela, y cresce para baixo: dy = -1 é "acima do alvo".
DIRS = [(1, 0), (1, -1), (1, 1), (-1, 0), (-1, -1), (-1, 1), (0, -1), (0, 1)]
NEAR_PX = 10            # folga entre o alvo e o rótulo colado (ícone tem ~9 px de raio)
FAR_PX = 40             # distância do rótulo afastado, que tem linha de chamada
N_NEAR = len(DIRS)      # slots 0..N_NEAR-1 = colados; N_NEAR..2*N_NEAR-1 = afastados
NO_COLLISION = -1       # o que Rect.collidelist devolve quando não colide com nada


@dataclass(frozen=True)
class Label:
    """Um rótulo a posicionar.

    key: identificador (hex da aeronave, ou texto do ponto do mapa).
    x, y: posição do alvo na tela (px).
    w, h: tamanho do texto do rótulo (px).
    """
    key: str
    x: float
    y: float
    w: int
    h: int


@dataclass(frozen=True)
class Placed:
    """Resultado do posicionamento de um rótulo: retângulo na tela e slot usado."""
    rect: pygame.Rect
    slot: int

    @property
    def leader(self) -> bool:
        """True se o rótulo ficou afastado (e precisa de linha de chamada)."""
        return self.slot >= N_NEAR


def _offset_start(target: float, size: int, direction: int, gap: float) -> float:
    """Coordenada do canto esquerdo/superior do rótulo num eixo.

    direction > 0: rótulo depois do alvo; < 0: antes do alvo; 0: centralizado.
    """
    if direction > 0:
        return target + gap
    if direction < 0:
        return target - gap - size
    return target - size / 2


def slot_rect(label: Label, slot: int) -> pygame.Rect:
    """Retângulo que o rótulo ocuparia no slot dado.

    Parâmetros:
        label: o rótulo (posição do alvo e tamanho do texto).
        slot: 0..2*N_NEAR-1 (direção = slot % N_NEAR; afastado se >= N_NEAR).
    Retorno:
        pygame.Rect na tela.
    """
    dx, dy = DIRS[slot % N_NEAR]
    if slot < N_NEAR:
        gap = NEAR_PX
    else:
        gap = FAR_PX
    left = _offset_start(label.x, label.w, dx, gap)
    top = _offset_start(label.y, label.h, dy, gap)
    return pygame.Rect(round(left), round(top), label.w, label.h)


def _overlap_cost(rect: pygame.Rect, taken: list[pygame.Rect], bounds: pygame.Rect) -> int:
    """Quanto um rótulo atrapalha nesta posição (px²).

    Soma a área que ele cobre de rótulos/símbolos já colocados com a área que
    fica fora do escopo. Só é usado para rótulos obrigatórios sem lugar livre.
    """
    inside = rect.clip(bounds)
    cost = rect.w * rect.h - inside.w * inside.h
    for index in rect.collidelistall(taken):
        overlap = rect.clip(taken[index])
        cost += overlap.w * overlap.h
    return cost


def _slot_order(previous_slot: int | None, candidate_slots: list[int]) -> list[int]:
    """Ordem de tentativa: o slot colado do quadro anterior primeiro (histerese)."""
    if previous_slot is None or not 0 <= previous_slot < N_NEAR:
        return candidate_slots
    others = [slot for slot in candidate_slots if slot != previous_slot]
    return [previous_slot] + others


def _place_one(label: Label, slots: list[int], taken: list[pygame.Rect],
               bounds: pygame.Rect, fits: Callable[[pygame.Rect], bool],
               mandatory: bool) -> Placed | None:
    """Acha a posição de um rótulo.

    Fluxo: percorre os slots; o primeiro livre (dentro e sem colisão) vence.
    Se nenhum estiver livre e o rótulo for obrigatório, fica o de menor custo
    de sobreposição; se não for obrigatório, devolve None (rótulo omitido).
    """
    best_cost: int | None = None
    best: Placed | None = None
    for slot in slots:
        rect = slot_rect(label, slot)
        if fits(rect) and rect.collidelist(taken) == NO_COLLISION:
            return Placed(rect, slot)
        if not mandatory:
            continue                # só os obrigatórios pagam pela conta da área
        cost = _overlap_cost(rect, taken, bounds)
        if best_cost is None or cost < best_cost:
            best_cost = cost
            best = Placed(rect, slot)
    return best


def place(labels: list[Label], obstacles: list[pygame.Rect], bounds: pygame.Rect,
          prev: dict[str, int] | None = None,
          must: set[str] | frozenset[str] = frozenset(),
          leaders: bool = True,
          fits: Callable[[pygame.Rect], bool] | None = None) -> dict[str, Placed]:
    """Posiciona os rótulos sem sobreposição.

    Parâmetros:
        labels: rótulos em ordem de prioridade (o primeiro escolhe antes).
        obstacles: áreas a não cobrir (os símbolos dos alvos). O próprio
            símbolo de um alvo pode estar aqui: os slots começam a NEAR_PX do
            alvo, fora do ícone, então nunca encostam nele.
        bounds: área útil (o escopo).
        prev: key -> slot usado no quadro anterior (para não "pular").
        must: keys cujo rótulo aparece mesmo sem lugar livre.
        leaders: False usa só os slots colados (no mapa, linha de chamada
            em ponto fixo só polui).
        fits: teste "cabe aqui?"; padrão = dentro de `bounds` (o mapa usa o
            círculo do escopo).
    Retorno:
        key -> Placed, só dos rótulos colocados.
    """
    if fits is None:
        fits = bounds.contains
    if prev is None:
        prev = {}
    if leaders:
        candidate_slots = list(range(2 * N_NEAR))
    else:
        candidate_slots = list(range(N_NEAR))

    taken = list(obstacles)
    placed: dict[str, Placed] = {}
    for label in labels:
        slots = _slot_order(prev.get(label.key), candidate_slots)
        result = _place_one(label, slots, taken, bounds, fits, label.key in must)
        if result is None:
            continue                # sem lugar: rótulo omitido
        placed[label.key] = result
        taken.append(result.rect)
    return placed


def leader_end(x: float, y: float, r: pygame.Rect) -> tuple[float, float]:
    """Ponto do rótulo mais próximo do alvo: onde a linha de chamada encosta.

    Parâmetros:
        x, y: posição do alvo.
        r: retângulo do rótulo.
    Retorno:
        (x, y) dentro do retângulo, o mais perto possível do alvo.
    """
    end_x = min(max(x, r.left), r.right - 1)
    end_y = min(max(y, r.top), r.bottom - 1)
    return end_x, end_y
