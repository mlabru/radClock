"""
Rastros (histórico de posições) e alerta local de conflito.

O Tracker roda na thread da tela, mas só trabalha quando chega um Snapshot
novo (cerca de 1x/s); a cada quadro o renderizador apenas lê `trails`,
`conflicts` e `pairs`.

Conflito, no estilo de um STCA simplificado (Short Term Conflict Alert):
  1. Cada aeronave em voo, com altitude e posição recente, é extrapolada em
     linha reta: velocidade e razão de subida constantes.
  2. Para cada par, pergunta-se: existe um instante t entre agora e
     `conflict_s` segundos em que a separação horizontal é menor que
     `conflict_km` E, ao mesmo tempo, a vertical é menor que `conflict_ft`?
  3. A resposta é analítica (sem testar t = 0, 1, 2... um por um):
     - horizontal: a distância ao quadrado entre as duas é uma parábola em t,
       |d + v*t|² ; o trecho em que ela fica abaixo de conflict_km² é um
       intervalo [t1, t2] (raízes de uma equação do 2º grau);
     - vertical: |dz + vz*t| < conflict_ft dá outro intervalo, pelo mesmo jeito;
     - conflito = os dois intervalos se cruzam dentro de [0, conflict_s]. O
       início do cruzamento é o "tempo até a perda de separação".
     Isso é exato e barato -- importante no Raspberry Pi com 100+ alvos.

Zonas de inibição (`[[radar.inhibit]]`): círculos até um teto, em volta de
aeródromos, onde aproximações paralelas e circuitos de tráfego disparariam o
alerta à toa. Um par só é ignorado se as DUAS aeronaves estão, agora, dentro
de alguma zona e abaixo do teto: quem chega de fora ou sobrevoa acima do teto
continua alertando.

Alerta sonoro: `new_pairs` traz os pares que acabaram de entrar em conflito.
Um par que some e volta em menos de CONFLICT_QUIET_S não conta como novo, para
o bipe não repetir quando o alerta pisca na borda do mínimo.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from math import cos, hypot, inf, radians, sin, sqrt

from radClock.core.config import RadarCfg
from radClock.core.fetcher import Snapshot
from radClock.core.model import Aircraft
from radClock.core.options import TRAIL_CHOICES_S
from radClock.core.projection import AzimuthalEquidistant

KT_TO_KMS = 1.852 / 3600.0      # nós -> km/s
SECONDS_PER_MINUTE = 60.0
TRAIL_MIN_DT_S = 0.5            # o readsb grava 1x/s; menos que isso é a mesma posição
CONFLICT_QUIET_S = 30.0         # par ausente por menos que isso não alerta de novo
NEAR_ZERO = 1e-12               # coeficiente abaixo disso é tratado como zero

TrailPoint = tuple[float, float, float]      # (instante monotônico, leste_km, norte_km)
Pair = tuple[str, str]                       # (hex_a, hex_b)


@dataclass(frozen=True)
class Zone:
    """Zona de inibição de conflito: círculo em km da estação, até um teto.

    e, n: centro (km a leste / ao norte da estação).
    r_km: raio.
    ceiling_ft: teto; acima dele a zona não vale.
    name: nome para exibição.
    """
    e: float
    n: float
    r_km: float
    ceiling_ft: float
    name: str

    def contains(self, e: float, n: float, z: float) -> bool:
        """A posição (e, n) na altitude z (ft) está dentro da zona?"""
        if z > self.ceiling_ft:
            return False
        squared_distance = (e - self.e) ** 2 + (n - self.n) ** 2
        return squared_distance <= self.r_km ** 2


@dataclass(frozen=True)
class Conflict:
    """Conflito de uma aeronave com outra.

    other: hex da outra aeronave.
    t_s: segundos até a perda de separação (0 = já perdida).
    """
    other: str
    t_s: float


@dataclass(frozen=True)
class _Kinematics:
    """Estado de uma aeronave para a extrapolação em linha reta.

    e, n: posição em km da estação, no instante do snapshot.
    ve, vn: velocidade em km/s (leste, norte).
    z: altitude em ft; vz: razão vertical em ft/s.
    """
    e: float
    n: float
    ve: float
    vn: float
    z: float
    vz: float


def velocity_kms(ac: Aircraft) -> tuple[float, float]:
    """Velocidade sobre o solo em km/s, decomposta em (leste, norte).

    Retorna (0, 0) se faltar velocidade ou rumo. O rumo é contado do norte,
    no sentido horário: por isso leste usa seno e norte usa cosseno.
    """
    if ac.gs_kt is None or ac.track is None:
        return 0.0, 0.0
    speed_kms = ac.gs_kt * KT_TO_KMS
    track_rad = radians(ac.track)
    return speed_kms * sin(track_rad), speed_kms * cos(track_rad)


def extrapolate(ac: Aircraft, e: float, n: float, age_s: float,
                max_s: float) -> tuple[float, float]:
    """Posição avançada em linha reta pela idade da posição ("dead reckoning").

    Usado para o símbolo andar suavemente entre um snapshot e outro.

    Parâmetros:
        ac: aeronave (velocidade e rumo).
        e, n: última posição recebida, em km da estação.
        age_s: há quantos segundos essa posição foi recebida.
        max_s: limite do avanço. Depois disso o alvo para onde estaria (já
            perto de ficar cinza) em vez de seguir em frente sem dado; ele não
            volta para trás, então não dá salto ao ficar velho.
    Retorno:
        (leste_km, norte_km) estimados. Curva não é prevista: quando chega a
        posição nova o alvo corrige alguns pixels.
    """
    elapsed_s = min(age_s, max_s)
    if elapsed_s <= 0:
        return e, n
    velocity_east, velocity_north = velocity_kms(ac)
    return e + velocity_east * elapsed_s, n + velocity_north * elapsed_s


def _kinematics(ac: Aircraft, e: float, n: float) -> _Kinematics:
    """Estado da aeronave trazido para o instante do snapshot.

    A posição recebida tem `seen_pos_s` segundos; ela é avançada esse tanto,
    para todas as aeronaves do par estarem no mesmo instante.
    """
    velocity_east, velocity_north = velocity_kms(ac)
    vertical_rate_fps = (ac.vrate_fpm or 0.0) / SECONDS_PER_MINUTE
    age_s = ac.seen_pos_s
    return _Kinematics(e + velocity_east * age_s,
                       n + velocity_north * age_s,
                       velocity_east,
                       velocity_north,
                       ac.alt_ft + vertical_rate_fps * age_s,
                       vertical_rate_fps)


def _interval_below(c0: float, c1: float, c2: float) -> tuple[float, float] | None:
    """Intervalo de t em que c2*t² + c1*t + c0 < 0.

    Exige c2 >= 0 (parábola "para cima" ou reta). Casos:
      - c2 ≈ 0 e c1 ≈ 0: constante; negativa = sempre, senão nunca;
      - c2 ≈ 0: reta; negativa de um lado da raiz -c0/c1;
      - c2 > 0: parábola; negativa entre as duas raízes (Bhaskara). Sem
        raízes reais (discriminante <= 0) nunca fica negativa.

    Retorno:
        (t_início, t_fim), podendo ser ±inf, ou None se nunca é negativa.
    """
    if c2 < NEAR_ZERO:
        if abs(c1) < NEAR_ZERO:
            if c0 < 0:
                return -inf, inf
            return None
        root = -c0 / c1
        if c1 > 0:
            return -inf, root
        return root, inf
    discriminant = c1 * c1 - 4 * c2 * c0
    if discriminant <= 0:
        return None
    sqrt_discriminant = sqrt(discriminant)
    first_root = (-c1 - sqrt_discriminant) / (2 * c2)
    second_root = (-c1 + sqrt_discriminant) / (2 * c2)
    return first_root, second_root


def time_to_conflict(a: _Kinematics, b: _Kinematics, h_km: float, v_ft: float,
                     horizon_s: float) -> float | None:
    """Primeiro t em [0, horizon_s] com as duas separações abaixo do mínimo.

    Parâmetros:
        a, b: estados das duas aeronaves.
        h_km: separação horizontal mínima; v_ft: separação vertical mínima.
        horizon_s: até quantos segundos à frente procurar.
    Retorno:
        segundos até a perda de separação (0 = já perdida), ou None.
    """
    # posição e velocidade de b relativas a a
    rel_e = b.e - a.e
    rel_n = b.n - a.n
    rel_ve = b.ve - a.ve
    rel_vn = b.vn - a.vn
    # |rel_pos + rel_vel*t|² - h² < 0, expandido em c2*t² + c1*t + c0
    horizontal = _interval_below(rel_e * rel_e + rel_n * rel_n - h_km * h_km,
                                 2 * (rel_e * rel_ve + rel_n * rel_vn),
                                 rel_ve * rel_ve + rel_vn * rel_vn)
    if horizontal is None:
        return None

    rel_z = b.z - a.z
    rel_vz = b.vz - a.vz
    # |rel_z + rel_vz*t| < v  <=>  (rel_z + rel_vz*t)² - v² < 0
    vertical = _interval_below(rel_z * rel_z - v_ft * v_ft,
                               2 * rel_z * rel_vz,
                               rel_vz * rel_vz)
    if vertical is None:
        return None

    start = max(horizontal[0], vertical[0], 0.0)
    end = min(horizontal[1], vertical[1], horizon_s)
    if start < end:
        return start
    return None


class Tracker:
    """Mantém os rastros e calcula os conflitos a cada snapshot novo.

    Atributos lidos pela tela:
        trails: hex -> deque de (instante, leste_km, norte_km), mais velho primeiro.
            Guarda `trail_keep_s` segundos: o maior rastro que a tela AJUSTES
            pode pedir, para que trocar 1 min por 2 min mostre o histórico na hora.
        conflicts: hex -> Conflict mais próximo daquela aeronave.
        pairs: pares (hex_a, hex_b) em conflito.
        new_pairs: pares que entraram em conflito neste snapshot (para o bipe).
        inhibited: quantos pares foram ignorados por zona de inibição.
        zones: zonas de inibição projetadas.
    """

    def __init__(self, proj: AzimuthalEquidistant, cfg: RadarCfg):
        self.proj = proj
        self.cfg = cfg
        self.trail_keep_s = max(cfg.trail_s, *TRAIL_CHOICES_S)
        self.trails: dict[str, deque[TrailPoint]] = {}
        self.conflicts: dict[str, Conflict] = {}
        self.pairs: list[Pair] = []
        self.new_pairs: list[Pair] = []
        self.inhibited = 0
        self.zones = [self._make_zone(zone_cfg) for zone_cfg in cfg.inhibit]
        self._pair_last_seen: dict[Pair, float] = {}
        self._last_snapshot_time = 0.0

    def _make_zone(self, zone_cfg) -> Zone:
        east_km, north_km = self.proj.forward(zone_cfg.lat, zone_cfg.lon)
        return Zone(east_km, north_km, zone_cfg.radius_km, zone_cfg.ceiling_ft, zone_cfg.name)

    def update(self, snap: Snapshot) -> bool:
        """Processa um snapshot, se for novo.

        Fluxo: projeta cada aeronave; acrescenta ao rastro; separa as que
        entram na conta de conflito; apaga pontos velhos dos rastros; calcula
        os conflitos; marca os pares novos.

        Retorno:
            True se o snapshot era novo (e `new_pairs` vale para ele).
        """
        if snap.updated == self._last_snapshot_time:
            return False
        now = snap.updated
        self._last_snapshot_time = now

        candidates: dict[str, _Kinematics] = {}
        for ac in snap.aircraft:
            east_km, north_km = self.proj.forward(ac.lat, ac.lon)
            self._add_trail_point(ac, east_km, north_km, now)
            if self._can_conflict(ac):
                candidates[ac.hex] = _kinematics(ac, east_km, north_km)

        self._prune_trails(now)
        self._find_conflicts(candidates)
        self._mark_new_pairs(now)
        return True

    def _add_trail_point(self, ac: Aircraft, east_km: float, north_km: float,
                         now: float) -> None:
        """Acrescenta a posição ao rastro (se a posição é recente).

        O rastro é guardado mesmo com o ajuste RASTRO desligado: custa pouco
        e, ao religar, ele já aparece completo.
        """
        if ac.seen_pos_s > self.cfg.stale_s:
            return
        position_time = now - ac.seen_pos_s
        trail = self.trails.setdefault(ac.hex, deque())
        if trail and position_time - trail[-1][0] < TRAIL_MIN_DT_S:
            return                  # mesma posição do último ponto
        trail.append((position_time, east_km, north_km))

    def _can_conflict(self, ac: Aircraft) -> bool:
        """A aeronave entra na conta de conflito? (em voo, com altitude, recente)"""
        if self.cfg.conflict_s <= 0:
            return False
        if ac.on_ground or ac.alt_ft is None:
            return False
        return ac.seen_pos_s <= self.cfg.stale_s

    def _prune_trails(self, now: float) -> None:
        """Remove pontos mais velhos que `trail_keep_s` e rastros vazios."""
        for hex_id in list(self.trails):
            trail = self.trails[hex_id]
            while trail and now - trail[0][0] > self.trail_keep_s:
                trail.popleft()
            if not trail:
                del self.trails[hex_id]

    def _mark_new_pairs(self, now: float) -> None:
        """Preenche `new_pairs` e esquece pares ausentes há mais de CONFLICT_QUIET_S."""
        last_seen = self._pair_last_seen
        self.new_pairs = []
        for pair in self.pairs:
            if pair not in last_seen or now - last_seen[pair] > CONFLICT_QUIET_S:
                self.new_pairs.append(pair)
        for pair in self.pairs:
            last_seen[pair] = now
        expired = [pair for pair, seen_at in last_seen.items()
                   if now - seen_at > CONFLICT_QUIET_S]
        for pair in expired:
            del last_seen[pair]

    def _in_any_zone(self, state: _Kinematics) -> bool:
        return any(zone.contains(state.e, state.n, state.z) for zone in self.zones)

    def _cannot_meet(self, a: _Kinematics, b: _Kinematics) -> bool:
        """Descarte rápido: longe demais para se aproximar dentro do horizonte.

        Mesmo voando uma direto para a outra, elas não fecham a distância a
        tempo. Evita a conta completa para a maioria dos pares.
        """
        cfg = self.cfg
        max_closing_km = (hypot(a.ve, a.vn) + hypot(b.ve, b.vn)) * cfg.conflict_s
        return hypot(b.e - a.e, b.n - a.n) > cfg.conflict_km + max_closing_km

    def _find_conflicts(self, candidates: dict[str, _Kinematics]) -> None:
        """Testa todos os pares de candidatos e atualiza conflicts/pairs/inhibited."""
        cfg = self.cfg
        conflicts: dict[str, Conflict] = {}
        pairs: list[Pair] = []
        inhibited = 0
        in_zone: set[str] = set()
        if self.zones:
            in_zone = {hex_id for hex_id, state in candidates.items()
                       if self._in_any_zone(state)}

        items = list(candidates.items())
        for index, (hex_a, state_a) in enumerate(items):
            for hex_b, state_b in items[index + 1:]:
                if self._cannot_meet(state_a, state_b):
                    continue
                seconds = time_to_conflict(state_a, state_b, cfg.conflict_km,
                                           cfg.conflict_ft, cfg.conflict_s)
                if seconds is None:
                    continue
                if hex_a in in_zone and hex_b in in_zone:
                    inhibited += 1
                    continue
                pairs.append((hex_a, hex_b))
                _keep_soonest(conflicts, hex_a, hex_b, seconds)
                _keep_soonest(conflicts, hex_b, hex_a, seconds)

        self.conflicts = conflicts
        self.pairs = pairs
        self.inhibited = inhibited


def _keep_soonest(conflicts: dict[str, Conflict], me: str, other: str, seconds: float) -> None:
    """Guarda o conflito de `me` com `other` se for o mais iminente de `me`."""
    current = conflicts.get(me)
    if current is None or seconds < current.t_s:
        conflicts[me] = Conflict(other, seconds)
