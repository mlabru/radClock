import itertools

import pygame

from radClock.ui.declutter import N_NEAR, Label, leader_end, place, slot_rect

BOUNDS = pygame.Rect(0, 0, 480, 480)


def sym(x, y):
    return pygame.Rect(x - 4, y - 4, 8, 8)


def no_overlaps(placed, symbols=()):
    rects = [p.rect for p in placed.values()]
    for a, b in itertools.combinations(rects, 2):
        assert not a.colliderect(b), (a, b)
    for r in rects:
        assert BOUNDS.contains(r)
        assert r.collidelist(list(symbols)) == -1


def test_isolated_label_goes_right_of_target():
    p = place([Label("a", 100, 100, 50, 36)], [sym(100, 100)], BOUNDS)
    assert p["a"].slot == 0 and p["a"].rect.left == 110 and not p["a"].leader


def test_label_near_right_edge_flips_left():
    p = place([Label("a", 470, 100, 50, 36)], [sym(470, 100)], BOUNDS)
    assert p["a"].rect.right <= 460 and BOUNDS.contains(p["a"].rect)


def test_close_targets_do_not_overlap():
    pts = [(200, 200), (212, 205), (205, 215), (190, 210), (220, 190)]
    labels = [Label(str(i), x, y, 52, 36) for i, (x, y) in enumerate(pts)]
    symbols = [sym(x, y) for x, y in pts]
    placed = place(labels, symbols, BOUNDS)
    no_overlaps(placed, symbols)
    assert any(p.leader for p in placed.values())   # cinco juntos: alguém vai para longe


def test_priority_keeps_the_preferred_spot():
    a, b = Label("sel", 200, 200, 52, 36), Label("x", 205, 200, 52, 36)
    assert place([a, b], [], BOUNDS)["sel"].slot == 0
    assert place([b, a], [], BOUNDS)["x"].slot == 0


def test_previous_near_slot_is_kept_but_far_slot_is_not():
    lb = Label("a", 100, 100, 50, 36)
    assert place([lb], [], BOUNDS, {"a": 3})["a"].slot == 3      # sem pular de lado
    assert place([lb], [], BOUNDS, {"a": N_NEAR + 2})["a"].slot == 0   # volta a colar


def test_overcrowded_hides_labels_but_never_the_mandatory_ones():
    labels = [Label(str(i), 240, 240, 60, 36) for i in range(40)]
    must = {"0", "39"}                               # o 39 é o último a escolher lugar
    placed = place(labels, [], BOUNDS, must=must)
    assert 0 < len(placed) < 40
    assert must <= set(placed)
    free = [p.rect for k, p in placed.items() if k not in must]
    for a, b in itertools.combinations(free, 2):     # os opcionais nunca se cobrem
        assert not a.colliderect(b)


def test_slots_never_cover_own_symbol_and_leader_touches_label():
    lb = Label("a", 240, 240, 52, 36)
    plane = pygame.Rect(240 - 9, 240 - 9, 18, 18)
    for s in range(2 * N_NEAR):
        r = slot_rect(lb, s)
        assert not r.colliderect(plane), s
        ex, ey = leader_end(lb.x, lb.y, r)
        assert r.collidepoint(ex, ey)
