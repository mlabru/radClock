#!/usr/bin/env python3
"""Simula o readsb: grava um aircraft.json com tráfego sintético a cada segundo.

Serve para desenvolver a tela sem dongle. Os dados são fictícios (hex na faixa
0xF0xxxx, callsigns TST...). A gravação é atômica (tmp + rename), como no readsb.

    python tools/fake_readsb.py --out /tmp/radclock/aircraft.json
"""
from __future__ import annotations

import argparse
import json
import math
import os
import time
from dataclasses import dataclass
from pathlib import Path

KM_PER_DEG_LAT = 111.2
KM_PER_NM = 1.852
SECONDS_PER_HOUR = 3600.0
HEAD_ON_PERIOD_S = 300.0            # o par em rota de colisão se cruza a cada 5 min


@dataclass
class OrbitSpec:
    """Uma aeronave em órbita circular em volta da estação."""
    radius_km: float
    speed_kt: float
    altitude_ft: int
    vertical_rate_fpm: int
    type_code: str
    squawk: str


ORBITS = [
    OrbitSpec(8, 140, 3500, -700, "C172", "1200"),
    OrbitSpec(18, 250, 9000, 1500, "AT76", "2101"),
    OrbitSpec(30, 420, 24000, 0, "A320", "4512"),
    OrbitSpec(45, 460, 37000, 0, "B738", "3341"),
    OrbitSpec(60, 480, 39000, 0, "B77W", "7700"),     # emergência, para testar o destaque
    OrbitSpec(12, 60, 1500, 0, "R44", "1200"),        # lento: sem vetor
    OrbitSpec(75, 450, 33000, -1200, "E195", "6120"),
]


def _to_lat_lon(lat0: float, lon0: float, east_km: float, north_km: float) -> tuple[float, float]:
    """Desloca (lat0, lon0) de east_km/north_km (aproximação plana, basta aqui)."""
    km_per_deg_lon = KM_PER_DEG_LAT * math.cos(math.radians(lat0))
    return lat0 + north_km / KM_PER_DEG_LAT, lon0 + east_km / km_per_deg_lon


def _orbiting_aircraft(lat0: float, lon0: float, t: float) -> list[dict]:
    """As aeronaves de ORBITS na posição do instante t (sentido alterna)."""
    out = []
    for index, spec in enumerate(ORBITS):
        direction = 1 if index % 2 == 0 else -1
        speed_km_s = spec.speed_kt * KM_PER_NM / SECONDS_PER_HOUR
        angular_speed = direction * speed_km_s / spec.radius_km     # rad/s
        angle = angular_speed * t + index * 1.3                      # fase diferente por avião
        east_km = spec.radius_km * math.sin(angle)
        north_km = spec.radius_km * math.cos(angle)
        track = (math.degrees(angle) + 90 * direction) % 360         # tangente à órbita
        lat, lon = _to_lat_lon(lat0, lon0, east_km, north_km)
        out.append({
            "hex": f"f0{index:04x}",
            "flight": f"TST{100 + index:<5}",
            "lat": lat, "lon": lon,
            "alt_baro": spec.altitude_ft, "gs": spec.speed_kt, "track": track,
            "baro_rate": spec.vertical_rate_fpm,
            "squawk": spec.squawk, "t": spec.type_code, "category": "A3",
            "seen": 0.2, "seen_pos": 0.4,
        })
    return out


def _head_on_pair(lat0: float, lon0: float, t: float) -> list[dict]:
    """Par em rumos opostos na mesma reta, 400 ft de diferença.

    A cada 5 min eles se cruzam 20 km ao norte da estação: testa o alerta de
    conflito.
    """
    speed_kt = 300
    speed_km_s = speed_kt * KM_PER_NM / SECONDS_PER_HOUR
    elapsed = t % HEAD_ON_PERIOD_S
    pair = [
        (-45 + speed_km_s * elapsed, 90, 11000),     # vem do oeste para leste
        (45 - speed_km_s * elapsed, 270, 11400),     # vem do leste para oeste
    ]
    out = []
    for index, (east_km, track, altitude_ft) in enumerate(pair):
        lat, lon = _to_lat_lon(lat0, lon0, east_km, 20)
        out.append({"hex": f"f0c0{index:02x}", "flight": f"TSTC{index}", "category": "A3",
                    "lat": lat, "lon": lon,
                    "alt_baro": altitude_ft, "gs": speed_kt, "track": track, "baro_rate": 0,
                    "squawk": "2000", "t": "B738", "seen": 0.2, "seen_pos": 0.4})
    return out


def _special_cases(lat0: float, lon0: float) -> list[dict]:
    """Casos de borda que a tela e o parser precisam tratar."""
    return [
        # posição velha: deve aparecer cinza
        {"hex": "f0aaaa", "flight": "TSTOLD", "lat": lat0 + 0.3, "lon": lon0 - 0.2,
         "alt_baro": 12000, "gs": 300, "track": 45, "seen": 20, "seen_pos": 25},
        # no solo: oculta por padrão
        {"hex": "f0bbbb", "flight": "TSTGND", "lat": lat0 + 0.01, "lon": lon0 + 0.01,
         "alt_baro": "ground", "gs": 12, "track": 90, "seen": 1, "seen_pos": 1},
        # só Mode S, sem posição: o parser tem de ignorar
        {"hex": "f0cccc", "alt_baro": 8000, "seen": 1},
    ]


def _traffic(lat0: float, lon0: float, t: float) -> list[dict]:
    """Todo o tráfego sintético no instante t (segundos desde o início)."""
    return (_orbiting_aircraft(lat0, lon0, t) + _head_on_pair(lat0, lon0, t)
            + _special_cases(lat0, lon0))


def main() -> None:
    """Grava o aircraft.json uma vez por segundo até Ctrl+C."""
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", type=Path, default=Path("/tmp/radclock/aircraft.json"))
    ap.add_argument("--lat", type=float, default=-23.21)
    ap.add_argument("--lon", type=float, default=-45.87)
    args = ap.parse_args()
    args.out.parent.mkdir(parents=True, exist_ok=True)
    tmp = args.out.with_suffix(".tmp")
    start_time = time.time()
    print(f"gravando {args.out} (Ctrl+C para parar)")
    while True:
        now = time.time()
        doc = {"now": now, "messages": 0, "aircraft": _traffic(args.lat, args.lon, now - start_time)}
        tmp.write_text(json.dumps(doc))
        os.replace(tmp, args.out)
        time.sleep(1.0)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
