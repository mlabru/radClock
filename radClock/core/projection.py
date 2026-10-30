"""Projeção azimutal equidistante centrada na estação.

O que é: um jeito de "achatar" a Terra num plano em que tudo é medido a partir
de um ponto central (a estação). Nessa projeção, a distância e o azimute de
qualquer ponto até o centro são exatos -- é exatamente o que um radar mostra:
um alvo a 100 km da antena aparece a 100 km do centro da tela, na direção certa.

Coordenadas usadas no programa inteiro:
    leste_km  (e): quilômetros a leste da estação (negativo = oeste)
    norte_km  (n): quilômetros ao norte da estação (negativo = sul)

Por que não a conta simples do firmware ESP32 (dx = dlon * 111.32 * cos(lat0)):
aquela aproximação erra alguns km na borda de 300 km e "quebra" no
antimeridiano (longitude 180). Aqui a diferença de longitude entra só via
seno/cosseno, então 179.5 e -179.5 ficam a 1 grau um do outro.

A inversa (plano -> lat/lon), usada para reprojetar a chuva, fica em
`echo.inverse_azeq`.
"""
from __future__ import annotations

from math import acos, atan2, cos, degrees, hypot, radians, sin

R_EARTH_KM = 6371.0088          # raio médio da Terra (esfera)

# abaixo disso (em radianos) o ângulo é tratado como zero: evita dividir por ~0
TINY_ANGLE_RAD = 1e-12


class AzimuthalEquidistant:
    """Converte (lat, lon) em (leste_km, norte_km) a partir da estação.

    Parâmetros do construtor:
        lat0, lon0: posição da estação (centro da projeção), em graus.
    """

    def __init__(self, lat0: float, lon0: float):
        self.lat0 = lat0
        self.lon0 = lon0
        # senos/cossenos da estação são constantes: calculados uma vez só
        center_lat_rad = radians(lat0)
        self._center_lon_rad = radians(lon0)
        self._sin_center_lat = sin(center_lat_rad)
        self._cos_center_lat = cos(center_lat_rad)

    def forward(self, lat: float, lon: float) -> tuple[float, float]:
        """Projeta um ponto da Terra no plano da estação.

        Fluxo (fórmulas clássicas de Snyder, "Map Projections", na esfera):
          1. `angular_distance` = ângulo, visto do centro da Terra, entre a
             estação e o ponto (lei dos cossenos esférica);
          2. a distância na superfície é R * angular_distance;
          3. essa distância é repartida em leste/norte conforme a direção.

        Parâmetros:
            lat, lon: posição do ponto, em graus.
        Retorno:
            (leste_km, norte_km). (0, 0) na própria estação; (inf, inf) no
            antípoda, onde a direção é indefinida (o ponto cai fora da tela).
        """
        lat_rad = radians(lat)
        delta_lon_rad = radians(lon) - self._center_lon_rad
        sin_lat = sin(lat_rad)
        cos_lat = cos(lat_rad)
        cos_delta_lon = cos(delta_lon_rad)

        cos_angular_distance = (self._sin_center_lat * sin_lat
                                + self._cos_center_lat * cos_lat * cos_delta_lon)
        # arredondamento pode dar 1.0000000002: o clamp evita erro no acos
        cos_angular_distance = max(-1.0, min(1.0, cos_angular_distance))
        angular_distance = acos(cos_angular_distance)
        sin_angular_distance = sin(angular_distance)

        if angular_distance < TINY_ANGLE_RAD:
            return 0.0, 0.0
        if sin_angular_distance < TINY_ANGLE_RAD:
            return float("inf"), float("inf")

        # fator de escala: transforma as componentes "unitárias" em km
        scale_km = R_EARTH_KM * angular_distance / sin_angular_distance
        east_km = scale_km * cos_lat * sin(delta_lon_rad)
        north_km = scale_km * (self._cos_center_lat * sin_lat
                               - self._sin_center_lat * cos_lat * cos_delta_lon)
        return east_km, north_km


def dist_bearing(e_km: float, n_km: float) -> tuple[float, float]:
    """Distância e azimute de um ponto do plano visto da estação.

    Parâmetros:
        e_km, n_km: posição no plano (km a leste, km ao norte).
    Retorno:
        (distância_km, azimute_graus), azimute verdadeiro em [0, 360),
        contado no sentido horário a partir do norte.
    """
    distance_km = hypot(e_km, n_km)
    # atan2(leste, norte), e não (norte, leste): azimute de navegação, não de matemática
    bearing_deg = degrees(atan2(e_km, n_km)) % 360.0
    return distance_km, bearing_deg
